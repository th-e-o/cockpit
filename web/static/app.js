'use strict';

const $ = (s) => document.querySelector(s);
// replyTo : tâche dont le prochain message est la suite (undefined tant que l'historique n'est pas chargé)
const state = { projects: [], selected: null, tasks: new Map(), order: [], lastId: 0, workerOnline: false, replay: false, replyTo: undefined,
  workers: [], diffs: new Map(), view: 'chat', bilan: null, bilanAt: 0, bilanJours: 7, repos: null };
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
  s = s.replace(/^\s*[-*]\s+/gm, '• ')
       .replace(/`([^`\n]+)`/g, '<code>$1</code>')
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
function authScreen({ title, text, button, onClick, alt, onAlt, hint, code = false }) {
  $('#auth').classList.remove('hidden');
  $('#pairForm').classList.toggle('hidden', !code);
  $('#pairCode').value = '';
  $('#pairBtn').disabled = false;
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
      hint: 'Nouvel appareil sans passkey synchronisée ? Lance « ./run.sh pair » dans Onyxia : scanne le QR code '
        + '(téléphone) ou saisis ici la clé affichée (ordinateur).',
      code: true,
    }));
  }
  authScreen({ title: 'Bienvenue', text: 'Pour connecter cet appareil, lance « ./run.sh pair » dans ton terminal Onyxia.',
    hint: 'Téléphone : scanne le QR code avec l\'appareil photo. Ordinateur : saisis la clé d\'appairage affichée sous le QR code.',
    code: true });
  if (!matchMedia('(pointer: coarse)').matches) $('#pairCode').focus();
  return false;
}

// Clé d'appairage saisie (ordinateur sans appareil photo) : même effet que le lien du QR code.
$('#pairCode').addEventListener('input', () => { $('#pairCode').value = $('#pairCode').value.toUpperCase(); });
$('#pairForm').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  $('#pairBtn').disabled = true;
  try {
    await api('appairer', { code: $('#pairCode').value });
    if (await afterPairing()) start();
  } catch (e) {
    $('#authHint').textContent = e.message;
    $('#pairBtn').disabled = false;
    $('#pairCode').select();
  }
});

function afterPairing() {
  return new Promise((resolve) => authScreen({
    title: `${/iPhone|iPad|Android/.test(navigator.userAgent) ? 'Téléphone' : 'Ordinateur'} connecté ✓`,
    text: /iPhone|iPad|Android/.test(navigator.userAgent)
      ? 'Crée une passkey : tu te reconnecteras d\'un regard, sans jamais taper de code.'
      : 'Crée une passkey (Windows Hello, Touch ID, clé de sécurité ou ton téléphone) : tu te reconnecteras sans code.',
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
const SERVICE_TOOLS = { lancer_service: '🚀 Lancer un service', arreter_service: '🛑 Arrêter un service', lancer_en_parallele: '🔀 Claude en parallèle',
  creer_depot: '📦 Nouveau dépôt GitHub' };

/** Carte dédiée au lancement / à l'arrêt d'un service Onyxia : ressources lisibles, quota dans la raison. */
function serviceInput(a) {
  const i = a.tool_input;
  if (a.tool.endsWith('arreter_service')) return `cockpit-${i.nom}`;
  return [`Nom : cockpit-${i.nom}`, `Dépôt : ${i.depot_git ? `${i.depot_git}${i.branche ? ` (${i.branche})` : ''}` : 'aucun (dossier vide)'}`,
    `Ressources : ${i.cpu || 2} CPU · ${i.memoire || '8Gi'} · ${i.disque || '10Gi'} de disque${i.gpu ? ' · 1 GPU' : ''}`].join('\n');
}

function approvalHtml(a) {
  const service = SERVICE_TOOLS[a.tool.replace(/^mcp__(services|cockpit)__/, '')];
  const input = a.tool.endsWith('lancer_en_parallele')
    ? (a.tool_input.taches || []).map((t, i) => `${i + 1}. ${t.projet ? `[${t.projet}] ` : ''}${t.instruction}`).join('\n\n')
    : a.tool.endsWith('creer_depot') ? [`Nom : ${a.tool_input.nom}`, `Visibilité : ${a.tool_input.prive === false ? 'PUBLIC' : 'privé'}`,
      a.tool_input.description ? `Description : ${a.tool_input.description}` : ''].filter(Boolean).join('\n')
    : service ? serviceInput(a)
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
    if (prev.length) html += `<details class="steps" data-k="steps"><summary>${prev.length} étape(s) faite(s)</summary>${prev.map(row).join('')}</details>`;
    const since = e.task.created ? `<span class="since" data-since="${Number(e.task.created)}"></span>` : '';
    html += `<div class="now">${e.task.status === 'running' ? '<span class="spin"></span>' : ''} ${esc(cur.label)}${since}</div>`;
  } else {
    const end = e.events.length ? e.events[e.events.length - 1].ts : 0;
    const took = end && e.task.created ? ` · ${duree(end - e.task.created)}` : '';
    html += `<details class="steps" data-k="steps"><summary>${steps.length} étape(s)${took}</summary>${steps.map(row).join('')}</details>`;
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
  if (!active) body += changesHtml(e) + worktreeHtml(e) + nextStepsHtml(e);

  return `<div class="meta">
      <span class="pill st-${esc(t.status)}">${STATUS[t.status] || esc(t.status)}</span>
      ${proj ? `<span class="pill">${esc(proj.name)}</span>` : ''}
      ${t.route_reason && t.project ? `<span title="${esc(t.route_reason)}">auto</span>` : ''}
      ${t.worktree ? `<span class="pill wt" title="Claude en parallèle, dans son propre worktree git">🌿 ${esc(t.worktree.branch)}</span>` : ''}
      ${t.parent ? `<span class="pill" title="lancée par un autre Claude (lancer_en_parallele)">🔀 sous-tâche</span>` : ''}
      ${ACTIVE.has(t.status) ? `<button class="btn ghost" data-cancel="${esc(t.id)}">Arrêter</button>`
        : t.id !== state.replyTo && t.kind !== 'service' ? `<button class="btn ghost" data-reply="${esc(t.id)}">Répondre</button>` : ''}
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
    const box = wrap.querySelector('.task');
    const open = new Set([...box.querySelectorAll('details[data-k][open]')].map((d) => d.dataset.k));
    box.innerHTML = taskHtml(e);
    box.querySelectorAll('details[data-k]').forEach((d) => { if (open.has(d.dataset.k)) d.open = true; });
    wrap.classList.toggle('suite', !!e.task.reply_to);
    wrap.classList.toggle('current', !!state.replyTo && conversationOf(id) === conversationOf(state.replyTo));
  }
  if (nearBottom || state.replay) feed.scrollTop = feed.scrollHeight;
  updatePending();
}

