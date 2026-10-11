// Service worker : interface disponible hors ligne et notifications push (sans contenu : on relit l'état).
const CACHE = 'cockpit-__VERSION__';
const SHELL = ['./', 'static/style.css?v=__VERSION__', 'static/app.js?v=__VERSION__', 'static/icon.svg', 'static/manifest.json?v=__VERSION__'];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).catch(() => {}));
  self.skipWaiting();
});

self.addEventListener('activate', (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))));
  self.clients.claim();
});

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.pathname.endsWith('api.php')) return; // l'API n'est jamais mise en cache
  e.respondWith(
    fetch(e.request)
      .then((res) => { const copy = res.clone(); caches.open(CACHE).then((c) => c.put(e.request, copy)); return res; })
      .catch(() => caches.match(e.request, { ignoreSearch: url.pathname.endsWith('/') })),
  );
});

self.addEventListener('push', (e) => {
  e.waitUntil((async () => {
    let title = 'Cockpit', body = 'Du nouveau dans le cockpit.', tag = 'cockpit', urgent = false;
    try {
      const res = await fetch('api.php?r=resume', { credentials: 'same-origin' });
      const s = await res.json();
      if (s.approvals?.length) {
        const a = s.approvals[0];
        title = s.approvals.length > 1 ? `⚠️ ${s.approvals.length} validations requises` : '⚠️ Validation requise';
        body = `${a.tool} — ${a.reason}`;
        tag = 'validation';
        urgent = true;
      } else if (s.finished?.length) {
        const t = s.finished[0];
        title = { done: '✅ Tâche terminée', error: '❌ Tâche en erreur', needs_project: '❓ Quel projet ?' }[t.status] || 'Cockpit';
        body = (t.result || t.instruction || '').slice(0, 180);
        tag = `tache-${t.id}`;
      }
      if (navigator.setAppBadge) navigator.setAppBadge(s.approvals?.length || 0).catch(() => {});
    } catch { /* session expirée ou hors ligne : notification générique */ }
    await self.registration.showNotification(title, { body, tag, renotify: urgent, requireInteraction: urgent, icon: 'static/icon-192.png', badge: 'static/icon-192.png' });
  })());
});

self.addEventListener('notificationclick', (e) => {
  e.notification.close();
  e.waitUntil(self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then((list) => {
    const win = list.find((c) => c.url.startsWith(self.registration.scope));
    return win ? win.focus() : self.clients.openWindow(self.registration.scope);
  }));
});
