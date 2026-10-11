'use strict';

const $ = (s) => document.querySelector(s);
// replyTo : tâche dont le prochain message est la suite (undefined tant que l'historique n'est pas chargé)
const state = { projects: [], selected: null, tasks: new Map(), order: [], lastId: 0, workerOnline: false, replay: false, replyTo: undefined };
const REPRISE_AUTO = 2 * 3600; // à l'ouverture, la dernière conversation continue si elle a moins de 2 h

const STATUS = {
  pending: 'en attente', routing: 'routage…', queued: 'en file', running: 'en cours',
  waiting_approval: 'validation requise', needs_project: 'projet à choisir',
  done: 'terminé', error: 'erreur', cancelled: 'annulé',
};
const ACTIVE = new Set(['pending', 'routing', 'queued', 'running', 'waiting_approval']);
const isStandalone = matchMedia('(display-mode: standalone)').matches || navigator.standalone === true;
const isIOS = /iPhone|iPad|iPod/.test(navigator.userAgent);

// ---------- utilitaires
const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

function md(src) {
  const blocks = [];
  let s = esc(src).replace(/```[\w-]*\n?([\s\S]*?)```/g, (_, code) => `\u0000${blocks.push(code) - 1}\u0000`);
  s = s.replace(/`([^`\n]+)`/g, '<code>$1</code>')
       .replace(/\*\*([^*\n]+)\*\*/g, '<b>$1</b>')
       .replace(/^#{1,6}\s+(.+)$/gm, '<b>$1</b>')
       .replace(/\n/g, '<br>');
  return s.replace(/\u0000(\d+)\u0000/g, (_, i) => `<pre><code>${blocks[i]}</code></pre>`);
}

class AuthError extends Error {}

async function api(route, body, params = '') {
  const res = await fetch(`api.php?r=${route}${params}`, body === undefined ? { credentials: 'same-origin' } : {
    method: 'POST', credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (res.status === 401 && !route.startsWith('passkey') && route !== 'appairer') throw new AuthError(data.detail);
  if (!res.ok) throw new Error(data.detail || res.statusText);
  return data;
}

const b64u = {
  enc: (buf) => btoa(String.fromCharCode(...new Uint8Array(buf))).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, ''),
  dec: (s) => Uint8Array.from(atob(s.replace(/-/g, '+').replace(/_/g, '/') + '==='.slice((s.length + 3) % 4)), (c) => c.charCodeAt(0)),
};

// ---------- connexion : appairage (QR) puis passkey
function authScreen({ title, text, button, onClick, alt, onAlt, hint }) {
  $('#auth').classList.remove('hidden');
  $('#authTitle').textContent = title;
  $('#authText').textContent = text || '';
  $('#authHint').textContent = hint || '';
  for (const [el, label, fn] of [[$('#authBtn'), button, onClick], [$('#authAlt'), alt, onAlt]]) {
    el.classList.toggle('hidden', !label);
    el.textContent = label || '';
    el.disabled = false;
    el.onclick = fn ? async () => {
      el.disabled = true;
      try { await fn(); } catch (e) { $('#authHint').textContent = e.name === 'NotAllowedError' ? 'Opération annulée.' : e.message; }
      el.disabled = false;
    } : null;
  }
}

async function createPasskey() {
  const o = await api('passkey_options_creation', {});
  const cred = await navigator.credentials.create({ publicKey: {
    challenge: b64u.dec(o.challenge), rp: o.rp,
    user: { ...o.user, id: b64u.dec(o.user.id) },
    pubKeyCredParams: [{ type: 'public-key', alg: -7 }],
    authenticatorSelection: { residentKey: 'required', userVerification: 'required' },
    excludeCredentials: o.excludeCredentials.map((c) => ({ ...c, id: b64u.dec(c.id) })),
    timeout: 120000,
  } });
  const r = cred.response;
  if (!r.getPublicKey) throw new Error('Navigateur trop ancien pour les passkeys.');
  await api('passkey_creer', {
    id: cred.id, clientDataJSON: b64u.enc(r.clientDataJSON), publicKey: b64u.enc(r.getPublicKey()),
    alg: r.getPublicKeyAlgorithm(), name: deviceName(),
  });
}

async function loginPasskey() {
  const o = await api('passkey_options_connexion', {});
  const cred = await navigator.credentials.get({ publicKey: {
    challenge: b64u.dec(o.challenge), rpId: o.rpId, userVerification: 'required', timeout: 120000,
  } });
  const r = cred.response;
  await api('passkey_connexion', {
    id: cred.id, clientDataJSON: b64u.enc(r.clientDataJSON),
    authenticatorData: b64u.enc(r.authenticatorData), signature: b64u.enc(r.signature),
  });
}

function deviceName() {
  const ua = navigator.userAgent;
  return /iPhone/.test(ua) ? 'iPhone' : /iPad/.test(ua) ? 'iPad' : /Android/.test(ua) ? 'Android'
    : /Mac/.test(ua) ? 'Mac' : /Windows/.test(ua) ? 'Windows' : 'Appareil';
}

function installHint() {
  if (isStandalone) return '';
  return isIOS ? 'Astuce : Partager ⎋ → « Sur l\'écran d\'accueil » pour l\'ouvrir comme une app (et recevoir les notifications).'
    : 'Astuce : menu du navigateur → « Installer l\'application ».';
}

async function ensureAuth() {
  const pair = location.hash.match(/pair=([\w-]+)/);
  if (pair) {
    history.replaceState(null, '', location.pathname);
    try {
      await api('appairer', { token: pair[1] });
      return afterPairing();
    } catch (e) {
      authScreen({ title: 'Lien expiré', text: e.message, hint: 'Relance « ./run.sh pair » dans Onyxia pour obtenir un nouveau QR code.' });
      return false;
    }
  }
  const me = await api('moi');
  if (me.authed) return true;
  if (me.has_passkey) {
    return new Promise((resolve) => authScreen({
      title: 'Cockpit', text: 'Connexion sécurisée par passkey.',
      button: isIOS ? 'Se connecter avec Face ID' : 'Se connecter avec ma passkey',
      onClick: async () => { await loginPasskey(); $('#auth').classList.add('hidden'); resolve(true); },
      hint: 'Nouvel appareil sans passkey synchronisée ? Lance « ./run.sh pair » dans Onyxia et scanne le QR code.',
    }));
  }
  authScreen({ title: 'Bienvenue', text: 'Pour connecter ce téléphone, lance dans ton terminal Onyxia :',
    hint: './run.sh pair — puis scanne le QR code avec l\'appareil photo.' });
  return false;
}

function afterPairing() {
  return new Promise((resolve) => authScreen({
    title: 'Téléphone connecté ✓',
    text: 'Crée une passkey : tu te reconnecteras d\'un regard, sans jamais taper de code.',
    button: isIOS ? 'Activer Face ID' : 'Créer ma passkey',
    onClick: async () => { await createPasskey(); $('#auth').classList.add('hidden'); resolve(true); },
    alt: 'Plus tard', onAlt: () => { $('#auth').classList.add('hidden'); resolve(true); },
    hint: installHint(),
  }));
}

// ---------- modèle
function entry(id) {
  if (!state.tasks.has(id)) {
    state.tasks.set(id, { task: { id, status: 'pending' }, events: [], approvals: new Map() });
    state.order.push(id);
  }
  return state.tasks.get(id);
}

function onEvent(evt) {
  const e = entry(evt.task_id);
  if (evt.kind === 'task') e.task = evt.task;
  else if (evt.kind === 'message') e.events.push(evt);
  else if (evt.kind === 'approval') {
    const isNew = !e.approvals.has(evt.approval.id);
    e.approvals.set(evt.approval.id, evt.approval);
    if (isNew && !evt.approval.decision && !state.replay) navigator.vibrate?.([200, 100, 200]);
  }
}

// ---------- rendu
const SERVICE_TOOLS = { lancer_service: '🚀 Lancer un service', arreter_service: '🛑 Arrêter un service' };

/** Carte dédiée au lancement / à l'arrêt d'un service Onyxia : ressources lisibles, quota dans la raison. */
function serviceInput(a) {
  const i = a.tool_input;
  if (a.tool.endsWith('arreter_service')) return `cockpit-${i.nom}`;
  return [`Nom : cockpit-${i.nom}`, `Dépôt : ${i.depot_git ? `${i.depot_git}${i.branche ? ` (${i.branche})` : ''}` : 'aucun (dossier vide)'}`,
    `Ressources : ${i.cpu || 2} CPU · ${i.memoire || '8Gi'} · ${i.disque || '10Gi'} de disque${i.gpu ? ' · 1 GPU' : ''}`].join('\n');
}

function approvalHtml(a) {
  const service = SERVICE_TOOLS[a.tool.replace(/^mcp__services__/, '')];
  const input = service ? serviceInput(a)
    : a.tool === 'Bash' ? a.tool_input.command
    : a.tool_input.file_path ? `${a.tool_input.file_path}\n\n${a.tool_input.content ?? a.tool_input.new_string ?? ''}`
    : JSON.stringify(a.tool_input, null, 2);
  const done = a.decision
    ? `<div class="timer">${{ approve: '✅ approuvé', deny: '⛔ refusé', timeout: '⌛ expiré (refusé)' }[a.decision]}</div>`
    : `<div class="actions">
         <button class="btn ok" data-approve="${esc(a.id)}">Approuver</button>
         <button class="btn bad" data-deny="${esc(a.id)}">Refuser</button>
       </div><div class="timer" data-expires="${Number(a.expires)}"></div>`;
  return `<div class="approval ${a.decision ? 'resolved' : ''}">
    <div class="why">${service ? esc(service) : `⚠️ ${esc(a.tool)}`} — ${esc(a.reason)}</div>
    <pre><code>${esc(String(input).slice(0, 4000))}</code></pre>${done}</div>`;
}

const duree = (s) => (s < 60 ? `${Math.max(1, Math.round(s))} s` : `${Math.floor(s / 60)} min${s % 60 >= 1 ? ` ${String(Math.floor(s % 60)).padStart(2, '0')}` : ''}`);

/** Étapes lisibles (« 📖 Lit README.md ») ; repli sur le nom de l'outil pour les anciens événements. */
function stepsOf(e) {
  const steps = [];
  for (const x of e.events) {
    if (x.type === 'tool_use' && x.label !== null) steps.push({ label: x.label || `🔧 ${x.tool}`, detail: x.text });
    else if (x.type === 'tool_error' && steps.length) steps[steps.length - 1].error = x.text;
  }
  return steps;
}

function progressHtml(e, active) {
  const steps = stepsOf(e);
  const todos = [...e.events].reverse().find((x) => x.todos)?.todos;
  let html = '';
  if (todos?.length) {
    const done = todos.filter((x) => x.status === 'completed').length;
    html += `<div class="todos"><div class="todohead">Plan · ${done}/${todos.length}</div>${todos.map((x) =>
      `<div class="todo ${esc(x.status)}">${x.status === 'completed' ? '✓' : x.status === 'in_progress' ? '◐' : '○'} ${esc(
        x.status === 'in_progress' ? x.activeForm || x.content : x.content)}</div>`).join('')}</div>`;
  }
  if (!steps.length) {
    return html + (active && e.task.status === 'running' ? '<div class="now"><span class="spin"></span> Réfléchit…</div>' : '');
  }
  const row = (s) => `<div class="step${s.error ? ' err' : ''}" title="${esc(s.detail || '')}">${esc(s.label)}${s.error ? ' <b>✗</b>' : ''}</div>`;
  if (active) {
    // En cours : les étapes précédentes repliées, l'étape actuelle mise en avant avec le temps écoulé.
    const cur = steps[steps.length - 1];
    const prev = steps.slice(0, -1);
    if (prev.length) html += `<details class="steps"><summary>${prev.length} étape(s) faite(s)</summary>${prev.map(row).join('')}</details>`;
    const since = e.task.created ? `<span class="since" data-since="${Number(e.task.created)}"></span>` : '';
    html += `<div class="now">${e.task.status === 'running' ? '<span class="spin"></span>' : ''} ${esc(cur.label)}${since}</div>`;
  } else {
    const end = e.events.length ? e.events[e.events.length - 1].ts : 0;
    const took = end && e.task.created ? ` · ${duree(end - e.task.created)}` : '';
    html += `<details class="steps"><summary>${steps.length} étape(s)${took}</summary>${steps.map(row).join('')}</details>`;
  }
  return html;
}

function taskHtml(e) {
  const t = e.task;
  const proj = state.projects.find((p) => p.id === t.project);
  const active = ACTIVE.has(t.status);
  const texts = e.events.filter((x) => x.type === 'text');
  const blocked = e.events.filter((x) => x.type === 'blocked');
  const lastText = active && texts.length ? texts[texts.length - 1].text : '';

  let body = progressHtml(e, active);
  body += blocked.map((x) => `<div class="blocked">⛔ ${esc(x.tool)} : ${esc(x.text)}</div>`).join('');
  body += [...e.approvals.values()].map(approvalHtml).join('');
  if (lastText) body += `<div class="text">${md(lastText)}</div>`;
  if (t.status === 'pending' && !state.workerOnline) body += '<div class="hint">Le worker Onyxia est hors ligne : la tâche partira dès sa reconnexion.</div>';
  if (t.status === 'needs_project') {
    body += `<div class="text">Je n'ai pas pu déterminer le projet${t.route_reason ? ` (${esc(t.route_reason)})` : ''}. Lequel ?</div>
      <div class="actions">${state.projects.map((p) => `<button class="btn" data-choose="${esc(p.id)}">${esc(p.name)}</button>`).join('')}</div>`;
  }
  if (t.result && !ACTIVE.has(t.status)) body += `<div class="result">${md(t.result)}</div>`;

  return `<div class="meta">
      <span class="pill st-${esc(t.status)}">${STATUS[t.status] || esc(t.status)}</span>
      ${proj ? `<span class="pill">${esc(proj.name)}</span>` : ''}
      ${t.route_reason && t.project ? `<span title="${esc(t.route_reason)}">auto</span>` : ''}
      ${ACTIVE.has(t.status) ? `<button class="btn ghost" data-cancel="${esc(t.id)}">Arrêter</button>`
        : t.id !== state.replyTo ? `<button class="btn ghost" data-reply="${esc(t.id)}">Répondre</button>` : ''}
    </div><div class="log">${body}</div>`;
}

function render(ids) {
  const feed = $('#feed');
  if (state.order.length) feed.querySelector('.empty')?.remove();
  const nearBottom = feed.scrollHeight - feed.scrollTop - feed.clientHeight < 120;
  for (const id of ids) {
    const e = state.tasks.get(id);
    let wrap = document.getElementById(`t-${id}`);
    if (!wrap) {
      wrap = document.createElement('section');
      wrap.id = `t-${id}`;
      wrap.dataset.task = id;
      wrap.innerHTML = '<div class="user"></div><div class="task"></div>';
      feed.appendChild(wrap);
    }
    wrap.querySelector('.user').textContent = e.task.instruction || '';
    wrap.querySelector('.task').innerHTML = taskHtml(e);
    wrap.classList.toggle('suite', !!e.task.reply_to);
    wrap.classList.toggle('current', !!state.replyTo && conversationOf(id) === conversationOf(state.replyTo));
  }
  if (nearBottom || state.replay) feed.scrollTop = feed.scrollHeight;
  updatePending();
}

// ---------- conversations
const conversationOf = (id) => state.tasks.get(id)?.task.conversation || id;

function setReplyTo(id) {
  state.replyTo = id;
  const t = id && state.tasks.get(id)?.task;
  $('#thread').classList.toggle('hidden', !t);
  if (t) $('#threadText').textContent = `↪ Suite de « ${t.instruction?.length > 50 ? `${t.instruction.slice(0, 50)}…` : t.instruction} »`;
  render(state.order);
}

/** Le dernier message envoyé dans la conversation suivie est la tâche à laquelle on répond. */
function followConversation() {
  if (!state.replyTo) return;
  const conv = conversationOf(state.replyTo);
  const last = [...state.order].reverse().find((id) => conversationOf(id) === conv);
  if (last && last !== state.replyTo) setReplyTo(last);
}

function updatePending() {
  let n = 0;
  for (const e of state.tasks.values()) for (const a of e.approvals.values()) if (!a.decision) n++;
  const b = $('#pendingBtn');
  b.textContent = n;
  b.classList.toggle('hidden', n === 0);
  document.title = n ? `(${n}) Cockpit` : 'Cockpit';
  navigator.setAppBadge?.(n).catch(() => {});
}

function renderProjects() {
  // Projets groupés par pod (étiquette seulement s'il y en a plusieurs), grisés si le worker est hors ligne.
  const chip = (p) =>
    `<button type="button" class="chip${p.online === false ? ' off' : ''}" role="radio" aria-checked="${p.id === state.selected}"
       data-project="${esc(p.id ?? '')}" title="${p.online === false ? 'pod hors ligne : la tâche partira à sa reconnexion' : ''}">
      ${esc(p.name)}${p.has_session ? ' <span class="s">●</span>' : ''}</button>`;
  const groups = new Map();
  for (const p of state.projects) {
    const key = p.worker ?? '';
    if (!groups.has(key)) groups.set(key, { name: p.worker_name || key, online: p.online !== false, projects: [] });
    groups.get(key).projects.push(p);
  }
  const labelled = groups.size > 1;
  $('#projects').innerHTML = chip({ id: null, name: 'Auto' }) + [...groups.values()].map((g) =>
    `${labelled ? `<span class="pod ${g.online ? 'on' : 'off'}" title="${g.online ? 'en ligne' : 'hors ligne'}">${esc(g.name)}</span>` : ''}${g.projects.map(chip).join('')}`).join('');
}

setInterval(() => {
  document.querySelectorAll('[data-expires]').forEach((el) => {
    const s = Math.max(0, Math.round(el.dataset.expires - Date.now() / 1000));
    el.textContent = `refus automatique dans ${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
  });
  document.querySelectorAll('[data-since]').forEach((el) => {
    el.textContent = ` · ${duree(Date.now() / 1000 - Number(el.dataset.since))}`;
  });
}, 1000);

