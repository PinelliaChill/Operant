import assert from 'node:assert/strict';
import test from 'node:test';
import {
  canBindSessionToThread,
  canDecideApproval,
  commandStateAfterTerminalEvent,
  approvalProjectionResolved,
  LIVE_EVENT_WINDOW_SIZE,
  approvalActionKey,
  connectionLossState,
  containsManualReconcile,
  emptyEventAccumulator,
  eventNeedsManualReconcile,
  isCurrentProjectionResponse,
  isThreadSelectionLocked,
  isTerminalEvent,
  approvalsForSession,
  reduceEvent,
  sessionOptionsFor,
  shouldReleaseApprovalPending,
  streamEndedBeforeTerminalError,
  terminalEventError,
  terminalRunOutcome,
  threadForSession,
  projectionGeneration,
} from '../src/live/liveState.ts';

function event(sequence: number, id = `evt-${sequence}`) {
  return {
    id,
    sequence,
    event_type: 'model.delta',
    schema_version: 'phase1e.v1',
    thread_id: 'thread-live',
    occurred_at: `2026-09-01T00:00:${String(sequence).padStart(2, '0')}Z`,
    payload: { delta_text: `delta-${sequence}` },
  } as const;
}

test('reducer deduplicates by resource scope and committed cursor', () => {
  const first = reduceEvent(emptyEventAccumulator(), 'thread-live', event(7));
  const duplicate = reduceEvent(first, 'thread-live', event(7, 'different-id'));
  const otherScope = reduceEvent(first, 'thread-other', event(7));

  assert.equal(first.events.length, 1);
  assert.equal(first.cursor, 7);
  assert.equal(duplicate, first);
  assert.equal(otherScope.events.length, 2);
  assert.equal(otherScope.cursor, 7);
});

test('cursor reducer keeps an int64 cursor lossless', () => {
  const cursor = 9007199254740993n;
  const result = reduceEvent(emptyEventAccumulator(), 'thread-live', {
    ...event(1),
    id: 'large-cursor-event',
    sequence: cursor,
  });
  assert.equal(result.cursor, cursor);
  assert.equal(reduceEvent(result, 'thread-live', {
    ...event(1),
    id: 'replayed-event',
    sequence: cursor,
  }), result);
});

test('manual reconcile marker is found in nested projection payloads', () => {
  assert.equal(containsManualReconcile({ outcome: { state: 'outcome_unknown' } }), true);
  assert.equal(containsManualReconcile({ items: [{ status: 'manual_reconcile_required' }] }), true);
  assert.equal(containsManualReconcile({ status: 'completed' }), false);
});

test('manual reconcile SSE error/detail is sticky input and preserved', () => {
  const errorEvent = {
    ...event(2),
    error: {
      code: 'run_outcome_unknown',
      message: 'Core cannot determine the outcome',
      retryable: true,
      recovery: 'manual_reconcile',
      detail: { command_id: 'cmd-1' },
    },
    detail: { source: 'sse' },
  };
  assert.equal(eventNeedsManualReconcile(errorEvent), true);
  assert.deepEqual(errorEvent.error.detail, { command_id: 'cmd-1' });
  assert.equal(connectionLossState('disconnected').phase, 'error');
  assert.equal(connectionLossState('reconnecting').phase, 'connecting');
});

test('session projection and approval action keys retain exact scopes', () => {
  const projected = {
    id: 'thread-created',
    title: 'thread-created',
    workspaceRef: '/work/a',
    workspace: '/work/a',
    status: 'active',
    createdAt: 'x',
    updatedAt: 'y',
    created_at: 'x',
    updated_at: 'y',
    archivedAt: null,
    legacyRefs: [{ source_type: 'session', source_id: 'session-created' }],
    sessionId: 'session-created',
    session_id: 'session-created',
    workflowRunId: null,
  };
  assert.equal(threadForSession([projected], 'session-created'), projected);
  assert.equal(threadForSession([projected], 'session-missing'), undefined);
  assert.notEqual(approvalActionKey('session-a', 'approval-a', 'approve'), approvalActionKey('session-a', 'approval-a', 'reject'));
  let result = emptyEventAccumulator();
  for (let sequence = 1; sequence <= LIVE_EVENT_WINDOW_SIZE + 10; sequence += 1) {
    result = reduceEvent(result, 'thread-live', event(sequence));
  }
  assert.equal(result.events.length, LIVE_EVENT_WINDOW_SIZE);
  assert.equal(result.seen.size, LIVE_EVENT_WINDOW_SIZE);
});

