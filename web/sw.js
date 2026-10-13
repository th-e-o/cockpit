// Service worker : interface disponible hors ligne et notifications push (sans contenu : on relit l'état).
const CACHE = 'cockpit-__VERSION__';
const SHELL = ['./', 'static/style.css?v=__VERSION__', 'static/app.js?v=__VERSION__', 'static/icon.svg', 'static/manifest.json?v=__VERSION__'];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(SHELL)).catch(() => {}));
  self.skipWaiting();
});

self.addEventListener('activate', (e) => {
  e.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((k) => k !== CACHE && k !== VUS).map((k) => caches.delete(k)))));
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

// ---------- notifications : une par nouveauté (validation, tâche finie, pod connecté), avec boutons d'action
// Les push arrivent sans contenu : on relit l'état (api.php?r=resume). Les nouveautés déjà notifiées sont
// mémorisées pour qu'une notification fermée ne revienne pas au push suivant.
const VUS = 'cockpit-notifications';
async function dejaVus() {
  try { return new Set(await (await (await caches.open(VUS)).match('vus'))?.json() || []); } catch { return new Set(); }
}
async function memoriser(vus) {
  try { await (await caches.open(VUS)).put('vus', new Response(JSON.stringify([...vus].slice(-300)))); } catch { /* sans importance */ }
}

const ICONE = { icon: 'static/icon-192.png', badge: 'static/icon-192.png' };
const pluriel = (n, m) => `${n} ${m}${n > 1 ? 's' : ''}`;

function resumeModifs(c) {
  if (!c || !(c.file_count || c.commits?.length)) return '';
  return [pluriel(c.file_count, 'fichier'), c.git !== false ? `+${c.added} −${c.removed}` : '',
    c.commits?.length ? pluriel(c.commits.length, 'commit') : ''].filter(Boolean).join(' · ') + '\n';
}

function notifications(s) {
  const out = [];
  for (const a of s.approvals || []) {
    out.push({ key: `v-${a.id}`, title: '⚠️ Validation requise', tag: `validation-${a.id}`, urgent: true,
      body: `${a.reason}${a.preview && !a.reason.includes(a.preview) ? `\n${a.preview}` : ''}`,
      actions: [{ action: 'approve', title: '✅ Approuver' }, { action: 'deny', title: '⛔ Refuser' }],
      data: { approval: a.id } });
  }
  for (const p of s.new_pods || []) {
    out.push({ key: `p-${p.id}-${p.since}`, title: `🟢 Pod « ${p.name} » connecté`, tag: `pod-${p.id}`,
      body: 'Le projet est prêt : tu peux lui envoyer une instruction.', data: {} });
  }
  for (const c of s.ci_failures || []) {
    const fix = `La CI GitHub échoue sur le commit ${c.sha}${c.checks?.length ? ` (${c.checks.join(', ')})` : ''}. `
      + `Consulte les journaux${c.url ? ` (${c.url})` : ''}, trouve la cause, corrige et pousse.`;
    out.push({ key: `ci-${c.task_id}-${c.sha}`, title: '❌ CI en échec', tag: `ci-${c.task_id}`,
      body: `${c.instruction}${c.checks?.length ? `\n${c.checks.join(' · ')}` : ''}`,
      actions: [{ action: 'ci-fix', title: '▶ Corriger' }], data: { task: c.task_id, fix } });
  }
  for (const t of s.finished || []) {
    if (t.status === 'done' && t.question?.text) {
      // Claude attend une réponse : la question d'abord, ses choix en boutons (questionnaire : réponse dans l'app)
      const items = t.question.items || [];
      const options = items.length ? [] : (t.question.options || []).slice(0, 2);
      const body = items.length ? `${items.length} questions : ${items.map((x, i) => `${i + 1}. ${x.question}`).join(' ')}` : t.question.text;
      out.push({ key: `q-${t.id}`, tag: `tache-${t.id}`, urgent: true, title: '❓ Claude attend ta réponse',
        body: body.replace(/\*\*|`/g, '').slice(0, 220),
        actions: options.map((o, i) => ({ action: `answer-${i}`, title: o.slice(0, 40) })),
        data: { task: t.id, options } });
      continue;
    }
    const steps = t.status === 'done' && !t.next_steps_dismissed ? (t.next_steps || []) : [];
    const suite = steps.length ? `\n💡 ${steps.map((x) => x.titre).join(' · ')}` : '';
    out.push({ key: `t-${t.id}-${t.status}`, tag: `tache-${t.id}`,
      title: { done: '✅ Tâche terminée', error: '❌ Tâche en erreur', needs_project: '❓ Quel projet ?' }[t.status] || 'Cockpit',
      body: (resumeModifs(t.changes) + (t.result || t.instruction || '').replace(/\*\*|`/g, '')).slice(0, 180) + suite,
      actions: steps.slice(0, 2).map((x, i) => ({ action: `next-${i}`, title: `▶ ${x.titre}`.slice(0, 40) })),
      data: { task: t.id, steps } });
  }
  return out;
}

