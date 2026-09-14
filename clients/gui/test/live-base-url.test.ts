import assert from 'node:assert/strict';
import test from 'node:test';
import {
  resolveBrowserOrDesktopLiveBaseUrl,
  resolveDesktopLiveBaseUrl,
  resolveLiveBaseUrl,
} from '../src/lib/liveBaseUrl.ts';

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

test('Tauri shell uses only the fixed localhost Core endpoint', () => {
  assert.equal(resolveDesktopLiveBaseUrl(true), 'http://127.0.0.1:8000');
  assert.equal(resolveDesktopLiveBaseUrl(false), null);
});

test('an HTTP debug WebView keeps its browser origin even when Tauri internals exist', () => {
  assert.equal(
    resolveBrowserOrDesktopLiveBaseUrl('http://127.0.0.1:3000', true),
    'http://127.0.0.1:3000',
  );
  assert.equal(
    resolveBrowserOrDesktopLiveBaseUrl('tauri://localhost', true),
    'http://127.0.0.1:8000',
  );
  assert.equal(
    resolveBrowserOrDesktopLiveBaseUrl('https://tauri.localhost', true),
    'http://127.0.0.1:8000',
  );
  assert.equal(
    resolveBrowserOrDesktopLiveBaseUrl('https://tauri.localhost', false),
    'https://tauri.localhost',
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