// ---------- synchronisation (interrogation du relais)
let pollTimer = null;
let polling = false;

async function poll() {
  if (polling) return;
  polling = true;
  clearTimeout(pollTimer);
  try {
    const s = await api('etat', undefined, `&since=${state.lastId}`);
    state.replay = state.lastId === 0;
    const touched = new Set();
    for (const evt of s.events) { onEvent(evt); touched.add(evt.task_id); }
    state.lastId = s.last_id;
    if (state.replyTo === undefined) {
      const last = state.tasks.get(state.order[state.order.length - 1])?.task;
      state.replyTo = null;
      if (last?.created && Date.now() / 1000 - last.created < REPRISE_AUTO) setReplyTo(last.id);
    }
    followConversation();
    if (JSON.stringify(s.projects) !== JSON.stringify(state.projects)) { state.projects = s.projects; renderProjects(); }
    if (s.worker_online !== state.workerOnline) {
      state.workerOnline = s.worker_online;
      state.order.forEach((id) => touched.add(id));
    }
    $('#conn').classList.toggle('on', s.worker_online);
    $('#conn').title = s.worker_online ? 'worker Onyxia connecté' : 'worker Onyxia hors ligne';
    render(touched);
    state.replay = false;
  } catch (e) {
    $('#conn').classList.remove('on');
    if (e instanceof AuthError) { polling = false; return start(); }
  }
  polling = false;
  if (!document.hidden) pollTimer = setTimeout(poll, 2000);
}