self.addEventListener('push', (e) => {
  e.waitUntil((async () => {
    let liste = [];
    let approvals = 0;
    try {
      const s = await (await fetch('api.php?r=resume', { credentials: 'same-origin' })).json();
      liste = notifications(s);
      approvals = s.approvals?.length || 0;
    } catch { /* session expirée ou hors ligne : notification générique */ }
    if (navigator.setAppBadge) navigator.setAppBadge(approvals).catch(() => {});
    const vus = await dejaVus();
    const nouvelles = liste.filter((n) => !vus.has(n.key));
    for (const n of nouvelles) {
      vus.add(n.key);
      await self.registration.showNotification(n.title, { body: n.body, tag: n.tag, actions: n.actions || [], data: n.data,
        renotify: !!n.urgent, requireInteraction: !!n.urgent, ...ICONE });
    }
    await memoriser(vus);
    // Chaque push doit afficher une notification : rien de neuf -> rappel discret de la plus importante.
    if (!nouvelles.length) {
      const n = liste[0] || { title: 'Cockpit', body: 'Du nouveau dans le cockpit.', tag: 'cockpit', data: {} };
      await self.registration.showNotification(n.title, { body: n.body, tag: n.tag, actions: n.actions || [], data: n.data, silent: true, ...ICONE });
    }
  })());
});

async function appel(route, body) {
  const r = await fetch(`api.php?r=${route}`, { method: 'POST', credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.json();
}

async function ouvrir(hash = '') {
  const list = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
  const win = list.find((c) => c.url.startsWith(self.registration.scope));
  if (win) { await win.focus(); if (hash) win.postMessage({ jump: hash }); return; }
  await self.clients.openWindow(self.registration.scope + (hash ? `#t=${hash}` : ''));
}

self.addEventListener('notificationclick', (e) => {
  const n = e.notification;
  const d = n.data || {};
  n.close();
  e.waitUntil((async () => {
    try {
      if ((e.action === 'approve' || e.action === 'deny') && d.approval) {
        await appel('decider', { approval_id: d.approval, decision: e.action });
        return;
      }
      if (e.action === 'ci-fix' && d.task && d.fix) {
        const t = await appel('tache', { instruction: d.fix, reply_to: d.task });
        await self.registration.showNotification('▶ Correction de la CI lancée', { body: 'Claude reprend la conversation.',
          tag: `tache-${t.id}`, silent: true, data: { task: t.id }, ...ICONE });
        return;
      }
      if (e.action?.startsWith('answer-') && d.task) {
        const answer = d.options?.[Number(e.action.slice(7))];
        if (!answer) return ouvrir(d.task);
        const t = await appel('tache', { instruction: answer, reply_to: d.task });
        await self.registration.showNotification(`↪ ${answer}`.slice(0, 60), { body: 'Réponse envoyée : Claude reprend la conversation.',
          tag: `tache-${t.id}`, silent: true, data: { task: t.id }, ...ICONE });
        return;
      }
      if (e.action?.startsWith('next-') && d.task) {
        const step = d.steps?.[Number(e.action.slice(5))];
        if (!step) return ouvrir(d.task);
        const t = await appel('tache', { instruction: step.instruction, reply_to: d.task });
        await self.registration.showNotification(`▶ ${step.titre}`, { body: 'Étape lancée : Claude reprend la conversation.',
          tag: `tache-${t.id}`, silent: true, data: { task: t.id }, ...ICONE });
        return;
      }
    } catch (err) {
      await self.registration.showNotification('Action impossible', { body: `${err.message} — ouvre le cockpit.`, tag: 'erreur', data: d, ...ICONE });
      return;
    }
    await ouvrir(d.task || '');
  })());
});