test('session creation is disabled without an active unbound selected Thread', () => {
  const activeUnbound = { status: 'active', sessionId: null } as never;
  const bound = { status: 'active', sessionId: 'session-a' } as never;
  const archived = { status: 'archived', sessionId: null } as never;
  assert.equal(canBindSessionToThread(undefined), false);
  assert.equal(canBindSessionToThread(activeUnbound), true);
  assert.equal(canBindSessionToThread(bound), false);
  assert.equal(canBindSessionToThread(archived), false);
});

test('reload exposes a preexisting projected Session without local details', () => {
  const thread = { id: 'thread-existing', sessionId: 'session-existing' } as never;
  const options = sessionOptionsFor([thread], []);

  assert.deepEqual(options, [{
    id: 'session-existing',
    boundThreadId: 'thread-existing',
  }]);
  assert.equal(options[0].details, undefined);
  // The exact projected ID is the run target; a cached Session object is not
  // required to construct the Phase 1E run request.
  assert.equal(options[0].id, 'session-existing');
});

test('a projected binding upgrades a cached Session without duplicating its option', () => {
  const thread = { id: 'thread-created', sessionId: 'session-created' } as never;
  const cached = { id: 'session-created', role_snapshot: { role_name: 'cached role' } } as never;
  const options = sessionOptionsFor([thread], [cached, cached]);

  assert.equal(options.length, 1);
  assert.equal(options[0].id, 'session-created');
  assert.equal(options[0].details, cached);
  assert.equal(options[0].boundThreadId, 'thread-created');
});

test('an unbound Thread does not borrow a page-local cached Session', () => {
  const thread = { id: 'thread-unbound', sessionId: null } as never;
  const cached = { id: 'session-other' } as never;
  const options = sessionOptionsFor([thread], [cached]);

  assert.equal(options.length, 1);
  assert.equal(options[0].id, 'session-other');
  assert.equal(options[0].boundThreadId, null);
});

test('an approval remains actionable while its run awaits projection', () => {
  const approval = { status: 'pending' } as never;
  const idleAction = { status: 'idle' } as never;
  const commandStatus = 'awaiting_projection';

  // The run command can be awaiting the next projection while Core waits for
  // either decision.  Approval gating must not inspect that command status.
  assert.equal(commandStatus, 'awaiting_projection');
  for (const decision of ['approve', 'reject'] as const) {
    assert.equal(canDecideApproval(approval, idleAction, 'connected', 'connected', false), true, decision);
  }
});

test('an approval submission in flight prevents duplicate decisions', () => {
  const approval = { status: 'pending' } as never;
  for (const status of ['sending', 'awaiting_projection'] as const) {
    assert.equal(
      canDecideApproval(approval, { status } as never, 'connected', 'connected', false),
      false,
    );
  }
  assert.equal(
    canDecideApproval(approval, { status: 'idle' } as never, 'connected', 'connected', true),
    false,
  );
});

test('approval projection correction keeps the active stream generation', () => {
  const activeGeneration = 17;

  assert.equal(projectionGeneration(activeGeneration, false), activeGeneration);
  assert.equal(projectionGeneration(activeGeneration, true), activeGeneration + 1);
});

test('approval stays pending until its exact Query projection is gone', () => {
  const pending = { sessionId: 'session-a', id: 'approval-a', status: 'pending' } as never;

  assert.equal(approvalProjectionResolved('session-a', 'approval-a', [pending]), false);
  assert.equal(approvalProjectionResolved('session-a', 'approval-a', [
    { sessionId: 'session-other', id: 'approval-a', status: 'pending' },
  ] as never), true);
  assert.equal(approvalProjectionResolved('session-a', 'approval-a', []), true);
});

test('projection correction commits only the newest response in its generation', () => {
  assert.equal(isCurrentProjectionResponse(3, 3, 8, 8), true);
  assert.equal(isCurrentProjectionResponse(2, 3, 8, 8), false);
  assert.equal(isCurrentProjectionResponse(3, 3, 7, 8), false);
});