// ---------- modifications (résumé git calculé par le worker) et branches parallèles
const FST = { A: 'ajouté', M: 'modifié', D: 'supprimé', T: 'type modifié' };
const plural = (n, w) => `${n} ${w}${n > 1 ? 's' : ''}`;

/** Résumé des modifications : l'événement « changes » (avec diffs à la demande), sinon celui de la tâche. */
function changesOf(e) {
  const ev = [...e.events].reverse().find((x) => x.type === 'changes');
  return { c: ev?.changes || e.task.changes, patches: !!ev?.has_patches };
}

function changesLine(c) {
  const parts = [plural(c.file_count, 'fichier')];
  if (c.git !== false && (c.added || c.removed)) parts.push(`<span class="add">+${c.added}</span> <span class="del">−${c.removed}</span>`);
  if (c.commits?.length) parts.push(plural(c.commits.length, 'commit'));
  return parts.join(' · ');
}

function branchLine(c) {
  if (!c.branch || c.branch === 'HEAD') return '';
  const push = !c.upstream ? 'non publiée' : c.ahead ? `${c.ahead} commit(s) à pousser` : 'à jour sur le dépôt ✓';
  return `<div class="branch">⎇ <b>${esc(c.branch)}</b> · ${push}</div>`;
}

function changesHtml(e) {
  const { c, patches } = changesOf(e);
  if (!c) return '';
  if (!c.file_count && !c.commits?.length) return '<div class="changes none">Aucun fichier modifié.</div>';
  const files = (c.files || []).map((f) => {
    const slash = f.path.lastIndexOf('/');
    const name = `<span class="dir">${esc(f.path.slice(0, slash + 1))}</span>${esc(f.path.slice(slash + 1))}`;
    const counts = f.binary ? '<span class="fn">binaire</span>'
      : c.git === false ? '' : `<span class="fn"><span class="add">+${f.added}</span> <span class="del">−${f.removed}</span></span>`;
    const row = `<span class="fst fst-${esc(f.status)}" title="${FST[f.status] || f.status}">${esc(f.status)}</span><span class="fp">${name}</span>${counts}`;
    return patches && !f.binary
      ? `<details class="file" data-k="f:${esc(f.path)}" data-task="${esc(e.task.id)}" data-path="${esc(f.path)}"><summary>${row}</summary><div class="diff">Chargement…</div></details>`
      : `<div class="file"><div class="frow">${row}</div></div>`;
  }).join('');
  const more = c.file_count > (c.files || []).length ? `<div class="hint">… et ${c.file_count - c.files.length} autre(s)</div>` : '';
  const commits = (c.commits || []).map((x) => `<div class="commit"><code>${esc(x.sha)}</code> ${esc(x.subject)}</div>`).join('');
  return `<div class="changes"><div class="chhead">📝 ${changesLine(c)}</div>${branchLine(c)}
    <div class="files">${files}${more}</div>${commits ? `<div class="commits">${commits}</div>` : ''}</div>`;
}

function diffHtml(patch) {
  if (!patch) return '<div class="hint">Diff indisponible (fichier trop gros ou limite atteinte).</div>';
  return `<pre class="diffpre">${patch.split('\n').map((l) => {
    const cls = l.startsWith('@@') ? 'h' : l.startsWith('+') ? 'a' : l.startsWith('-') ? 'd' : '';
    return `<span${cls ? ` class="${cls}"` : ''}>${esc(l) || ' '}</span>`;
  }).join('')}</pre>`;
}

async function loadDiff(details) {
  const id = details.dataset.task;
  if (!state.diffs.has(id)) state.diffs.set(id, api('diff', undefined, `&task=${encodeURIComponent(id)}`).catch((err) => { state.diffs.delete(id); throw err; }));
  try {
    const { patches } = await state.diffs.get(id);
    details.querySelector('.diff').innerHTML = diffHtml(patches[details.dataset.path]);
  } catch (err) {
    details.querySelector('.diff').textContent = `Diff introuvable : ${err.message}`;
  }
}
document.addEventListener('toggle', (ev) => { if (ev.target.matches?.('details.file[open]')) loadDiff(ev.target); }, true);

