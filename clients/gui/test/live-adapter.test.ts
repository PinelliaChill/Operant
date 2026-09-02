import assert from 'node:assert/strict';
import test from 'node:test';
import {
  LiveAdapterError,
  LiveClientAdapter,
  UnsupportedLiveCapabilityError,
  mapApprovalProjection,
  mapProjectProjection,
  mapSseFrame,
  mapThreadProjection,
} from '../src/live/liveAdapter.ts';

function fakeClient(overrides = {}) {
  return {
    protocolVersion: 'phase1e.v1',
    schemaDigest: '0'.repeat(64),
    negotiateProtocol: async () => ({
      protocol_version: 'phase1e.v1',
      schema_digest: '0'.repeat(64),
      min_client_version: 'phase1e.v1',
      capabilities: [],
    }),
    listProjects: async () => [],
    listThreads: async () => [],
    listPendingApprovals: async () => [],
    listWorkspaceFiles: async () => ({ entries: [] }),
    createSession: async () => ({ id: 'session-created' }),
    runSessionStream: async () => ({
      receipt: null,
      metadata: { status: 200, idempotencyReplayed: false },
      events: (async function* () {})(),
    }),
    submitApproval: async () => ({ accepted: true, changed: true, approved: true, status: 'approved' }),
    ...overrides,
  };
}

test('live adapter negotiates through the generated client instance', async () => {
  let calls = 0;
  const client = fakeClient({ negotiateProtocol: async (force: boolean) => {
    calls += 1;
    assert.equal(force, true);
    return { protocol_version: 'phase1e.v1', schema_digest: '0'.repeat(64), min_client_version: 'phase1e.v1', capabilities: [] };
  } });
  const adapter = new LiveClientAdapter(client as never);
  const metadata = await adapter.connect();
  assert.equal(metadata.protocol_version, 'phase1e.v1');
  assert.equal(calls, 1);
});

test('live createSession forwards the exact selected Thread binding', async () => {
  let request: unknown;
  const adapter = new LiveClientAdapter(fakeClient({
    createSession: async (value: unknown) => {
      request = value;
      return { id: 'session-created' };
    },
  }) as never);
  await adapter.createSession({ roleId: 'role-a', threadId: 'thread-selected' });
  assert.deepEqual(request, {
    role_id: 'role-a',
    new_role: undefined,
    model_profile_id: undefined,
    effort: undefined,
    budget_overrides: undefined,
    thread_id: 'thread-selected',
  });
});

test('project and thread relationships use exact generated IDs and legacy refs', () => {
  const project = mapProjectProjection({
    project_id: 'project-a',
    workspace_ref: '/work/a',
    readable: true,
    writable: false,
    created_at: '2026-09-01T00:00:00Z',
    threads: [
      { id: 'thread-exact', status: 'active', created_at: 'x', updated_at: 'y' },
    ],
    workflow_runs: [
      { id: 'run-exact', status: 'active', current_stage: 'run', summary: '', created_at: 'x', updated_at: 'y' },
    ],
  });
  assert.deepEqual(project.threadIds, ['thread-exact']);
  assert.deepEqual(project.runIds, ['run-exact']);

  const thread = mapThreadProjection({
    id: 'thread-exact',
    cursor: null,
    parent_thread_id: null,
    workspace_ref: '/work/other',
    status: 'active',
    legacy_refs: [
      { source_type: 'session', source_id: 'session-exact' },
      { source_type: 'workflow_run', source_id: 'run-exact' },
    ],
    created_at: 'x',
    updated_at: 'y',
    archived_at: null,
  });
  assert.equal(thread.sessionId, 'session-exact');
  assert.equal(thread.workflowRunId, 'run-exact');
  assert.equal(thread.workspace, '/work/other');
});

test('run stream keeps Promise plus AsyncIterable and lossless replay cursor', async () => {
  let receivedOptions;
  const largeCursor = 9007199254740993n;
  const client = fakeClient({
    runSessionStream: async (...args: unknown[]) => {
      receivedOptions = args[2];
      return {
        receipt: null,
        metadata: { status: 200, idempotencyReplayed: false },
        events: (async function* () {
          yield {
            id: largeCursor,
            event: 'model.delta',
            data: { id: 'evt-large', event_type: 'model.delta', session_id: 'session-a', payload: { delta_text: 'ok' } },
            resource_scope: 'session:session-a',
            stream_kind: 'session.run',
          };
        })(),
      };
    },
  });
  const adapter = new LiveClientAdapter(client as never);
  const stream = await adapter.runSessionStream(
    'session-a',
    { message: 'hello', workspace: '/work/a', thread_id: 'thread-a' },
    { idempotencyKey: 'stable-key', lastEventId: largeCursor },
  );
  const frames = [];
  for await (const frame of stream.events) frames.push(frame);
  assert.deepEqual(receivedOptions, { idempotencyKey: 'stable-key', lastEventId: largeCursor });
  assert.equal(frames[0].id, largeCursor);
  const event = mapSseFrame(frames[0], 'session-a');
  assert.equal(event.sequence, largeCursor);
  assert.equal(event.thread_id, undefined);
});

test('SSE error recovery and detail survive generated frame mapping', () => {
  const frame = {
    id: 12n,
    event: 'run.error',
    resource_scope: 'session:session-a',
    stream_kind: 'session.run',
    data: {
      event_type: 'run.error',
      error: {
        code: 'run_outcome_unknown',
        message: 'Outcome cannot be determined',
        retryable: true,
        recovery: 'manual_reconcile',
        detail: { command_id: 'cmd-a' },
      },
      detail: { source: 'core' },
      payload: { state: 'outcome_unknown' },
    },
  } as const;
  const event = mapSseFrame(frame, 'session-a');
  assert.equal(event.error?.recovery, 'manual_reconcile');
  assert.deepEqual(event.error?.detail, { command_id: 'cmd-a' });
  assert.deepEqual(event.detail, { source: 'core' });
});

test('missing Schema operations and session scopes fail explicitly', async () => {
  let createCalls = 0;
  const adapter = new LiveClientAdapter(fakeClient({
    createSession: async () => {
      createCalls += 1;
      return { id: 'session-created' };
    },
  }) as never);
  await assert.rejects(() => adapter.listThreadMessages('thread-a'), UnsupportedLiveCapabilityError);
  await assert.rejects(() => adapter.cancelSession('session-a'), UnsupportedLiveCapabilityError);
  await assert.rejects(() => adapter.listPendingApprovals(''), (error: unknown) => (
    error instanceof LiveAdapterError && error.detail.code === 'session_required'
  ));
  await assert.rejects(() => adapter.createSession({ threadId: '' }));
  await assert.rejects(() => adapter.createSession({ threadId: 'thread-a' }));
  await assert.rejects(() => adapter.createSession({ threadId: 'thread-a', newRole: { name: '', system_prompt: '', model_profile_id: '' } }));
  assert.equal(createCalls, 0);
});

test('approval projection carries its required session scope', () => {
  const approval = mapApprovalProjection('session-a', {
    approval_id: 'approval-a',
    tool_call_id: 'tool-a',
    category: 'command',
    detail: 'run tests',
    action_hash: '0'.repeat(64),
    status: 'pending',
    requested_at: 'x',
    expires_at: 'y',
    continuation_available: true,
  });
  assert.equal(approval.sessionId, 'session-a');
  assert.equal(approval.toolCallId, 'tool-a');
});