document.addEventListener('visibilitychange', () => { if (!document.hidden) poll(); });

// ---------- notifications push
async function enableNotifications() {
  if (!('PushManager' in window)) {
    throw new Error(isIOS && !isStandalone ? 'Sur iPhone, ajoute d\'abord le cockpit à l\'écran d\'accueil, puis ouvre-le depuis l\'icône.' : 'Notifications non prises en charge par ce navigateur.');
  }
  if (await Notification.requestPermission() !== 'granted') throw new Error('Notifications refusées dans les réglages.');
  const reg = await navigator.serviceWorker.ready;
  const { key } = await api('vapid');
  const sub = await reg.pushManager.getSubscription() || await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64u.dec(key) });
  await api('push_abonner', { endpoint: sub.endpoint });
  localStorage.setItem('cockpit_push', '1');
}

async function refreshPushSubscription() {
  // Réenregistre l'abonnement existant (il peut changer ou avoir été purgé côté serveur).
  try {
    if (window.Notification?.permission !== 'granted') return;
    const sub = await (await navigator.serviceWorker.ready).pushManager.getSubscription();
    if (sub) await api('push_abonner', { endpoint: sub.endpoint });
  } catch { /* sans importance */ }
}

// ---------- actions
document.addEventListener('click', async (ev) => {
  const b = ev.target.closest('button');
  if (!b) return;
  try {
    if (b.dataset.approve || b.dataset.deny) {
      b.parentElement.querySelectorAll('button').forEach((x) => (x.disabled = true));
      await api('decider', { approval_id: b.dataset.approve || b.dataset.deny, decision: b.dataset.approve ? 'approve' : 'deny' });
      poll();
    } else if (b.dataset.cancel) {
      await api('annuler', { task_id: b.dataset.cancel });
      poll();
    } else if (b.dataset.reply) {
      setReplyTo(b.dataset.reply);
      input.focus();
    } else if (b.id === 'newConv') {
      setReplyTo(null);
      input.focus();
    } else if (b.dataset.choose) {
      await api('choisir_projet', { task_id: b.closest('section').dataset.task, project: b.dataset.choose });
      poll();
    } else if ('project' in b.dataset) {
      state.selected = b.dataset.project || null;
      renderProjects();
    } else if ('close' in b.dataset) {
      b.closest('dialog').close();
    } else if (b.id === 'pendingBtn') {
      document.querySelector('.approval:not(.resolved)')?.scrollIntoView({ behavior: 'smooth', block: 'center' });
    } else if (b.id === 'menuBtn') {
      $('#mNotif').classList.toggle('hidden', window.Notification?.permission === 'granted' && localStorage.getItem('cockpit_push') === '1');
      $('#workerInfo').textContent = state.workerOnline ? '🟢 Worker Onyxia connecté' : '🔴 Worker Onyxia hors ligne (lance ./run.sh dans ton service VSCode)';
      $('#menuDlg').showModal();
    } else if (b.id === 'mNotif') {
      await enableNotifications();
      b.classList.add('hidden');
      alert('Notifications activées : tu seras prévenu des validations à faire, même écran verrouillé.');
    } else if (b.id === 'mAudit') {
      await openAudit();
    } else if (b.id === 'mDevices') {
      await openDevices();
    } else if (b.dataset.delDevice) {
      if (confirm('Supprimer cette passkey ?')) { await api('appareil_supprimer', { id: b.dataset.delDevice }); await openDevices(); }
    } else if (b.id === 'addPasskey') {
      await createPasskey();
      await openDevices();
    } else if (b.id === 'mLogout') {
      await api('deconnexion', {});
      location.reload();
    }
  } catch (e) {
    if (e instanceof AuthError) return start();
    if (e.name !== 'NotAllowedError') alert(e.message);
  }
});