const lastOfConversation = (id) => [...state.order].reverse().find((x) => conversationOf(x) === conversationOf(id));

function worktreeHtml(e) {
  const t = e.task;
  const msgs = e.events.filter((x) => x.type === 'worktree')
    .map((x) => `<div class="wtmsg ${x.ok ? 'ok' : 'bad'}">${x.ok ? '✓' : '⚠️'} ${esc(x.text)}</div>`).join('');
  if (!t.worktree) return msgs;
  const st = t.worktree.state;
  let html = `<div class="wtbox"><div>🌿 Travail en parallèle sur la branche <b>${esc(t.worktree.branch)}</b>${
    st === 'merged' ? ' — fusionnée ✓' : st === 'discarded' ? ' — jetée' : ''}</div>`;
  if (st === 'active' && lastOfConversation(t.id) === t.id) {
    html += `<div class="actions"><button class="btn ok" data-wt="merge" data-task-id="${esc(t.id)}">Fusionner dans le projet</button>
      <button class="btn bad" data-wt="discard" data-task-id="${esc(t.id)}">Jeter la branche</button></div>`;
  }
  return html + msgs + '</div>';
}

// ---------- prochaines étapes proposées par Claude (suivi)
/** Étapes à suivre : celles de la dernière tâche terminée d'une conversation, tant qu'elles ne sont pas ignorées. */
function pendingSteps(e) {
  const t = e.task;
  return t.status === 'done' && t.next_steps?.length && !t.next_steps_dismissed && lastOfConversation(t.id) === t.id ? t.next_steps : null;
}

function stepsList(t, steps) {
  return steps.map((x, i) => `<div class="next">
      <button class="nmain" data-next-edit="${esc(t.id)}" data-i="${i}" title="${esc(x.instruction)}">${esc(x.titre)}</button>
      <button class="nrun" data-next-run="${esc(t.id)}" data-i="${i}" aria-label="lancer : ${esc(x.titre)}" title="Lancer maintenant">▶</button></div>`).join('');
}

function nextStepsHtml(e) {
  const steps = pendingSteps(e);
  if (!steps) return '';
  return `<div class="nexts"><div class="nhead"><span>💡 Prochaines étapes</span>
      <button class="btn ghost" data-dismiss="${esc(e.task.id)}">Ignorer</button></div>${stepsList(e.task, steps)}
    <div class="hint">Toucher une étape la prépare dans la saisie ; ▶ la lance tout de suite.</div></div>`;
}

function followHtml(items) {
  if (!items.length) return '';
  return `<section class="sect"><h3>Prochaines étapes · ${items.length}</h3><div class="follows">${items.map((e) => {
    const proj = state.projects.find((p) => p.id === e.task.project);
    return `<article class="follow"><div class="fhead"><b>${esc(proj?.name || e.task.project || '')}</b>
        <span class="muted" data-ago="${Number(e.task.updated || e.task.created)}"></span>
        <button class="btn ghost" data-dismiss="${esc(e.task.id)}">Ignorer</button></div>
      <button class="forigin" data-jump="${esc(e.task.id)}">↳ ${esc(e.task.instruction || '')}</button>
      ${stepsList(e.task, pendingSteps(e))}</article>`;
  }).join('')}</div></section>`;
}

async function runNextStep(id, i, now) {
  const t = state.tasks.get(id)?.task;
  const step = t?.next_steps?.[i];
  if (!step) return;
  if (state.view === 'dash' && !matchMedia('(min-width: 1100px)').matches) setView('chat');
  if (!now) {
    setReplyTo(id);
    input.value = step.instruction;
    input.dispatchEvent(new Event('input'));
    input.focus();
    return;
  }
  const n = await api('tache', { instruction: step.instruction, project: t.project, reply_to: id });
  onEvent({ kind: 'task', task_id: n.id, task: n });
  setReplyTo(n.id);
  await poll();
}

// ---------- tableau de bord
const GO = 1024 ** 3;
const fmtGo = (b) => `${(b / GO).toLocaleString('fr-FR', { maximumFractionDigits: b < 10 * GO ? 1 : 0 })} Go`;
const fmtCost = (x) => `${(x || 0).toLocaleString('fr-FR', { minimumFractionDigits: 2, maximumFractionDigits: 2 })} $`;
const dureeLongue = (s) => (s < 3600 ? duree(s) : s < 86400 ? `${Math.floor(s / 3600)} h ${String(Math.floor(s % 3600 / 60)).padStart(2, '0')}`
  : `${Math.floor(s / 86400)} j ${Math.floor(s % 86400 / 3600)} h`);
function ago(ts) {
  const s = Math.max(0, Date.now() / 1000 - ts);
  return s < 60 ? 'à l\'instant' : s < 3600 ? `il y a ${Math.floor(s / 60)} min` : s < 86400 ? `il y a ${Math.floor(s / 3600)} h` : `il y a ${Math.floor(s / 86400)} j`;
}

