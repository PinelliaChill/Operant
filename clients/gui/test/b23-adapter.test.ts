import assert from 'node:assert/strict';
import test from 'node:test';
import {
  B23AdapterError,
  B23ManagementAdapter,
  blocksB23Management,
  mapManagementResult,
  mapManagementState,
  normalizeB23Error,
  type B23ClientLike,
  type B23ManagementCommand,
} from '../src/live/b23Adapter.ts';

const state = {
  projects: [{
    project_id: 'project-1', name: 'Operant', workspace_id: 'workspace-1', archived: false,
    memory_enabled: true, installation_id: null,
  }],
  global_enabled: true,
  catalog: [{ plugin_id: 'memory-standard', name: 'Standard Memory', description: 'Project memory', config_schema: {} }],
  installations: [],
  datasets: [],
  settings: [{ key: 'memory.enabled', value: true, source: 'project', scope: 'project-1', effective_at: '2026-09-12T00:00:00Z' }],
  skills: [{ skill_id: 'skill-1', name: 'Standard Skill', state: 'installed', package_ref: 'skill://standard', project_ids: ['project-1'], trust_status: 'user_installed' }],
  skill_catalog: [{ package_ref: 'skill://standard', name: 'Standard Skill' }],
  records: [{
    record_id: 'record-1', dataset_id: 'dataset-1', project_id: 'project-1', content: 'Use uv.',
    state: 'confirmed', revision: 3, evidence: 'session-1', version: 2, sources: [{ type: 'session', id: 'session-1' }], proposals: [],
  }],
  artifacts: [{ artifact_id: 'artifact-1', content_hash: 'a'.repeat(64), size_bytes: 128, lifecycle: 'active', pinned: false, blocked: false }],
};

function fakeClient(overrides: Partial<B23ClientLike> = {}): B23ClientLike {
  return {
    protocolVersion: 'b2-3.v1',
    schemaDigest: 'd'.repeat(64),
    negotiateProtocol: async () => ({ protocol_version: 'b2-3.v1', min_client_version: 'b2-3.v1', schema_digest: 'd'.repeat(64), capabilities: ['management'] }),
    getManagement: async () => state,
    executeManagementCommand: async () => ({ status: 'accepted', message: 'Core accepted', state }),
    ...overrides,
  };
}

test('B2-3 mapping keeps project, source/version and candidate proposal fields', () => {
  const mapped = mapManagementState(state);
  assert.equal(mapped.projects[0]?.project_id, 'project-1');
  assert.equal(mapped.records[0]?.version, 2);
  assert.deepEqual(mapped.records[0]?.sources[0], { type: 'session', id: 'session-1' });
  assert.equal(mapped.skill_catalog[0]?.package_ref, 'skill://standard');
  assert.deepEqual(mapped.skills[0]?.project_ids, ['project-1']);
  assert.equal(mapped.artifacts?.[0]?.artifact_id, 'artifact-1');
});

test('B2-3 mapping rejects an incomplete authoritative projection', () => {
  assert.throws(() => mapManagementState({ ...state, projects: [{ ...state.projects[0], workspace_id: '' }] }), B23AdapterError);
  assert.throws(() => mapManagementResult({ status: 'accepted', message: 'ok', state: { ...state, settings: [{ ...state.settings[0], source: '' }] } }), B23AdapterError);
});

test('B2-3 optional lifecycle fields normalize null without manufacturing cleanup errors', () => {
  const mapped = mapManagementState({
    ...state,
    projects: [{ project_id: 'project-1', name: 'Operant', workspace_id: 'workspace-1' }],
    datasets: [{ dataset_id: 'dataset-1', plugin_id: 'memory-standard', installation_id: null, state: 'retained', record_count: 1, exceptions: null }],
    artifacts: null,
  });
  assert.deepEqual(mapped.datasets[0]?.exceptions, []);
  assert.deepEqual(mapped.artifacts, undefined);
});

test('B2-3 adapter negotiates the generated protocol before reading management state', async () => {
  const calls: string[] = [];
  const adapter = new B23ManagementAdapter(fakeClient({
    negotiateProtocol: async () => { calls.push('negotiate'); return { protocol_version: 'b2-3.v1', min_client_version: 'b2-3.v1', schema_digest: 'd'.repeat(64), capabilities: [] }; },
    getManagement: async () => { calls.push('management'); return state; },
  }));
  const metadata = await adapter.connect();
  const projection = await adapter.getManagement();
  assert.equal(metadata.protocol_version, 'b2-3.v1');
  assert.equal(projection.projects[0]?.name, 'Operant');
  assert.deepEqual(calls, ['negotiate', 'management']);
});

test('B2-3 commands forward one explicit idempotency key and never replay locally', async () => {
  let requested: { command: B23ManagementCommand; options: unknown } | undefined;
  const adapter = new B23ManagementAdapter(fakeClient({
    executeManagementCommand: async (command, options) => {
      requested = { command: command as B23ManagementCommand, options };
      return { status: 'accepted', message: 'saved', state };
    },
  }));
  const command: B23ManagementCommand = { action: 'memory_save', project_id: 'project-1', content: 'A fact', confirmed: true };
  const result = await adapter.executeManagementCommand(command, 'one-attempt-key');
  assert.equal(result.status, 'accepted');
  assert.deepEqual(requested, { command, options: { idempotencyKey: 'one-attempt-key' } });
});

test('B2-3 transport uncertainty is typed for manual reconciliation', () => {
  const error = normalizeB23Error({ code: 'transport_unavailable', message: 'offline', retryable: true, recovery: 'retry_later' });
  assert.equal(error.detail.outcomeUnknown, true);
  const manual = normalizeB23Error({ code: 'manual_reconcile_required', message: 'check state', retryable: false, recovery: 'manual_reconcile' });
  assert.equal(manual.detail.outcomeUnknown, true);
});

test('B2-3 business rejection does not block the last good management projection', () => {
  const detail = normalizeB23Error({ code: 'memory_disabled', message: 'global memory is off', retryable: false, recovery: 'none' }).detail;
  assert.equal(blocksB23Management(detail), false);
  const transport = normalizeB23Error({ code: 'transport_unavailable', message: 'offline', retryable: true, recovery: 'retry_later' }).detail;
  assert.equal(blocksB23Management(transport), true);
});
