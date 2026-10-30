'use strict';

const $ = (s) => document.querySelector(s);
// replyTo : tâche dont le prochain message est la suite (undefined tant que l'historique n'est pas chargé)
const state = { projects: [], selected: null, tasks: new Map(), order: [], lastId: 0, workerOnline: false, replay: false, replyTo: undefined,
  workers: [], diffs: new Map(), view: 'chat', bilan: null, bilanAt: 0, bilanJours: 7, repos: null,
  commands: {}, review: new Map(), model: '', variants: 1, memory: null, autopilot: null, hard: null, week: null, deleted: {}, report: null, reportAt: 0 };
const MODELES = { rapide: '⚡ Rapide', standard: '● Standard', approfondi: '◆ Approfondi' };
/** « claude-opus-5-5 » -> « Opus 5.5 » ; « claude-sonnet-4-5-20250929 » -> « Sonnet 4.5 ». */
function modelName(id) {
  const m = String(id || '').match(/claude-(?:(\d+(?:-\d+)?)-)?(opus|sonnet|haiku|fable)(?:-(\d{1,2})(?!\d)(?:-(\d{1,2})(?!\d))?)?/i);
  if (!m) return String(id || '');
  const family = m[2][0].toUpperCase() + m[2].slice(1).toLowerCase();
  const version = m[3] ? `${m[3]}${m[4] ? `.${m[4]}` : ''}` : (m[1] || '').replace('-', '.');
  return `${family} ${version}`.trim();
}
const MODEL_ICON = { Opus: '◆', Sonnet: '●', Haiku: '⚡', Fable: '✦' };
function modelPill(t) {
  const auto = t.model_auto ? ` · choisi automatiquement (${MODELES[t.model_auto.choice] || t.model_auto.choice}) : ${t.model_auto.reason}` : '';
  if (t.model_used) {
    const name = modelName(t.model_used);
    const others = (t.models || []).filter((x) => x !== t.model_used).map(modelName).filter((x, i, a) => a.indexOf(x) === i && x !== name);
    return `<span class="pill model" title="modèle qui a écrit les réponses : ${esc(t.model_used)}${others.length ? ` · sous-agents : ${esc(others.join(', '))}` : ''}${esc(auto)}">`
      + `${MODEL_ICON[name.split(' ')[0]] || '◇'} ${esc(name)}${t.model_auto ? ' <small>auto</small>' : ''}${others.length ? ` <small>+ ${esc(others.join(', '))}</small>` : ''}</span>`;
  }
  if (t.model_auto) return `<span class="pill model" title="${esc(auto.slice(3))}">${esc(MODELES[t.model_auto.choice] || t.model_auto.choice)} <small>auto</small></span>`;
  return t.model ? `<span class="pill" title="modèle demandé">${esc(MODELES[t.model] || t.model)}</span>` : '';
}
const REPRISE_AUTO = 2 * 3600; // à l'ouverture, la dernière conversation continue si elle a moins de 2 h

const STATUS = {
  pending: 'en attente', routing: 'routage…', queued: 'en file', running: 'en cours',
  waiting_approval: 'validation requise', needs_project: 'projet à choisir',
  done: 'terminé', error: 'erreur', cancelled: 'annulé', paused: 'en pause',
};
const ACTIVE = new Set(['pending', 'routing', 'queued', 'running', 'waiting_approval']);
const isStandalone = matchMedia('(display-mode: standalone)').matches || navigator.standalone === true;
const isIOS = /iPhone|iPad|iPod/.test(navigator.userAgent);

// ---------- utilitaires
/** Largeurs et hauteurs des barres : via le CSSOM, autorisé par la CSP (style-src 'self'), contrairement à style="…". */
function sizes(root) {
  root.querySelectorAll('[data-w]').forEach((el) => { el.style.width = `${el.dataset.w}%`; });
  root.querySelectorAll('[data-h]').forEach((el) => { el.style.height = `${el.dataset.h}%`; });
}
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
    `Ressources : ${i.cpu || 2} CPU · ${i.memoire || '8Gi'} · ${i.disque || '10Gi'} de disque${i.gpu ? ' · 1 GPU' : ' · sans GPU'}`].join('\n');
}

// ---------- règles « Toujours autoriser » (créées depuis une validation, appliquées par tous les pods)
const MULTI = new Set(['git', 'npm', 'pnpm', 'yarn', 'docker', 'kubectl', 'helm', 'gh', 'uv', 'pip', 'cargo', 'go', 'mc', 'aws',
  'gcloud', 'terraform', 'poetry', 'conda', 'make', 'bun', 'npx', 'python', 'python3', 'node', 'Rscript']);
const reEsc = (s) => s.replace(/[.*+?^${}()|[\]\\/]/g, '\\$&');