/** Jauge : piste du même ton, remplissage accent ; au-delà de 85 % passe en alerte, avec icône et libellé. */
function meter(label, ratio, text, title) {
  if (ratio == null || Number.isNaN(ratio)) return `<div class="meter"><span>${label}</span><div class="bar"></div><span class="mv">—</span></div>`;
  const r = Math.min(1, Math.max(0, ratio));
  const level = r >= 0.95 ? 'bad' : r >= 0.85 ? 'warn' : '';
  const flag = level === 'bad' ? '⛔ ' : level === 'warn' ? '⚠ ' : '';
  return `<div class="meter" title="${esc(title || '')}"><span>${label}</span>
    <div class="bar ${level}" role="meter" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Math.round(r * 100)}" aria-label="${label}"><i style="width:${(r * 100).toFixed(1)}%"></i></div>
    <span class="mv">${flag}${text}</span></div>`;
}

function currentStep(e) {
  if (e.task.status === 'waiting_approval') return '⚠️ Attend ta validation';
  if (e.task.status === 'queued') return '⏳ En file d\'attente';
  if (e.task.status === 'routing') return '🧭 Choisit le projet';
  if (e.task.status === 'pending') return state.workerOnline ? '⏳ Transmise au pod' : '⏳ Pod hors ligne';
  const steps = stepsOf(e);
  return steps.length ? steps[steps.length - 1].label : 'Réfléchit…';
}

function liveCard(e) {
  const t = e.task;
  const proj = state.projects.find((p) => p.id === t.project);
  const todos = [...e.events].reverse().find((x) => x.todos)?.todos;
  const done = todos ? todos.filter((x) => x.status === 'completed').length : 0;
  const plan = todos?.length ? `<div class="plan"><div class="bar"><i style="width:${(done / todos.length * 100).toFixed(0)}%"></i></div><span>Plan ${done}/${todos.length}</span></div>` : '';
  return `<button class="live${t.status === 'waiting_approval' ? ' alert' : ''}" data-jump="${esc(t.id)}">
    <div class="lhead"><span class="pill st-${esc(t.status)}">${STATUS[t.status] || esc(t.status)}</span>
      <b>${esc(proj?.name || t.project || (t.kind === 'service' ? 'Onyxia' : 'Auto'))}</b>${t.worktree ? `<span class="wt">🌿 parallèle</span>` : ''}
      <span class="since" data-since="${Number(t.created)}"></span></div>
    <div class="linst">${esc(t.instruction || '')}</div>
    <div class="lstep">${t.status === 'running' ? '<span class="spin"></span>' : ''}<span>${esc(currentStep(e))}</span></div>${plan}</button>`;
}

function podCard(w) {
  const st = w.state || {};
  const r = st.resources || {};
  const cpuTxt = r.cpu == null ? '—' : `${Math.round(r.cpu * 100)} % de ${String(r.cpu_cores).replace('.', ',')} cœurs`;
  const meters = !w.online ? '' : meter('CPU', r.cpu, cpuTxt, 'part de la limite CPU du pod')
    + meter('Mémoire', r.mem_limit ? r.mem_used / r.mem_limit : null, r.mem_limit ? `${fmtGo(r.mem_used)} / ${fmtGo(r.mem_limit)}` : '—', 'ensemble de travail / limite du conteneur')
    + meter('Disque', r.disk_total ? r.disk_used / r.disk_total : null, r.disk_total ? `${fmtGo(r.disk_used)} / ${fmtGo(r.disk_total)}` : '—', 'volume ~/work');
  const running = [...state.tasks.values()].filter((e) => e.task.worker === w.id && ['running', 'waiting_approval'].includes(e.task.status)).length;
  const max = st.max_agents || 0;
  const slots = max ? `<div class="slots" title="${running} Claude actif(s) sur ${max} possibles">${Array.from({ length: max }, (_, i) =>
    `<i class="${i < running ? 'on' : ''}"></i>`).join('')}<span>${running}/${max} Claude${st.queued ? ` · ${st.queued} en file` : ''}</span></div>` : '';
  const projects = (w.projects || []).map((pid) => {
    const p = state.projects.find((x) => x.id === pid);
    const g = st.projects?.[pid];
    const git = !g ? '<span class="muted">sans git</span>' : [
      `⎇ ${esc(g.branch || '?')}`,
      g.dirty ? `<span class="warnt">● ${plural(g.dirty, 'fichier')} non commité${g.dirty > 1 ? 's' : ''}</span>` : '<span class="okt">propre</span>',
      g.ahead ? `↑${g.ahead} à pousser` : '', g.behind ? `↓${g.behind} en retard` : '',
    ].filter(Boolean).join(' · ');
    const last = g?.last_commit ? `<div class="lastc" title="${esc(g.last_commit.sha)}">${esc(g.last_commit.subject)} · <span data-ago="${g.last_commit.ts}"></span></div>` : '';
    return `<div class="proj"><div class="pname">${esc(p?.name || pid)}</div><div class="pgit">${git}</div>${last}</div>`;
  }).join('');
  const wts = (st.worktrees || []).map((wt) => {
    const id = [...state.order].reverse().find((x) => conversationOf(x) === wt.conversation);
    const e = id && state.tasks.get(id);
    const busy = e && ACTIVE.has(e.task.status);
    const btns = e?.task.worktree && !busy ? `<button class="btn ghost" data-wt="merge" data-task-id="${esc(id)}">Fusionner</button><button class="btn ghost" data-wt="discard" data-task-id="${esc(id)}">Jeter</button>` : '';
    return `<div class="wtrow"><span>🌿 <b>${esc(wt.branch)}</b> <span class="muted">${esc(wt.project)}</span>${busy ? ' · <span class="acc">en cours</span>' : ''}</span>
      <span class="wtbtns">${e ? `<button class="btn ghost" data-jump="${esc(id)}">Voir</button>` : ''}${btns}</span></div>`;
  }).join('');
  return `<article class="podcard${w.online ? '' : ' off'}">
    <div class="podhead"><span class="dot${w.online ? ' on' : ''}"></span><b>${esc(w.name)}</b>
      <span class="tag">${w.orchestrator ? 'orchestrateur' : 'projet'}</span>
      <span class="muted podseen">${w.online ? (st.started ? `actif depuis <span data-uptime="${st.started}"></span>` : 'en ligne') : `hors ligne · vu <span data-ago="${w.seen}"></span>`}</span></div>
    ${slots}${meters ? `<div class="meters">${meters}</div>` : ''}
    ${projects ? `<div class="projs">${projects}</div>` : ''}
    ${wts ? `<div class="wts"><div class="sub">Branches parallèles</div>${wts}</div>` : ''}
    <div class="podfoot">Aujourd'hui : ${plural(w.tasks_today, 'tâche')} · ${fmtCost(w.cost_today)}</div></article>`;
}

