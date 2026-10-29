/* Keep news online and current; cache only this small offline shell. */
const CACHE = 'paranapop-shell-v1';
const OFFLINE = '/static/pwa/offline.html';
const ASSETS = [OFFLINE, '/static/pwa/icon-192.png', '/static/pwa/icon-512.png'];

self.addEventListener('install', (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(ASSETS)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', (event) => {
  event.waitUntil(caches.keys().then((keys) => Promise.all(keys
    .filter((key) => key.startsWith('paranapop-shell-') && key !== CACHE)
    .map((key) => caches.delete(key)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== 'GET' || url.origin !== self.location.origin) return;
  if (ASSETS.includes(url.pathname)) {
    event.respondWith(caches.match(url.pathname).then((cached) => cached || fetch(event.request)));
  } else if (event.request.mode === 'navigate' && !/^\/(admin|api|media)(\/|$)/.test(url.pathname)
    && !url.pathname.startsWith('/geradorpop')) {
    event.respondWith(fetch(event.request).catch(() => caches.match(OFFLINE)));
  }
});
self.addEventListener('push', (event) => {
  let data = {};
  try { data = event.data ? event.data.json() : {}; } catch (_) { /* Show a useful fallback. */ }
  let target = new URL('/', self.location.origin);
  try {
    const candidate = new URL(data.url || '/', self.location.origin);
    if (candidate.origin === self.location.origin && (candidate.pathname === '/' || candidate.pathname.startsWith('/p/'))) target = candidate;
  } catch (_) { /* Only same-origin news links are accepted. */ }
  event.waitUntil(self.registration.showNotification(data.title || 'Paraná Pop', {
    body: data.body || 'Tem notícia nova no Paraná Pop. Toque para conferir.',
    icon: '/static/pwa/icon-192.png', badge: '/static/pwa/badge-96.png',
    tag: data.tag || 'paranapop', data: { url: target.href },
  }));
});
self.addEventListener('notificationclick', (event) => {
  event.notification.close();
  const target = event.notification.data?.url || new URL('/', self.location.origin).href;
  event.waitUntil(self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(async (windows) => {
    for (const client of windows) {
      if (client.url === target && 'focus' in client) return client.focus();
    }
    return self.clients.openWindow(target);
  }));
});