/** Règle ciblée proposée pour une validation : commande (1 ou 2 premiers mots), dossier, ou outil. */
function ruleFor(a) {
  const i = a.tool_input || {};
  if (a.tool === 'Bash') {
    const seg = (a.reason.match(/(?:commande non répertoriée|opération sensible) : (.+)$/) || [])[1];
    if (!seg || /`|\$\(/.test(i.command || '')) return null;
    const words = seg.replace(/^(\w+=\S*\s+)+/, '').trim().split(/\s+/);
    const n = MULTI.has(words[0]) && words[1] && !words[1].startsWith('-') ? 2 : 1;
    const prefix = words.slice(0, n);
    return { tool: 'Bash', pattern: `^${prefix.map(reEsc).join('\\s+')}\\b`, short: `${prefix.join(' ')} …`,
      label: `les commandes « ${prefix.join(' ')} … »`, example: i.command };
  }
  const path = i.file_path || i.notebook_path;
  if (path) {
    const dir = path.replace(/\/[^/]*$/, '');
    if (!dir || /\/\.git(\/|$)|\.github\/workflows/.test(dir)) return null;
    return { tool: 'files', pattern: `^${reEsc(dir)}/`, short: `${dir}/`, label: `les écritures dans ${dir}/`, example: path };
  }
  if (/lancer_service|arreter_service|creer_depot|lancer_en_parallele|WebFetch|NotebookEdit/.test(a.tool)) {
    const short = a.tool.replace(/^mcp__\w+__/, '');
    return { tool: a.tool, pattern: '', short, label: `l'outil ${short}`, example: '' };
  }
  return null;
}

async function openRules() {
  const rules = await api('regles');
  showList('Règles d\'autorisation', `<p class="hint">Créées avec « Toujours autoriser » : ces demandes ne te sont plus posées, dans tous les pods.
    Les refus définitifs et les commandes masquées (\$(…)) restent toujours bloqués.</p>${rules.map((r) => `<div class="dev"><div>${esc(r.label)}
    <small>${r.example ? `ex. ${esc(r.example.slice(0, 80))} · ` : ''}créée ${ago(r.created)}</small></div>
    <button class="btn ghost" data-del-rule="${r.id}">Supprimer</button></div>`).join('') || '<p class="hint">Aucune règle pour l\'instant.</p>'}`);
}

/** Gérer les projets : retirer un projet du cockpit (le pod, le dépôt et le service Onyxia ne sont pas touchés), ou le restaurer. */
async function openProjects() {
  const count = new Map();
  for (const e of state.tasks.values()) count.set(e.task.project, (count.get(e.task.project) || 0) + 1);
  const hosted = (id) => state.workers.some((w) => w.orchestrator && (w.projects || []).includes(id));
  const rows = state.projects.map((p) => `<div class="dev"><div>${esc(p.name || p.id)}
      <small>${p.online ? '● pod en ligne' : '○ pod hors ligne'} · ${plural(count.get(p.id) || 0, 'tâche')}${p.worker_name ? ` · ${esc(p.worker_name)}` : ''}</small></div>
      ${hosted(p.id) ? '<small title="Hébergé par l\'orchestrateur : à retirer de config/projects.yaml">🔒</small>'
        : `<button class="btn ghost" data-del-project="${esc(p.id)}">Supprimer</button>`}</div>`).join('');
  const gone = Object.entries(state.deleted || {}).map(([id, d]) => `<div class="dev"><div>${esc(d.name || id)} <small>retiré ${ago(d.at)}</small></div>
      <button class="btn ghost" data-restore-project="${esc(id)}">Restaurer</button></div>`).join('');
  showList('Projets', `<p class="hint">Supprimer retire le projet du cockpit : liste, mémoire, propositions et périmètres du full auto / mode hard.
    Le pod, le dépôt git et le service Onyxia ne sont pas touchés (le pod continue de tourner) ; un projet retiré peut être restauré.
    Impossible tant qu'une tâche tourne dessus.</p>
    <label class="check"><input type="checkbox" id="projHistory"> Supprimer aussi son historique (tâches, événements) — définitif</label>
    ${rows || '<p class="hint">Aucun projet.</p>'}${gone ? `<h3>Retirés</h3>${gone}` : ''}`);
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
         ${ruleFor(a) ? `<button class="btn ghost always" data-always="${esc(a.id)}" title="approuve et ne demande plus pour : ${esc(ruleFor(a).label)}">Toujours autoriser « ${esc(ruleFor(a).short)} »</button>` : ''}
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
  body += e.events.filter((x) => x.type === 'steer').map((x) => `<div class="steer">✋ ${esc(x.text)}</div>`).join('');
  body += e.events.filter((x) => x.type === 'steer_late').map((x) => `<div class="hint">✋ Message arrivé après la fin de la tâche :
    « ${esc(x.text)} » <button class="btn ghost" data-resend="${esc(t.id)}" data-text="${esc(x.text)}">Envoyer comme suite</button></div>`).join('');
  body += imagesHtml(e);
  const limited = [...e.events].reverse().find((x) => x.type === 'rate_limited');
  if (limited && active) body += `<div class="blocked">⏳ Limite d'usage de Claude atteinte${limited.resets_at ? ` · ${esc(resetLabel(limited.resets_at))}` : ''}</div>`;
  const auto = e.events.filter((x) => x.type === 'trusted');
  if (auto.length) {
    body += `<details class="trusted" data-k="trusted"><summary>🛡 ${auto.length} validation${auto.length > 1 ? 's' : ''} passée${auto.length > 1 ? 's' : ''} automatiquement</summary>
      ${auto.map((x) => `<div class="step">${esc(x.tool)} : ${esc(x.text)}</div>`).join('')}</details>`;
  }
  body += blocked.map((x) => `<div class="blocked">⛔ ${esc(x.tool)} : ${esc(x.text)}</div>`).join('');
  body += [...e.approvals.values()].map(approvalHtml).join('');
  if (lastText) body += `<div class="text">${md(lastText)}</div>`;
  if (t.status === 'paused') body += `<div class="hint">⏸ En pause : limite d'usage atteinte${t.resume_at ? ` · reprise ${esc(resetLabel(t.resume_at))}` : ''} (reprise automatique si le full auto ou le mode hard est armé, ou avec le bouton ▶ Reprendre).</div>`;
  if (t.status === 'pending' && t.waiting_project) body += `<div class="hint">⏳ En attente du pod « ${esc(t.project)} » : la tâche partira dès que son worker sera connecté (1 à 3 min après le lancement).</div>`;
  else if (t.status === 'pending' && !state.workerOnline) body += '<div class="hint">Le worker Onyxia est hors ligne : la tâche partira dès sa reconnexion.</div>';
  if (t.status === 'needs_project') {
    body += `<div class="text">Je n'ai pas pu déterminer le projet${t.route_reason ? ` (${esc(t.route_reason)})` : ''}. Lequel ?</div>
      <div class="actions">${state.projects.map((p) => `<button class="btn" data-choose="${esc(p.id)}">${esc(p.name)}</button>`).join('')}</div>`;
  }
  if (t.result && !ACTIVE.has(t.status)) body += `<div class="result">${md(t.result)}</div>`;
  if (openQuestion(e)) body += questionHtml(t);
  if (!active) body += changesHtml(e) + testsHtml(e) + ciPrHtml(e) + worktreeHtml(e) + nextStepsHtml(e);

  return `<div class="meta">
      <span class="pill st-${esc(t.status)}">${STATUS[t.status] || esc(t.status)}</span>
      ${proj ? `<span class="pill">${esc(proj.name)}</span>` : ''}
      ${t.route_reason && t.project ? `<span title="${esc(t.route_reason)}">auto</span>` : ''}
      ${t.worktree ? `<span class="pill wt" title="Claude en parallèle, dans son propre worktree git">🌿 ${esc(t.worktree.branch)}</span>` : ''}
      ${t.parent ? `<span class="pill" title="lancée par un autre Claude (lancer_en_parallele)">🔀 sous-tâche</span>` : ''}
      ${modelPill(t)}
      ${costPill(t)}
      ${t.group ? `<span class="pill var" title="variantes de la même demande, à comparer">🧬 Variante ${variantIndex(t)}</span>` : ''}
      ${ACTIVE.has(t.status) ? `<button class="btn ghost" data-cancel="${esc(t.id)}">Arrêter</button>`
        : t.status === 'paused' ? `<button class="btn" data-resume="${esc(t.id)}">▶ Reprendre</button>`
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
    }
    const body = groupBody(e.task.project);  // la tâche suit son projet (routée plus tard : elle change de menu)
    if (wrap.parentElement !== body) body.appendChild(wrap);
    wrap.querySelector('.user').textContent = shownInstruction(e.task.instruction);
    const box = wrap.querySelector('.task');
    const open = new Set([...box.querySelectorAll('details[data-k][open]')].map((d) => d.dataset.k));
    const draft = box.querySelector('form.question textarea');
    const typed = draft?.value;
    const focused = draft && document.activeElement === draft;
    box.innerHTML = taskHtml(e);
    restoreQuestions(box);
    const again = box.querySelector('form.question textarea');
    if (again && typed) again.value = typed;
    if (again && focused) again.focus();
    box.querySelectorAll('details[data-k]').forEach((d) => { if (open.has(d.dataset.k)) d.open = true; });
    sizes(box);
    wrap.classList.toggle('suite', !!e.task.reply_to);
    wrap.classList.toggle('current', !!state.replyTo && conversationOf(id) === conversationOf(state.replyTo));
  }
  updateGroups();
  if (nearBottom || state.replay) feed.scrollTop = feed.scrollHeight;
  updatePending();
  updateComposer();
}

// ---------- fil groupé par projet : un menu déroulant par projet, le plus récemment actif en bas
const GROUPS_KEY = 'cockpit_groups';  // projet -> ouvert (true) ou replié (false), choisi par l'utilisateur
const groupPrefs = () => { try { return JSON.parse(localStorage.getItem(GROUPS_KEY) || '{}'); } catch { return {}; } };
const saveGroupPref = (id, open) => { try { localStorage.setItem(GROUPS_KEY, JSON.stringify({ ...groupPrefs(), [id]: open })); } catch { /* sans importance */ } };
const groupId = (project) => project || '_auto';
const groupName = (project) => (project ? state.projects.find((p) => p.id === project)?.name || project : '💬 Sans projet (Auto)');
const groupEl = (id) => [...document.querySelectorAll('#feed details.pgroup')].find((d) => d.dataset.g === id);

/** Ouvre ou ferme un menu par programme, sans que cela passe pour un choix de l'utilisateur. */
function setGroupOpen(d, open) {
  if (d.open === open) return;
  d._prog = true;
  d.open = open;
}

function groupBody(project) {
  const id = groupId(project);
  let d = groupEl(id);
  if (!d) {
    d = document.createElement('details');
    d.className = 'pgroup';
    d.dataset.g = id;
    d.innerHTML = '<summary><span class="pname"></span><span class="pmeta"></span></summary><div class="pbody"></div>';
    d.addEventListener('toggle', () => {
      if (d._prog) { d._prog = false; return; }
      saveGroupPref(id, d.open);
    });
    $('#feed').appendChild(d);
  }
  return d.querySelector('.pbody');
}

function updateGroups() {
  const feed = $('#feed');
  const groups = [...feed.querySelectorAll('details.pgroup')];
  const info = new Map();
  for (const id of state.order) {
    const e = state.tasks.get(id);
    const g = groupId(e.task.project);
    const i = info.get(g) || { n: 0, active: 0, pending: 0, last: 0 };
    i.n++;
    if (ACTIVE.has(e.task.status)) i.active++;
    for (const a of e.approvals.values()) if (!a.decision) i.pending++;
    i.last = Math.max(i.last, e.task.updated || e.task.created || 0);
    info.set(g, i);
  }
  const prefs = groupPrefs();
  const newest = [...info.entries()].sort((a, b) => b[1].last - a[1].last)[0]?.[0];
  for (const d of groups) {
    const i = info.get(d.dataset.g);
    if (!i) continue;
    const parts = [plural(i.n, 'tâche')];
    if (i.active) parts.push(`⚙ ${i.active} en cours`);
    if (i.pending) parts.push(`⚠ ${i.pending} à valider`);
    d.querySelector('.pname').textContent = groupName(d.dataset.g === '_auto' ? null : d.dataset.g);
    d.querySelector('.pmeta').textContent = parts.join(' · ');
    d.classList.toggle('working', i.active > 0);
    d.classList.toggle('attn', i.pending > 0);
    if (!d.dataset.init) {  // premier affichage : le choix de l'utilisateur, sinon ouvert si actif ou le plus récent
      d.dataset.init = '1';
      setGroupOpen(d, d.dataset.g in prefs ? prefs[d.dataset.g] : i.active > 0 || i.pending > 0 || d.dataset.g === newest);
    } else if (i.pending > 0) {
      setGroupOpen(d, true);  // une validation attend : on ne la cache pas
    }
  }
  const sorted = groups.filter((d) => info.has(d.dataset.g)).sort((a, b) => info.get(a.dataset.g).last - info.get(b.dataset.g).last);
  if (sorted.some((d, k) => d !== groups[k])) sorted.forEach((d) => feed.appendChild(d));
  $('#feedBar').classList.toggle('hidden', groups.length < 2);
}

/** Ouvre le menu du projet d'une tâche (suivre une conversation, aller à une tâche) ; renvoie la section de la tâche. */
function openGroupOf(taskId) {
  const el = document.getElementById(`t-${taskId}`);
  const d = el?.closest('details.pgroup');
  if (d) setGroupOpen(d, true);
  return el;
}

function foldAll(open) {
  const prefs = {};
  document.querySelectorAll('#feed details.pgroup').forEach((d) => { setGroupOpen(d, open); prefs[d.dataset.g] = open; });
  try { localStorage.setItem(GROUPS_KEY, JSON.stringify({ ...groupPrefs(), ...prefs })); } catch { /* sans importance */ }
}
$('#foldAll').addEventListener('click', () => foldAll(false));
$('#unfoldAll').addEventListener('click', () => foldAll(true));

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
    <div class="files">${files}${more}</div>${commits ? `<div class="commits">${commits}</div>` : ''}${patches ? reviewBar(e.task) : ''}</div>`;
}

function diffHtml(patch) {
  if (!patch) return '<div class="hint">Diff indisponible (fichier trop gros ou limite atteinte).</div>';
  return `<pre class="diffpre">${numberedDiff(patch).map((x) =>
    `<span${x.cls ? ` class="${x.cls}"` : ''}${x.ln ? ` data-ln="${x.ln}"` : ''}>${esc(x.l) || ' '}</span>`).join('')}</pre>`;
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
// « Plus de détails » du tableau de bord : ouvert ou replié, mémorisé (le tableau de bord est redessiné souvent)
document.addEventListener('toggle', (ev) => {
  if (!ev.target.matches?.('details[data-more]') || state.dashMore === ev.target.open) return;
  state.dashMore = ev.target.open;
  try { localStorage.setItem('cockpit_more', state.dashMore ? '1' : ''); } catch { /* sans importance */ }
  state.dashHtml = '';
  if (state.dashMore) loadBilan();
}, true);

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

// ---------- questions de Claude : en évidence, réponse d'un geste ou en texte libre
/** Question ouverte : posée par la dernière tâche (terminée) de sa conversation, pas encore répondue. */
const openQuestion = (e) => !!(e.task.question?.text && e.task.status === 'done' && lastOfConversation(e.task.id) === e.task.id);

function questionHtml(t, compact = false) {
  const q = t.question;
  // Plusieurs questions : un choix par question (modifiable) + précisions, envoyés ensemble
  const items = (q.items || []).map((it, i) => `<div class="qitem" data-q="${esc(it.question)}">
      <div class="qnum">${i + 1}. ${md(it.question)}</div>
      ${it.options?.length ? `<div class="qopts">${it.options.map((o) => `<button type="button" class="btn" data-pick="${esc(o)}" aria-pressed="false">${esc(o)}</button>`).join('')}</div>` : ''}
    </div>`).join('');
  return `<form class="question${compact ? ' compact' : ''}" data-question="${esc(t.id)}">
    <div class="qhead">❓ Claude attend ta réponse</div>
    <div class="qtext">${md(q.text)}</div>${items}
    ${!items && q.options?.length ? `<div class="qopts">${q.options.map((o) => `<button type="button" class="btn" data-answer="${esc(t.id)}" data-text="${esc(o)}">${esc(o)}</button>`).join('')}</div>` : ''}
    <div class="qrow"><textarea rows="2" placeholder="${items ? 'Précisions ou réponses libres (facultatif)…' : 'Ta réponse…'}" aria-label="ta réponse"></textarea>
      <button type="submit" class="btn ok">Répondre</button></div></form>`;
}

/** Choix et texte en cours par question (clé : id de la tâche). Les cartes sont réécrites à chaque événement
 *  (fil, tableau de bord, mini-fenêtre) : sans cette mémoire, un choix fait disparaît au prochain rafraîchissement. */
const qdraft = new Map();
const qdraftOf = (id) => { if (!qdraft.has(id)) qdraft.set(id, { picks: {}, text: '' }); return qdraft.get(id); };

/** Remet dans les formulaires de `root` les choix et le texte mémorisés (après un innerHTML). */
function restoreQuestions(root) {
  root.querySelectorAll('form.question').forEach((f) => {
    const d = qdraft.get(f.dataset.question);
    if (!d) return;
    f.querySelectorAll('.qitem').forEach((it) => {
      const want = d.picks[it.dataset.q];
      it.querySelectorAll('[data-pick]').forEach((b) => b.setAttribute('aria-pressed', String(want !== undefined && b.dataset.pick === want)));
    });
    const ta = f.querySelector('textarea');
    if (ta && d.text && !ta.value) ta.value = d.text;
  });
}
document.addEventListener('input', (ev) => {
  const ta = ev.target.closest?.('form.question textarea');
  if (ta) qdraftOf(ta.closest('form.question').dataset.question).text = ta.value;
});

/** Réponse d'un questionnaire : « - question → choix » pour chaque choix fait, puis les précisions. */
function composeAnswer(form) {
  const lines = [...form.querySelectorAll('.qitem')].map((it) => {
    const pick = it.querySelector('[data-pick][aria-pressed="true"]');
    return pick ? `- ${it.dataset.q} → ${pick.dataset.pick}` : '';
  }).filter(Boolean);
  const free = form.querySelector('textarea').value.trim();
  if (!lines.length) return free;
  return `${lines.join('\n')}${free ? `\n\nPrécisions : ${free}` : ''}`;
}

async function answerQuestion(id, text) {
  if (!text?.trim()) return;
  const n = await api('tache', { instruction: text.trim(), reply_to: id });
  qdraft.delete(id);
  onEvent({ kind: 'task', task_id: n.id, task: n });
  setReplyTo(n.id);
  // Réponse donnée depuis le tableau de bord sur téléphone : on suit la conversation qui reprend
  if (state.view === 'dash' && !matchMedia('(min-width: 1100px)').matches) setView('chat');
  await poll();
}

async function onQuestionSubmit(ev) {
  const form = ev.target.closest?.('form.question');
  if (!form) return;
  ev.preventDefault();
  const btn = form.querySelector('[type=submit]');
  btn.disabled = true;
  const text = composeAnswer(form);
  if (!text) { btn.disabled = false; form.querySelector('textarea').focus(); return; }
  try { await answerQuestion(form.dataset.question, text); } catch (e) { alert(e.message); btn.disabled = false; }
}
document.addEventListener('submit', onQuestionSubmit);
document.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && (e.ctrlKey || e.metaKey) && e.target.closest?.('form.question')) e.target.closest('form.question').requestSubmit();
});

// ---------- consommation de tokens : visible, et conseils pour l'économiser
const fmtTokens = (n) => (n >= 1e6 ? `${(n / 1e6).toLocaleString('fr-FR', { maximumFractionDigits: 1 })} M`
  : n >= 1000 ? `${Math.round(n / 1000)} k` : String(n || 0));
const SESSION_LOURDE = 0.05;  // une tâche qui prend plus de 5 % de la session de 5 h est signalée

function costPill(t) {
  if (!t.tokens?.total) return '';
  const cached = t.tokens.total ? Math.round(100 * (t.tokens.cache_read || 0) / t.tokens.total) : 0;
  const d = t.session_delta;
  const heavy = d != null && d >= SESSION_LOURDE;
  return `<span class="pill cost${heavy ? ' heavy' : ''}" title="tokens : ${t.tokens.total.toLocaleString('fr-FR')} (dont ${cached} % relus depuis le cache, bien moins coûteux)${d != null ? ` · environ ${Math.round(d * 100)} % de ta session de 5 h` : ''}">`
    + `≈ ${fmtTokens(t.tokens.total)} tokens${d != null ? ` · ${d < 0.005 ? '<1' : `+${Math.round(d * 100)}`} % session` : ''}</span>`;
}

const conversationTasks = (id) => state.order.filter((x) => conversationOf(x) === conversationOf(id)).map((x) => state.tasks.get(x).task);
const STATUS_RX = /\b(où en est|ou en est|avancement|état d(u|e|es)|statut|ça avance|ca avance|tu en es où|progr[eè]s)\b/i;