function recentRow(e) {
  const t = e.task;
  const { c } = changesOf(e);
  const proj = state.projects.find((p) => p.id === t.project);
  const icon = { done: '✅', error: '❌', cancelled: '⏹', needs_project: '❓' }[t.status] || '•';
  return `<button class="recent" data-jump="${esc(t.id)}"><span class="ri">${icon}</span>
    <span class="rbody"><span class="rinst">${esc(t.instruction || '')}</span>
      <span class="rmeta">${esc(proj?.name || t.project || '')}${c && (c.file_count || c.commits?.length) ? ` · ${changesLine(c)}` : ''}${t.worktree ? ' · 🌿' : ''}</span></span>
    <span class="rtime" data-ago="${Number(t.updated || t.created)}"></span></button>`;
}

// ---------- bilan par projet
const BILAN_PERIODES = [[1, 'Aujourd\'hui'], [7, '7 jours'], [30, '30 jours']];
const jourFr = (ts) => new Date(ts * 1000).toLocaleDateString('fr-FR', { weekday: 'short', day: 'numeric', month: 'short' });

async function loadBilan(force = false) {
  if (state.bilanLoading || !dashVisible()) return;
  if (!force && state.bilan?.days === state.bilanJours && Date.now() - state.bilanAt < 30000) return;
  state.bilanLoading = true;
  try {
    state.bilan = await api('bilan', undefined, `&jours=${state.bilanJours}`);
    state.bilanAt = Date.now();
  } catch { /* nouvel essai au prochain rendu */ }
  state.bilanLoading = false;
  renderDash();
}

/** Activité par jour (tâches) : une seule série, barres fines arrondies posées sur la ligne de base. */
function activityChart(days, start) {
  const max = Math.max(1, ...days);
  const bars = days.map((n, i) => {
    const label = `${jourFr(start + i * 86400 + 43200)} : ${n} tâche${n > 1 ? 's' : ''}`;
    return `<span class="col" title="${esc(label)}"><i class="${n ? 'b' : 'z'}" style="height:${n ? Math.max(10, n / max * 100).toFixed(0) : 0}%"></i></span>`;
  }).join('');
  const total = days.reduce((a, b) => a + b, 0);
  return `<figure class="activity"><div class="bars" role="img" aria-label="Activité : ${total} tâche(s) sur ${days.length} jours">${bars}</div>
    <figcaption><span>${jourFr(start + 43200)}</span><span>aujourd'hui</span></figcaption></figure>`;
}

function bilanCard(b, p) {
  const proj = state.projects.find((x) => x.id === p.project);
  const w = state.workers.find((x) => (x.projects || []).includes(p.project));
  const g = w?.state?.projects?.[p.project];
  const stat = (v, l, sub = '') => `<div class="bstat"><div class="v">${v}</div><div class="l">${l}</div>${sub ? `<div class="s">${sub}</div>` : ''}</div>`;
  const statuts = [p.done ? `✅ ${p.done}` : '', p.error ? `❌ ${p.error}` : '', p.active ? `◐ ${p.active}` : ''].filter(Boolean).join(' · ');
  const git = g ? [`⎇ ${esc(g.branch || '?')}`, g.dirty ? `<span class="warnt">● ${g.dirty} non commité${g.dirty > 1 ? 's' : ''}</span>` : '<span class="okt">propre</span>',
    g.ahead ? `↑${g.ahead} à pousser` : ''].filter(Boolean).join(' · ') : '';
  const files = p.top_files.length ? `<div class="bsub">Fichiers les plus touchés</div>${p.top_files.map((f) => {
    const i = f.path.lastIndexOf('/');
    return `<div class="tfile"><span class="fp"><span class="dir">${esc(f.path.slice(0, i + 1))}</span>${esc(f.path.slice(i + 1))}</span><span class="muted">×${f.count}</span></div>`;
  }).join('')}` : '';
  const icon = { done: '✅', error: '❌', cancelled: '⏹', needs_project: '❓' };
  const recent = p.recent.map((t) => `<button class="brecent" data-jump="${esc(t.id)}"><span>${icon[t.status] || '◐'}</span>
    <span class="rinst">${esc(t.instruction)}</span><span class="rtime" data-ago="${t.updated}"></span></button>`).join('');
  return `<article class="bcard">
    <div class="podhead"><b>${esc(proj?.name || p.project)}</b>${w ? `<span class="tag"><span class="dot${w.online ? ' on' : ''}"></span>${esc(w.name)}</span>` : ''}
      <span class="muted podseen">actif <span data-ago="${p.last}"></span></span></div>
    ${git ? `<div class="pgit">${git}</div>` : ''}
    <div class="bstats">
      ${stat(p.tasks, plural(p.tasks, 'tâche').replace(/^\d+ /, ''), statuts)}
      ${stat(p.duration_s ? dureeLongue(p.duration_s) : '—', 'de travail')}
      ${stat(p.file_count, plural(p.file_count, 'fichier').replace(/^\d+ /, ''), p.added || p.removed ? `<span class="add">+${p.added}</span> <span class="del">−${p.removed}</span>` : '')}
      ${stat(p.commits, plural(p.commits, 'commit').replace(/^\d+ /, ''))}
    </div>
    ${b.days > 1 ? activityChart(p.days, b.start) : ''}
    ${files ? `<div class="tfiles">${files}</div>` : ''}
    ${recent ? `<div class="brecents">${recent}</div>` : ''}
  </article>`;
}

