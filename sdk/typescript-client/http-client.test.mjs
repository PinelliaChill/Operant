import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdtempSync, rmSync } from 'node:fs';
import { createRequire } from 'node:module';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { after, test } from 'node:test';
import { fileURLToPath } from 'node:url';

const root = resolve(dirname(fileURLToPath(import.meta.url)), '../..');
const require = createRequire(import.meta.url);
const output = mkdtempSync(join(tmpdir(), 'operant-http-client-test-'));
try {
  execFileSync(process.execPath, [
    join(root, 'clients/gui/node_modules/typescript/bin/tsc'),
    '--outDir', output, '--module', 'commonjs', '--target', 'es2022',
    '--moduleResolution', 'node', '--skipLibCheck',
    join(root, 'sdk/typescript-client/http-client.ts'),
  ], { cwd: root, stdio: 'pipe' });
} catch (error) {
  rmSync(output, { recursive: true, force: true });
  throw error;
}
after(() => rmSync(output, { recursive: true, force: true }));
const { HttpClient } = require(join(output, 'typescript-client/http-client.js'));
const client = new HttpClient('http://127.0.0.1:1');

test('unconnected legacy projections never fabricate results or contact Core', async () => {
  let calls = 0;
  const original = globalThis.fetch;
  globalThis.fetch = async () => { calls++; throw new Error('unexpected network'); };
  try {
    const requests = [
      ['listThreads', []], ['getThread', ['thread-a']], ['listThreadMessages', ['thread-a']],
      ['getContextRevision', ['thread-a']], ['compactContext', ['thread-a']],
      ['listPendingApprovals', []], ['listPendingApprovals', ['session-a']],
      ['listGraphDrafts', []], ['getGraphDraft', ['draft-a']],
      ['saveGraphDraft', [{ workspace: '/tmp/synthetic', name: 'synthetic' }]],
      ['compileGraphDraft', ['draft-a']], ['publishGraphDraft', ['draft-a']],
      ['listGraphRevisions', []], ['listWorkflowRuns', []], ['getWorkflowRun', ['run-a']],
      ['startWorkflowRun', [{ task: 'test', workspace: '/tmp/synthetic' }]],
      ['resumeWorkflowRun', ['run-a', false]],
      ['listRemoteHosts', []], ['listRemoteDevices', []],
      ['requestDevicePairing', ['host-a', 'device-a', 'read', '000000']],
      ['revokeRemoteDevice', ['device-a']], ['sendRemoteCommand', ['host-a', 's-a', 'hello']],
      ['listSessions', ['/tmp/synthetic']],
    ];
    for (const [method, args] of requests) {
      await assert.rejects(client[method](...args), (error) => {
        assert.equal(error.code, 'SCHEMA_INCOMPATIBLE', method);
        assert.equal(error.recoverable, false, method);
        return true;
      });
    }
    assert.throws(() => client.subscribeEvents(), { code: 'SCHEMA_INCOMPATIBLE' });
    assert.throws(() => client.runSessionStream('s-a', 'test', '/tmp/synthetic', () => {}),
      { code: 'SCHEMA_INCOMPATIBLE' });
    assert.equal(calls, 0);
    assert.equal(client.isMock, false);
  } finally {
    globalThis.fetch = original;
  }
});

test('implemented health request still returns only the server result', async () => {
  const original = globalThis.fetch;
  const requests = [];
  globalThis.fetch = async (url) => {
    requests.push(url);
    return new Response(JSON.stringify({ status: 'server-status' }), { status: 200 });
  };
  try {
    assert.deepEqual(await client.checkHealth(), { status: 'server-status' });
    assert.equal(requests.length, 1);
  } finally {
    globalThis.fetch = original;
  }
});
