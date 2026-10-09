/* agy/deck service worker — app shell offline, API selalu network */
const CACHE = "agydeck-v3-1";
const SHELL = ["/", "/static/index.html", "/static/style.css", "/static/app.js",
  "/static/manifest.json", "/static/icon.svg"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", (e) => {
  e.waitUntil(caches.keys().then((ks) =>
    Promise.all(ks.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
    .then(() => self.clients.claim()));
});
self.addEventListener("fetch", (e) => {
  const u = new URL(e.request.url);
  if (u.pathname.startsWith("/api") || u.pathname === "/ws") return; // network only
  if (u.pathname.startsWith("/static/")) {
    e.respondWith(caches.match(e.request).then((hit) =>
      hit || fetch(e.request).then((res) => {
        const copy = res.clone();
        caches.open(CACHE).then((c) => c.put(e.request, copy));
        return res;
      })));
    return;
  }
  e.respondWith(fetch(e.request).catch(() => caches.match("/static/index.html")));
});