function bilanHtml() {
  const b = state.bilan;
  const seg = `<div class="seg" role="group" aria-label="période">${BILAN_PERIODES.map(([d, l]) =>
    `<button type="button" data-bilan="${d}" aria-pressed="${state.bilanJours === d}">${l}</button>`).join('')}</div>`;
  const body = !b || b.days !== state.bilanJours ? '<div class="emptyd">Chargement…</div>'
    : b.projects.length ? `<div class="bilans">${b.projects.map((p) => bilanCard(b, p)).join('')}</div>`
    : '<div class="emptyd">Aucune tâche sur cette période.</div>';
  return `<section class="sect"><div class="secthead"><h3>Bilan par projet</h3>${seg}</div>${body}</section>`;
}

// ---------- nouveau pod sur un dépôt git
const slug = (url) => (url.replace(/\/+$/, '').replace(/\.git$/, '').split('/').pop() || '').toLowerCase()
  .replace(/[^a-z0-9-]/g, '-').replace(/-+/g, '-').replace(/^-|-$/g, '').slice(0, 30).replace(/-$/, '') || 'projet';

function setPodMode(mode) {
  document.querySelectorAll('[data-pod-mode]').forEach((x) => x.setAttribute('aria-pressed', String(x.dataset.podMode === mode)));
  $('#podExisting').classList.toggle('hidden', mode === 'new');
  $('#podNew').classList.toggle('hidden', mode !== 'new');
  $('#podBranch').closest('label').classList.toggle('hidden', mode === 'new');
  if (mode === 'new' && !$('#newRepoName').value) $('#newRepoName').value = $('#podName').value;
}
const podMode = () => ($('#podNew').classList.contains('hidden') ? 'existing' : 'new');
$('#podName').addEventListener('input', () => {
  $('#podName').dataset.touched = '1';
  if (podMode() === 'new' && !$('#newRepoName').dataset.touched) $('#newRepoName').value = $('#podName').value;
});
$('#newRepoName').addEventListener('input', () => {
  $('#newRepoName').dataset.touched = '1';
  if (!$('#podName').dataset.touched) $('#podName').value = slug($('#newRepoName').value);
});

async function openPodDialog() {
  $('#menuDlg').close();
  $('#podForm').reset();
  delete $('#newRepoName').dataset.touched;
  delete $('#podName').dataset.touched;
  setPodMode('existing');
  $('#podHint').textContent = '';
  $('#podSubmit').disabled = false;
  $('#podDlg').showModal();
  renderRepos();
  try {
    const r = await api('depots');
    state.repos = r.depots;
    if (!r.orchestrateur) $('#podHint').textContent = '⚠️ Orchestrateur hors ligne : le pod ne pourra pas être lancé.';
    else if (!r.depots.length) $('#podHint').textContent = 'Liste des dépôts indisponible : colle l\'URL du dépôt.';
  } catch (e) {
    state.repos = [];
    $('#podHint').textContent = e.message;
  }
  renderRepos();
}

function renderRepos() {
  const q = $('#repoSearch').value.trim().toLowerCase();
  const list = state.repos;
  if (!list) { $('#repoList').innerHTML = '<div class="hint">Chargement de tes dépôts…</div>'; return; }
  const hits = list.filter((r) => !r.archived && (!q || `${r.name} ${r.description}`.toLowerCase().includes(q))).slice(0, q ? 20 : 8);
  const sel = $('#podRepo').value;
  $('#repoList').innerHTML = hits.map((r) => `<button type="button" class="repo" role="option" aria-selected="${r.url === sel}"
      data-repo="${esc(r.url)}" data-branch="${esc(r.branch)}">
      <span class="rn">${r.private ? '🔒 ' : ''}${esc(r.name)}</span>
      <span class="rd">${esc(r.description || '')}</span>
      <span class="rp">${r.pushed ? `poussé ${ago(Date.parse(r.pushed) / 1000)}` : ''}</span></button>`).join('')
    || `<div class="hint">${list.length ? 'Aucun dépôt ne correspond.' : ''}</div>`;
}

