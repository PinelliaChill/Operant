import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import test from 'node:test';

const publicAsset = (name: string) => new URL(`../public/${name}`, import.meta.url);

test('PWA manifest is installable and starts on the hash-router chat route', async () => {
  const manifest = JSON.parse(await readFile(publicAsset('manifest.webmanifest'), 'utf8'));
  assert.equal(manifest.display, 'standalone');
  assert.equal(manifest.start_url, '/#/chat');
  assert.ok(Array.isArray(manifest.icons) && manifest.icons.length > 0);
});

test('service worker never handles Core API, authenticated, or write requests', async () => {
  const worker = await readFile(publicAsset('service-worker.js'), 'utf8');
  assert.match(worker, /request\.method !== 'GET'/);
  assert.match(worker, /url\.pathname\.startsWith\('\/v1\/'\)/);
  assert.match(worker, /request\.headers\.has\('authorization'\)/);
  assert.doesNotMatch(worker, /addEventListener\(['"]sync['"]|backgroundSync|indexedDB/i);
});

test('service worker precaches only anonymous public shell assets for offline reload', async () => {
  const worker = await readFile(publicAsset('service-worker.js'), 'utf8');
  assert.match(worker, /entry\.text\(\)/);
  assert.match(worker, /url\.pathname\.startsWith\('\/assets\/'\)/);
  assert.match(worker, /credentials: 'omit'/);
  assert.match(worker, /request\.credentials === 'omit'/);
  assert.match(worker, /private\|no-store/);
  assert.doesNotMatch(worker, /cache\.addAll/);
});
