// Minimal service worker — network passthrough.
// Exists so the app qualifies as installable; all data stays live from the server.
self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', (e) => e.waitUntil(self.clients.claim()));
self.addEventListener('fetch', () => {});