function showList(title, html) {
  $('#menuDlg').close();
  $('#listTitle').textContent = title;
  $('#listBody').innerHTML = html;
  if (!$('#listDlg').open) $('#listDlg').showModal();
}

async function openAudit() {
  const rows = await api('audit', undefined, '&limit=150');
  showList('Journal d\'audit', `<div class="auditList">${rows.map((r) => {
    const tag = r.decision || r.policy || '';
    const what = r.tool ? `${r.tool} ${typeof r.input === 'string' ? r.input : JSON.stringify(r.input)}` : (r.instruction || r.result || r.reason || '');
    return `<div class="row">${esc(r.ts)} · ${esc(r.event)} ${r.project ? `· ${esc(r.project)}` : ''} ${tag ? `<b class="${esc(tag)}">${esc(tag)}</b>` : ''}<br>${esc(String(what).slice(0, 300))}</div>`;
  }).join('') || 'Vide.'}</div>`);
}

async function openDevices() {
  const list = await api('appareils');
  const fmt = (ts) => (ts ? new Date(ts * 1000).toLocaleDateString('fr-FR') : 'jamais');
  showList('Passkeys', list.map((d) => `<div class="dev"><div>${esc(d.nom)}<small>créée le ${fmt(d.cree_le)} · utilisée ${fmt(d.utilisee_le)}</small></div>
    <button class="btn ghost" data-del-device="${esc(d.id)}">Supprimer</button></div>`).join('')
    + '<div class="actions"><button class="btn ok" id="addPasskey">+ Ajouter une passkey sur cet appareil</button></div>'
    + '<p class="hint">Les passkeys iCloud / Google se synchronisent entre tes appareils. Pour un appareil d\'un autre écosystème : « ./run.sh pair ».</p>');
}

