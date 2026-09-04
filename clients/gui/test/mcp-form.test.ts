import assert from 'node:assert/strict';
import test from 'node:test';
import { buildMcpServerRequest } from '../src/live45/mcpForm.ts';

const digest = `registry.example/mcp@sha256:${'a'.repeat(64)}`;

test('stdio form uses only a server-returned root with relative cwd and digest-pinned image', () => {
  const request = buildMcpServerRequest({
    serverId: 'local', transport: 'stdio', stdioArgv: '["node", "server.js"]',
    cwdRef: 'tools/mcp', workspaceRootRef: 'trusted-root', dockerImage: digest,
    endpointRef: 'IGNORED_ENDPOINT', secretRef: 'IGNORED_SECRET',
  }, ['trusted-root']);
  assert.deepEqual(request, {
    server_id: 'local', transport: 'stdio', stdio_argv: ['node', 'server.js'],
    cwd_ref: 'tools/mcp', workspace_root_ref: 'trusted-root', docker_image: digest,
  });
  assert.throws(() => buildMcpServerRequest({
    serverId: 'local', transport: 'stdio', stdioArgv: '["node"]', cwdRef: '.',
    workspaceRootRef: 'typed-by-user', dockerImage: digest, endpointRef: '', secretRef: '',
  }, ['trusted-root']), /Core 返回/);
  assert.throws(() => buildMcpServerRequest({
    serverId: 'local', transport: 'stdio', stdioArgv: '["node"]', cwdRef: '../escape',
    workspaceRootRef: 'trusted-root', dockerImage: 'node:latest', endpointRef: '', secretRef: '',
  }, ['trusted-root']), /sha256 digest/);
});
test('legacy SSE form emits secret references and no stdio fields', () => {
  const request = buildMcpServerRequest({
    serverId: 'remote', transport: 'legacy_sse', stdioArgv: '["ignored"]', cwdRef: '/ignored',
    workspaceRootRef: 'ignored', dockerImage: 'ignored', endpointRef: 'MCP_ENDPOINT',
    secretRef: 'MCP_SECRET',
  }, []);
  assert.deepEqual(request, {
    server_id: 'remote', transport: 'legacy_sse', endpoint_ref: 'MCP_ENDPOINT',
    secret_ref: 'MCP_SECRET',
  });
  assert.equal('stdio_argv' in request, false);
  assert.equal('workspace_root_ref' in request, false);
  assert.throws(() => buildMcpServerRequest({
    serverId: 'remote', transport: 'legacy_sse', stdioArgv: '[]', cwdRef: '.',
    workspaceRootRef: '', dockerImage: '', endpointRef: 'https://secret.example', secretRef: '',
  }, []), /环境变量名/);
});
