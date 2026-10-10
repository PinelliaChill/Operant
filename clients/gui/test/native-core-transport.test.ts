import assert from 'node:assert/strict';
import test from 'node:test';
import { createNativeCoreTransport, detectNativeCoreEnvironment } from '../src/lib/nativeCoreTransport.ts';
import type { Phase1ERequest, Phase1EResponse } from '../../../sdk/typescript-client/phase1e-transport.ts';

const base = 'http://127.0.0.1:8000';
const packaged = { native: true, expectedOrigin: base };

function request(method: string, path: string, suffix = ''): Phase1ERequest {
  return { method, path, url: `${base}${path}${suffix}` };
}

test('native transport preserves the generated client wire request and raw response', async () => {
  const calls: unknown[] = [];
  let fallbackCount = 0;
  const transport = createNativeCoreTransport(async (value) => {
    calls.push(value);
    return { status: 201, headers: { 'idempotency-key': 'create-key', 'idempotency-replayed': 'true' }, text: '{"revision":9007199254740993}' };
  }, async () => {
    fallbackCount += 1;
    throw new Error('unsigned fetch must not run');
  }, packaged);
  const body = JSON.stringify({ title: '新任务 ✨', workspace_id: 'workspace_1' });
  const wire = {
    ...request('POST', '/v1/setup/conversations', '?request_id=k%2B1&label=%E4%B8%AD%E6%96%87'),
    headers: { 'Idempotency-Key': 'create-key', 'Content-Type': 'application/json', 'X-Operant-Client-Version': 'onboarding.v1' },
    body,
  };
  const response = await transport(wire);
  assert.deepEqual(calls, [{
    operationId: 'initializeConversation', pathParams: {},
    rawQuery: 'request_id=k%2B1&label=%E4%B8%AD%E6%96%87', headers: wire.headers, body,
  }]);
  assert.equal(response.status, 201);
  assert.deepEqual(response.headers, { 'idempotency-key': 'create-key', 'idempotency-replayed': 'true' });
  assert.equal(await (response.text as () => string)(), '{"revision":9007199254740993}');
  assert.equal(fallbackCount, 0);
});

test('native transport uses only exact schema operations across protected clients', async () => {
  const operations: Array<[string, string, string, Record<string, string>]> = [
    ['GET', '/v1/setup/conversations/thread_1/metadata', 'getConversationMetadata', { thread_id: 'thread_1' }],
    ['POST', '/v1/local-control/sessions/session_1/act', 'actLocalControlSession', { session_id: 'session_1' }],
    ['GET', '/v1/remote-control/gateway/connections', 'listRemoteGatewayConnections', {}],
    ['POST', '/v1/extensions/plugin_1/enable', 'enableExtension', { plugin_id: 'plugin_1' }],
    ['POST', '/v1/workbench/threads/thread_1/skill-commands', 'executeSkillCommand', { thread_id: 'thread_1' }],
    ['GET', '/v1/graph/runs/run_1/writer-workspaces', 'listWriterWorkspaces', { run_id: 'run_1' }],
    ['POST', '/v1/merge-runs/run_1/finalize', 'finalizeMergeRun', { merge_run_id: 'run_1' }],
    ['POST', '/v1/writer-workspaces/workspace_1/container/start', 'startContainerWriter', { workspace_id: 'workspace_1' }],
    ['GET', '/v1/local-callers/devices', 'listCallerDevices', {}],
    ['GET', '/v1/local-callers/requests/result', 'getCallerRequest', {}],
    ['POST', '/v1/local-callers/devices/caller_0123456789abcdef0123456789abcdef/revoke', 'revokeCallerDevice', { device_id: 'caller_0123456789abcdef0123456789abcdef' }],
  ];
  const seen: unknown[] = [];
  const transport = createNativeCoreTransport(async (value) => {
    seen.push(value);
    return { status: 200, headers: {}, text: '{}' };
  }, async () => { throw new Error('unsigned fetch must not run'); }, packaged);
  for (const [method, path] of operations) await transport(request(method, path));
  assert.deepEqual(seen, operations.map(([, , operationId, pathParams]) => ({
    operationId, pathParams, rawQuery: '', headers: {}, body: '',
  })));
});

test('device-password posts and unrelated routes keep their existing fetch transport', async () => {
  const fetched: string[] = [];
  const fallback = async (wire: Phase1ERequest): Promise<Phase1EResponse> => {
    fetched.push(wire.path ?? '');
    return { status: 204, text: '' };
  };
  const transport = createNativeCoreTransport(async () => {
    throw new Error('native invoke must not run');
  }, fallback, packaged);
  for (const path of ['/v1/remote-control/commands', '/v1/remote-control/devices/pair', '/v1/remote-control/session-query']) {
    await transport(request('POST', path));
  }
  await transport(request('GET', '/v1/graph/runs/run_1'));
  await transport(request('GET', '/v1/protocol/onboarding'));
  assert.deepEqual(fetched, [
    '/v1/remote-control/commands', '/v1/remote-control/devices/pair', '/v1/remote-control/session-query',
    '/v1/graph/runs/run_1', '/v1/protocol/onboarding',
  ]);
  await createNativeCoreTransport(async () => { throw new Error('native invoke must not run'); }, fallback,
    { native: false, expectedOrigin: null })(request('POST', '/v1/setup/conversations'));
  assert.equal(fetched.at(-1), '/v1/setup/conversations');
});