$('#repoSearch').addEventListener('input', renderRepos);
$('#podRepo').addEventListener('change', () => { if ($('#podRepo').value && !$('#podName').value) $('#podName').value = slug($('#podRepo').value); });
$('#repoList').addEventListener('click', (ev) => {
  const b = ev.target.closest('[data-repo]');
  if (!b) return;
  ev.stopPropagation();
  $('#podRepo').value = b.dataset.repo;
  $('#podName').value = slug(b.dataset.repo);
  $('#podBranch').value = '';
  $('#podBranch').placeholder = `par défaut (${b.dataset.branch})`;
  renderRepos();
});

$('#podForm').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const nom = $('#podName').value.trim();
  if (!/^[a-z0-9]([a-z0-9-]{0,28}[a-z0-9])?$/.test(nom)) { $('#podHint').textContent = 'Nom : minuscules, chiffres et tirets (30 au plus).'; return; }
  $('#podSubmit').disabled = true;
  try {
    const nouveau = podMode() === 'new';
    const t = await api('lancer_pod', { nom, depot_git: nouveau ? '' : $('#podRepo').value.trim(), branche: nouveau ? '' : $('#podBranch').value.trim(),
      cpu: $('#podCpu').value, memoire: $('#podMem').value, disque: $('#podDisk').value, gpu: $('#podGpu').checked,
      ...(nouveau ? { creer_depot: { nom: $('#newRepoName').value.trim() || nom, prive: $('#newRepoPrivate').checked } } : {}) });
    onEvent({ kind: 'task', task_id: t.id, task: t });
    $('#podDlg').close();
    await poll();
  } catch (e) {
    if (e instanceof AuthError) return start();
    $('#podHint').textContent = e.message;
    $('#podSubmit').disabled = false;
  }
});

const dashVisible = () => getComputedStyle($('#dash')).display !== 'none';

function renderDash() {
  const tasks = [...state.tasks.values()];
  const active = tasks.filter((e) => ACTIVE.has(e.task.status));
  const working = active.filter((e) => ['running', 'waiting_approval'].includes(e.task.status)).length;
  $('#tabCount').textContent = active.length;
  $('#tabCount').classList.toggle('hidden', !active.length);
  if (!dashVisible()) return;
  let pending = 0;
  for (const e of tasks) for (const a of e.approvals.values()) if (!a.decision) pending++;
  const online = state.workers.filter((w) => w.online);
  const capacity = online.reduce((n, w) => n + (w.state?.max_agents || 0), 0);
  const today = state.workers.reduce((n, w) => n + (w.tasks_today || 0), 0);
  const cost = state.workers.reduce((n, w) => n + (w.cost_today || 0), 0);
  const recent = tasks.filter((e) => !ACTIVE.has(e.task.status) && e.task.instruction)
    .sort((a, b) => (b.task.updated || b.task.created) - (a.task.updated || a.task.created)).slice(0, 8);

  const kpi = (v, l, cls = '', title = '') => `<div class="kpi ${cls}" title="${esc(title)}"><div class="v">${v}</div><div class="l">${l}</div></div>`;
  const html = `<div class="dashwrap">
    <div class="kpis">
      ${kpi(`${working}<small>/${capacity || '—'}</small>`, 'Claude au travail', '', 'agents actifs / capacité des pods en ligne')}
      ${kpi(pending ? `⚠ ${pending}` : '0', 'Validation(s) en attente', pending ? 'warn' : '')}
      ${kpi(today, 'Tâches aujourd\'hui')}
      ${kpi(fmtCost(cost), 'Coût du jour')}
    </div>
    <section class="sect"><h3>En cours</h3>${active.length
      ? `<div class="lives">${active.map(liveCard).join('')}</div>`
      : '<div class="emptyd">Aucun Claude ne travaille. Envoie une instruction ; pour en lancer plusieurs, ouvre une nouvelle conversation par tâche.</div>'}</section>
    ${followHtml(tasks.filter(pendingSteps).sort((a, b) => (b.task.updated || b.task.created) - (a.task.updated || a.task.created)).slice(0, 6))}
    ${bilanHtml()}
    <section class="sect"><div class="secthead"><h3>Pods · ${online.length} en ligne</h3><button type="button" class="btn ghost" id="newPod">＋ Nouveau pod</button></div>${state.workers.length
      ? `<div class="pods">${state.workers.map(podCard).join('')}</div>`
      : '<div class="emptyd">Aucun pod connecté. Lance « ./run.sh » dans un service VSCode Onyxia.</div>'}</section>
    ${recent.length ? `<section class="sect"><h3>Terminées récemment</h3><div class="recents">${recent.map(recentRow).join('')}</div></section>` : ''}
  </div>`;
  // Reconstruit seulement si le contenu change (survol, focus et clics préservés) ; les durées vivent dans tick().
  if (html !== state.dashHtml) { $('#dash').innerHTML = html; state.dashHtml = html; tick(); }
  loadBilan();
}

function setView(v) {
  state.view = v;
  document.body.classList.toggle('view-dash', v === 'dash');
  $('#tabChat').setAttribute('aria-selected', String(v === 'chat'));
  $('#tabDash').setAttribute('aria-selected', String(v === 'dash'));
  try { localStorage.setItem('cockpit_view', v); } catch { /* sans importance */ }
  renderDash();
}

function jumpTo(id) {
  const el = document.getElementById(`t-${id}`);
  if (!el) return; // tâche ancienne, absente du fil
  if (state.view === 'dash' && !matchMedia('(min-width: 1100px)').matches) setView('chat');
  el.scrollIntoView({ behavior: 'smooth', block: 'center' });
  el.classList.remove('flash');
  void el.offsetWidth;
  el.classList.add('flash');
}

