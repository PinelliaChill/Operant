import assert from 'node:assert/strict';
import test from 'node:test';
import {
  IdempotencyKeyRegistry,
  RUNTIME_CURSOR_SCOPE_WINDOW,
  RUNTIME_EVENT_WINDOW,
  canAutomaticallyReplayCommand,
  consumeIteratorForEpoch,
  containsUnknownOutcome,
  cursorForScope,
  emptyRuntimeEventState,
  eventsForScope,
  graphRunIsTerminal,
  reconnectRecovery,
  reduceRuntimeEvent,
} from '../src/live23/runtimeState.ts';

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((nextResolve, nextReject) => {
    resolve = nextResolve;
    reject = nextReject;
  });
  return { promise, resolve, reject };
}

test('runtime events deduplicate within resource scope and keep int64 cursors', () => {
  const cursor = 9007199254740993n;
  const event = {
    id: 'event-1',
    cursor,
    eventType: 'node.succeeded',
    resourceScope: 'graph_run:run-1',
    payload: {},
  };
  const first = reduceRuntimeEvent(emptyRuntimeEventState(), event);
  assert.equal(cursorForScope(first, event.resourceScope), cursor);
  assert.equal(reduceRuntimeEvent(first, event), first);
  const otherScope = reduceRuntimeEvent(first, { ...event, cursor: 3, resourceScope: 'team_run:team-1' });
  assert.equal(otherScope.events.length, 2);
  assert.equal(cursorForScope(otherScope, event.resourceScope), cursor);
  assert.equal(cursorForScope(otherScope, 'team_run:team-1'), 3);
  assert.deepEqual(eventsForScope(otherScope, event.resourceScope), [event]);
  assert.equal(eventsForScope(otherScope, 'graph_run:missing').length, 0);
});

test('runtime event memory is bounded without losing committed cursor', () => {
  let state = emptyRuntimeEventState();
  for (let index = 1; index <= RUNTIME_EVENT_WINDOW + 7; index += 1) {
    state = reduceRuntimeEvent(state, {
      id: `event-${index}`,
      cursor: index,
      eventType: 'node.updated',
      resourceScope: 'graph_run:run-1',
      payload: {},
    });
  }
  assert.equal(state.events.length, RUNTIME_EVENT_WINDOW);
  assert.equal(state.seen.size, RUNTIME_EVENT_WINDOW);
  assert.equal(cursorForScope(state, 'graph_run:run-1'), RUNTIME_EVENT_WINDOW + 7);
});

test('runtime cursors are isolated per resource scope and bounded', () => {
  let state = reduceRuntimeEvent(emptyRuntimeEventState(), {
    id: 'run-a-100', cursor: 100, eventType: 'node.updated', resourceScope: 'graph_run:run-a', payload: {},
  });
  assert.equal(cursorForScope(state, 'graph_run:run-b'), 0);
  state = reduceRuntimeEvent(state, {
    id: 'run-b-1', cursor: 1, eventType: 'node.updated', resourceScope: 'graph_run:run-b', payload: {},
  });
  assert.equal(cursorForScope(state, 'graph_run:run-a'), 100);
  assert.equal(cursorForScope(state, 'graph_run:run-b'), 1);

  for (let index = 0; index <= RUNTIME_CURSOR_SCOPE_WINDOW; index += 1) {
    state = reduceRuntimeEvent(state, {
      id: `scope-${index}`, cursor: 1, eventType: 'node.updated', resourceScope: `graph_run:scope-${index}`, payload: {},
    });
  }
  assert.equal(state.cursors.size, RUNTIME_CURSOR_SCOPE_WINDOW);
});

test('disconnect recovery replays before authoritative Query correction', () => {
  assert.deepEqual(reconnectRecovery(17), [
    { kind: 'replay', cursor: 17 },
    { kind: 'query' },
  ]);
});

