import assert from 'node:assert/strict';
import test from 'node:test';
import { groupModelConnections, pendingOAuthUrl } from '../src/features/settings/modelConnectionPresentation.ts';

test('legacy ChatGPT connections remain available only in advanced settings', () => {
  const connections = [
    { provider: 'chatgpt', connection_id: 'old' },
    { provider: 'codex', connection_id: 'new' },
    { provider: 'gemini', connection_id: 'google' },
    { provider: 'openai-compatible', connection_id: 'key' },
  ];
  const { visible, legacy } = groupModelConnections(connections);
  assert.deepEqual(visible.map((item) => item.connection_id), ['new', 'google', 'key']);
  assert.deepEqual(legacy.map((item) => item.connection_id), ['old']);
});

test('CLI login without an authorization URL has no browser action', () => {
  assert.equal(pendingOAuthUrl({ status: 'pending', authorization_url: null }), null);
  assert.equal(pendingOAuthUrl({ status: 'pending', authorization_url: '  ' }), null);
  assert.equal(pendingOAuthUrl({ status: 'pending', authorization_url: ' https://example.com/login ' }), 'https://example.com/login');
  assert.equal(pendingOAuthUrl({ status: 'cancelled', authorization_url: 'https://example.com/login' }), null);
});