addEventListener('resize', () => renderDash());

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

function tick() {
  document.querySelectorAll('[data-uptime]').forEach((el) => { el.textContent = dureeLongue(Date.now() / 1000 - Number(el.dataset.uptime)); });
  document.querySelectorAll('[data-expires]').forEach((el) => {
    const s = Math.max(0, Math.round(el.dataset.expires - Date.now() / 1000));
    el.textContent = `refus automatique dans ${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
  });
  document.querySelectorAll('[data-ago]').forEach((el) => { el.textContent = ago(Number(el.dataset.ago)); });
  document.querySelectorAll('[data-since]').forEach((el) => {
    el.textContent = ` · ${duree(Date.now() / 1000 - Number(el.dataset.since))}`;
  });
}
setInterval(tick, 1000);

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
      const last = [...state.order].reverse().map((id) => state.tasks.get(id).task).find((t) => t.kind !== 'service');
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
    if (state.pendingJump && document.getElementById(`t-${state.pendingJump}`)) { jumpTo(state.pendingJump); state.pendingJump = null; }
    state.workers = s.workers || [];
    renderDash();
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
    } else if (b.dataset.nextEdit || b.dataset.nextRun) {
      if (b.dataset.nextRun) b.disabled = true;
      await runNextStep(b.dataset.nextEdit || b.dataset.nextRun, Number(b.dataset.i), !!b.dataset.nextRun);
    } else if (b.dataset.dismiss) {
      await api('ignorer_suite', { task_id: b.dataset.dismiss });
      poll();
    } else if (b.dataset.podMode) {
      setPodMode(b.dataset.podMode);
    } else if (b.dataset.bilan) {
      state.bilanJours = Number(b.dataset.bilan);
      try { localStorage.setItem('cockpit_bilan', b.dataset.bilan); } catch { /* sans importance */ }
      renderDash();
    } else if (b.id === 'mInstall') {
      $('#menuDlg').close();
      await installPrompt?.prompt();
    } else if (b.id === 'newPod' || b.id === 'mPod') {
      await openPodDialog();
    } else if (b.dataset.view) {
      setView(b.dataset.view);
    } else if (b.dataset.jump) {
      jumpTo(b.dataset.jump);
    } else if (b.dataset.wt) {
      if (b.dataset.wt === 'discard' && !confirm('Jeter cette branche et tout son travail ?')) return;
      b.disabled = true;
      await api('worktree', { task_id: b.dataset.taskId, action: b.dataset.wt });
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

// ---------- application installée (Android, PC) : invite d'installation, raccourcis de l'icône, clavier
let installPrompt = null;
addEventListener('beforeinstallprompt', (ev) => {
  ev.preventDefault();
  installPrompt = ev;
  $('#mInstall').classList.remove('hidden');
});
addEventListener('appinstalled', () => { installPrompt = null; $('#mInstall').classList.add('hidden'); });

/** Raccourcis de l'icône (manifest) : ?vue=pods ouvre le tableau de bord, ?action=pod le formulaire « Nouveau pod ». */
function applyLaunchParams() {
  const hash = location.hash.match(/^#t=(\w+)/);
  if (hash) { state.pendingJump = hash[1]; history.replaceState(null, '', location.pathname + location.search); }
  const q = new URLSearchParams(location.search);
  if (!q.has('vue') && !q.has('action')) return;
  history.replaceState(null, '', location.pathname);
  if (q.get('vue') === 'pods') setView('dash');
  if (q.get('action') === 'pod') openPodDialog();
}

// Clavier : « / » ou Ctrl/⌘+K pour écrire, Échap pour quitter la saisie, Alt+1 / Alt+2 pour changer de vue.
document.addEventListener('keydown', (e) => {
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName);
  if ((e.key === 'k' && (e.ctrlKey || e.metaKey)) || (e.key === '/' && !typing)) {
    e.preventDefault();
    if (state.view === 'dash' && !matchMedia('(min-width: 1100px)').matches) setView('chat');
    input.focus();
  } else if (e.key === 'Escape' && document.activeElement === input) {
    input.blur();
  } else if (e.altKey && (e.key === '1' || e.key === '2')) {
    e.preventDefault();
    setView(e.key === '1' ? 'chat' : 'dash');
  }
});

// ---------- démarrage
async function start() {
  try {
    if (!(await ensureAuth())) return;
    $('#auth').classList.add('hidden');
    refreshPushSubscription();
    poll();
    applyLaunchParams();
  } catch (e) {
    authScreen({ title: 'Hors ligne', text: e.message, button: 'Réessayer', onClick: start });
  }
}

try {
  if ([1, 7, 30].includes(Number(localStorage.getItem('cockpit_bilan')))) state.bilanJours = Number(localStorage.getItem('cockpit_bilan'));
  if (localStorage.getItem('cockpit_view') === 'dash') setView('dash');
} catch { /* navigation privée */ }
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('sw.js').catch(console.error);
  // Notification touchée alors que le cockpit est ouvert : aller à la tâche concernée.
  navigator.serviceWorker.addEventListener('message', (e) => { if (e.data?.jump) { state.pendingJump = e.data.jump; poll(); } });
}
// Lien d'appairage ouvert alors que le cockpit est déjà affiché : seul le #hash change.
window.addEventListener('hashchange', () => { if (location.hash.includes('pair=')) start(); });
start();