/** Conseil affiché sous la saisie, le plus utile d'abord (ou rien). */
function costTip(text) {
  const cur = state.replyTo && state.tasks.get(state.replyTo)?.task;
  if (STATUS_RX.test(text)) return { text: '💡 L\'avancement des projets est visible gratuitement dans le tableau de bord (tâches, modifications, tests, PR).', action: 'Voir', act: 'view-dash' };
  if (!cur && !state.selected) {
    const proj = state.projects.find((p) => p.name.length > 3 && new RegExp(`\\b${p.name.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\b`, 'i').test(text));
    if (proj) return { text: `💡 Ta demande concerne « ${proj.name} » : choisis sa puce pour l'envoyer directement, sans passer par l'orchestrateur.`, action: `Choisir ${proj.name}`, act: `project:${proj.id}` };
  }
  if (state.model === 'approfondi' && text.trim().length > 0 && text.trim().length < 60 && !cur) {
    return { text: '💡 Opus pour une demande courte : « Modèle auto » choisirait un modèle plus léger si c\'est une simple question.', action: 'Modèle auto', act: 'model-auto' };
  }
  if (cur && !ACTIVE.has(cur.status)) {
    const conv = conversationTasks(cur.id);
    const relu = conv.reduce((n, t) => Math.max(n, (t.tokens?.cache_read || 0) + (t.tokens?.input || 0)), 0);
    const pause = Date.now() / 1000 - (cur.updated || cur.created) > 300;
    if (conv.length >= 8 || relu > 300000) {
      return { text: `💡 Conversation longue (${conv.length} échanges) : chaque message relit tout l'historique${pause ? ', et le cache a expiré après la pause' : ''}. Pour un nouveau sujet, une nouvelle conversation coûte bien moins.`, action: 'Nouvelle conversation', act: 'new-conv' };
    }
    if (pause && relu > 100000) {
      return { text: '💡 Pause de plus de 5 min : le cache de Claude a expiré, ce message relira l\'historique de la conversation à plein tarif.', action: 'Nouvelle conversation', act: 'new-conv' };
    }
  }
  return null;
}

function renderCostTip() {
  const tip = costTip(input.value);
  const el = $('#costTip');
  el.classList.toggle('hidden', !tip);
  if (!tip) return;
  const html = `<span>${esc(tip.text)}</span><button type="button" class="btn ghost" data-tip="${esc(tip.act)}">${esc(tip.action)}</button>`;
  if (el.dataset.html !== html) { el.innerHTML = html; el.dataset.html = html; }
}

// ---------- nouveau pod demandé dans la conversation d'un projet
const wantsNewPod = (text) => /\b(nouveau|nouvel|nouvelle|autre|propre)\s+(pod|projet|repo|dépôt|depot|service)\b|\bdans un (nouveau|autre) pod\b|\b(crée|créer|creer|lance|lancer|ouvre|ouvrir)\s+(un|le)\s+(pod|projet)\b/i.test(text);
const isOrchestratorProject = (pid) => state.workers.some((w) => w.orchestrator && (w.projects || []).includes(pid));

// ---------- recadrage, modèle, variantes, commandes rapides
const shownInstruction = (s) => String(s || '').replace(/\n\n\(Variante \d\/\d : [\s\S]*\)$/, '');
// Ordre de création (celui du fil) : les variantes d'une demande naissent dans la même seconde.
const variantsOf = (group) => state.order.map((id) => state.tasks.get(id)).filter((e) => e.task.group === group);
function variantIndex(t) {
  const list = variantsOf(t.group);
  return `${list.findIndex((e) => e.task.id === t.id) + 1}/${list.length}`;
}

/** Saisie : recadrer Claude s'il travaille sur la conversation suivie, sinon nouvelle instruction ou suite. */
function updateComposer() {
  const cur = state.replyTo && state.tasks.get(state.replyTo)?.task;
  const steering = !!cur && ACTIVE.has(cur.status);
  input.placeholder = steering ? '✋ Recadrer Claude pendant qu\'il travaille…' : 'Ton instruction…';
  $('#composer').classList.toggle('steering', steering);
  if (cur) {
    const txt = shownInstruction(cur.instruction);
    const proj = state.projects.find((p) => p.id === cur.project);
    $('#threadText').textContent = `${steering ? '✋ Recadrer' : '↪ Suite de'} « ${txt.length > 40 ? `${txt.slice(0, 40)}…` : txt} »`
      + (proj ? ` · ${proj.name}` : '');
  }
  $('#variantSel').classList.toggle('hidden', !!state.replyTo);
  renderCostTip();
  renderQuick();
}

const DEFAULT_COMMANDS = [
  { titre: '🧪 Tests', instruction: 'Lance les tests du projet et corrige ce qui échoue.' },
  { titre: '📋 État du projet', instruction: 'Résume l\'état du projet : ce qui marche, ce qui est en cours, les problèmes connus et les prochaines étapes.' },
  { titre: '🔍 Relis mon travail', instruction: 'Relis les modifications non publiées et le dernier commit : bugs, cas oubliés, simplifications possibles. Corrige ce qui est sûr, liste le reste.' },
  { titre: '🚀 Publie', instruction: 'Commite le travail en cours avec un message clair, pousse-le sur une branche de travail et ouvre une pull request.' },
];
const currentProject = () => state.selected || (state.replyTo && state.tasks.get(state.replyTo)?.task.project) || null;
const commandsFor = (key) => state.commands[key] || (key === '*' ? DEFAULT_COMMANDS : []);

function renderQuick() {
  const proj = currentProject();
  const list = [...commandsFor('*'), ...(proj ? commandsFor(proj) : [])];
  const html = list.map((c, i) => `<button type="button" class="chip qc" data-quick="${i}" title="${esc(c.instruction)}">${esc(c.titre)}</button>`).join('')
    + '<button type="button" class="chip qc edit" id="editQuick" title="modifier les commandes rapides">✎</button>';
  if ($('#quick').dataset.html !== html) { $('#quick').innerHTML = html; $('#quick').dataset.html = html; }
  state.quickList = list;
}

async function loadCommands() {
  try { state.commands = await api('commandes'); } catch { /* commandes par défaut */ }
  renderQuick();
}

function openCommands() {
  const proj = currentProject();
  const scopes = [['*', 'Toutes les conversations'], ...state.projects.map((p) => [p.id, `Projet ${p.name}`])];
  $('#cmdScope').innerHTML = scopes.map(([k, l]) => `<option value="${esc(k)}"${k === (proj || '*') ? ' selected' : ''}>${esc(l)}</option>`).join('');
  fillCommandRows();
  $('#cmdHint').textContent = '';
  $('#cmdDlg').showModal();
}

function fillCommandRows() {
  const rows = commandsFor($('#cmdScope').value);
  $('#cmdRows').innerHTML = rows.map((c) => commandRow(c)).join('') || '<p class="hint">Aucune commande : ajoute-en une.</p>';
}
const commandRow = (c = { titre: '', instruction: '' }) => `<div class="cmdrow">
  <input class="ct" maxlength="60" placeholder="Titre (ex. 🧪 Tests)" value="${esc(c.titre)}">
  <textarea class="ci" rows="2" placeholder="Instruction envoyée à Claude">${esc(c.instruction)}</textarea>
  <button type="button" class="btn ghost" data-del-cmd>Supprimer</button></div>`;

$('#cmdScope').addEventListener('change', fillCommandRows);
$('#cmdForm').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const commandes = [...document.querySelectorAll('#cmdRows .cmdrow')].map((r) => ({
    titre: r.querySelector('.ct').value.trim(), instruction: r.querySelector('.ci').value.trim() })).filter((c) => c.titre && c.instruction);
  try {
    await api('commandes_maj', { project: $('#cmdScope').value, commandes });
    $('#cmdDlg').close();
    await loadCommands();
  } catch (e) { $('#cmdHint').textContent = e.message; }
});

// ---------- mémoire du projet (CLAUDE.md, lu par Claude au début de chaque tâche)
async function openMemory(project) {
  const p = state.projects.find((x) => x.id === project);
  $('#memTitle').textContent = `🧠 Mémoire · ${p?.name || project}`;
  $('#memText').value = '';
  $('#memHint').textContent = 'Chargement…';
  $('#memDlg').dataset.project = project;
  $('#memDlg').showModal();
  try {
    const m = await api('memoire', undefined, `&project=${encodeURIComponent(project)}`);
    $('#memText').value = m.content || '';
    $('#memDlg').dataset.hash = m.hash || '';
    $('#memHint').textContent = m.absent ? 'Mémoire pas encore publiée par le pod (worker à mettre à jour ?).'
      : m.content ? `CLAUDE.md du projet · publié ${ago(m.maj)}` : 'Pas encore de CLAUDE.md : conventions, commandes utiles, choix d\'architecture…';
    $('#memSave').disabled = !!m.absent;
  } catch (e) { $('#memHint').textContent = e.message; }
}
$('#memForm').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  try {
    await api('memoire_maj', { project: $('#memDlg').dataset.project, content: $('#memText').value, hash: $('#memDlg').dataset.hash });
    $('#memDlg').close();
  } catch (e) { $('#memHint').textContent = e.message; }
});

// ---------- captures, tests, CI, pull request, revue
function imagesHtml(e) {
  const shots = e.events.filter((x) => x.type === 'image');
  if (!shots.length) return '';
  return `<div class="shots">${shots.map((x) => `<figure><button class="shot" data-img="${esc(x.image)}" data-caption="${esc(x.caption || '')}">
    <img src="api.php?r=image&amp;id=${esc(x.image)}" alt="${esc(x.caption || 'capture')}" loading="lazy"></button>
    ${x.caption ? `<figcaption>${esc(x.caption)}</figcaption>` : ''}</figure>`).join('')}</div>`;
}

function testsHtml(e) {
  const r = [...e.events].reverse().find((x) => x.type === 'tests');
  if (!r) return '';
  if (r.skipped) return `<div class="hint">🧪 ${esc(r.skipped)}</div>`;
  return `<details class="tests ${r.ok ? 'ok' : 'ko'}" data-k="tests"><summary>🧪 Tests ${r.ok ? '✅ réussis' : '❌ en échec'} · ${duree(r.duration_s || 1)}
    <code>${esc(r.command)}</code></summary><pre>${esc(r.output || '')}</pre></details>`;
}

function ciPrHtml(e) {
  const t = e.task;
  let html = '';
  const ci = [...e.events].reverse().find((x) => x.type === 'ci');
  if (ci && ci.state !== 'none') {
    const lab = { pending: '⏳ CI en cours', success: '✅ CI réussie', failure: '❌ CI en échec' }[ci.state] || ci.state;
    const failed = (ci.checks || []).filter((c) => c.conclusion && !['success', 'neutral', 'skipped'].includes(c.conclusion)).map((c) => c.name);
    html += `<div class="ci ${esc(ci.state)}">${lab} · <code>${esc(ci.sha)}</code>${failed.length ? ` · ${esc(failed.join(', '))}` : ''}
      ${ci.url ? `· <a href="${esc(ci.url)}" target="_blank" rel="noopener">détails</a>` : ''}</div>`;
  }
  html += prsOf(e).map((pr) => prBox(pr, t.id)).join('');
  const { c } = changesOf(e);
  const conv = state.order.filter((id) => conversationOf(id) === conversationOf(t.id));
  const hasPr = conv.some((id) => prsOf(state.tasks.get(id)).length);
  if (t.status === 'done' && c?.branch && !['main', 'master', 'HEAD'].includes(c.branch) && !hasPr && lastOfConversation(t.id) === t.id) {
    html += `<div class="actions"><button class="btn" data-open-pr="${esc(t.id)}">🔀 Ouvrir une pull request</button></div>`;
  }
  return html;
}

// ---------- pull requests : lien direct et fusion depuis le cockpit
const PR_RX = /https:\/\/github\.com\/[\w.-]+\/[\w.-]+\/pull\/\d+/g;

/** PR d'une tâche : ouvertes avec ouvrir_pr (titre, branches), ou citées dans la réponse de Claude. */
function prsOf(e) {
  const out = new Map();
  for (const x of e.events) if (x.type === 'pr' && x.url) out.set(x.url, x);
  for (const url of String(e.task.result || '').match(PR_RX) || []) {
    if (!out.has(url)) out.set(url, { url, number: url.split('/').pop(), title: '' });
  }
  return [...out.values()];
}

/** PR réglées (fusionnées, ou fermées sans fusion) : adresse -> « merged » | « closed ». */
const donePrs = () => new Map([...state.tasks.values()].flatMap((e) => e.events
  .filter((x) => x.type === 'pr_merged' || x.type === 'pr_closed').map((x) => [x.url, x.type === 'pr_merged' ? 'merged' : 'closed'])));

function prBox(pr, taskId, compact = false) {
  const done = donePrs().get(pr.url);
  const merged = !!done;
  const repo = pr.url.replace(/^https:\/\/github\.com\/|\/pull\/\d+$/g, '');
  return `<div class="prbox${done === 'merged' ? ' merged' : ''}${done === 'closed' ? ' closed' : ''}${compact ? ' compact' : ''}">
    <div class="prtitle">🔀 Pull request #${esc(pr.number)}${pr.title ? ` · ${esc(pr.title)}` : ''}</div>
    <div class="muted prmeta">${esc(repo)}${pr.head ? ` · ${esc(pr.head)} → ${esc(pr.base)}` : ''}${done === 'merged' ? ' · <b class="okt">✅ fusionnée</b>' : done === 'closed' ? ' · <b>fermée</b>' : ''}</div>
    <div class="actions"><a class="btn" href="${esc(pr.url)}" target="_blank" rel="noopener">Voir sur GitHub</a>
      ${merged ? '' : `<button class="btn ok" data-merge-url="${esc(pr.url)}" data-task-id="${esc(taskId)}">Fusionner</button>`}</div></div>`;
}

