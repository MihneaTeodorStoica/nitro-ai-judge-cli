"use strict";

const CACHE = "naij-play-manager-offline-v4";
const OFFLINE = "/nitro/assets/offline.html";
const ASSETS = [
  OFFLINE,
  "/nitro/assets/app.css",
  "/nitro/assets/offline.js",
  "/nitro/assets/logo.svg",
  "/nitro/assets/inter-latin.woff2",
  "/nitro/assets/inter-latin-ext.woff2",
  "/nitro/assets/lexend-deca-latin.woff2",
  "/nitro/assets/lexend-deca-latin-ext.woff2",
];

self.addEventListener("install", event => {
  event.waitUntil(
    caches.open(CACHE).then(cache => cache.addAll(ASSETS)).then(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", event => {
  event.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(key => key.startsWith("naij-play-manager-offline-") && key !== CACHE).map(key => caches.delete(key))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", event => {
  const path = new URL(event.request.url).pathname;
  if (ASSETS.includes(path)) {
    event.respondWith((async () => {
      try {
        const response = await fetch(event.request);
        if (response.ok) {
          const cache = await caches.open(CACHE);
          await cache.put(event.request, response.clone());
        }
        return response;
      } catch (_error) {
        return caches.match(event.request, { ignoreSearch: true });
      }
    })());
    return;
  }
  if (event.request.mode !== "navigate") return;
  event.respondWith(fetch(event.request).catch(() => caches.match(OFFLINE)));
});