// ---------- saisie
const input = $('#input');
input.addEventListener('input', () => { input.style.height = 'auto'; input.style.height = `${input.scrollHeight}px`; });
input.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey && !matchMedia('(pointer: coarse)').matches) { e.preventDefault(); $('#composer').requestSubmit(); }
});

$('#composer').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const instruction = input.value.trim();
  if (!instruction) return;
  $('#send').disabled = true;
  try {
    const t = await api('tache', { instruction, project: state.selected, reply_to: state.replyTo || null });
    input.value = '';
    input.style.height = 'auto';
    onEvent({ kind: 'task', task_id: t.id, task: t });
    setReplyTo(t.id);
    await poll();
  } catch (e) {
    if (e instanceof AuthError) start(); else alert(e.message);
  }
  $('#send').disabled = false;
});

// ---------- démarrage
async function start() {
  try {
    if (!(await ensureAuth())) return;
    $('#auth').classList.add('hidden');
    refreshPushSubscription();
    poll();
  } catch (e) {
    authScreen({ title: 'Hors ligne', text: e.message, button: 'Réessayer', onClick: start });
  }
}

if ('serviceWorker' in navigator) navigator.serviceWorker.register('sw.js').catch(console.error);
// Lien d'appairage ouvert alors que le cockpit est déjà affiché : seul le #hash change.
window.addEventListener('hashchange', () => { if (location.hash.includes('pair=')) start(); });
start();