function prsSection(tasks) {
  const merged = donePrs();
  const seen = new Map();
  for (const e of tasks) for (const pr of prsOf(e)) if (!merged.has(pr.url) && !seen.has(pr.url)) seen.set(pr.url, { pr, e });
  if (!seen.size) return '';
  return `<section class="sect"><h3>🔀 Pull requests à valider · ${seen.size}</h3><div class="follows">${[...seen.values()].map(({ pr, e }) =>
    `<article class="follow"><div class="fhead"><b>${esc(state.projects.find((p) => p.id === e.task.project)?.name || e.task.project || '')}</b>
      <button class="btn ghost" data-jump="${esc(e.task.id)}">Voir la tâche</button></div>${prBox(pr, e.task.id, true)}</article>`).join('')}</div></section>`;
}

/** Numéros de ligne d'un diff (nouvelle version ; « −n » pour une ligne supprimée), pour les commentaires de revue. */
function numberedDiff(patch) {
  let oldLn = 0, newLn = 0;
  return patch.split('\n').map((l) => {
    const h = l.match(/^@@ -(\d+)(?:,\d+)? \+(\d+)/);
    if (h) { oldLn = Number(h[1]); newLn = Number(h[2]); return { l, cls: 'h' }; }
    if (l.startsWith('+')) return { l, cls: 'a', ln: String(newLn++) };
    if (l.startsWith('-')) return { l, cls: 'd', ln: `−${oldLn++}` };
    if (l.startsWith('\\')) return { l, cls: '' };
    oldLn++;
    return { l, cls: '', ln: String(newLn++) };
  });
}

function reviewBar(t) {
  const list = state.review.get(t.id) || [];
  if (!list.length) return '<div class="hint rvhint">💬 Touche une ligne d\'un diff pour la commenter : tes remarques partent ensemble à Claude.</div>';
  return `<div class="review"><div class="rvhead">💬 Revue · ${list.length} commentaire${list.length > 1 ? 's' : ''}</div>
    ${list.map((r, i) => `<div class="rvitem"><code>${esc(r.path)}:${esc(r.ln)}</code> ${esc(r.text)}
      <button class="btn ghost" data-rv-del="${esc(t.id)}" data-i="${i}" aria-label="retirer">✕</button></div>`).join('')}
    <div class="actions"><button class="btn ok" data-rv-send="${esc(t.id)}">Envoyer la revue à Claude</button></div></div>`;
}

