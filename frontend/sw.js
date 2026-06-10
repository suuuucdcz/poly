/* Service worker PWA — coque en cache (chargement instantané, même au réveil de
   Render), mais données /api/ TOUJOURS en réseau (jamais périmées). */
const CACHE = "polyquant-v6";
const SHELL = [
  "/",
  "/static/style.css?v=6",
  "/static/app.js?v=6",
  "/static/icon-192.png",
  "/static/icon-512.png",
  "/manifest.webmanifest"
];

self.addEventListener("install", (e) => {
  e.waitUntil(
    caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.map((k) => (k !== CACHE ? caches.delete(k) : null))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);

  // Données live : réseau uniquement
  if (url.pathname.startsWith("/api/")) {
    e.respondWith(fetch(e.request));
    return;
  }

  // Coque (même origine, GET) : stale-while-revalidate
  if (e.request.method === "GET" && url.origin === self.location.origin) {
    e.respondWith(
      caches.open(CACHE).then((cache) =>
        cache.match(e.request).then((cached) => {
          const network = fetch(e.request)
            .then((res) => {
              if (res && res.status === 200) cache.put(e.request, res.clone());
              return res;
            })
            .catch(() => cached);
          return cached || network;
        })
      )
    );
  }
});
