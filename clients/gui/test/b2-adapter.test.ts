import assert from 'node:assert/strict';
import test from 'node:test';
import type * as B2 from '../../sdk/typescript-client/b2.generated';
import {
  B2AdapterError,
  B2LiveAdapter,
  mapDiscoveredModels,
  mapModelProfile,
  mapRolePreset,
  mapSessionHistory,
  mapTask,
  normalizeB2Error,
} from '../src/live/b2Adapter.ts';

function fakeClient(overrides: Record<string, unknown> = {}) {
  return {
    protocolVersion: 'b2.v1',
    schemaDigest: 'd'.repeat(64),
    negotiateProtocol: async () => ({
      protocol_version: 'b2.v1',
      schema_digest: 'd'.repeat(64),
      min_client_version: 'b2.v1',
      capabilities: ['models', 'roles', 'tasks'],
    }),
    listModels: async () => [],
    createModel: async (request: B2.CreateModelProfileRequest) => request,
    updateModel: async (_id: string, request: B2.UpdateModelProfileRequest) => ({
      ...request,
      name: request.name || 'updated',
      model_id: request.model_id || 'model',
      base_url: request.base_url || 'http://core.test/v1',
      secret_ref: request.secret_ref || 'ENV_KEY',
    }),
    discoverModels: async () => ({ model_ids: ['model-from-core'] }),
    listRoles: async () => [],
    createRole: async (request: B2.CreateRoleRequest) => request,
    updateRole: async (_id: string, request: B2.UpdateRoleRequest) => ({
      ...request,
      name: request.name || 'updated',
      system_prompt: request.system_prompt || 'prompt',
      model_profile_id: request.model_profile_id || 'profile',
    }),
    cancelSession: async () => ({ accepted: true }),
    listTasks: async () => ({ items: [], next_offset: null }),
    getTask: async () => ({
      source: { source_type: 'session', source_id: 'session-1' }, thread_id: null,
      project_id: null, workspace_id: null, source_status: 'completed', revision: 1,
      created_at: '2026-09-10T00:00:00Z', title: 'task', actions: [],
    }),
    getSessionHistory: async () => ({ session: {}, thread_id: null, agents: [], items: [], next_cursor: null }),
    ...overrides,
  };
}

test('B2 adapter validates model and role projections without exposing credential values', () => {
  const model = mapModelProfile({
    id: 'profile-a', name: 'Primary', provider: 'openai-compatible', model_id: 'gpt-exact',
    base_url: 'https://provider.test/v1', secret_ref: 'OPERANT_API_KEY', enabled: true,
  });
  assert.equal(model.model_id, 'gpt-exact');
  assert.equal(model.secret_ref, 'OPERANT_API_KEY');
  assert.equal('api_key' in model, false);

  const role = mapRolePreset({
    id: 'role-a', version: 3, name: 'Coder', system_prompt: 'Read and change code',
    model_profile_id: 'profile-a', effort: 'high', tool_policy: { allowed_tools: [] },
  });
  assert.equal(role.version, 3);
  assert.equal(role.model_profile_id, 'profile-a');
  assert.deepEqual(role.tool_policy?.allowed_tools, []);
});

test('Discovery preserves only the exact model IDs returned by Core', () => {
  assert.deepEqual(mapDiscoveredModels({ model_ids: ['gpt-5.6-luna', 'provider-model'] }), ['gpt-5.6-luna', 'provider-model']);
  assert.throws(() => mapDiscoveredModels({ model_ids: ['ok', 1] } as never), B2AdapterError);
});

test('Task mapping preserves source identity and server action availability', () => {
  const task = mapTask({
    source: { source_type: 'workflow_run', source_id: 'workflow-1' },
    thread_id: null,
    project_id: 'project-1',
    workspace_id: 'workspace-1',
    source_status: 'running',
    revision: 4,
    created_at: '2026-09-10T00:00:00Z',
    title: 'Run task',
    actions: [
      { action: 'inspect', availability: 'available', reason_code: null, projection_revision: 4 },
      { action: 'cancel', availability: 'blocked', reason_code: 'already_waiting', projection_revision: 4 },
    ],
  });
  assert.deepEqual(task.source, { source_type: 'workflow_run', source_id: 'workflow-1' });
  assert.equal(task.actions[1]?.availability, 'blocked');
  assert.equal(task.actions[1]?.reason_code, 'already_waiting');
});

test('Session history keeps canonical payloads and AgentInstance identities intact', () => {
  const history = mapSessionHistory({
    session: { id: 'session-1', role_snapshot: {} } as B2.Session,
    thread_id: 'thread-1',
    agents: [{ session_id: 'session-1', role_snapshot: {}, id: 'agent-1', status: 'completed' } as B2.AgentInstance],
    items: [{
      id: 'item-1', cursor: 8, thread_id: 'thread-1', turn_id: 'turn-1', position: 1,
      payload: { type: 'user_message', text: 'actual canonical text' },
    }],
    next_cursor: 8,
  });
  assert.equal(history.items[0]?.payload.type, 'user_message');
  assert.equal((history.items[0]?.payload as B2.UserMessagePayload).text, 'actual canonical text');
  assert.equal(history.agents[0]?.id, 'agent-1');
});

test('B2 adapter negotiates exact protocol and forwards cancel idempotency key', async () => {
  let receivedOptions: B2.CancelSessionOptions | undefined;
  const adapter = new B2LiveAdapter(fakeClient({
    cancelSession: async (_sessionId: string, options: B2.CancelSessionOptions) => {
      receivedOptions = options;
      return { accepted: true };
    },
  }));
  const protocol = await adapter.connect();
  assert.equal(protocol.protocol_version, 'b2.v1');
  const result = await adapter.cancelSession('session-1', 'stable-cancel-key');
  assert.equal(result.accepted, true);
  assert.deepEqual(receivedOptions, { idempotencyKey: 'stable-cancel-key' });
});

test('B2 adapter resolves one task by source identity without scanning a page', async () => {
  let requested: unknown;
  const adapter = new B2LiveAdapter(fakeClient({
    getTask: async (sourceId: string, options: B2.GetTaskOptions) => {
      requested = [sourceId, options];
      return {
        source: { source_type: 'workflow_run', source_id: sourceId }, thread_id: null,
        project_id: null, workspace_id: null, source_status: 'running', revision: 2,
        created_at: '2026-09-10T00:00:00Z', title: 'legacy run', actions: [],
      };
    },
  }) as never);
  const task = await adapter.getTask('workflow-1', 'workflow_run');
  assert.equal(task.source.source_id, 'workflow-1');
  assert.deepEqual(requested, ['workflow-1', { sourceType: 'workflow_run' }]);
});

test('B2 errors remain typed and transport failures are explicit', () => {
  const typed = normalizeB2Error({ code: 'permission_denied', message: 'Denied', retryable: false, recovery: 'none', detail: { scope: 'x' } });
  assert.equal(typed.detail.code, 'permission_denied');
  assert.deepEqual(typed.detail.detail, { scope: 'x' });
  const transport = normalizeB2Error(new Error('offline'));
  assert.equal(transport.detail.code, 'transport_unavailable');
  assert.equal(transport.detail.retryable, true);
});
