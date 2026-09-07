/*
 * Trillion's service worker — playbook/mobile-pwa.md §7.
 *
 * It caches NOTHING. That is the entire design, and it is deliberate.
 *
 * A service worker exists here only so iOS treats the page as an installable
 * PWA and gives it a standalone window. The moment it also caches the shell,
 * an iOS home-screen install will happily serve a broken build for days —
 * there is no reload gesture in a standalone window, and the OS is under no
 * obligation to check for a new one. The playbook is blunt about it: "don't
 * cache the shell; iOS PWA will strand users on broken shells for days."
 *
 * So every fetch goes straight to the network. There is no offline mode, and
 * there should not be one: this is a client for a server that does the
 * thinking. Offline, there is nothing to say.
 *
 * The two lifecycle handlers make a deploy land on the next open rather than
 * the open after that:
 *   skipWaiting()        — a new worker replaces the old one immediately
 *                          instead of waiting for every tab to close.
 *   clients.claim()      — and takes over the already-open page.
 * Paired with `Cache-Control: no-store` on the shell (serve.py's index
 * handler), that is the whole cache-busting story.
 */

self.addEventListener("install", () => {
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  // Clear anything a previous version of this worker may have cached, so an
  // upgrade from a caching worker doesn't leave a stale shell behind.
  event.waitUntil(
    (async () => {
      const names = await caches.keys();
      await Promise.all(names.map((n) => caches.delete(n)));
      await self.clients.claim();
    })()
  );
});

// A true pass-through. Registering no fetch handler at all would also work,
// but some engines only treat a worker as "controlling" once it has one, and
// an explicit no-op documents the intent better than an absence does.
self.addEventListener("fetch", (event) => {
  event.respondWith(fetch(event.request));
});