test('a fulfilled iterator result cannot write after its epoch becomes stale', async () => {
  const pending = deferred<IteratorResult<string>>();
  let epoch = 1;
  const written: string[] = [];
  const consuming = consumeIteratorForEpoch(
    { next: () => pending.promise },
    () => epoch === 1,
    (value) => written.push(value),
  );

  epoch = 2;
  pending.resolve({ done: false, value: 'late event from old workspace' });

  assert.equal(await consuming, 'stale');
  assert.deepEqual(written, []);
});

test('a rejected iterator result is ignored after its epoch becomes stale', async () => {
  const pending = deferred<IteratorResult<string>>();
  let epoch = 1;
  const consuming = consumeIteratorForEpoch(
    { next: () => pending.promise },
    () => epoch === 1,
    () => assert.fail('stale stream must not publish an event'),
  );

  epoch = 2;
  pending.reject(new Error('late disconnect from old run'));

  assert.equal(await consuming, 'stale');
});

test('current iterator EOF and rejection remain visible to the active scope', async () => {
  const ended = consumeIteratorForEpoch(
    { next: async () => ({ done: true, value: undefined }) },
    () => true,
    () => assert.fail('EOF must not publish a value'),
  );
  assert.equal(await ended, 'eof');

  const failure = new Error('active stream disconnected');
  await assert.rejects(
    consumeIteratorForEpoch(
      { next: async () => { throw failure; } },
      () => true,
      () => assert.fail('rejection must not publish a value'),
    ),
    failure,
  );
});

test('workspace switch clears Graph and Team projections before a pending event resolves', async () => {
  const pending = deferred<IteratorResult<string>>();
  let workspaceEpoch = 1;
  let graphProjection = ['old graph'];
  let teamProjection = ['old team'];
  const consuming = consumeIteratorForEpoch(
    { next: () => pending.promise },
    () => workspaceEpoch === 1,
    (value) => graphProjection.push(value),
  );

  workspaceEpoch += 1;
  graphProjection = [];
  teamProjection = [];
  pending.resolve({ done: false, value: 'late old-workspace event' });

  assert.equal(await consuming, 'stale');
  assert.deepEqual(graphProjection, []);
  assert.deepEqual(teamProjection, []);
});

test('normal EOF is idle only for authoritative terminal graph states', () => {
  assert.equal(graphRunIsTerminal('completed'), true);
  assert.equal(graphRunIsTerminal('failed'), true);
  assert.equal(graphRunIsTerminal('cancelled'), true);
  assert.equal(graphRunIsTerminal('interrupted'), true);
  assert.equal(graphRunIsTerminal('manual_reconcile_required'), true);
  assert.equal(graphRunIsTerminal('running'), false);
  assert.equal(graphRunIsTerminal('waiting_input'), false);
});

test('unknown side effects are sticky and never automatically replayed', () => {
  assert.equal(containsUnknownOutcome({ attempt: { status: 'manual_reconcile_required' } }), true);
  assert.equal(containsUnknownOutcome({ error: { code: 'action_outcome_unknown' } }), true);
  assert.equal(canAutomaticallyReplayCommand({ status: 'failed', retryable: true }), true);
  assert.equal(canAutomaticallyReplayCommand({ status: 'outcome_unknown', retryable: true }), false);
});

test('idempotency registry reuses a key until the logical action is released', () => {
  const keys = new IdempotencyKeyRegistry();
  const first = keys.get('start-run:definition-1');
  assert.equal(keys.get('start-run:definition-1'), first);
  assert.notEqual(keys.get('ack:delivery-1'), first);
  keys.release('start-run:definition-1');
  assert.notEqual(keys.get('start-run:definition-1'), first);
});

test('idempotency keys remain header-safe when the logical action contains Chinese text', () => {
  const key = new IdempotencyKeyRegistry().get('消息:你好，世界');
  assert.match(key, /^[\x20-\x7e]+$/);
});