function reviewInstruction(list) {
  return 'Revue de ton travail, commentaires ligne par ligne :\n\n'
    + list.map((r) => `- \`${r.path}\` ligne ${r.ln}${r.code ? ` (\`${r.code.slice(1).trim().slice(0, 80)}\`)` : ''} : ${r.text}`).join('\n')
    + '\n\nApplique ces remarques, puis résume ce que tu as changé.';
}

document.addEventListener('click', (ev) => {
  const line = ev.target.closest('.diffpre span[data-ln]');
  if (!line) return;
  const det = line.closest('details.file');
  det.querySelector('.rvform')?.remove();
  const form = document.createElement('form');
  form.className = 'rvform';
  form.innerHTML = `<label>💬 ${esc(det.dataset.path)} · ligne ${esc(line.dataset.ln)}</label>
    <textarea rows="2" placeholder="Ta remarque (ex. gère le cas vide)" required></textarea>
    <div class="actions"><button type="submit" class="btn ok">Ajouter à la revue</button><button type="button" class="btn ghost" data-rv-cancel>Annuler</button></div>`;
  line.closest('.diff').appendChild(form);
  form.querySelector('textarea').focus();
  form.addEventListener('submit', (e) => {
    e.preventDefault();
    const id = det.dataset.task;
    const list = state.review.get(id) || [];
    list.push({ path: det.dataset.path, ln: line.dataset.ln, code: line.textContent, text: form.querySelector('textarea').value.trim() });
    state.review.set(id, list);
    render([id]);
  });
});

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
    <div class="bar ${level}" role="meter" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${Math.round(r * 100)}" aria-label="${label}"><i data-w="${(r * 100).toFixed(1)}"></i></div>
    <span class="mv">${flag}${text}</span></div>`;
}

function currentStep(e) {
  if (e.task.status === 'waiting_approval') return '⚠️ Attend ta validation';
  if (e.task.status === 'queued') return '⏳ En file d\'attente';
  if (e.task.status === 'routing') return '🧭 Choisit le projet';
  if (e.task.status === 'pending') return e.task.waiting_project ? `⏳ Attend le pod « ${e.task.project} »`
    : state.workerOnline ? '⏳ Transmise au pod' : '⏳ Pod hors ligne';
  const steps = stepsOf(e);
  return steps.length ? steps[steps.length - 1].label : 'Réfléchit…';
}

function liveCard(e) {
  const t = e.task;
  const proj = state.projects.find((p) => p.id === t.project);
  const todos = [...e.events].reverse().find((x) => x.todos)?.todos;
  const done = todos ? todos.filter((x) => x.status === 'completed').length : 0;
  const plan = todos?.length ? `<div class="plan"><div class="bar"><i data-w="${(done / todos.length * 100).toFixed(0)}"></i></div><span>Plan ${done}/${todos.length}</span></div>` : '';
  return `<button class="live${t.status === 'waiting_approval' ? ' alert' : ''}" data-jump="${esc(t.id)}">
    <div class="lhead"><span class="pill st-${esc(t.status)}">${STATUS[t.status] || esc(t.status)}</span>
      <b>${esc(proj?.name || t.project || (t.kind === 'service' ? 'Onyxia' : 'Auto'))}</b>${t.model_used ? `<span class="muted">${esc(modelName(t.model_used))}</span>` : ''}${t.worktree ? `<span class="wt">🌿 parallèle</span>` : ''}
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
    return `<div class="proj"><div class="pname">${esc(p?.name || pid)}
        <button class="btn ghost mem" data-memory="${esc(pid)}" title="mémoire du projet (CLAUDE.md)">🧠 Mémoire</button></div>
      <div class="pgit">${git}</div>${last}</div>`;
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
    ${st.vscode?.url && w.online ? `<div class="podlinks"><a class="btn" href="${esc(st.vscode.url)}" target="_blank" rel="noopener">🖥 Ouvrir VSCode</a>
      ${st.vscode.password ? `<button class="btn ghost" data-copy-pw="${esc(w.id)}" title="copie le mot de passe de VSCode">🔑 Mot de passe</button>` : ''}</div>` : ''}
    ${slots}${st.preview?.url && w.online ? `<a class="preview" href="${esc(st.preview.url)}" target="_blank" rel="noopener">👁 Aperçu de l'application · port ${esc(st.preview.port)}</a>` : ''}
    ${meters ? `<div class="meters">${meters}</div>` : ''}
    ${projects ? `<div class="projs">${projects}</div>` : ''}
    ${wts ? `<div class="wts"><div class="sub">Branches parallèles</div>${wts}</div>` : ''}
    <div class="podfoot">Aujourd'hui : ${plural(w.tasks_today, 'tâche')}</div></article>`;
}

/** Variantes d'une même demande, côte à côte : modifications, tests, capture ; « Garder » fusionne et jette les autres. */
function variantsHtml(tasks) {
  const groups = [...new Set(tasks.filter((e) => e.task.group).map((e) => e.task.group))]
    .map(variantsOf).filter((g) => g.length > 1 && g.some((e) => e.task.worktree?.state !== 'discarded' || ACTIVE.has(e.task.status)))
    .filter((g) => !g.some((e) => e.task.worktree?.state === 'merged'));
  if (!groups.length) return '';
  return `<section class="sect"><h3>Variantes à comparer</h3>${groups.map((g) => {
    const allDone = g.every((e) => !ACTIVE.has(e.task.status));
    return `<article class="vgroup"><div class="vinst">${esc(shownInstruction(g[0].task.instruction))}</div><div class="vcols">${g.map((e, i) => {
      const t = e.task;
      const { c } = changesOf(e);
      const shot = [...e.events].reverse().find((x) => x.type === 'image');
      const tests = [...e.events].reverse().find((x) => x.type === 'tests' && !x.skipped);
      return `<div class="vcol"><div class="lhead"><b>Variante ${i + 1}</b><span class="pill st-${esc(t.status)}">${STATUS[t.status] || esc(t.status)}</span>
          ${t.model ? `<span class="muted">${esc(MODELES[t.model] || '')}</span>` : ''}</div>
        ${shot ? `<button class="shot" data-img="${esc(shot.image)}" data-caption="${esc(shot.caption || '')}"><img src="api.php?r=image&amp;id=${esc(shot.image)}" alt="capture" loading="lazy"></button>` : ''}
        <div class="rmeta">${c && (c.file_count || c.commits?.length) ? changesLine(c) : ACTIVE.has(t.status) ? esc(currentStep(e)) : 'aucune modification'}
          ${tests ? ` · 🧪 ${tests.ok ? '✅' : '❌'}` : ''}</div>
        <div class="actions"><button class="btn ghost" data-jump="${esc(t.id)}">Voir</button>
          ${allDone && t.worktree?.state === 'active' ? `<button class="btn ok" data-keep="${esc(t.id)}">✓ Garder</button>` : ''}</div></div>`;
    }).join('')}</div></article>`;
  }).join('')}</section>`;
}

function questionsSection(tasks) {
  const open = tasks.filter(openQuestion);
  if (!open.length) return '';
  return `<section class="sect"><h3>❓ En attente de ta réponse · ${open.length}</h3><div class="follows">${open.map((e) => {
    const proj = state.projects.find((p) => p.id === e.task.project);
    return `<article class="follow ask"><div class="fhead"><b>${esc(proj?.name || e.task.project || '')}</b>
      <button class="btn ghost" data-jump="${esc(e.task.id)}">Voir</button></div>
      <button class="forigin" data-jump="${esc(e.task.id)}">↳ ${esc(shownInstruction(e.task.instruction))}</button>${questionHtml(e.task, true)}</article>`;
  }).join('')}</div></section>`;
}

function recentRow(e) {
  const t = e.task;
  const { c } = changesOf(e);
  const proj = state.projects.find((p) => p.id === t.project);
  const icon = { done: '✅', error: '❌', cancelled: '⏹', needs_project: '❓', paused: '⏸' }[t.status] || '•';
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
    return `<span class="col" title="${esc(label)}"><i class="${n ? 'b' : 'z'}" data-h="${n ? Math.max(10, n / max * 100).toFixed(0) : 0}"></i></span>`;
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
  const icon = { done: '✅', error: '❌', cancelled: '⏹', needs_project: '❓', paused: '⏸' };
  const recent = p.recent.map((t) => `<button class="brecent" data-jump="${esc(t.id)}"><span>${icon[t.status] || '◐'}</span>
    <span class="rinst">${esc(t.instruction)}</span><span class="rtime" data-ago="${t.updated}"></span></button>`).join('');
  return `<article class="bcard">
    <div class="podhead"><b>${esc(proj?.name || p.project)}</b>${w ? `<span class="tag"><span class="dot${w.online ? ' on' : ''}"></span>${esc(w.name)}</span>` : ''}
      <button class="btn ghost mem" data-memory="${esc(p.project)}" title="mémoire du projet (CLAUDE.md)">🧠</button>
      <span class="muted podseen">actif <span data-ago="${p.last}"></span></span></div>
    ${git ? `<div class="pgit">${git}</div>` : ''}
    <div class="bstats">
      ${stat(p.tasks, plural(p.tasks, 'tâche').replace(/^\d+ /, ''), statuts)}
      ${stat(p.tokens ? fmtTokens(p.tokens) : '—', 'tokens', p.duration_s ? dureeLongue(p.duration_s) : '')}
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
      apercu: $('#podPreview').checked,
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

// ---------- usage du forfait Claude
const USAGE_LABELS = { five_hour: 'Session (5 h)', seven_day: 'Semaine', seven_day_opus: 'Semaine · Opus', seven_day_sonnet: 'Semaine · Sonnet' };
const latestUsage = () => state.workers.map((w) => w.state?.usage).filter((u) => u?.limits?.length).sort((a, b) => b.ts - a.ts)[0] || null;

function resetLabel(ts) {
  if (!ts) return '';
  const d = new Date(ts * 1000);
  const hm = d.toLocaleTimeString('fr-FR', { hour: '2-digit', minute: '2-digit' });
  return ts - Date.now() / 1000 < 86400 && d.getDate() === new Date().getDate() ? `réinitialisée à ${hm}`
    : `réinitialisée ${d.toLocaleDateString('fr-FR', { weekday: 'short', day: 'numeric' })} à ${hm}`;
}

function usageHtml(u) {
  if (!u) return '';
  const rows = u.limits.map((l) => meter(USAGE_LABELS[l.type] || l.type, l.utilization,
    l.utilization == null ? (l.status === 'rejected' ? '⛔ limite atteinte' : '—') : `${Math.round(l.utilization * 100)} %`,
    resetLabel(l.resets_at)) + (l.resets_at ? `<div class="ureset">${esc(resetLabel(l.resets_at))}</div>` : '')).join('');
  return `<section class="sect"><h3>Usage de Claude</h3><div class="usagecard"><div class="meters">${rows}</div>
    <div class="hint">Mis à jour ${ago(u.ts)}${u.source === 'agent' ? ' (signalé pendant les tâches)' : ''}</div></div></section>`;
}

/** Barre fine sous l'en-tête : usage de la session de 5 h (ou de la limite la plus avancée). */
function renderUsageBar() {
  const u = latestUsage();
  const l = u && (u.limits.find((x) => x.type === 'five_hour' && x.utilization != null) || u.limits.find((x) => x.utilization != null));
  const bar = $('#usageBar');
  bar.classList.toggle('hidden', !l);
  if (!l) return;
  const r = l.utilization;
  bar.classList.toggle('warn', r >= 0.8 && r < 0.95);
  bar.classList.toggle('bad', r >= 0.95);
  bar.querySelector('i').style.width = `${(r * 100).toFixed(1)}%`;
  bar.querySelector('span').textContent = `${USAGE_LABELS[l.type] || ''} ${Math.round(r * 100)} %${r >= 0.8 ? ` · ${resetLabel(l.resets_at)}` : ''}`;
  bar.title = `Usage de Claude — ${USAGE_LABELS[l.type]} : ${Math.round(r * 100)} % (${resetLabel(l.resets_at)})`;
}

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
  const debutJour = new Date().setHours(0, 0, 0, 0) / 1000;
  const duJour = tasks.filter((e) => e.task.created >= debutJour && e.task.tokens?.total);
  const tokensJour = duJour.reduce((n, e) => n + e.task.tokens.total, 0);
  const gourmandes = duJour.sort((a, b) => b.task.tokens.total - a.task.tokens.total).slice(0, 3);
  const recent = tasks.filter((e) => !ACTIVE.has(e.task.status) && e.task.instruction)
    .sort((a, b) => (b.task.updated || b.task.created) - (a.task.updated || a.task.created)).slice(0, 8);

  const kpi = (v, l, cls = '', title = '') => `<div class="kpi ${cls}" title="${esc(title)}"><div class="v">${v}</div><div class="l">${l}</div></div>`;
  // L'essentiel d'abord (ce qui attend une action, l'autonomie, le travail en cours, les pods) ; le détail est replié.
  const weekIncite = state.week && state.week.hours_left <= 48 && state.week.remaining >= 0.2 && !state.hard?.enabled;
  const html = `<div class="dashwrap">
    ${questionsSection(tasks)}
    ${prsSection(tasks)}
    ${variantsHtml(tasks)}
    ${state.memory ? autonomyHtml(projectName) : ''}
    ${state.hard?.enabled ? reportHtml() : ''}
    ${weekIncite ? weekHtml() : ''}
    ${usageHtml(latestUsage())}
    <section class="sect"><h3>En cours</h3>${active.length
      ? `<div class="lives">${active.map(liveCard).join('')}</div>`
      : '<div class="emptyd">Aucun Claude ne travaille. Écris une instruction, ou arme le full auto.</div>'}</section>
    ${proposalsHtml()}
    <section class="sect"><div class="secthead"><h3>Pods · ${online.length} en ligne</h3><button type="button" class="btn ghost" id="newPod">＋ Nouveau pod</button></div>${state.workers.length
      ? `<div class="pods">${state.workers.map(podCard).join('')}</div>`
      : '<div class="emptyd">Aucun pod connecté. Lance « ./run.sh » dans un service VSCode Onyxia.</div>'}</section>
    ${recent.length ? `<section class="sect"><h3>Terminées récemment</h3><div class="recents">${recent.slice(0, 5).map(recentRow).join('')}</div></section>` : ''}
    <details class="more" data-more ${state.dashMore ? 'open' : ''}><summary>Plus de détails</summary><div class="dashwrap">
    <div class="kpis">
      ${kpi(`${working}<small>/${capacity || '—'}</small>`, 'Claude au travail', '', 'agents actifs / capacité des pods en ligne')}
      ${kpi(pending ? `⚠ ${pending}` : '0', 'Validation(s) en attente', pending ? 'warn' : '')}
      ${kpi(today, 'Tâches aujourd\'hui')}
      ${kpi(fmtTokens(tokensJour), 'Tokens aujourd\'hui', '', 'tokens consommés par les tâches du jour (cache compris)')}
    </div>
    ${gourmandes.length ? `<section class="sect"><h3>Tâches les plus gourmandes aujourd'hui</h3><div class="recents">${gourmandes.map((e) =>
      `<button class="recent" data-jump="${esc(e.task.id)}"><span class="ri">🔥</span><span class="rbody"><span class="rinst">${esc(shownInstruction(e.task.instruction))}</span>
        <span class="rmeta">${esc(state.projects.find((p) => p.id === e.task.project)?.name || '')} · ≈ ${fmtTokens(e.task.tokens.total)} tokens${e.task.session_delta != null ? ` · +${Math.round(e.task.session_delta * 100)} % session` : ''}${e.task.model_used ? ` · ${esc(modelName(e.task.model_used))}` : ''}</span></span></button>`).join('')}</div>
      <p class="hint">Pour économiser : une conversation par sujet, la puce du projet plutôt qu'Auto, le tableau de bord pour l'avancement, « Modèle auto ».</p></section>` : ''}
    ${weekIncite ? '' : weekHtml()}
    ${state.hard?.enabled ? '' : reportHtml()}
    ${followHtml(tasks.filter(pendingSteps).sort((a, b) => (b.task.updated || b.task.created) - (a.task.updated || a.task.created)).slice(0, 6))}
    ${state.memory && !state.memory.proposals.length ? `<section class="sect"><div class="secthead"><h3>Propositions</h3><button type="button" class="btn ghost" data-prop="ajouter">＋ Ajouter</button></div>
      <div class="emptyd">Rien en attente : les prochaines étapes des tâches terminées arrivent ici, et le full auto en génère quand la file est vide.</div></section>` : ''}
    ${projectsHtml()}
    ${bilanHtml()}
    </div></details>
  </div>`;
  // Reconstruit seulement si le contenu change (survol, focus et clics préservés) ; les durées vivent dans tick().
  if (html !== state.dashHtml) { $('#dash').innerHTML = html; state.dashHtml = html; restoreQuestions($('#dash')); sizes($('#dash')); tick(); }
  loadBilan();
  loadReport();
}

// ---------- état de mémoire (projets suivis, propositions) et mode full auto
const fmtWhen = (ts) => new Date(ts * 1000).toLocaleString('fr-FR', { weekday: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' });
const fmtLeft = (h) => (h >= 48 ? `${Math.round(h / 24)} j` : h >= 1 ? `${Math.round(h)} h` : `${Math.max(1, Math.round(h * 60))} min`);

/** Reste de la limite hebdomadaire : ces tokens sont perdus à la réinitialisation, d'où l'incitation à lancer le mode hard. */
function weekHtml() {
  const w = state.week;
  if (!w) return '';
  const incite = w.hours_left <= 48 && w.remaining >= 0.2 && !state.hard?.enabled;
  const est = w.remaining_tokens != null
    ? `≈ <b>${fmtTokens(w.remaining_tokens)}</b> de tokens à dépenser${w.tasks_estimate ? ` · ≈ ${w.tasks_estimate} tâche(s) de taille moyenne` : ''}`
    : 'Estimation en tokens disponible après quelques tâches.';
  return `<section class="sect"><div class="secthead"><h3>⏳ Reste de la semaine</h3></div>
    <div class="weekcard ${incite ? 'incite' : ''}">
      ${meter('Semaine', w.utilization, `${Math.round(w.utilization * 100)} %`, `Limite hebdomadaire : ${Math.round(w.utilization * 100)} % utilisée`)}
      <div class="wkline"><span>Réinitialisation dans <b>${fmtLeft(w.hours_left)}</b></span><span>${est}</span></div>
      ${incite ? `<div class="wkincite">Il reste ${Math.round(w.remaining * 100)} % de ta semaine : sans activité, ces tokens seront perdus.</div>` : ''}
      ${state.hard?.enabled ? '' : `<button class="btn ${incite ? '' : 'ghost'}" data-hard="on" data-hours="${Math.max(1, Math.min(336, Math.ceil(w.hours_left)))}">☠ Utiliser le reste avec le mode hard</button>`}
      <div class="hint">Estimation prudente : le budget est déduit des tokens consommés par les tâches du cockpit cette semaine.</div>
    </div></section>`;
}

/** Une ligne de la carte Autonomie : état, périmètre, compteurs et bouton. */
function autoRow({ icon, title, on, chips, text, button }) {
  return `<div class="arow ${on ? 'on' : ''}"><div class="ahead"><span class="aicon">${icon}</span><b>${title}</b>
      <span class="badge ${on ? 'on' : ''}">${on ? 'Actif' : 'Inactif'}</span></div>
    ${chips ? `<div class="chips">${chips}</div>` : ''}<div class="hint">${text}</div><div class="abtn">${button}</div></div>`;
}

/** Ce que fait ou attend l'autonomie en ce moment : tâches automatiques en cours, raison d'attente, pods relancés. */
function liveAutonomy(name) {
  if (!state.autopilot?.enabled && !state.hard?.enabled) return '';
  const live = state.workers.find((w) => w.orchestrator && w.online)?.state?.autopilot;
  const running = [...state.tasks.values()].filter((e) => e.task.auto && ACTIVE.has(e.task.status));
  const lines = running.map((e) => `<button class="live" data-jump="${esc(e.task.id)}">⚙ <b>${esc(name(e.task.project))}</b> — ${esc(shownInstruction(e.task.instruction).slice(0, 90))}
    <span class="muted">${esc(currentStep(e))}</span></button>`);
  if (live?.waiting) lines.push(`<div class="live wait">⏳ ${esc(live.waiting.replace(/^en attente : /, 'En attente : '))}</div>`);
  for (const [id, w] of Object.entries(live?.woken || {})) {
    lines.push(`<div class="live ${w.ok ? '' : 'bad'}">${w.ok ? '⏻' : '⚠'} <b>${esc(name(id))}</b> — ${w.ok ? 'worker du pod relancé' : `pod non relancé : ${esc(w.text)}`}
      <span class="muted">${ago(w.ts)}</span></div>`);
  }
  if (!lines.length) lines.push('<div class="live wait">⏳ Rien en cours : le prochain lancement part dans la minute si une proposition ou une reprise attend.</div>');
  return `<div class="autolive"><div class="hint">En direct</div>${lines.join('')}</div>`;
}

function autonomyHtml(name) {
  const a = state.autopilot, h = state.hard;
  const chip = (t) => `<span class="chip">${t}</span>`;
  const scope = (ids) => (ids?.length ? ids.map((id) => chip(esc(name(id)))).join('') : chip('tous les projets'));
  const auto = autoRow({
    icon: '🤖', title: 'Full auto', on: !!a?.enabled,
    chips: a?.enabled ? scope(a.projects) + chip(`${a.max_parallel || 1} de front`) + chip(`${a.launched_today}/${a.max_per_day} aujourd'hui`)
      + chip(`seuil ${Math.round(a.threshold * 100)} %`) + chip(`jusqu'au ${esc(fmtWhen(a.until))}`) : '',
    text: a?.enabled ? 'Enchaîne le travail resté en plan puis les propositions, repart après chaque réinitialisation. Seules les opérations destructrices restent demandées.'
      : `${a?.stopped ? `Arrêté : ${esc(a.stopped)}. ` : ''}Enchaîne seul le travail resté en plan puis les propositions. Tu choisis les projets et vois ce qu'il compte faire avant d'armer.`,
    button: a?.enabled ? '<button class="btn ghost" data-auto="off">Désarmer</button>' : '<button class="btn" data-auto="on">🤖 Armer le full auto…</button>',
  });
  const hard = autoRow({
    icon: '☠', title: 'Mode hard', on: !!h?.enabled,
    chips: h?.enabled ? scope(h.projects) + chip(`jusqu'au ${esc(fmtWhen(h.until))}`) + (a?.enabled ? chip('full auto armé') : chip('sans full auto')) : '',
    text: h?.enabled ? 'Plus aucune validation, questions tranchées par Claude. Un compte rendu est dressé ci-dessous.'
      : 'Pleine autonomie : validations approuvées, questions tranchées par Claude, full auto armé par défaut, compte rendu à la fin.',
    button: h?.enabled ? '<button class="btn ghost" data-hard="off">Désactiver</button>' : '<button class="btn" data-hard="on">☠ Activer le mode hard…</button>',
  });
  return `<section class="sect"><div class="secthead"><h3>⚡ Autonomie</h3></div><div class="autocard">${auto}${hard}</div>${liveAutonomy(name)}</section>`;
}

const projectName = (id) => state.projects.find((p) => p.id === id)?.name || state.memory?.projects.find((p) => p.id === id)?.name || id || 'Auto';

/** Propositions en attente (lancées à la main ou par le full auto) : affichées seulement s'il y en a. */
function proposalsHtml() {
  const m = state.memory;
  if (!m?.proposals.length) return '';
  const proposals = m.proposals.map((x) => `<article class="follow"><div class="fhead"><b>${esc(x.titre)}</b><span class="muted">${esc(projectName(x.project))}</span></div>
      <div class="hint">${esc(x.instruction.replace(/\n\n\(Tâche lancée par le mode full auto[^)]*\)/, '').slice(0, 220))}</div>
      <div class="fhead"><button class="btn" data-prop="lancer" data-id="${esc(x.id)}">▶ Lancer</button>
        <button class="btn ghost" data-prop="ignorer" data-id="${esc(x.id)}">Ignorer</button></div></article>`).join('');
  return `<section class="sect"><div class="secthead"><h3>Propositions · ${m.proposals.length}</h3><button type="button" class="btn ghost" data-prop="ajouter">＋ Ajouter</button></div>
    <div class="follows">${proposals}</div></section>`;
}

