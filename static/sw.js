// Service worker : met en cache l'interface (utilisable hors-ligne pour éditer / consulter).
// Les appels /api/* (PDF, OMR) nécessitent le serveur.
const CACHE = 'qcm-v10';
const SHELL = [
  '/', '/index.html', '/manifest.webmanifest', '/css/app.css',
  '/js/app.js', '/js/ui.js', '/js/store.js', '/js/crypto.js', '/js/base45.js', '/js/grading.js',
  '/js/editor.js', '/js/ai.js', '/js/scanner.js', '/js/results.js', '/js/settings.js',
  '/icons/icon.svg', '/icons/icon-192.png', '/icons/icon-512.png',
];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

// réseau d'abord (toujours la dernière version), cache en secours hors-ligne ; API jamais en cache
self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.origin !== location.origin || url.pathname.startsWith('/api/')) return;
  e.respondWith(
    fetch(e.request)
      .then((res) => {
        if (res.ok) {
          const copy = res.clone();
          caches.open(CACHE).then((c) => c.put(e.request, copy));
        }
        return res;
      })
      .catch(() => caches.match(e.request, { ignoreSearch: true })),
  );
});
