import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { resolveTerminalStreamUrl, terminalStreamProtocols } from '../src/live/terminalStreamUrl.ts';

test('terminal WebSocket URL is local and carries no one-use token', () => {
  assert.equal(
    resolveTerminalStreamUrl('http://localhost:3000', 'terminal/id'),
    'ws://localhost:3000/v1/workbench/terminals/terminal%2Fid/stream',
  );
  assert.equal(
    resolveTerminalStreamUrl('https://127.0.0.1:8000', 'terminal-1'),
    'wss://127.0.0.1:8000/v1/workbench/terminals/terminal-1/stream',
  );
  assert.throws(() => resolveTerminalStreamUrl('https://example.com', 'terminal-1'), /本机/);
  assert.throws(() => resolveTerminalStreamUrl('http://localhost:3000', ''), /终端 ID/);
  assert.deepEqual(terminalStreamProtocols('one_time-token'), ['operant.terminal.v1', 'operant.token.one_time-token']);
  assert.throws(() => terminalStreamProtocols('token/unsafe'), /连接凭据/);
});

test('packaged desktop CSP permits only its fixed local Core HTTP and terminal WebSocket', () => {
  const config = JSON.parse(readFileSync(new URL('../../desktop/src-tauri/tauri.conf.json', import.meta.url), 'utf8')) as { app: { security: { csp: string } } };
  const connect = config.app.security.csp.split(';').map((item) => item.trim()).find((item) => item.startsWith('connect-src '));
  assert.ok(connect);
  assert.deepEqual(connect.split(/\s+/).slice(1), ["'self'", 'http://127.0.0.1:8000', 'ws://127.0.0.1:8000']);
});