/** Mémoire de chaque projet (CLAUDE.md) et son état. */
function projectsHtml() {
  const m = state.memory;
  if (!m) return '';
  const memoryLine = (p) => {
    const mm = p.memory;
    if (!mm) return '';
    if (mm.state === 'ok') {
      return `<div class="memline ok">🧠 Mémoire · ${plural(mm.lines, 'ligne')}${mm.updated ? ` · ${ago(mm.updated)}` : ''}
        <button class="btn ghost" data-memory="${esc(p.id)}">Voir / modifier</button></div>`;
    }
    if (mm.state === 'absente') {
      return `<div class="memline bad">⚠ Pas de mémoire : c'est la première chose que Claude fera sur ce projet.
        <button class="btn" data-memo-init="${esc(p.id)}">🧠 Créer maintenant</button></div>`;
    }
    return '<div class="memline">🧠 Mémoire pas encore lue (pod hors ligne ?)</div>';
  };
  const projects = m.projects.map((p) => `<article class="follow ${p.memory?.state === 'absente' ? 'ask' : p.active ? '' : 'ask'}"><div class="fhead"><b>${esc(p.name || p.id)}</b>
      <span class="muted">${p.updated ? `<span data-ago="${Number(p.updated)}"></span>` : ''}</span><span>${p.active ? `⚙ ${p.active} en cours` : p.status === 'en_pause' ? '⏸ en pause' : p.status === 'nouveau' ? '✨ nouveau' : ''}</span></div>
      ${memoryLine(p)}${p.summary ? `<div class="hint">${esc(p.summary)}</div>` : ''}</article>`).join('');
  return `<section class="sect"><div class="secthead"><h3>Mémoire · ${m.projects.length} projet(s)</h3></div>
    <div class="follows">${projects || '<div class="emptyd">Aucun projet connu.</div>'}</div></section>`;
}

async function autoToggle(on) {
  if (on) return openHardDialog('auto');
  await api('autopilote', { enabled: false });
  poll();
}

async function hardToggle(on, hours = 0) {
  if (on) return openHardDialog('hard', hours);
  await api('mode_hard', { enabled: false });
  state.reportAt = 0;
  poll();
}

const hardChosen = () => [...document.querySelectorAll('#hardProjects input:checked')].map((i) => i.value);

/** Dialogue commun au full auto et au mode hard : projets concernés, durée, parallélisme, plafonds, et aperçu de ce que Claude compte faire. */
const DIALOG_MODES = {
  hard: { title: '☠ Mode hard', go: '☠ Activer le mode hard', hours: 8, perDay: 40, threshold: 95,
    intro: 'Pleine autonomie : plus aucune validation, questions tranchées par Claude, compte rendu à la fin. Choisis les projets concernés ; les autres ne sont pas touchés.' },
  auto: { title: '🤖 Full auto', go: '🤖 Armer le full auto', hours: 24, perDay: 12, threshold: 85,
    intro: 'Claude enchaîne seul les propositions et reprend le travail resté en plan, tant que l\'usage de la session reste sous le seuil, et repart après chaque réinitialisation. Seules les opérations destructrices restent demandées (refusées au bout de 15 min sans réponse). Choisis les projets concernés.' },
};
let dialogMode = 'hard';

function openHardDialog(mode = 'hard', hours = 0) {
  dialogMode = mode;
  const m = DIALOG_MODES[mode];
  $('#hardTitle').textContent = m.title;
  $('#hardIntro').textContent = m.intro;
  $('#hardGo').textContent = m.go;
  $('#hardHours').value = hours || m.hours;
  $('#hardPerDay').value = m.perDay;
  $('#hardThreshold').value = m.threshold;
  $('#hardAutoRow').classList.toggle('hidden', mode !== 'hard');
  $('#hardAuto').checked = true;  // le mode hard arme le full auto par défaut
  const list = state.projects;
  $('#hardProjects').innerHTML = list.length ? list.map((p) => `<label class="check"><input type="checkbox" value="${esc(p.id)}" ${p.online ? 'checked' : ''}>
      <span>${esc(p.name || p.id)}</span><span class="off">${p.online ? '● pod en ligne' : '○ pod hors ligne'}</span></label>`).join('')
    : '<div class="hint">Aucun projet connu.</div>';
  $('#hardPreview').textContent = 'Chargement…';
  $('#hardGo').disabled = false;
  $('#hardDlg').showModal();
  refreshHardPreview();
}

/** Estimation en tokens du plan affiché, rapportée à ce qu'il reste de la semaine. */
function tokenLine(r) {
  const t = r.tokens || {};
  const est = t.plan ? `≈ ${fmtTokens(t.plan)} tokens pour ${r.total} tâche(s) (≈ ${fmtTokens(t.avg_task)} par tâche)` : '';
  const left = t.remaining ? `${est ? ' · ' : ''}il reste ≈ ${fmtTokens(t.remaining)} cette semaine (${Math.round(t.remaining_ratio * 100)} %)` : '';
  return est || left ? `<div class="muted">💰 ${est}${left}</div>` : '';
}

let hardPreviewTimer = 0;
function refreshHardPreview() {
  clearTimeout(hardPreviewTimer);
  hardPreviewTimer = setTimeout(async () => {
    const ids = hardChosen();
    if (!ids.length) { $('#hardPreview').textContent = 'Choisis au moins un projet.'; $('#hardGo').disabled = true; return; }
    $('#hardGo').disabled = false;
    try {
      const r = await api('hard_apercu', undefined, `&projects=${encodeURIComponent(ids.join(','))}`);
      const name = (id) => r.projects.find((p) => p.id === id)?.name || id || 'Auto';
      const kind = { memoire: '🧠 mémoire', reprise: '↻ reprise', proposition: '💡' };
      const offline = r.projects.filter((p) => !p.online);
      const lines = r.plan.map((x) => `<li>${kind[x.kind] || ''} <b>${esc(name(x.project))}</b> — ${esc(x.titre)}${x.online ? '' : ' <span class="muted">(pod hors ligne : en attente)</span>'}
        <span class="muted">· ${ago(x.since)}</span></li>`).join('');
      $('#hardPreview').innerHTML = `<b>Ce que le mode hard compte faire</b>
        ${r.plan.length ? `<ol>${lines}</ol>${r.total > r.plan.length ? `<div class="muted">… et ${r.total - r.plan.length} de plus</div>` : ''}
          <div class="muted">Dans l'ordre : d'abord le travail resté en plan (le plus ancien en premier), puis les propositions.</div>`
        : '<div class="muted">Rien en mémoire pour ces projets : Claude proposera d\'abord des tâches à partir de l\'historique de chaque projet.</div>'}
        ${tokenLine(r)}
        ${offline.length ? `<div class="muted">⏸ Hors ligne, donc à l'arrêt tant que leur pod ne revient pas : ${offline.map((p) => `${esc(p.name)}${p.waiting ? ` (${p.waiting} tâche(s) en attente depuis ${ago(p.waiting_since)})` : ''}`).join(', ')}.</div>` : ''}`;
    } catch (e) { $('#hardPreview').textContent = e.message; }
  }, 250);
}

$('#hardProjects').addEventListener('change', refreshHardPreview);
const hardSelect = (pick) => {
  document.querySelectorAll('#hardProjects input').forEach((i) => { i.checked = pick(state.projects.find((p) => p.id === i.value)); });
  refreshHardPreview();
};
$('#hardAll').addEventListener('click', () => hardSelect(() => true));
$('#hardOnline').addEventListener('click', () => hardSelect((p) => !!p?.online));
$('#hardNone').addEventListener('click', () => hardSelect(() => false));
$('#hardForm').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const projects = hardChosen();
  const hours = Number($('#hardHours').value), parallel = Number($('#hardParallel').value);
  const body = { enabled: true, hours, max_parallel: parallel, projects, max_per_day: Number($('#hardPerDay').value) || undefined,
    threshold: (Number($('#hardThreshold').value) || 85) / 100 };
  if (!projects.length || !hours || !parallel) return;
  const hard = dialogMode === 'hard';
  const sure = hard
    ? `Activer le mode hard pendant ${hours} h sur ${projects.length} projet(s), ${parallel} de front${$('#hardAuto').checked ? ', full auto armé' : ', sans full auto'} ?\nClaude ne te demandera plus rien : validations approuvées (écrire n'importe où dans le pod, pousser sur main, cluster, sudo), questions tranchées par lui. Un compte rendu sera dressé. Seuls les outils interdits (deny) restent bloqués.`
    : `Armer le full auto pendant ${hours} h sur ${projects.length} projet(s), ${parallel} de front, ${body.max_per_day || 12} tâche(s) par jour au plus ?`;
  if (!confirm(sure)) return;
  $('#hardGo').disabled = true;
  try {
    await api(hard ? 'mode_hard' : 'autopilote', hard ? { ...body, auto: $('#hardAuto').checked } : body);
    $('#hardDlg').close();
    state.reportAt = 0;
    poll();
  } catch (e) { $('#hardPreview').textContent = e.message; $('#hardGo').disabled = false; }
});

/** Compte rendu de ce qui a été fait depuis l'armement du mode hard (rafraîchi toutes les 20 s tant que le tableau de bord est visible). */
async function loadReport() {
  if (state.reportLoading || !state.hard?.since || !dashVisible() || Date.now() - state.reportAt < 20000) return;
  state.reportLoading = true;
  try { state.report = await api('compte_rendu'); state.reportAt = Date.now(); } catch { /* nouvel essai au prochain rendu */ }
  state.reportLoading = false;
  renderDash();
}

function reportHtml() {
  const r = state.report;
  if (!state.hard?.since || !r?.since) return '';
  const t = r.totals || {};
  const icon = { done: '✅', error: '❌', cancelled: '⏹' };
  const projects = r.projects.map((p) => `<article class="follow ${p.tasks.some((x) => x.status === 'error') ? 'ask' : ''}"><div class="fhead"><b>${esc(state.projects.find((x) => x.id === p.project)?.name || p.project)}</b>
      <span class="muted">${p.tasks.length} tâche(s)</span></div>${p.tasks.map((x) => `<div class="hint"><button class="forigin" data-jump="${esc(x.id)}">${icon[x.status] || '⏳'} ${esc(x.instruction)}${x.answered ? ' · 🤖 question tranchée' : ''}</button>
      ${x.result ? `<div>${esc(x.result.replace(/\*\*|`/g, '').slice(0, 260))}</div>` : ''}
      ${x.files || x.commits.length ? `<div class="muted">${x.files} fichier(s) +${x.added} −${x.removed}${x.commits.length ? ` · ${x.commits.length} commit(s)` : ''}${x.branch ? ` · ${esc(x.branch)}` : ''}${x.tests ? (x.tests.ok ? ' · tests ✓' : ' · tests ✗') : ''}</div>` : ''}</div>`).join('')}</article>`).join('');
  const sensitive = r.sensitive?.length ? `<div class="hint">⚠ Opérations sensibles passées sans validation : ${r.sensitive.map((x) => `<code>${esc(x.what)}</code>`).join(' · ')}</div>` : '';
  return `<section class="sect"><div class="secthead"><h3>📋 Compte rendu · ${r.enabled ? 'en cours' : 'terminé'}</h3></div>
    <div class="hint">Depuis ${esc(new Date(r.since * 1000).toLocaleString('fr-FR', { weekday: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }))} :
      ${t.tasks || 0} tâche(s) · ✅ ${t.done || 0}${t.error ? ` · ❌ ${t.error}` : ''}${t.active ? ` · ⏳ ${t.active}` : ''} · ${t.files || 0} fichier(s) +${t.added || 0} −${t.removed || 0} · ${t.commits || 0} commit(s)
      · ${t.approved || 0} validation(s) passée(s) automatiquement${t.questions ? ` · ${t.questions} question(s) tranchée(s) par Claude` : ''}</div>
    ${sensitive}<div class="follows">${projects || '<div class="emptyd">Aucune tâche pour l\'instant.</div>'}</div></section>`;
}

async function propositionAction(b) {
  if (b.dataset.prop === 'ajouter') {
    const titre = prompt('Titre de la proposition'); if (!titre) return;
    const instruction = prompt('Instruction complète pour Claude'); if (!instruction) return;
    await api('proposition', { action: 'ajouter', titre, instruction, project: state.selected || null });
  } else {
    b.disabled = true;
    await api('proposition', { action: b.dataset.prop, id: b.dataset.id });
  }
  poll();
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
  const el = openGroupOf(id);
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
  if (id) openGroupOf(id);
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
  const q = [...state.tasks.values()].filter(openQuestion).length;
  const b = $('#pendingBtn');
  b.textContent = n && q ? `${n} · ❓${q}` : n ? `${n}` : `❓ ${q}`;
  b.title = [n ? `${n} validation(s) en attente` : '', q ? `${q} question(s) de Claude en attente de ta réponse` : ''].filter(Boolean).join(' · ');
  b.classList.toggle('hidden', n + q === 0);
  b.classList.toggle('ask', !n && q > 0);
  document.title = n + q ? `(${n + q}) Cockpit` : 'Cockpit';
  navigator.setAppBadge?.(n + q).catch(() => {});
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
  if (pip) tickIn(pip.document);
  tickIn(document);
}
function tickIn(doc) {
  const $ = (sel) => doc.querySelector(sel) || document.createElement('span');
  doc.querySelectorAll('[data-uptime]').forEach((el) => { el.textContent = dureeLongue(Date.now() / 1000 - Number(el.dataset.uptime)); });
  doc.querySelectorAll('[data-expires]').forEach((el) => {
    const s = Math.max(0, Math.round(el.dataset.expires - Date.now() / 1000));
    el.textContent = `refus automatique dans ${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
  });
  doc.querySelectorAll('[data-ago]').forEach((el) => { el.textContent = ago(Number(el.dataset.ago)); });
  doc.querySelectorAll('[data-since]').forEach((el) => {
    el.textContent = ` · ${duree(Date.now() / 1000 - Number(el.dataset.since))}`;
  });
}
setInterval(tick, 1000);

