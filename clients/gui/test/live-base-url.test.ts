import assert from 'node:assert/strict';
import test from 'node:test';
import { resolveLiveBaseUrl } from '../src/lib/liveBaseUrl.ts';

test('live client uses the browser origin for Vite and production proxies', () => {
  assert.equal(
    resolveLiveBaseUrl('http://127.0.0.1:3000'),
    'http://127.0.0.1:3000',
  );
  assert.notEqual(
    resolveLiveBaseUrl('http://127.0.0.1:3000'),
    'http://127.0.0.1:8000',
  );
  assert.equal(
    resolveLiveBaseUrl('https://operant.example.test/'),
    'https://operant.example.test',
  );
});

test('missing or non-browser origins fail instead of falling back to Core or Mock', () => {
  assert.throws(
    () => resolveLiveBaseUrl(undefined),
    /no Core-host or Mock fallback is configured/,
  );
  assert.throws(
    () => resolveLiveBaseUrl(''),
    /no Core-host or Mock fallback is configured/,
  );
  assert.throws(
    () => resolveLiveBaseUrl('file:///tmp/operant/index.html'),
    /Expected an HTTP\(S\) origin/,
  );
});
