const SHELL_CACHE = 'operant-shell-v3';
const SHELL_FILES = ['/', '/manifest.webmanifest', '/app-icon.svg', '/favicon.svg'];

async function cacheAnonymousShell(cache, paths) {
  for (const path of [...new Set(paths)]) {
    const request = new Request(path, { credentials: 'omit', cache: 'reload' });
    const response = await fetch(request);
    const cacheControl = response.headers.get('cache-control') || '';
    if (!response.ok || response.type !== 'basic' || /(?:private|no-store)/i.test(cacheControl)) {
      throw new Error(`Refused to cache unsafe shell response: ${path}`);
    }
    await cache.put(path, response);
  }
}

self.addEventListener('install', (event) => {
  event.waitUntil((async () => {
    const cache = await caches.open(SHELL_CACHE);
    await cacheAnonymousShell(cache, SHELL_FILES);

    // Registration happens after the first page load, so the worker has not
    // observed Vite's hashed JS/CSS requests yet. Discover those same-origin
    // build assets from the cached entry document and make the installed shell
    // independently reloadable offline.
    const entry = await cache.match('/');
    if (!entry) throw new Error('PWA entry document was not cached');
    const html = await entry.text();
    const assets = [...html.matchAll(/(?:src|href)="([^"#?]+)"/g)]
      .map((match) => new URL(match[1], self.location.origin))
      .filter((url) => url.origin === self.location.origin && url.pathname.startsWith('/assets/'))
      .map((url) => url.pathname);
    await cacheAnonymousShell(cache, assets);
  })());
  self.skipWaiting();
});

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches.keys().then((keys) => Promise.all(
      keys.filter((key) => key !== SHELL_CACHE).map((key) => caches.delete(key)),
    )),
  );
  self.clients.claim();
});

self.addEventListener('fetch', (event) => {
  const request = event.request;
  const url = new URL(request.url);

  // Never cache or defer Core traffic, non-GET requests, credentials, or a
  // cross-origin response. In particular, Approval and Command writes cannot
  // become a background queue.
  if (
    request.method !== 'GET'
    || url.origin !== self.location.origin
    || url.pathname.startsWith('/v1/')
    || url.pathname === '/healthz'
    || request.headers.has('authorization')
  ) return;

  if (request.mode === 'navigate') {
    event.respondWith(fetch(request).catch(() => caches.match('/')));
    return;
  }

  const isShellAsset = ['script', 'style', 'image', 'font'].includes(request.destination);
  if (!isShellAsset) return;
  event.respondWith(
    caches.match(request).then((cached) => cached || fetch(request).then((response) => {
      const cacheControl = response.headers.get('cache-control') || '';
      if (
        request.credentials === 'omit'
        && response.ok
        && response.type === 'basic'
        && !/(?:private|no-store)/i.test(cacheControl)
      ) {
        const copy = response.clone();
        void caches.open(SHELL_CACHE).then((cache) => cache.put(request, copy));
      }
      return response;
    })),
  );
});