// ---------- synchronisation (interrogation du relais)
let pollTimer = null;

// ---------- mini-fenêtre toujours visible (Document Picture-in-Picture : Chrome, Edge sur ordinateur)
let pip = null;
const pipSupported = () => 'documentPictureInPicture' in window;

/** Contenu de la mini-fenêtre : ce qui attend une réponse d'abord, puis le travail en cours et ce qui vient de finir. */
function pipHtml() {
  const tasks = [...state.tasks.values()];
  const approvals = tasks.flatMap((e) => [...e.approvals.values()].filter((a) => !a.decision).map((a) => ({ e, a })));
  const questions = tasks.filter(openQuestion);
  const active = tasks.filter((e) => ACTIVE.has(e.task.status));
  const now = Date.now() / 1000;
  const finished = tasks.filter((e) => !ACTIVE.has(e.task.status) && e.task.instruction && !openQuestion(e)
    && now - (e.task.updated || e.task.created) < 1800).sort((a, b) => (b.task.updated || 0) - (a.task.updated || 0)).slice(0, 3);
  const u = latestUsage();
  const l = u?.limits.find((x) => x.type === 'five_hour' && x.utilization != null);
  const where = (e) => `${esc(state.projects.find((p) => p.id === e.task.project)?.name || e.task.project || '')} · ${esc(shownInstruction(e.task.instruction).slice(0, 70))}`;
  let html = `<div class="piphead"><b>Cockpit</b><span>${active.length} en cours</span>${l ? `<span>Session ${Math.round(l.utilization * 100)} %</span>` : ''}
    <button class="btn ghost" data-pip-focus title="revenir au cockpit">↗</button></div>`;
  html += approvals.map(({ e, a }) => `<div class="pipcard alert"><div class="pipwhere">${where(e)}</div>${approvalHtml(a)}</div>`).join('');
  html += questions.map((e) => `<div class="pipcard alert"><div class="pipwhere">${where(e)}</div>${questionHtml(e.task, true)}</div>`).join('');
  html += active.map((e) => `<div class="pipcard"><div class="pipwhere">${where(e)}</div>
    <div class="lstep">${e.task.status === 'running' ? '<span class="spin"></span>' : ''}<span>${esc(currentStep(e))}</span></div></div>`).join('');
  html += finished.map((e) => {
    const { c } = changesOf(e);
    const icon = { done: '✅', error: '❌', cancelled: '⏹', needs_project: '❓', paused: '⏸' }[e.task.status] || '•';
    return `<div class="pipcard done">${icon} ${where(e)}${c && (c.file_count || c.commits?.length) ? `<div class="rmeta">${changesLine(c)}</div>` : ''}</div>`;
  }).join('');
  if (!approvals.length && !questions.length && !active.length && !finished.length) html += '<div class="emptyd">Rien à faire pour l\'instant 👌</div>';
  return { html, attention: approvals.length + questions.length };
}

function renderPip() {
  if (!pip) return;
  const { html, attention } = pipHtml();
  if (html === state.pipHtml) return;
  const doc = pip.document;
  const drafts = new Map([...doc.querySelectorAll('form.question')].map((f) => [f.dataset.question, f.querySelector('textarea').value]));
  doc.body.innerHTML = `<div class="pipwrap">${html}</div>`;
  doc.querySelectorAll('form.question').forEach((f) => { if (drafts.get(f.dataset.question)) f.querySelector('textarea').value = drafts.get(f.dataset.question); });
  restoreQuestions(doc.body);
  sizes(doc.body);
  // Une nouvelle validation ou question : la mini-fenêtre clignote pour attirer l'œil
  if (attention > (state.pipAttention || 0)) { doc.body.classList.remove('flash'); void doc.body.offsetWidth; doc.body.classList.add('flash'); }
  state.pipAttention = attention;
  state.pipHtml = html;
  tick();
}

async function openPip() {
  if (!pipSupported()) { alert('Mini-fenêtre indisponible ici : utilise Chrome ou Edge sur ordinateur.'); return; }
  if (pip) { pip.focus(); return; }
  pip = await documentPictureInPicture.requestWindow({ width: 380, height: 520 });
  const doc = pip.document;
  doc.title = 'Cockpit';
  const link = doc.createElement('link');
  link.rel = 'stylesheet';
  link.href = document.querySelector('link[rel="stylesheet"]').href;
  doc.head.append(link);
  doc.body.className = 'pip';
  if (document.documentElement.dataset.theme) doc.documentElement.dataset.theme = document.documentElement.dataset.theme;
  doc.addEventListener('click', onClick);
  doc.addEventListener('submit', onQuestionSubmit);
  doc.addEventListener('input', (ev) => {
    const ta = ev.target.closest?.('form.question textarea');
    if (ta) qdraftOf(ta.closest('form.question').dataset.question).text = ta.value;
  });
  pip.addEventListener('pagehide', () => { pip = null; state.pipHtml = ''; state.pipAttention = 0; });
  state.pipHtml = '';
  renderPip();
  poll();
}
let polling = false;

async function poll() {
  if (polling) return;
  polling = true;
  clearTimeout(pollTimer);
  try {
    // h : empreintes des blocs déjà reçus ; le relais n'envoie que ceux qui ont changé (les autres sont absents de s)
    const s = await api('etat', undefined, `&since=${state.lastId}&h=${encodeURIComponent(state.etatH || '')}`);
    state.etatH = s.h || '';
    state.replay = state.lastId === 0;
    const touched = new Set();
    let prMerged = false;
    for (const evt of s.events) { onEvent(evt); touched.add(evt.task_id); prMerged ||= evt.type === 'pr_merged' || evt.type === 'pr_closed'; }
    if (prMerged) state.order.forEach((id) => touched.add(id));  // la PR peut être affichée sur une autre conversation
    // Une conversation qui avance redessine ses cartes précédentes (question répondue, étapes proposées…)
    if (!state.replay) {
      const convs = new Set([...touched].map(conversationOf));
      for (const id of state.order) if (convs.has(conversationOf(id))) touched.add(id);
    }
    state.lastId = s.last_id;
    if (state.replyTo === undefined) {
      const last = [...state.order].reverse().map((id) => state.tasks.get(id).task).find((t) => t.kind !== 'service');
      state.replyTo = null;
      if (last?.created && Date.now() / 1000 - last.created < REPRISE_AUTO) setReplyTo(last.id);
    }
    followConversation();
    if ('projects' in s && JSON.stringify(s.projects) !== JSON.stringify(state.projects)) { state.projects = s.projects; renderProjects(); }
    if (s.worker_online !== state.workerOnline) {
      state.workerOnline = s.worker_online;
      state.order.forEach((id) => touched.add(id));
    }
    $('#conn').classList.toggle('on', s.worker_online);
    $('#conn').title = s.worker_online ? 'worker Onyxia connecté' : 'worker Onyxia hors ligne';
    render(touched);
    if (state.pendingJump && document.getElementById(`t-${state.pendingJump}`)) { jumpTo(state.pendingJump); state.pendingJump = null; }
    if ('workers' in s) state.workers = s.workers || [];
    if ('memory' in s) state.memory = s.memory || null;
    if ('autopilot' in s) state.autopilot = s.autopilot || null;
    if ('hard' in s) state.hard = s.hard || null;
    if ('week' in s) state.week = s.week || null;
    if ('deleted_projects' in s) state.deleted = s.deleted_projects || {};
    renderUsageBar();
    renderPip();
    renderDash();
    state.replay = false;
  } catch (e) {
    $('#conn').classList.remove('on');
    if (e instanceof AuthError) { polling = false; return start(); }
  }
  polling = false;
  if (!document.hidden || pip) pollTimer = setTimeout(poll, 2000);  // la mini-fenêtre reste à jour
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
async function onClick(ev) {
  const b = ev.target.closest('button');
  if (!b) return;
  try {
    if (b.dataset.approve || b.dataset.deny) {
      b.parentElement.querySelectorAll('button').forEach((x) => (x.disabled = true));
      await api('decider', { approval_id: b.dataset.approve || b.dataset.deny, decision: b.dataset.approve ? 'approve' : 'deny' });
      poll();
    } else if (b.dataset.resume) {
      b.disabled = true;
      await api('reprendre', { task_id: b.dataset.resume });
      poll();
    } else if (b.dataset.cancel) {
      await api('annuler', { task_id: b.dataset.cancel });
      poll();
    } else if (b.dataset.quick) {
      const c = state.quickList?.[Number(b.dataset.quick)];
      if (c) { input.value = c.instruction; input.dispatchEvent(new Event('input')); input.focus(); }
    } else if (b.id === 'editQuick') {
      openCommands();
    } else if (b.id === 'addCmd') {
      $('#cmdRows').querySelector('p.hint')?.remove();
      $('#cmdRows').insertAdjacentHTML('beforeend', commandRow());
    } else if ('delCmd' in b.dataset) {
      b.closest('.cmdrow').remove();
    } else if (b.dataset.memoInit) {
      b.disabled = true;
      try { await api('memoire_init', { project: b.dataset.memoInit }); await poll(); } catch (e) { alert(e.message); b.disabled = false; }
    } else if (b.dataset.memory) {
      await openMemory(b.dataset.memory);
    } else if (b.dataset.img) {
      $('#imgFull').src = `api.php?r=image&id=${encodeURIComponent(b.dataset.img)}`;
      $('#imgCaption').textContent = b.dataset.caption || '';
      $('#imgDlg').showModal();
    } else if (b.dataset.resend) {
      const n = await api('tache', { instruction: b.dataset.text, reply_to: lastOfConversation(b.dataset.resend) });
      onEvent({ kind: 'task', task_id: n.id, task: n });
      setReplyTo(n.id);
      await poll();
    } else if (b.dataset.openPr) {
      b.disabled = true;
      const n = await api('tache', { reply_to: b.dataset.openPr, instruction: 'Ouvre une pull request pour ce travail avec ouvrir_pr : '
        + 'titre clair, description avec le contexte, les changements, les tests et les points d\'attention.' });
      onEvent({ kind: 'task', task_id: n.id, task: n });
      setReplyTo(n.id);
      await poll();
    } else if (b.dataset.rvSend) {
      const list = state.review.get(b.dataset.rvSend) || [];
      if (!list.length) return;
      b.disabled = true;
      const n = await api('tache', { instruction: reviewInstruction(list), reply_to: lastOfConversation(b.dataset.rvSend) });
      state.review.delete(b.dataset.rvSend);
      onEvent({ kind: 'task', task_id: n.id, task: n });
      setReplyTo(n.id);
      await poll();
    } else if (b.dataset.rvDel) {
      state.review.get(b.dataset.rvDel)?.splice(Number(b.dataset.i), 1);
      render([b.dataset.rvDel]);
    } else if ('rvCancel' in b.dataset) {
      b.closest('.rvform').remove();
    } else if (b.dataset.keep) {
      if (!confirm('Garder cette variante (fusionnée dans le projet) et jeter les autres ?')) return;
      await api('garder_variante', { task_id: b.dataset.keep });
      poll();
    } else if (b.dataset.copyPw) {
      // Mot de passe jamais écrit dans la page : lu dans l'état du pod au moment de la copie
      const pw = state.workers.find((x) => x.id === b.dataset.copyPw)?.state?.vscode?.password;
      if (pw) {
        await navigator.clipboard.writeText(pw);
        b.textContent = '✓ Copié';
        setTimeout(() => { b.textContent = '🔑 Mot de passe'; }, 2000);
      }
    } else if (b.dataset.mergeUrl) {
      const n = b.dataset.mergeUrl.split('/').pop();
      if (!confirm(`Fusionner la pull request #${n} ? Elle sera intégrée à la branche de base (ce qui peut déclencher un déploiement).`)) return;
      b.disabled = true;
      const t = await api('fusionner_pr', { task_id: b.dataset.taskId, url: b.dataset.mergeUrl });
      onEvent({ kind: 'task', task_id: t.id, task: t });
      await poll();
    } else if (b.dataset.always) {
      const a = [...state.tasks.values()].flatMap((e) => [...e.approvals.values()]).find((x) => x.id === b.dataset.always);
      const r = a && ruleFor(a);
      if (!r || !confirm(`Approuver, et ne plus jamais demander pour ${r.label} (dans tous les pods) ?\nTu pourras supprimer cette règle dans ☰ → Règles d'autorisation.`)) return;
      b.parentElement.querySelectorAll('button').forEach((x) => (x.disabled = true));
      await api('regle_ajouter', { tool: r.tool, pattern: r.pattern, label: r.label, example: r.example });
      await api('decider', { approval_id: a.id, decision: 'approve' });
      poll();
    } else if (b.id === 'mProjects') {
      await openProjects();
    } else if (b.dataset.delProject) {
      const id = b.dataset.delProject;
      const name = state.projects.find((p) => p.id === id)?.name || id;
      const history = $('#projHistory')?.checked;
      if (!confirm(`Retirer « ${name} » du cockpit ?\nLe pod, le dépôt git et le service Onyxia ne sont pas touchés ; le projet disparaît de la liste, de la mémoire et des propositions (restaurable).${history ? '\n\nSon HISTORIQUE (tâches, événements) sera supprimé définitivement.' : ''}`)) return;
      try {
        await api('projet_supprimer', { project: id, history });
        if (history) {  // le fil et le tableau de bord n'affichent plus ses tâches
          for (const [tid, e] of [...state.tasks]) if (e.task.project === id) { state.tasks.delete(tid); document.getElementById(`t-${tid}`)?.remove(); }
          state.order = state.order.filter((tid) => state.tasks.has(tid));
        }
        if (state.selected === id) state.selected = null;
        await poll();
        renderProjects();
        updateGroups();
      } catch (e) { alert(e.message); }
      await openProjects();
    } else if (b.dataset.restoreProject) {
      try { await api('projet_restaurer', { project: b.dataset.restoreProject }); await poll(); renderProjects(); } catch (e) { alert(e.message); }
      await openProjects();
    } else if (b.id === 'mRules') {
      await openRules();
    } else if (b.dataset.delRule) {
      if (confirm('Supprimer cette règle ? La validation te sera de nouveau demandée.')) { await api('regle_supprimer', { id: Number(b.dataset.delRule) }); await openRules(); }
    } else if (b.dataset.tip) {
      const act = b.dataset.tip;
      if (act === 'view-dash') setView('dash');
      else if (act === 'new-conv') { setReplyTo(null); input.focus(); }
      else if (act === 'model-auto') { state.model = ''; $('#modelSel').value = ''; try { localStorage.setItem('cockpit_model', ''); } catch { /* sans importance */ } }
      else if (act.startsWith('project:')) { state.selected = act.slice(8); renderProjects(); }
      renderCostTip();
    } else if (b.dataset.nextEdit || b.dataset.nextRun) {
      if (b.dataset.nextRun) b.disabled = true;
      await runNextStep(b.dataset.nextEdit || b.dataset.nextRun, Number(b.dataset.i), !!b.dataset.nextRun);
    } else if (b.dataset.hard) {
      await hardToggle(b.dataset.hard === 'on', Number(b.dataset.hours) || 0);
    } else if (b.dataset.auto) {
      await autoToggle(b.dataset.auto === 'on');
    } else if (b.dataset.prop) {
      await propositionAction(b);
    } else if (b.dataset.dismiss) {
      await api('ignorer_suite', { task_id: b.dataset.dismiss });
      poll();
    } else if (b.dataset.podMode) {
      setPodMode(b.dataset.podMode);
    } else if (b.dataset.bilan) {
      state.bilanJours = Number(b.dataset.bilan);
      try { localStorage.setItem('cockpit_bilan', b.dataset.bilan); } catch { /* sans importance */ }
      renderDash();
    } else if (b.dataset.themeSet) {
      setTheme(b.dataset.themeSet);
    } else if (b.id === 'dashToggle') {
      setDashHidden(!document.body.classList.contains('dash-hidden'));
    } else if (b.id === 'mPip' || b.id === 'pipBtn') {
      $('#menuDlg').close();
      await openPip();
    } else if ('pipFocus' in b.dataset) {
      window.focus();
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
      if (state.view === 'dash' && !matchMedia('(min-width: 1100px)').matches) setView('chat');
      (document.querySelector('#feed .approval:not(.resolved)') || document.querySelector('#feed form.question'))
        ?.scrollIntoView({ behavior: 'smooth', block: 'center' });
    } else if (b.dataset.pick !== undefined) {
      // Un choix par question : le choix touché remplace le précédent (et se retire s'il est touché à nouveau)
      const on = b.getAttribute('aria-pressed') !== 'true';
      b.closest('.qitem').querySelectorAll('[data-pick]').forEach((x) => x.setAttribute('aria-pressed', 'false'));
      b.setAttribute('aria-pressed', String(on));
      const picks = qdraftOf(b.closest('form.question').dataset.question).picks;
      const q = b.closest('.qitem').dataset.q;
      if (on) picks[q] = b.dataset.pick; else delete picks[q];
    } else if (b.dataset.answer) {
      b.disabled = true;
      await answerQuestion(b.dataset.answer, b.dataset.text);
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
}
document.addEventListener('click', onClick);

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
input.addEventListener('input', () => { input.style.height = 'auto'; input.style.height = `${input.scrollHeight}px`; renderCostTip(); });
input.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey && !matchMedia('(pointer: coarse)').matches) { e.preventDefault(); $('#composer').requestSubmit(); }
});

