// Service worker de Alerta Xsense
//  - Permite instalar la app y abrirla sin señal (muestra los últimos datos descargados).
//  - Recibe las notificaciones push de Firebase aunque la app esté cerrada,
//    y al tocarlas abre la app directo en la cámara de la alerta.
const CACHE = "alerta-xsense-v2";
const BASE = ["./", "index.html", "manifest.webmanifest", "icons/icon.svg",
              "icons/icon-192.png", "icons/icon-512.png", "icons/badge-96.png"];

// ------------------------------------------------------------ Firebase Messaging
importScripts(
  "vendor/firebase-12.19.0/firebase-app-compat.js",
  "vendor/firebase-12.19.0/firebase-messaging-compat.js"
);
firebase.initializeApp({
  apiKey: "AIzaSyD3ydrhq5Togj7kYv66MC224kUJWKalUko",
  authDomain: "alerta-xsense.firebaseapp.com",
  projectId: "alerta-xsense",
  storageBucket: "alerta-xsense.firebasestorage.app",
  messagingSenderId: "104854126939",
  appId: "1:104854126939:web:63d0619b74774e6a22da7e",
});
const messaging = firebase.messaging();

// El monitor envía mensajes "de datos": aquí armamos la notificación que se ve en el celular
messaging.onBackgroundMessage((payload) => mostrarNotificacion(payload.data || {}));

function mostrarNotificacion(d) {
  const base = self.registration.scope;
  return self.registration.showNotification(d.titulo || "Alerta Xsense", {
    body: d.cuerpo || "",
    icon: base + "icons/icon-192.png",
    badge: base + "icons/badge-96.png",
    image: d.imagen || undefined,
    tag: d.tag || undefined,           // una notificación por cámara: la nueva reemplaza a la anterior
    renotify: true,                    // ...pero igual vuelve a sonar
    requireInteraction: d.tipo === "desviacion" || d.tipo === "error",
    vibrate: [200, 100, 200],
    timestamp: Number(d.ts) || Date.now(),
    data: { url: d.url || base },
  });
}

self.addEventListener("notificationclick", (e) => {
  e.notification.close();
  const url = (e.notification.data && e.notification.data.url) || self.registration.scope;
  e.waitUntil((async () => {
    const ventanas = await clients.matchAll({ type: "window", includeUncontrolled: true });
    for (const v of ventanas) {
      if (v.url.startsWith(self.registration.scope)) {
        await v.focus();
        return v.navigate(url).catch(() => v.postMessage({ abrir: url }));
      }
    }
    return clients.openWindow(url);
  })());
});

// ------------------------------------------------------------ Caché (modo sin señal)
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

  // La app: primero la red (para recibir mejoras); si falla, la copia guardada
  e.respondWith(
    fetch(e.request)
      .then((r) => { const copia = r.clone(); caches.open(CACHE).then((c) => c.put(e.request, copia)); return r; })
      .catch(() => caches.match(e.request))
  );
});
