/* Minimal offline shell for the Alfredo PWA.
 * - App shell: cache-first with runtime caching of hashed assets.
 * - API calls are never cached (they are authenticated and stateful).
 */
const CACHE_NAME = "alfredo-shell-v4";
const SCOPE = self.registration.scope;

self.addEventListener("install", (event) => {
  self.skipWaiting();
  event.waitUntil(
    caches
      .open(CACHE_NAME)
      .then((cache) =>
        cache.addAll([
          SCOPE,
          `${SCOPE}manifest.webmanifest`,
          `${SCOPE}icon-512.png`,
          `${SCOPE}icon-192.png`,
          `${SCOPE}alfredo_robot.png`,
        ])
      )
      .catch(() => undefined),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) =>
        Promise.all(keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key))),
      )
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/api/")) return;

  // Navigations are network-first so a new build is picked up on the next open,
  // falling back to the cached shell only while offline.
  if (request.mode === "navigate") {
    event.respondWith(
      fetch(request)
        .then((response) => {
          if (response.ok) {
            const copy = response.clone();
            caches.open(CACHE_NAME).then((cache) => cache.put(SCOPE, copy));
          }
          return response;
        })
        .catch(() => caches.match(SCOPE).then((cached) => cached || Response.error())),
    );
    return;
  }

  event.respondWith(
    caches.match(request).then((cached) => {
      if (cached) return cached;
      return fetch(request)
        .then((response) => {
          if (response.ok) {
            const copy = response.clone();
            caches.open(CACHE_NAME).then((cache) => cache.put(request, copy));
          }
          return response;
        })
        .catch(() => {
          if (request.mode === "navigate") return caches.match(SCOPE);
          return Response.error();
        });
    }),
  );
});

/* ------------------------- Web Push: approvals ------------------------- */

self.addEventListener("push", (event) => {
  let payload = {};
  try {
    payload = event.data ? event.data.json() : {};
  } catch {
    payload = { body: event.data ? event.data.text() : "" };
  }

  const title = payload.title || "Alfredo";
  const options = {
    body: payload.body || "Richiesta di approvazione in attesa.",
    icon: `${SCOPE}icon.svg`,
    badge: `${SCOPE}icon.svg`,
    tag: payload.interrupt_id ? `alfredo-approval-${payload.interrupt_id}` : "alfredo-approval",
    renotify: true,
    requireInteraction: true,
    data: {
      url: payload.url || "./",
      run_id: payload.run_id,
      interrupt_id: payload.interrupt_id,
    },
  };

  event.waitUntil(self.registration.showNotification(title, options));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const target = `${SCOPE}?tab=approvals`;

  event.waitUntil(
    self.clients
      .matchAll({ type: "window", includeUncontrolled: true })
      .then((clientList) => {
        for (const client of clientList) {
          if (client.url.startsWith(SCOPE)) {
            client.postMessage({ type: "open-approvals" });
            if ("focus" in client) return client.focus();
          }
        }
        if (self.clients.openWindow) return self.clients.openWindow(target);
        return undefined;
      }),
  );
});