test('unknown or malformed protected requests never fall back to unsigned fetch', async () => {
  let invoked = 0;
  let fetched = 0;
  const transport = createNativeCoreTransport(async () => {
    invoked += 1;
    return { status: 200, headers: {}, text: '{}' };
  }, async () => {
    fetched += 1;
    return { status: 200, text: '{}' };
  }, packaged);
  const invalid = [
    request('PUT', '/v1/setup/conversations'),
    request('GET', '/v1/setup/unknown'),
    request('POST', '/v1/writer-workspaces/workspace_1/container/unknown'),
    request('POST', '/v1/workbench/threads/thread_1/skill-commands/retry'),
    request('GET', '/v1/graph/runs/run_1/writer-conflicts/unknown'),
    request('POST', '/v1/local-callers/pair'),
    request('POST', '/v1/local-callers/challenges'),
    { ...request('GET', '/v1/setup/state'), url: 'https://example.com/v1/setup/state' },
    { ...request('GET', '/v1/setup/state'), path: '/v1/setup/other' },
    { ...request('GET', '/v1/setup/state'), url: `${base}/v1/protocol/onboarding` },
    request('GET', '/v1/%73etup/state'),
    request('GET', '/v1%252Fsetup/state'),
    { ...request('GET', '/v1/setup/conversations/thread_1/metadata'), url: `${base}/v1/setup/conversations/thread%2F1/metadata` },
  ];
  for (const wire of invalid) await assert.rejects(transport(wire));
  assert.equal(invoked, 0);
  assert.equal(fetched, 0);
});

test('lost native write result is a transport failure and never silently replays over fetch', async () => {
  let calls = 0;
  let fetched = 0;
  const transport = createNativeCoreTransport(async () => {
    calls += 1;
    throw new Error('secret native detail');
  }, async () => {
    fetched += 1;
    return { status: 200, text: '{}' };
  }, packaged);
  await assert.rejects(transport({ ...request('POST', '/v1/setup/conversations'), headers: { 'Idempotency-Key': 'original-key' } }),
    (error: unknown) => error instanceof Error && /结果未确认/.test(error.message) && !error.message.includes('secret'));
  assert.equal(calls, 1);
  assert.equal(fetched, 0);
});

test('Tauri development on the current numeric loopback origin uses native transport', async () => {
  const previous = Object.getOwnPropertyDescriptor(globalThis, 'window');
  try {
    Object.defineProperty(globalThis, 'window', {
      configurable: true,
      value: { __TAURI_INTERNALS__: {}, location: new URL('http://127.0.0.1:3000/chat') },
    });
    const environment = detectNativeCoreEnvironment();
    assert.deepEqual(environment, { native: true, expectedOrigin: 'http://127.0.0.1:3000' });
    let nativeCalls = 0;
    let fallbackCalls = 0;
    const transport = createNativeCoreTransport(async () => {
      nativeCalls += 1;
      return { status: 200, headers: {}, text: '{}' };
    }, async () => {
      fallbackCalls += 1;
      return { status: 200, text: '{}' };
    }, environment);
    await transport({ method: 'GET', path: '/v1/setup/state', url: 'http://127.0.0.1:3000/v1/setup/state' });
    assert.equal(nativeCalls, 1);
    await assert.rejects(transport({ method: 'GET', path: '/v1/setup/state', url: `${base}/v1/setup/state` }));
    assert.equal(fallbackCalls, 0);
    await assert.rejects(createNativeCoreTransport(async () => {
      throw new Error('native request failed');
    }, async () => {
      fallbackCalls += 1;
      return { status: 200, text: '{}' };
    }, environment)({ method: 'POST', path: '/v1/setup/conversations', url: 'http://127.0.0.1:3000/v1/setup/conversations' }));
    assert.equal(fallbackCalls, 0);

    Object.defineProperty(globalThis, 'window', {
      configurable: true,
      value: { __TAURI_INTERNALS__: {}, location: new URL('tauri://localhost/') },
    });
    assert.deepEqual(detectNativeCoreEnvironment(), packaged);

    Object.defineProperty(globalThis, 'window', {
      configurable: true,
      value: { __TAURI_INTERNALS__: {}, location: new URL('http://example.com/chat') },
    });
    const unsafe = detectNativeCoreEnvironment();
    assert.deepEqual(unsafe, { native: true, expectedOrigin: null });
    await assert.rejects(createNativeCoreTransport(async () => {
      nativeCalls += 1;
      return { status: 200, headers: {}, text: '{}' };
    }, async () => {
      fallbackCalls += 1;
      return { status: 200, text: '{}' };
    }, unsafe)({ method: 'GET', path: '/v1/setup/state', url: 'http://example.com/v1/setup/state' }));
    assert.equal(fallbackCalls, 0);

    Object.defineProperty(globalThis, 'window', {
      configurable: true,
      value: { location: new URL('http://127.0.0.1:3000/chat') },
    });
    assert.deepEqual(detectNativeCoreEnvironment(), { native: false, expectedOrigin: null });
  } finally {
    if (previous) Object.defineProperty(globalThis, 'window', previous);
    else Reflect.deleteProperty(globalThis, 'window');
  }
});