test('pending run or Session command blocks Thread changes with an explicit state', () => {
  assert.equal(isThreadSelectionLocked('thread-a', 'thread-b', true, false, 'idle'), true);
  assert.equal(isThreadSelectionLocked('thread-a', 'thread-b', false, true, 'idle'), true);
  assert.equal(isThreadSelectionLocked('thread-a', 'thread-b', false, false, 'sending'), true);
  assert.equal(isThreadSelectionLocked('thread-a', 'thread-b', false, false, 'awaiting_projection'), true);
  assert.equal(isThreadSelectionLocked('thread-a', 'thread-b', false, false, 'idle', true), true);
  assert.equal(isThreadSelectionLocked('thread-a', 'thread-b', false, false, 'idle', false, 'awaiting_projection'), true);
  assert.equal(isThreadSelectionLocked('thread-a', 'thread-b', false, false, 'error'), false);
  assert.equal(isThreadSelectionLocked('thread-a', 'thread-a', true, true, 'sending'), false);
});

test('unbound selected Thread has no approvals from another Session', () => {
  const approvals = [
    { id: 'approval-a', sessionId: 'session-a', status: 'pending' },
    { id: 'approval-b', sessionId: 'session-b', status: 'pending' },
  ] as never;
  assert.deepEqual(approvalsForSession(approvals, null), []);
  assert.deepEqual(approvalsForSession(approvals, 'session-a'), [approvals[0]]);
});

test('only deterministic approval failures release the pending lock', () => {
  assert.equal(shouldReleaseApprovalPending({
    code: 'approval_conflict',
    retryable: false,
    recovery: 'none',
  }), true);
  assert.equal(shouldReleaseApprovalPending({
    code: 'transport_unavailable',
    retryable: true,
    recovery: 'retry_later',
  }), false);
  assert.equal(shouldReleaseApprovalPending({
    code: 'session_manual_reconcile_required',
    retryable: false,
    recovery: 'manual_reconcile',
  }), false);
  assert.equal(shouldReleaseApprovalPending({
    code: 'approval_unknown_error',
    retryable: false,
    recovery: 'none',
  }), false);
  assert.equal(shouldReleaseApprovalPending({
    code: 'approval_conflict',
    retryable: false,
    recovery: 'none',
    detail: { recovery: 'manual_reconcile' },
  }), false);
});

test('normal stream EOF is a retryable failure for the original command key', () => {
  const failure = streamEndedBeforeTerminalError();
  assert.equal(failure.code, 'stream_ended_before_terminal');
  assert.equal(failure.retryable, true);
  assert.equal(failure.recovery, 'retry_same_idempotency_key');
});

test('only terminal SSE releases a pending run and failures stay typed', () => {
  const failed = {
    ...event(3),
    event_type: 'agent.failed',
    payload: {
      error_type: 'ProviderError',
      message: 'provider unavailable',
      recoverable: false,
    },
  } as never;
  const approvalRequired = { ...event(4), event_type: 'tool.approval_required' } as never;
  const awaitingCommand = { status: 'awaiting_projection', idempotencyKey: 'run-1' } as never;

  assert.equal(terminalRunOutcome(failed), 'failed');
  assert.equal(terminalRunOutcome(approvalRequired), null);
  assert.deepEqual(commandStateAfterTerminalEvent(failed, awaitingCommand), { status: 'idle' });
  assert.deepEqual(commandStateAfterTerminalEvent(approvalRequired, awaitingCommand), awaitingCommand);
  assert.deepEqual(terminalEventError(failed), {
    code: 'agent_failed',
    message: 'provider unavailable',
    retryable: false,
    recovery: 'none',
    detail: failed.payload,
  });
  assert.equal(terminalEventError(approvalRequired), undefined);
});

test('all known terminal failure events release command state and preserve typed codes', () => {
  const expectedCodes = {
    'budget.exhausted': 'budget_exhausted',
    'agent.no_progress': 'agent_no_progress',
    'agent.max_turns': 'agent_max_turns',
    'session.run_failed': 'session_run_failed',
  } as const;

  for (const [eventType, expectedCode] of Object.entries(expectedCodes)) {
    const terminal = {
      ...event(5),
      event_type: eventType,
      payload: { reason: 'known terminal reason' },
    } as never;
    assert.equal(terminalRunOutcome(terminal), 'failed', eventType);
    assert.equal(isTerminalEvent(terminal), true, eventType);
    assert.deepEqual(
      commandStateAfterTerminalEvent(terminal, { status: 'awaiting_projection', idempotencyKey: 'run-1' }),
      { status: 'idle' },
      eventType,
    );
    assert.equal(terminalEventError(terminal)?.code, expectedCode, eventType);
  }
});
