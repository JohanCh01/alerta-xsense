// Service worker: permite instalar la app y abrirla aunque no haya señal
// (muestra los últimos datos descargados). Los datos siempre se piden a la red primero.
const CACHE = "alerta-xsense-v1";
const BASE = ["./", "index.html", "manifest.webmanifest", "icons/icon.svg", "icons/icon-192.png", "icons/icon-512.png"];

self.addEventListener("install", (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(BASE)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys()
      .then((ks) => Promise.all(ks.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin) return;

  // Datos y gráficos: primero la red; si no hay señal, la última copia guardada
  if (url.pathname.includes("/data/") || url.pathname.includes("/graficos/")) {
    const clave = url.origin + url.pathname;
    e.respondWith(
      fetch(e.request)
        .then((r) => { const copia = r.clone(); caches.open(CACHE).then((c) => c.put(clave, copia)); return r; })
        .catch(() => caches.match(clave))
    );
    return;
  }

  // La app: primero la red (para recibir mejoras), si falla la copia guardada
  e.respondWith(
    fetch(e.request)
      .then((r) => { const copia = r.clone(); caches.open(CACHE).then((c) => c.put(e.request, copia)); return r; })
      .catch(() => caches.match(e.request))
  );
});
