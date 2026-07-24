// FieldBase offline shell. Job actions are queued by the technician page and
// replayed after connectivity returns; this cache keeps the assigned-job page
// readable while the network is unavailable.
const CACHE = 'fieldbase-field-shell-v2';
const OFFLINE_PATHS = ['/static/manifest.json'];

self.addEventListener('install', event => {
  event.waitUntil(caches.open(CACHE).then(cache => cache.addAll(OFFLINE_PATHS)).catch(() => null));
  self.skipWaiting();
});

self.addEventListener('activate', event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(key => key !== CACHE).map(key => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', event => {
  if (event.request.method !== 'GET') return;
  const url = new URL(event.request.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname === '/logout') {
    event.respondWith(fetch(event.request).then(response => {
      caches.delete(CACHE);
      return response;
    }));
    return;
  }
  if (url.pathname !== '/employee' && url.pathname !== '/api/employee/jobs') return;
  event.respondWith(
    fetch(event.request)
      .then(response => {
        if (response.ok) caches.open(CACHE).then(cache => cache.put(event.request, response.clone()));
        return response;
      })
      .catch(() => caches.match(event.request))
  );
});
