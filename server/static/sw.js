// Minimal service worker: caches the app shell so the PWA can install.
// Live data (Socket.IO, WebRTC) is never cached.
const CACHE = 'claude-rc-car-v9';
const SHELL = ['/static/style.css', '/static/app.js', '/static/icon.svg', '/manifest.webmanifest'];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener('activate', (e) => {
  e.waitUntil(caches.keys().then((keys) =>
    Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))));
  self.clients.claim();
});

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.pathname.startsWith('/socket.io')) return;
  if (SHELL.includes(url.pathname)) {
    // Network first, cache fallback
    e.respondWith(fetch(e.request).catch(() => caches.match(e.request)));
  }
});