$('#composer').addEventListener('submit', async (ev) => {
  ev.preventDefault();
  const instruction = input.value.trim();
  if (!instruction) return;
  $('#send').disabled = true;
  try {
    let cur = state.replyTo && state.tasks.get(state.replyTo)?.task;
    // Nouveau projet ou nouveau pod demandé dans la conversation d'un projet : seul l'orchestrateur crée des pods.
    if (cur && wantsNewPod(instruction) && !isOrchestratorProject(cur.project)
      && confirm(`Seul l'orchestrateur peut créer un nouveau pod ou un nouveau projet. Envoyer ce message à l'orchestrateur `
        + `dans une nouvelle conversation, plutôt qu'à « ${cur.project} » ?`)) {
      setReplyTo(null);
      state.selected = null;
      renderProjects();
      cur = null;
    }
    if (cur && ACTIVE.has(cur.status)) {
      // Claude travaille : le message lui parvient pendant la tâche (il est interrompu et reprend avec).
      await api('recadrer', { task_id: cur.id, message: instruction });
    } else {
      const variants = state.replyTo ? 1 : state.variants;
      const t = await api('tache', { instruction, project: state.selected, reply_to: state.replyTo || null,
        model: state.model || null, variants });
      onEvent({ kind: 'task', task_id: t.id, task: t });
      setReplyTo(t.id);
    }
    input.value = '';
    input.style.height = 'auto';
    await poll();
  } catch (e) {
    if (e instanceof AuthError) start(); else alert(e.message);
  }
  $('#send').disabled = false;
});

// ---------- commande vocale : dictée dans la saisie (reconnaissance vocale du navigateur, en français).
// Finir par « envoie » (ou « envoyer ») envoie l'instruction ; « annule » efface la dictée en cours.
const Speech = window.SpeechRecognition || window.webkitSpeechRecognition;
let voice = null;
function stopVoice() { voice?.stop(); }
function startVoice() {
  if (!Speech || voice) return;
  const rec = new Speech();
  rec.lang = 'fr-FR';
  rec.continuous = true;
  rec.interimResults = true;
  const before = input.value.trim();
  let said = '';
  const show = (extra = '') => {
    input.value = [before, (said + extra).trim()].filter(Boolean).join(' ');
    input.dispatchEvent(new Event('input'));
  };
  rec.onresult = (ev) => {
    let interim = '';
    for (let i = ev.resultIndex; i < ev.results.length; i++) {
      const r = ev.results[i];
      if (r.isFinal) said += ` ${r[0].transcript}`; else interim += ` ${r[0].transcript}`;
    }
    if (/\bannul(e|er|ez)[\s.!]*$/i.test(said)) { said = ''; show(); rec.abort(); return; }
    const send = /\s*\benvo(ie|ies|yer|yez)[\s.!]*$/i;
    if (send.test(said)) {
      said = said.replace(send, '');
      show();
      rec.abort();
      if (input.value.trim()) $('#composer').requestSubmit();
      return;
    }
    show(interim);
  };
  rec.onerror = (ev) => {
    if (ev.error === 'not-allowed' || ev.error === 'service-not-allowed') alert('Micro refusé : autorise-le pour le cockpit dans les réglages du navigateur.');
  };
  rec.onend = () => {
    voice = null;
    $('#mic').setAttribute('aria-pressed', 'false');
    $('#mic').setAttribute('aria-label', 'dicter');
  };
  try { rec.start(); } catch { return; }
  voice = rec;
  $('#mic').setAttribute('aria-pressed', 'true');
  $('#mic').setAttribute('aria-label', 'arrêter la dictée');
}
if (Speech) $('#mic').classList.remove('hidden');
$('#mic').addEventListener('click', () => (voice ? stopVoice() : startVoice()));

$('#modelSel').addEventListener('change', () => {
  state.model = $('#modelSel').value;
  renderCostTip();
  try { localStorage.setItem('cockpit_model', state.model); } catch { /* sans importance */ }
});
$('#variantSel').addEventListener('change', () => { state.variants = Number($('#variantSel').value); });

// ---------- thème (auto, clair, sombre) et répartition fil / tableau de bord sur ordinateur
function setTheme(theme) {
  if (theme === 'auto') delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = theme;
  try { localStorage.setItem('cockpit_theme', theme); } catch { /* sans importance */ }
  if (pip) { if (theme === 'auto') delete pip.document.documentElement.dataset.theme; else pip.document.documentElement.dataset.theme = theme; }
  document.querySelectorAll('[data-theme-set]').forEach((b) => b.setAttribute('aria-pressed', String(b.dataset.themeSet === theme)));
  // Couleur de la barre du navigateur / de l'app installée
  requestAnimationFrame(() => document.querySelector('meta[name="theme-color"]')
    ?.setAttribute('content', getComputedStyle(document.body).getPropertyValue('--panel').trim() || '#0f1115'));
}
const currentTheme = () => document.documentElement.dataset.theme || 'auto';

const CHAT_MIN = 360;
function setChatWidth(px, save = true) {
  const layout = $('#layout');
  if (px == null) { layout.style.removeProperty('--chat-w'); } else {
    const w = Math.round(Math.max(CHAT_MIN, Math.min(px, layout.clientWidth - 320)));
    layout.style.setProperty('--chat-w', `${w}px`);
    $('#splitter').setAttribute('aria-valuenow', String(w));
  }
  if (save) try { localStorage.setItem('cockpit_chat_w', px == null ? '' : String(px)); } catch { /* sans importance */ }
  renderDash();
}

function setDashHidden(hidden) {
  document.body.classList.toggle('dash-hidden', hidden);
  $('#dashToggle').textContent = hidden ? '⇤' : '⇥';
  $('#dashToggle').setAttribute('aria-label', hidden ? 'afficher le tableau de bord' : 'masquer le tableau de bord');
  try { localStorage.setItem('cockpit_dash_hidden', hidden ? '1' : ''); } catch { /* sans importance */ }
  renderDash();
}

$('#splitter').addEventListener('pointerdown', (ev) => {
  ev.preventDefault();
  const sp = $('#splitter');
  sp.setPointerCapture(ev.pointerId);
  document.body.classList.add('resizing');
  const left = $('#layout').getBoundingClientRect().left;
  const move = (e) => setChatWidth(e.clientX - left, false);
  const up = (e) => {
    sp.removeEventListener('pointermove', move);
    document.body.classList.remove('resizing');
    setChatWidth(e.clientX - left);
  };
  sp.addEventListener('pointermove', move);
  sp.addEventListener('pointerup', up, { once: true });
});
$('#splitter').addEventListener('dblclick', () => setChatWidth(null));
$('#splitter').addEventListener('keydown', (e) => {
  if (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight') return;
  e.preventDefault();
  setChatWidth($('#chat').getBoundingClientRect().width + (e.key === 'ArrowRight' ? 40 : -40));
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

// Clavier : « / » ou Ctrl/⌘+K pour écrire, Échap pour quitter la saisie, Alt+1 / Alt+2 pour changer de vue, Alt+M pour dicter.
document.addEventListener('keydown', (e) => {
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName);
  if ((e.key === 'k' && (e.ctrlKey || e.metaKey)) || (e.key === '/' && !typing)) {
    e.preventDefault();
    if (state.view === 'dash' && !matchMedia('(min-width: 1100px)').matches) setView('chat');
    input.focus();
  } else if (e.key === 'Escape' && document.activeElement === input) {
    input.blur();
  } else if (e.altKey && e.key.toLowerCase() === 'd') {
    e.preventDefault();
    setDashHidden(!document.body.classList.contains('dash-hidden'));
  } else if (e.altKey && e.key.toLowerCase() === 'm' && Speech) {
    e.preventDefault();
    if (voice) stopVoice(); else startVoice();
  } else if (e.altKey && e.key.toLowerCase() === 'p') {
    e.preventDefault();
    openPip();
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
    loadCommands();
    applyLaunchParams();
  } catch (e) {
    authScreen({ title: 'Hors ligne', text: e.message, button: 'Réessayer', onClick: start });
  }
}

if (pipSupported() && !matchMedia('(pointer: coarse)').matches) { $('#mPip').classList.remove('hidden'); $('#pipBtn').classList.remove('hidden'); }
try {
  setTheme(localStorage.getItem('cockpit_theme') || 'auto');
  if (localStorage.getItem('cockpit_dash_hidden')) setDashHidden(true);
  const w = Number(localStorage.getItem('cockpit_chat_w'));
  if (w) setChatWidth(w, false);
} catch { /* navigation privée */ }
try {
  state.model = localStorage.getItem('cockpit_model') || '';
  $('#modelSel').value = state.model;
  if ([1, 7, 30].includes(Number(localStorage.getItem('cockpit_bilan')))) state.bilanJours = Number(localStorage.getItem('cockpit_bilan'));
  state.dashMore = localStorage.getItem('cockpit_more') === '1';
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
