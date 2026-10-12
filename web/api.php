<?php
/**
 * Cockpit : relais entre la PWA (téléphone) et le worker Claude Code (pod Onyxia).
 * Appel : api.php?r=<route>, en GET (lecture) ou POST (écriture, corps JSON).
 *
 * Le worker n'est jamais exposé : il interroge ce relais (long-polling) avec sa clé.
 * Le téléphone s'authentifie par passkey (Face ID / empreinte), après un appairage par QR code.
 */

declare(strict_types=1);
header('Content-Type: application/json; charset=utf-8');
header('Cache-Control: no-store');
header('X-Content-Type-Options: nosniff');

const SESSION_COOKIE = 'cockpit_s';
const SESSION_DUREE = 180 * 86400;      // glissante
const APPAIRAGE_DUREE = 600;            // lien QR valable 10 min
const DEFI_DUREE = 300;                 // challenge WebAuthn
const LONG_POLL = 20;                   // secondes d'attente côté worker
const WORKER_EN_LIGNE = 45;             // worker considéré hors ligne au-delà
const LIMITE_ECHECS_IP = [20, 900];     // échecs d'authentification par IP
const TACHES_VISIBLES = 30;
const CONTEXTE_ECHANGES = 6;              // échanges précédents transmis au worker (routage, reprise sans session)
const WORKERS_VISIBLES = 7 * 86400;     // tableau de bord : pods vus dans les 7 derniers jours
const TACHES_FILLES_MAX = 5;            // lancer_en_parallele : sous-tâches par appel
const MODELES = ['rapide', 'standard', 'approfondi'];
const VARIANTES_MAX = 3;
const IMAGE_MAX = 1_600_000;            // capture d'écran (JPEG), octets
const MEMOIRE_MAX = 50_000;             // CLAUDE.md d'un projet
const COMMANDES_MAX = 20;               // commandes rapides par liste
const ACTIFS = ['pending', 'routing', 'queued', 'running', 'waiting_approval'];
const CONFIANCE_DUREES = [0, 15, 30, 60, 120];  // minutes (0 : désactivé)

/** Conversations en mode confiance : {conversation: fin (horodatage)}. */
function confiances(): array {
    q('DELETE FROM confiances WHERE jusqua <= ?', [time()]);
    return array_map('intval', q('SELECT conversation, jusqua FROM confiances')->fetchAll(PDO::FETCH_KEY_PAIR));
}

$CONFIG = require __DIR__ . '/config.php';

function repondre($data, int $code = 200): never {
    http_response_code($code);
    echo json_encode($data, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
    exit;
}

function erreur(string $message, int $code = 400): never {
    repondre(['detail' => $message], $code);
}

set_exception_handler(function (Throwable $e) use ($CONFIG) {
    error_log('cockpit: ' . $e);
    erreur(($CONFIG['debug'] ?? false) ? $e->getMessage() : 'erreur serveur', 500);
});

// ---------------------------------------------------------------- base

function db(): PDO {
    static $pdo = null;
    if ($pdo) return $pdo;
    $dir = __DIR__ . '/data';
    if (!is_dir($dir)) mkdir($dir, 0700, true);
    if (!is_file("$dir/.htaccess")) file_put_contents("$dir/.htaccess", "Require all denied\n");
    $pdo = new PDO('sqlite:' . $dir . '/cockpit.db', null, null, [
        PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION,
        PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC,
    ]);
    $pdo->exec('PRAGMA journal_mode=WAL; PRAGMA busy_timeout=5000; PRAGMA foreign_keys=ON;');
    $pdo->exec(<<<SQL
CREATE TABLE IF NOT EXISTS taches (
    id TEXT PRIMARY KEY, instruction TEXT NOT NULL, projet TEXT, nouvelle_session INTEGER NOT NULL DEFAULT 0,
    statut TEXT NOT NULL DEFAULT 'pending', raison_routage TEXT NOT NULL DEFAULT '', resultat TEXT NOT NULL DEFAULT '',
    cout REAL NOT NULL DEFAULT 0, cree_le INTEGER NOT NULL, maj_le INTEGER NOT NULL,
    livree INTEGER NOT NULL DEFAULT 0, annulation INTEGER NOT NULL DEFAULT 0, choix_projet TEXT);
CREATE TABLE IF NOT EXISTS evenements (
    id INTEGER PRIMARY KEY AUTOINCREMENT, tache_id TEXT NOT NULL, donnees TEXT NOT NULL, ts INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS evenements_tache ON evenements(tache_id);
CREATE TABLE IF NOT EXISTS validations (
    id TEXT PRIMARY KEY, tache_id TEXT NOT NULL, donnees TEXT NOT NULL, decision TEXT,
    decision_livree INTEGER NOT NULL DEFAULT 0, cree_le INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS audit (id INTEGER PRIMARY KEY AUTOINCREMENT, donnees TEXT NOT NULL, ts INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS passkeys (
    id TEXT PRIMARY KEY, cle_publique TEXT NOT NULL, nom TEXT NOT NULL, cree_le INTEGER NOT NULL, utilisee_le INTEGER);
CREATE TABLE IF NOT EXISTS sessions (
    jeton_hash TEXT PRIMARY KEY, cree_le INTEGER NOT NULL, expire_le INTEGER NOT NULL, appareil TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS appairages (jeton_hash TEXT PRIMARY KEY, expire_le INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS defis (defi TEXT PRIMARY KEY, type TEXT NOT NULL, expire_le INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS abonnements_push (endpoint TEXT PRIMARY KEY, cree_le INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS echecs (ip TEXT NOT NULL, ts INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS reglages (cle TEXT PRIMARY KEY, valeur TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS workers (
    id TEXT PRIMARY KEY, nom TEXT NOT NULL, instance TEXT NOT NULL, projets TEXT NOT NULL DEFAULT '[]',
    orchestrateur INTEGER NOT NULL DEFAULT 1, vu_le INTEGER NOT NULL DEFAULT 0);
-- Mode confiance : validations de routine auto-approuvées pour une conversation jusqu'à « jusqua »
CREATE TABLE IF NOT EXISTS confiances (conversation TEXT PRIMARY KEY, jusqua INTEGER NOT NULL);
SQL);
    $colonnes = array_column($pdo->query('PRAGMA table_info(taches)')->fetchAll(), 'name');
    // extra : champs fournis par le worker (modifications, worktree) ; action_worktree : demande du téléphone.
    // action : tâche exécutée directement par le worker, sans agent (lancement d'un pod depuis le téléphone).
    // attente_projet : tâche confiée à un projet dont le pod démarre (livrée quand son worker se connecte) ;
    // modele : rapide|standard|approfondi ; isoler : variante (worktree dédié) ; groupe : variantes d'une même demande ;
    // recadrages : messages envoyés pendant la tâche, à livrer au worker.
    foreach (['worker_id', 'conversation', 'suite_de', 'session', 'extra', 'parent', 'action_worktree', 'action',
              'modele', 'isoler', 'groupe', 'recadrages', 'attente_projet'] as $c) {
        if (!in_array($c, $colonnes, true)) $pdo->exec("ALTER TABLE taches ADD COLUMN $c TEXT");
    }
    // Clé d'appairage à saisir (ordinateur sans appareil photo) : même ligne que le jeton du lien QR.
    if (!in_array('code_hash', array_column($pdo->query('PRAGMA table_info(appairages)')->fetchAll(), 'name'), true)) {
        $pdo->exec('ALTER TABLE appairages ADD COLUMN code_hash TEXT');
    }
    $colonnesW = array_column($pdo->query('PRAGMA table_info(workers)')->fetchAll(), 'name');
    if (!in_array('etat', $colonnesW, true)) $pdo->exec('ALTER TABLE workers ADD COLUMN etat TEXT');
    if (!in_array('apparu_le', $colonnesW, true)) $pdo->exec('ALTER TABLE workers ADD COLUMN apparu_le INTEGER NOT NULL DEFAULT 0');
    return $pdo;
}

function q(string $sql, array $params = []): PDOStatement {
    $st = db()->prepare($sql);
    $st->execute($params);
    return $st;
}

function reglage(string $cle, ?string $valeur = null): ?string {
    if ($valeur !== null) {
        q('INSERT INTO reglages(cle, valeur) VALUES(?, ?) ON CONFLICT(cle) DO UPDATE SET valeur = excluded.valeur', [$cle, $valeur]);
        return $valeur;
    }
    $v = q('SELECT valeur FROM reglages WHERE cle = ?', [$cle])->fetchColumn();
    return $v === false ? null : $v;
}

// ---------------------------------------------------------------- utilitaires

function b64url(string $bin): string { return rtrim(strtr(base64_encode($bin), '+/', '-_'), '='); }
function b64url_dec(string $s): string {
    $bin = base64_decode(strtr($s, '-_', '+/') . str_repeat('=', (4 - strlen($s) % 4) % 4), true);
    if ($bin === false) erreur('encodage invalide');
    return $bin;
}
function jeton(): string { return b64url(random_bytes(32)); }

const CODE_ALPHABET = '0123456789ABCDEFGHJKMNPQRSTVWXYZ'; // base32 de Crockford : ni I, L, O, U

/** Clé d'appairage lisible : 10 caractères (50 bits), affichée en deux groupes de 5. */
function code_appairage(): string {
    $c = '';
    for ($i = 0; $i < 10; $i++) $c .= CODE_ALPHABET[random_int(0, 31)];
    return $c;
}

/** Saisie tolérante : minuscules, espaces et tirets, O pour 0, I ou L pour 1. */
function normaliser_code(string $saisie): string {
    return strtr(preg_replace('/[^A-Z0-9]/', '', strtoupper($saisie)), ['O' => '0', 'I' => '1', 'L' => '1']);
}
function h(string $jeton): string { return hash('sha256', $jeton); }

function corps(): array {
    if ($_SERVER['REQUEST_METHOD'] !== 'POST') return [];
    // Exiger du JSON : une requête inter-sites ne peut pas l'envoyer sans pré-vérification CORS (anti-CSRF).
    if (!str_starts_with($_SERVER['CONTENT_TYPE'] ?? '', 'application/json')) erreur('JSON attendu', 415);
    $data = json_decode(file_get_contents('php://input') ?: '{}', true);
    return is_array($data) ? $data : erreur('JSON invalide');
}

function hote(): string {
    global $CONFIG;
    return $CONFIG['hote'] ?? strtolower(explode(':', $_SERVER['HTTP_HOST'] ?? 'localhost')[0]);
}

function origine(): string {
    global $CONFIG;
    if (!empty($CONFIG['origine'])) return $CONFIG['origine'];
    $https = ($_SERVER['HTTPS'] ?? '') === 'on' || ($_SERVER['HTTP_X_FORWARDED_PROTO'] ?? '') === 'https';
    return ($https ? 'https://' : 'http://') . ($_SERVER['HTTP_HOST'] ?? 'localhost');
}

/** Chemin du cockpit sur le site (ex. /cockpit/), pour le cookie et le lien d'appairage. */
function base(): string {
    global $CONFIG;
    return $CONFIG['base'] ?? (rtrim(dirname($_SERVER['SCRIPT_NAME'] ?? '/'), '/') . '/');
}

function ip(): string { return $_SERVER['REMOTE_ADDR'] ?? '?'; }

function limiter_echecs(): void {
    [$max, $fenetre] = LIMITE_ECHECS_IP;
    q('DELETE FROM echecs WHERE ts < ?', [time() - $fenetre]);
    if ((int)q('SELECT COUNT(*) FROM echecs WHERE ip = ?', [ip()])->fetchColumn() >= $max) {
        erreur('trop de tentatives, réessaie dans 15 minutes', 429);
    }
}

function echec(string $message): never {
    q('INSERT INTO echecs(ip, ts) VALUES(?, ?)', [ip(), time()]);
    erreur($message, 401);
}

// ---------------------------------------------------------------- sessions (téléphone)

function ouvrir_session(): void {
    $jeton = jeton();
    q('INSERT INTO sessions(jeton_hash, cree_le, expire_le, appareil) VALUES(?, ?, ?, ?)',
      [h($jeton), time(), time() + SESSION_DUREE, substr($_SERVER['HTTP_USER_AGENT'] ?? '', 0, 200)]);
    poser_cookie($jeton, time() + SESSION_DUREE);
}

function poser_cookie(string $valeur, int $expire): void {
    setcookie(SESSION_COOKIE, $valeur, [
        'expires' => $expire, 'path' => base(), 'httponly' => true,
        'secure' => str_starts_with(origine(), 'https://'), 'samesite' => 'Strict',
    ]);
}

function session_valide(): bool {
    $jeton = $_COOKIE[SESSION_COOKIE] ?? '';
    if ($jeton === '') return false;
    $s = q('SELECT expire_le FROM sessions WHERE jeton_hash = ?', [h($jeton)])->fetch();
    if (!$s || $s['expire_le'] < time()) return false;
    if ($s['expire_le'] - time() < SESSION_DUREE - 86400) { // prolonge au plus une fois par jour
        q('UPDATE sessions SET expire_le = ? WHERE jeton_hash = ?', [time() + SESSION_DUREE, h($jeton)]);
        poser_cookie($jeton, time() + SESSION_DUREE);
    }
    return true;
}

function exiger_session(): void {
    if (!session_valide()) erreur('non authentifié', 401);
}

function exiger_worker(): void {
    global $CONFIG;
    $cle = preg_replace('/^Bearer\s+/i', '', $_SERVER['HTTP_AUTHORIZATION'] ?? $_SERVER['REDIRECT_HTTP_AUTHORIZATION'] ?? '');
    if ($cle === '' || !hash_equals($CONFIG['worker_cle_sha256'], h($cle))) {
        limiter_echecs();
        echec('clé worker invalide');
    }
}

// ---------------------------------------------------------------- WebAuthn (passkeys)

function nouveau_defi(string $type): string {
    q('DELETE FROM defis WHERE expire_le < ?', [time()]);
    $defi = b64url(random_bytes(32));
    q('INSERT INTO defis(defi, type, expire_le) VALUES(?, ?, ?)', [$defi, $type, time() + DEFI_DUREE]);
    return $defi;
}

/** Vérifie clientDataJSON (type, challenge à usage unique, origine) et renvoie son contenu brut. */
function verifier_client_data(string $clientDataB64, string $type): string {
    $brut = b64url_dec($clientDataB64);
    $cd = json_decode($brut, true);
    if (!is_array($cd) || ($cd['type'] ?? '') !== $type) echec('type WebAuthn invalide');
    if (($cd['origin'] ?? '') !== origine()) echec('origine invalide');
    $st = q('DELETE FROM defis WHERE defi = ? AND type = ? AND expire_le >= ?', [$cd['challenge'] ?? '', $type, time()]);
    if ($st->rowCount() !== 1) echec('challenge expiré ou inconnu');
    return $brut;
}

function pem_depuis_spki(string $der): string {
    return "-----BEGIN PUBLIC KEY-----\n" . chunk_split(base64_encode($der), 64, "\n") . "-----END PUBLIC KEY-----\n";
}

function utilisateur_id(): string {
    return reglage('webauthn_user_id') ?? reglage('webauthn_user_id', b64url(random_bytes(16)));
}

// ---------------------------------------------------------------- Web Push (sans contenu : le SW relit l'état)

function vapid(): array {
    $pem = reglage('vapid_prive');
    if (!$pem) {
        $cle = openssl_pkey_new(['curve_name' => 'prime256v1', 'private_key_type' => OPENSSL_KEYTYPE_EC]);
        openssl_pkey_export($cle, $pem);
        reglage('vapid_prive', $pem);
    }
    $ec = openssl_pkey_get_details(openssl_pkey_get_private($pem))['ec'];
    $pub = "\x04" . str_pad($ec['x'], 32, "\0", STR_PAD_LEFT) . str_pad($ec['y'], 32, "\0", STR_PAD_LEFT);
    return [$pem, b64url($pub)];
}

/** Signature ECDSA DER -> format brut r||s (64 octets) attendu par JWT ES256. */
function der_vers_brut(string $der): string {
    $pos = 2;
    if (ord($der[1]) & 0x80) $pos += ord($der[1]) & 0x7f;
    $out = '';
    for ($i = 0; $i < 2; $i++) {
        $len = ord($der[$pos + 1]);
        $int = ltrim(substr($der, $pos + 2, $len), "\0");
        $out .= str_pad($int, 32, "\0", STR_PAD_LEFT);
        $pos += 2 + $len;
    }
    return $out;
}

function notifier(): void {
    $abonnements = q('SELECT endpoint FROM abonnements_push')->fetchAll(PDO::FETCH_COLUMN);
    if (!$abonnements || !function_exists('curl_init')) return;
    [$pem, $pub] = vapid();
    foreach ($abonnements as $endpoint) {
        $p = parse_url($endpoint);
        $entete = b64url(json_encode(['typ' => 'JWT', 'alg' => 'ES256']));
        $charge = b64url(json_encode(['aud' => $p['scheme'] . '://' . $p['host'], 'exp' => time() + 43200, 'sub' => origine()]));
        openssl_sign("$entete.$charge", $sig, $pem, OPENSSL_ALGO_SHA256);
        $jwt = "$entete.$charge." . b64url(der_vers_brut($sig));
        $ch = curl_init($endpoint);
        curl_setopt_array($ch, [
            CURLOPT_POST => true, CURLOPT_POSTFIELDS => '', CURLOPT_RETURNTRANSFER => true, CURLOPT_TIMEOUT => 5,
            CURLOPT_HTTPHEADER => ['TTL: 3600', 'Urgency: high', 'Content-Length: 0', "Authorization: vapid t=$jwt, k=$pub"],
        ]);
        curl_exec($ch);
        $code = curl_getinfo($ch, CURLINFO_HTTP_CODE);
        curl_close($ch);
        if ($code === 404 || $code === 410) q('DELETE FROM abonnements_push WHERE endpoint = ?', [$endpoint]);
    }
}

// ---------------------------------------------------------------- état

function worker_en_ligne(): bool {
    return (bool)q('SELECT 1 FROM workers WHERE vu_le > ?', [time() - WORKER_EN_LIGNE])->fetchColumn();
}

/** Projets de tous les workers connus, avec leur worker et leur statut (en ligne ou non). */
function projets(): array {
    $out = [];
    foreach (q('SELECT id, nom, projets, vu_le FROM workers ORDER BY vu_le DESC')->fetchAll() as $w) {
        $enLigne = time() - (int)$w['vu_le'] < WORKER_EN_LIGNE;
        foreach (json_decode($w['projets'], true) ?: [] as $p) {
            if (isset($out[$p['id']]) && !$enLigne) continue; // un worker en ligne l'emporte
            $out[$p['id']] = $p + ['worker' => $w['id'], 'worker_name' => $w['nom'], 'online' => $enLigne];
        }
    }
    return array_values($out);
}

/** Worker qui héberge un projet (null si inconnu). */
function worker_du_projet(?string $projet): ?string {
    foreach (projets() as $p) if ($p['id'] === $projet) return $p['worker'];
    return null;
}

function inserer_evenement(string $tacheId, array $evt): void {
    q('INSERT INTO evenements(tache_id, donnees, ts) VALUES(?, ?, ?)',
      [$tacheId, json_encode($evt, JSON_UNESCAPED_UNICODE), time()]);
}

function tache_publique(array $t): array {
    $extra = json_decode($t['extra'] ?? '', true) ?: [];
    return [
        'id' => $t['id'], 'instruction' => $t['instruction'], 'project' => $t['projet'], 'status' => $t['statut'],
        'route_reason' => $t['raison_routage'], 'result' => $t['resultat'], 'cost_usd' => (float)$t['cout'],
        'created' => (int)$t['cree_le'], 'updated' => (int)$t['maj_le'], 'conversation' => $t['conversation'] ?? $t['id'],
        'reply_to' => $t['suite_de'] ?? null, 'worker' => $t['worker_id'] ?? null, 'parent' => $t['parent'] ?? null,
        'changes' => $extra['changes'] ?? null, 'worktree' => $extra['worktree'] ?? null,
        'duration_s' => $extra['duration_s'] ?? null, 'kind' => empty($t['action']) ? null : 'service',
        'next_steps' => $extra['next_steps'] ?? null, 'next_steps_dismissed' => !empty($extra['next_steps_dismissed']),
        'model' => $t['modele'] ?? null, 'group' => $t['groupe'] ?? null, 'tests' => $extra['tests'] ?? null,
        'waiting_project' => !empty($t['attente_projet']),
    ];
}

/** Orchestrateur en ligne (seul pod autorisé à lancer des services), ou null. */
function orchestrateur_en_ligne(): ?string {
    $id = q('SELECT id FROM workers WHERE orchestrateur = 1 AND vu_le > ? ORDER BY vu_le DESC LIMIT 1', [time() - WORKER_EN_LIGNE])->fetchColumn();
    return $id === false ? null : $id;
}

/** Bilan par projet sur une période : tâches, temps de travail, fichiers touchés, lignes, commits, activité par jour. */
function bilan(int $jours): array {
    $debut = strtotime('today') - ($jours - 1) * 86400;
    $projets = [];
    foreach (q('SELECT * FROM taches WHERE projet IS NOT NULL AND cree_le >= ? ORDER BY cree_le DESC, rowid DESC', [$debut])->fetchAll() as $t) {
        $p = &$projets[$t['projet']];
        $p ??= ['project' => $t['projet'], 'tasks' => 0, 'done' => 0, 'error' => 0, 'active' => 0, 'duration_s' => 0, 'cost' => 0.0,
                'added' => 0, 'removed' => 0, 'commits' => 0, 'files' => [], 'last' => 0, 'recent' => [], 'days' => array_fill(0, $jours, 0)];
        $x = json_decode($t['extra'] ?? '', true) ?: [];
        $p['tasks']++;
        if ($t['statut'] === 'done') $p['done']++;
        elseif ($t['statut'] === 'error') $p['error']++;
        elseif (!in_array($t['statut'], ['cancelled', 'needs_project'], true)) $p['active']++;
        $p['duration_s'] += (int)($x['duration_s'] ?? 0);
        $p['cost'] += (float)$t['cout'];
        $c = $x['changes'] ?? [];
        $p['added'] += (int)($c['added'] ?? 0);
        $p['removed'] += (int)($c['removed'] ?? 0);
        $p['commits'] += count($c['commits'] ?? []);
        foreach ($c['files'] ?? [] as $f) $p['files'][$f['path']] = ($p['files'][$f['path']] ?? 0) + 1;
        $p['last'] = max($p['last'], (int)$t['maj_le']);
        $jour = intdiv((int)$t['cree_le'] - $debut, 86400);
        if ($jour >= 0 && $jour < $jours) $p['days'][$jour]++;
        if (count($p['recent']) < 3) $p['recent'][] = ['id' => $t['id'], 'instruction' => mb_substr($t['instruction'], 0, 160),
                                                       'status' => $t['statut'], 'updated' => (int)$t['maj_le']];
        unset($p);
    }
    foreach ($projets as &$p) {
        arsort($p['files']);
        $p['file_count'] = count($p['files']);
        $p['top_files'] = array_map(fn($k, $v) => ['path' => (string)$k, 'count' => $v], array_keys(array_slice($p['files'], 0, 4, true)), array_slice($p['files'], 0, 4, true));
        unset($p['files']);
        $p['cost'] = round($p['cost'], 4);
    }
    unset($p);
    usort($projets, fn($a, $b) => $b['last'] <=> $a['last']);
    return ['days' => $jours, 'start' => $debut, 'projects' => array_values($projets)];
}

/** Pods pour le tableau de bord : état envoyé par chaque worker, et activité du jour (tâches, coût). */
function workers_publics(): array {
    $jour = strtotime('today');
    $stats = [];
    foreach (q('SELECT worker_id, COUNT(*) n, SUM(cout) cout FROM taches WHERE cree_le >= ? GROUP BY worker_id', [$jour])->fetchAll() as $r) {
        $stats[$r['worker_id'] ?? ''] = ['tasks_today' => (int)$r['n'], 'cost_today' => round((float)$r['cout'], 4)];
    }
    return array_map(fn($w) => [
        'id' => $w['id'], 'name' => $w['nom'], 'orchestrator' => (bool)$w['orchestrateur'],
        'online' => time() - (int)$w['vu_le'] < WORKER_EN_LIGNE, 'seen' => (int)$w['vu_le'],
        'projects' => array_column(json_decode($w['projets'], true) ?: [], 'id'),
        'state' => json_decode($w['etat'] ?? '', true) ?: null,
    ] + ($stats[$w['id']] ?? ['tasks_today' => 0, 'cost_today' => 0]),
        q('SELECT * FROM workers WHERE vu_le > ? ORDER BY orchestrateur DESC, nom', [time() - WORKERS_VISIBLES])->fetchAll());
}

/** Événement envoyé au téléphone : les diffs ne partent qu'à la demande (route « diff »), pour alléger le fil. */
function evenement_public(string $donnees): array {
    $e = json_decode($donnees, true);
    if (($e['type'] ?? '') === 'changes' && isset($e['patches'])) {
        $e['has_patches'] = (bool)$e['patches'];
        unset($e['patches']);
    }
    return $e;
}

/** Échanges précédents de la conversation (du plus ancien au plus récent), pour le routage et la reprise. */
function contexte(array $t): array {
    $lignes = q('SELECT instruction, resultat, projet FROM taches WHERE conversation = ? AND cree_le <= ? AND id != ?
                 ORDER BY cree_le DESC, rowid DESC LIMIT ' . CONTEXTE_ECHANGES, [$t['conversation'], $t['cree_le'], $t['id']])->fetchAll();
    return array_reverse(array_map(fn($l) => ['instruction' => mb_substr($l['instruction'], 0, 1500),
        'result' => mb_substr($l['resultat'], 0, 1500), 'project' => $l['projet']], $lignes));
}

/** Dernière session Claude de la conversation pour ce projet (reprise après redémarrage du worker). */
function session_conversation(array $t): ?string {
    if (!$t['projet']) return null;
    $s = q('SELECT session FROM taches WHERE conversation = ? AND projet = ? AND session IS NOT NULL AND id != ?
            ORDER BY maj_le DESC LIMIT 1', [$t['conversation'], $t['projet'], $t['id']])->fetchColumn();
    return $s === false ? null : $s;
}

function evenement_tache(string $id): void {
    $t = q('SELECT * FROM taches WHERE id = ?', [$id])->fetch();
    if ($t) inserer_evenement($id, ['kind' => 'task', 'task_id' => $id, 'task' => tache_publique($t)]);
}

// ---------------------------------------------------------------- routes

$route = $_GET['r'] ?? '';
$in = corps();

switch ($route) {

    // ----- authentification
    case 'moi':
        repondre([
            'authed' => session_valide(),
            'has_passkey' => (bool)q('SELECT 1 FROM passkeys LIMIT 1')->fetchColumn(),
            'worker_online' => worker_en_ligne(),
        ]);

    case 'appairer': // lien du QR code, ou clé d'appairage saisie (ordinateur), générés par « ./run.sh pair »
        limiter_echecs();
        if (isset($in['code'])) {
            $code = normaliser_code((string)$in['code']);
            if (strlen($code) !== 10) echec("clé d'appairage invalide : 10 caractères, par exemple K7P4Q-X2MDA");
            $st = q('DELETE FROM appairages WHERE code_hash = ? AND expire_le >= ?', [h($code), time()]);
            if ($st->rowCount() !== 1) echec("clé d'appairage incorrecte, expirée ou déjà utilisée");
        } else {
            $st = q('DELETE FROM appairages WHERE jeton_hash = ? AND expire_le >= ?', [h((string)($in['token'] ?? '')), time()]);
            if ($st->rowCount() !== 1) echec("lien d'appairage expiré ou déjà utilisé");
        }
        ouvrir_session();
        repondre(['ok' => true]);

    case 'passkey_options_creation':
        exiger_session();
        repondre([
            'challenge' => nouveau_defi('webauthn.create'),
            'rp' => ['id' => hote(), 'name' => 'Cockpit'],
            'user' => ['id' => utilisateur_id(), 'name' => 'cockpit@' . hote(), 'displayName' => 'Cockpit'],
            'excludeCredentials' => array_map(fn($id) => ['type' => 'public-key', 'id' => $id],
                q('SELECT id FROM passkeys')->fetchAll(PDO::FETCH_COLUMN)),
        ]);

    case 'passkey_creer':
        exiger_session();
        verifier_client_data((string)($in['clientDataJSON'] ?? ''), 'webauthn.create');
        if ((int)($in['alg'] ?? 0) !== -7) erreur('algorithme non pris en charge (ES256 requis)');
        $pem = pem_depuis_spki(b64url_dec((string)($in['publicKey'] ?? '')));
        if (!openssl_pkey_get_public($pem)) erreur('clé publique invalide');
        $id = (string)($in['id'] ?? '');
        if (!preg_match('/^[A-Za-z0-9_-]{16,1024}$/', $id)) erreur('identifiant invalide');
        $nom = mb_substr(trim((string)($in['name'] ?? '')) ?: 'Appareil', 0, 60);
        q('INSERT OR REPLACE INTO passkeys(id, cle_publique, nom, cree_le) VALUES(?, ?, ?, ?)', [$id, $pem, $nom, time()]);
        repondre(['ok' => true]);

    case 'passkey_options_connexion':
        limiter_echecs();
        repondre(['challenge' => nouveau_defi('webauthn.get'), 'rpId' => hote()]);

    case 'passkey_connexion':
        limiter_echecs();
        $pk = q('SELECT * FROM passkeys WHERE id = ?', [(string)($in['id'] ?? '')])->fetch();
        if (!$pk) echec('passkey inconnue');
        $clientData = verifier_client_data((string)($in['clientDataJSON'] ?? ''), 'webauthn.get');
        $authData = b64url_dec((string)($in['authenticatorData'] ?? ''));
        if (strlen($authData) < 37 || !hash_equals(hash('sha256', hote(), true), substr($authData, 0, 32))) echec('rpId invalide');
        $flags = ord($authData[32]);
        if (!($flags & 0x01) || !($flags & 0x04)) echec('vérification biométrique requise');
        $ok = openssl_verify($authData . hash('sha256', $clientData, true), b64url_dec((string)($in['signature'] ?? '')),
                             $pk['cle_publique'], OPENSSL_ALGO_SHA256);
        if ($ok !== 1) echec('signature invalide');
        q('UPDATE passkeys SET utilisee_le = ? WHERE id = ?', [time(), $pk['id']]);
        ouvrir_session();
        repondre(['ok' => true]);

    case 'deconnexion':
        q('DELETE FROM sessions WHERE jeton_hash = ?', [h($_COOKIE[SESSION_COOKIE] ?? '')]);
        poser_cookie('', 1);
        repondre(['ok' => true]);

    case 'appareils':
        exiger_session();
        repondre(q('SELECT id, nom, cree_le, utilisee_le FROM passkeys ORDER BY cree_le')->fetchAll());

    case 'appareil_supprimer':
        exiger_session();
        q('DELETE FROM passkeys WHERE id = ?', [(string)($in['id'] ?? '')]);
        repondre(['ok' => true]);

    // ----- notifications
    case 'vapid':
        exiger_session();
        repondre(['key' => vapid()[1]]);

    case 'push_abonner':
        exiger_session();
        $endpoint = (string)($in['endpoint'] ?? '');
        if (!preg_match('#^https://[^\s]+$#', $endpoint)) erreur('abonnement invalide');
        q('INSERT OR IGNORE INTO abonnements_push(endpoint, cree_le) VALUES(?, ?)', [$endpoint, time()]);
        repondre(['ok' => true]);

    // ----- téléphone
    case 'etat':
        exiger_session();
        $depuis = (int)($_GET['since'] ?? 0);
        if ($depuis > 0) {
            $evts = q('SELECT id, donnees FROM evenements WHERE id > ? ORDER BY id LIMIT 1000', [$depuis])->fetchAll();
        } else {
            $evts = q('SELECT id, donnees FROM evenements WHERE tache_id IN
                         (SELECT id FROM taches ORDER BY cree_le DESC LIMIT ' . TACHES_VISIBLES . ') ORDER BY id')->fetchAll();
        }
        $dernier = $evts ? (int)end($evts)['id'] : max($depuis, (int)q('SELECT COALESCE(MAX(id), 0) FROM evenements')->fetchColumn());
        repondre([
            'events' => array_map(fn($e) => evenement_public($e['donnees']), $evts),
            'last_id' => $dernier,
            'projects' => projets(),
            'worker_online' => worker_en_ligne(),
            'pending' => (int)q('SELECT COUNT(*) FROM validations WHERE decision IS NULL')->fetchColumn(),
            'workers' => workers_publics(),
            'trust' => (object)confiances(),
        ]);

    case 'diff': // diffs par fichier des modifications d'une tâche
        exiger_session();
        $d = q("SELECT donnees FROM evenements WHERE tache_id = ? AND donnees LIKE '%\"type\":\"changes\"%' ORDER BY id DESC LIMIT 1",
               [(string)($_GET['task'] ?? '')])->fetchColumn();
        repondre(['patches' => $d === false ? (object)[] : ((json_decode($d, true)['patches'] ?? null) ?: (object)[])]);

    case 'bilan':
        exiger_session();
        $jours = (int)($_GET['jours'] ?? 7);
        repondre(bilan(in_array($jours, [1, 7, 30], true) ? $jours : 7));

    case 'depots': // dépôts git de l'utilisateur, publiés par l'orchestrateur (formulaire « Nouveau pod »)
        exiger_session();
        repondre(['depots' => json_decode(reglage('depots') ?? '[]', true) ?: [], 'maj' => (int)(reglage('depots_maj') ?? 0),
                  'orchestrateur' => orchestrateur_en_ligne() !== null]);

    case 'lancer_pod': // nouveau pod Onyxia sur un dépôt git : exécuté directement par l'orchestrateur, sans agent
        exiger_session();
        $nom = (string)($in['nom'] ?? '');
        // Nouveau dépôt GitHub : dépôt et branche saisis ignorés (le pod clone le dépôt créé)
        $depot = empty($in['creer_depot']) ? trim((string)($in['depot_git'] ?? '')) : '';
        $branche = empty($in['creer_depot']) ? trim((string)($in['branche'] ?? '')) : '';
        if (!preg_match('/^[a-z0-9](?:[a-z0-9-]{0,28}[a-z0-9])?$/', $nom)) erreur('nom invalide : minuscules, chiffres et tirets (30 au plus)');
        if ($depot !== '' && (!preg_match('#^https://[A-Za-z0-9._~/-]+$#', $depot) || str_contains($depot, '..'))) erreur('dépôt invalide : URL https:// sans identifiants');
        if ($branche !== '' && !preg_match('#^[A-Za-z0-9._/-]{1,100}$#', $branche)) erreur('branche invalide');
        if (in_array($nom, array_column(projets(), 'id'), true)) erreur("un projet « $nom » existe déjà");
        $nouveauDepot = null;
        if (!empty($in['creer_depot'])) {
            $nd = (array)$in['creer_depot'];
            $nomDepot = trim((string)($nd['nom'] ?? $nom));
            if (!preg_match('/^[A-Za-z0-9._-]{1,100}$/', $nomDepot) || str_ends_with($nomDepot, '.git')) erreur('nom de dépôt invalide');
            $nouveauDepot = ['nom' => $nomDepot, 'prive' => (bool)($nd['prive'] ?? true)];
        }
        $orch = orchestrateur_en_ligne() ?? erreur('orchestrateur hors ligne : lance ./run.sh dans le pod admin', 409);
        $action = ['type' => 'lancer_service', 'nom' => $nom, 'depot_git' => $depot, 'branche' => $branche,
                   'cpu' => (string)($in['cpu'] ?? '2'), 'memoire' => (string)($in['memoire'] ?? '8Gi'),
                   'disque' => (string)($in['disque'] ?? '10Gi'), 'gpu' => !empty($in['gpu'])]
                 + ($nouveauDepot ? ['creer_depot' => $nouveauDepot] : []);
        $id = bin2hex(random_bytes(5));
        $source = $nouveauDepot ? 'nouveau dépôt GitHub ' . ($nouveauDepot['prive'] ? 'privé ' : 'public ') . "« {$nouveauDepot['nom']} »" : ($depot !== '' ? preg_replace('#^https://(www\.)?github\.com/#', '', $depot) . ($branche !== '' ? " ($branche)" : '') : 'dossier vide');
        q('INSERT INTO taches(id, instruction, cree_le, maj_le, worker_id, conversation, action) VALUES(?, ?, ?, ?, ?, ?, ?)',
          [$id, "🚀 Nouveau pod « $nom » : $source", time(), time(), $orch, $id, json_encode($action, JSON_UNESCAPED_UNICODE)]);
        evenement_tache($id);
        repondre(tache_publique(q('SELECT * FROM taches WHERE id = ?', [$id])->fetch()));

    case 'confiance': // mode confiance d'une conversation, pour une durée limitée (0 : désactivé)
        exiger_session();
        $minutes = (int)($in['minutes'] ?? -1);
        if (!in_array($minutes, CONFIANCE_DUREES, true)) erreur('durée invalide : ' . implode(', ', CONFIANCE_DUREES) . ' minutes');
        $conv = (string)($in['conversation'] ?? '');
        if (!q('SELECT 1 FROM taches WHERE conversation = ? OR id = ?', [$conv, $conv])->fetchColumn()) erreur('conversation introuvable', 404);
        if ($minutes === 0) q('DELETE FROM confiances WHERE conversation = ?', [$conv]);
        else q('INSERT INTO confiances(conversation, jusqua) VALUES(?, ?) ON CONFLICT(conversation) DO UPDATE SET jusqua = excluded.jusqua',
               [$conv, time() + 60 * $minutes]);
        q('INSERT INTO audit(donnees, ts) VALUES(?, ?)', [json_encode(['ts' => date('Y-m-d\\TH:i:s'), 'event' => 'trust',
            'conversation' => $conv, 'minutes' => $minutes], JSON_UNESCAPED_UNICODE), time()]);
        repondre(['ok' => true, 'trust' => (object)confiances()]);

    case 'recadrer': // message envoyé à Claude pendant qu'il travaille : il est interrompu et reprend avec
        exiger_session();
        $texte = trim((string)($in['message'] ?? ''));
        if ($texte === '' || mb_strlen($texte) > 4000) erreur('message vide ou trop long');
        $t = q('SELECT * FROM taches WHERE id = ?', [(string)($in['task_id'] ?? '')])->fetch() ?: erreur('tâche introuvable', 404);
        if (!in_array($t['statut'], ACTIFS, true)) erreur('la tâche est terminée : envoie plutôt une suite', 409);
        $file = json_decode($t['recadrages'] ?? '', true) ?: [];
        $file[] = $texte;
        q('UPDATE taches SET recadrages = ? WHERE id = ?', [json_encode($file, JSON_UNESCAPED_UNICODE), $t['id']]);
        inserer_evenement($t['id'], ['kind' => 'message', 'task_id' => $t['id'], 'type' => 'steer', 'text' => $texte, 'ts' => time()]);
        repondre(['ok' => true]);

    case 'image': // capture d'écran envoyée par un agent
        exiger_session();
        $id = (string)($_GET['id'] ?? '');
        $f = __DIR__ . "/data/images/$id.jpg";
        if (!preg_match('/^[0-9a-f]{12}$/', $id) || !is_file($f)) erreur('image introuvable', 404);
        header('Content-Type: image/jpeg');
        header('Cache-Control: private, max-age=31536000, immutable');
        header('Content-Length: ' . filesize($f));
        readfile($f);
        exit;

    case 'memoire': // CLAUDE.md d'un projet, publié par son worker
        exiger_session();
        $m = json_decode(reglage('memoire:' . (string)($_GET['project'] ?? '')) ?? 'null', true);
        repondre($m ?: ['content' => '', 'hash' => '', 'maj' => 0, 'absent' => true]);

    case 'memoire_maj':
        exiger_session();
        $projet = (string)($in['project'] ?? '');
        $contenu = (string)($in['content'] ?? '');
        if (strlen($contenu) > MEMOIRE_MAX) erreur('mémoire trop longue');
        $m = json_decode(reglage("memoire:$projet") ?? 'null', true) ?: erreur('projet inconnu ou mémoire pas encore publiée', 404);
        if (($in['hash'] ?? '') !== $m['hash']) erreur('la mémoire a changé entre-temps : recharge-la', 409);
        reglage("memoire_edit:$projet", json_encode(['content' => $contenu, 'ts' => time()], JSON_UNESCAPED_UNICODE));
        repondre(['ok' => true]);

    case 'commandes': // commandes rapides : liste commune (« * ») et listes par projet
        exiger_session();
        repondre(json_decode(reglage('commandes') ?? 'null', true) ?: (object)[]);

    case 'commandes_maj':
        exiger_session();
        $cle = (string)($in['project'] ?? '*');
        $liste = $in['commandes'] ?? null;
        if (!is_array($liste) || count($liste) > COMMANDES_MAX) erreur('liste invalide');
        $propre = [];
        foreach ($liste as $c) {
            $titre = trim((string)($c['titre'] ?? ''));
            $instr = trim((string)($c['instruction'] ?? ''));
            if ($titre === '' || $instr === '' || mb_strlen($titre) > 60 || mb_strlen($instr) > 4000) erreur('commande invalide');
            $propre[] = ['titre' => $titre, 'instruction' => $instr];
        }
        $toutes = json_decode(reglage('commandes') ?? 'null', true) ?: [];
        $toutes[$cle] = $propre;
        reglage('commandes', json_encode($toutes, JSON_UNESCAPED_UNICODE));
        repondre(['ok' => true]);

    case 'garder_variante': // variantes : la choisie est fusionnée, les autres jetées
        exiger_session();
        $t = q('SELECT * FROM taches WHERE id = ?', [(string)($in['task_id'] ?? '')])->fetch() ?: erreur('tâche introuvable', 404);
        if (!$t['groupe']) erreur("cette tâche n'est pas une variante");
        $groupe = q('SELECT * FROM taches WHERE groupe = ?', [$t['groupe']])->fetchAll();
        foreach ($groupe as $g) if (in_array($g['statut'], ACTIFS, true)) erreur('une variante travaille encore', 409);
        foreach ($groupe as $g) {
            // Dernière tâche de chaque conversation (une variante a pu recevoir des suites) qui porte le worktree
            $der = q("SELECT * FROM taches WHERE conversation = ? AND extra LIKE '%\"worktree\"%' ORDER BY cree_le DESC, rowid DESC LIMIT 1",
                     [$g['conversation'] ?? $g['id']])->fetch();
            $etat = $der ? (json_decode($der['extra'], true)['worktree']['state'] ?? '') : '';
            if ($der && $etat === 'active') q('UPDATE taches SET action_worktree = ? WHERE id = ?', [$g['id'] === $t['id'] ? 'merge' : 'discard', $der['id']]);
        }
        repondre(['ok' => true]);

    case 'ignorer_suite': // les prochaines étapes proposées ne sont plus suivies (tableau de bord)
        exiger_session();
        $t = q('SELECT * FROM taches WHERE id = ?', [(string)($in['task_id'] ?? '')])->fetch() ?: erreur('tâche introuvable', 404);
        $extra = json_decode($t['extra'] ?? '', true) ?: [];
        $extra['next_steps_dismissed'] = true;
        q('UPDATE taches SET extra = ? WHERE id = ?', [json_encode($extra, JSON_UNESCAPED_UNICODE), $t['id']]);
        evenement_tache($t['id']);
        repondre(['ok' => true]);

    case 'worktree': // fusionner ou jeter la branche d'une tâche lancée en parallèle
        exiger_session();
        $action = (string)($in['action'] ?? '');
        if (!in_array($action, ['merge', 'discard'], true)) erreur('action invalide');
        $t = q('SELECT * FROM taches WHERE id = ?', [(string)($in['task_id'] ?? '')])->fetch() ?: erreur('tâche introuvable', 404);
        if (!(json_decode($t['extra'] ?? '', true)['worktree'] ?? null)) erreur('cette tâche n\'a pas de worktree');
        if (in_array($t['statut'], ['pending', 'routing', 'queued', 'running', 'waiting_approval'], true)) erreur('tâche en cours', 409);
        q('UPDATE taches SET action_worktree = ? WHERE id = ?', [$action, $t['id']]);
        repondre(['ok' => true]);

    case 'resume': // appelé par le service worker à la réception d'une notification push
        exiger_session();
        $validations = array_map(function ($d) {
            $a = json_decode($d, true);
            $i = $a['tool_input'] ?? [];
            // Aperçu de ce qui sera exécuté (corps de la notification, avec les boutons Approuver / Refuser)
            $apercu = $i['command'] ?? $i['file_path'] ?? $i['url'] ?? (isset($i['nom']) ? (string)$i['nom'] : '');
            return ['id' => $a['id'] ?? '', 'tool' => $a['tool'] ?? '', 'reason' => $a['reason'] ?? '', 'task_id' => $a['task_id'] ?? '',
                    'preview' => mb_substr((string)$apercu, 0, 160)];
        }, q('SELECT donnees FROM validations WHERE decision IS NULL ORDER BY cree_le')->fetchAll(PDO::FETCH_COLUMN));
        $pods = array_map(fn($w) => ['id' => $w['id'], 'name' => $w['nom'], 'since' => (int)$w['apparu_le']],
            q('SELECT id, nom, apparu_le FROM workers WHERE apparu_le > ? AND orchestrateur = 0 AND vu_le > ?',
              [time() - 600, time() - WORKER_EN_LIGNE])->fetchAll());
        $terminees = array_map('tache_publique', q("SELECT * FROM taches WHERE statut IN ('done','error','needs_project')
            AND maj_le > ? ORDER BY maj_le DESC LIMIT 3", [time() - 300])->fetchAll());
        $ci = [];
        foreach (q("SELECT tache_id, donnees FROM evenements WHERE ts > ? AND donnees LIKE '%\"type\":\"ci\"%' ORDER BY id", [time() - 900])->fetchAll() as $e) {
            $d = json_decode($e['donnees'], true);
            $ci[$e['tache_id']] = $d;  // dernier état de la CI par tâche
        }
        $echecs = [];
        foreach ($ci as $tid => $d) {
            if (($d['state'] ?? '') !== 'failure') continue;
            $t = q('SELECT instruction FROM taches WHERE id = ?', [$tid])->fetch();
            $echecs[] = ['task_id' => $tid, 'sha' => $d['sha'] ?? '', 'url' => $d['url'] ?? '', 'instruction' => mb_substr($t['instruction'] ?? '', 0, 120),
                         'checks' => array_values(array_map(fn($c) => $c['name'], array_filter($d['checks'] ?? [], fn($c) => !in_array($c['conclusion'] ?? null, [null, 'success', 'neutral', 'skipped'], true))))];
        }
        repondre(['approvals' => $validations, 'finished' => $terminees, 'new_pods' => $pods, 'ci_failures' => $echecs]);

    case 'tache':
        exiger_session();
        $instruction = trim((string)($in['instruction'] ?? ''));
        if ($instruction === '' || mb_strlen($instruction) > 10000) erreur('instruction vide ou trop longue');
        $projet = $in['project'] ?? null;
        if ($projet !== null && !in_array($projet, array_column(projets(), 'id'), true)) erreur('projet inconnu');
        $modele = $in['model'] ?? null;
        if ($modele !== null && !in_array($modele, MODELES, true)) erreur('modèle inconnu');
        $variantes = max(1, (int)($in['variants'] ?? 1));
        if ($variantes > VARIANTES_MAX) erreur('au plus ' . VARIANTES_MAX . ' variantes');
        $id = bin2hex(random_bytes(5));
        $conversation = $id;
        $suite = null;
        if (!empty($in['reply_to'])) {
            // Suite d'une conversation : même projet et même modèle par défaut, la session Claude sera reprise.
            $parent = q('SELECT id, projet, conversation, modele FROM taches WHERE id = ?', [(string)$in['reply_to']])->fetch() ?: erreur('conversation introuvable');
            $suite = $parent['id'];
            $conversation = $parent['conversation'] ?? $parent['id'];
            $projet ??= $parent['projet'];
            $modele ??= $parent['modele'];
            if ($variantes > 1) erreur('les variantes démarrent une nouvelle conversation');
        }
        if ($variantes > 1 && $projet === null) erreur('choisis un projet pour lancer des variantes');
        // Variantes : une conversation chacune, toutes dans un worktree dédié, à comparer puis garder ou jeter.
        $ids = [$id];
        for ($k = 2; $k <= $variantes; $k++) $ids[] = bin2hex(random_bytes(5));
        db()->beginTransaction();
        foreach ($ids as $k => $tid) {
            $texte = $variantes > 1 ? $instruction . "\n\n(Variante " . ($k + 1) . "/$variantes : d'autres agents Claude traitent la même "
                . "demande en parallèle, chacun dans sa branche. Prends une approche qui te soit propre, va au bout et commite ; "
                . "si le résultat est visuel, joins une capture avec capturer_ecran.)" : $instruction;
            q('INSERT INTO taches(id, instruction, projet, nouvelle_session, cree_le, maj_le, worker_id, conversation, suite_de, modele, isoler, groupe)
               VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
              [$tid, $texte, $projet, empty($in['new_session']) ? 0 : 1, time(), time(), worker_du_projet($projet),
               $k ? $tid : $conversation, $k ? null : $suite, $modele, $variantes > 1 ? 1 : null, $variantes > 1 ? $id : null]);
            evenement_tache($tid);
        }
        db()->commit();
        repondre(tache_publique(q('SELECT * FROM taches WHERE id = ?', [$id])->fetch()) + ['variants' => $ids]);

    case 'annuler':
        exiger_session();
        q("UPDATE taches SET annulation = 1 WHERE id = ? AND statut NOT IN ('done','error','cancelled')", [(string)($in['task_id'] ?? '')]);
        // Une tâche jamais prise en charge par le worker est annulée directement.
        $id = (string)($in['task_id'] ?? '');
        if (q("UPDATE taches SET statut = 'cancelled', livree = 1, maj_le = ? WHERE id = ? AND livree = 0", [time(), $id])->rowCount()) {
            evenement_tache($id);
        }
        repondre(['ok' => true]);

    case 'choisir_projet':
        exiger_session();
        $projet = (string)($in['project'] ?? '');
        if (!in_array($projet, array_column(projets(), 'id'), true)) erreur('projet inconnu');
        $st = q("UPDATE taches SET choix_projet = ? WHERE id = ? AND statut = 'needs_project'", [$projet, (string)($in['task_id'] ?? '')]);
        if (!$st->rowCount()) erreur('choix impossible');
        repondre(['ok' => true]);

    case 'decider':
        exiger_session();
        $decision = (string)($in['decision'] ?? '');
        if (!in_array($decision, ['approve', 'deny'], true)) erreur('décision invalide');
        $st = q('UPDATE validations SET decision = ? WHERE id = ? AND decision IS NULL', [$decision, (string)($in['approval_id'] ?? '')]);
        if (!$st->rowCount()) erreur('validation expirée ou déjà traitée', 409);
        repondre(['ok' => true]);

    case 'audit':
        exiger_session();
        $lim = min(500, max(1, (int)($_GET['limit'] ?? 150)));
        repondre(array_map(fn($r) => json_decode($r, true),
            q("SELECT donnees FROM audit ORDER BY id DESC LIMIT $lim")->fetchAll(PDO::FETCH_COLUMN)));

    // ----- worker (pod Onyxia)
    case 'w_appairage':
        exiger_worker();
        $jeton = jeton();
        q('DELETE FROM appairages WHERE expire_le < ?', [time()]);
        $code = code_appairage();
        q('INSERT INTO appairages(jeton_hash, code_hash, expire_le) VALUES(?, ?, ?)', [h($jeton), h($code), time() + APPAIRAGE_DUREE]);
        repondre(['url' => origine() . base() . '#pair=' . $jeton, 'code' => substr($code, 0, 5) . '-' . substr($code, 5),
                  'expires_in' => APPAIRAGE_DUREE]);

    case 'w_poll':
        exiger_worker();
        set_time_limit(LONG_POLL + 15);
        // Chaque worker a un identifiant stable (par pod) ; chaque processus a une instance aléatoire : un long-poll
        // resté ouvert côté serveur après l'arrêt d'un processus (PHP ne détecte pas la déconnexion) ne doit plus
        // livrer de tâches. Un nouveau processus du même worker remplace l'ancien.
        $wid = (string)($in['worker_id'] ?? 'principal');
        if (!preg_match('/^[A-Za-z0-9_.-]{1,64}$/', $wid)) erreur('identifiant de worker invalide');
        $instance = (string)($in['instance'] ?? '');
        $orchestrateur = ($in['orchestrateur'] ?? true) ? 1 : 0;
        $nouveauPod = !empty($in['hello']) && !q('SELECT 1 FROM workers WHERE id = ?', [$wid])->fetchColumn();
        if (!empty($in['hello'])) {
            q('INSERT INTO workers(id, nom, instance, orchestrateur, vu_le, apparu_le) VALUES(?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET nom = excluded.nom, instance = excluded.instance,
                 orchestrateur = excluded.orchestrateur, vu_le = excluded.vu_le',
              [$wid, mb_substr((string)($in['name'] ?? $wid), 0, 80), $instance, $orchestrateur, time(), time()]);
            if ($nouveauPod && !$orchestrateur) notifier(); // « Pod connecté » : le projet est prêt
        }
        if (q('SELECT instance FROM workers WHERE id = ?', [$wid])->fetchColumn() !== $instance) repondre(['superseded' => true]);
        if (isset($in['projects'])) {
            q('UPDATE workers SET projets = ? WHERE id = ?', [json_encode($in['projects'], JSON_UNESCAPED_UNICODE), $wid]);
            // Tâches qui attendaient l'un de ces projets (pod tout juste lancé) : confiées à ce worker
            foreach ($in['projects'] as $pr) {
                foreach (q('SELECT id FROM taches WHERE attente_projet = 1 AND projet = ?', [(string)($pr['id'] ?? '')])->fetchAll(PDO::FETCH_COLUMN) as $tid) {
                    q('UPDATE taches SET worker_id = ?, attente_projet = NULL, maj_le = ? WHERE id = ?', [$wid, time(), $tid]);
                    evenement_tache($tid);
                }
            }
        }
        if (isset($in['state']) && is_array($in['state'])) {
            $etat = json_encode($in['state'], JSON_UNESCAPED_UNICODE);
            if (strlen($etat) <= 64000) q('UPDATE workers SET etat = ? WHERE id = ?', [$etat, $wid]);
        }
        if (!empty($in['hello'])) {
            // Ce worker vient de (re)démarrer : ce qu'il exécutait est perdu.
            foreach (q("SELECT id FROM taches WHERE livree = 1 AND worker_id = ? AND statut NOT IN ('done','error','cancelled','needs_project')", [$wid])->fetchAll(PDO::FETCH_COLUMN) as $id) {
                q("UPDATE taches SET statut = 'error', resultat = 'Interrompue : le worker a redémarré.', maj_le = ? WHERE id = ?", [time(), $id]);
                evenement_tache($id);
            }
            foreach (q('SELECT v.id, v.tache_id, v.donnees FROM validations v JOIN taches t ON t.id = v.tache_id
                        WHERE v.decision IS NULL AND t.worker_id = ?', [$wid])->fetchAll() as $v) {
                $a = json_decode($v['donnees'], true);
                $a['decision'] = 'timeout';
                q("UPDATE validations SET decision = 'timeout', decision_livree = 1, donnees = ? WHERE id = ?",
                  [json_encode($a, JSON_UNESCAPED_UNICODE), $v['id']]);
                inserer_evenement($v['tache_id'], ['kind' => 'approval', 'task_id' => $v['tache_id'], 'approval' => $a]);
            }
        }
        // Tâches à livrer : celles de ce worker, et (orchestrateur) celles sans worker, à router.
        $mienne = $orchestrateur ? '(worker_id = ? OR (worker_id IS NULL AND attente_projet IS NULL))' : 'worker_id = ?';
        $fin = microtime(true) + ($in['wait'] ?? LONG_POLL);
        do {
            db()->beginTransaction();
            if (q('SELECT instance FROM workers WHERE id = ?', [$wid])->fetchColumn() !== $instance) {
                db()->rollBack();
                repondre(['superseded' => true]);
            }
            q('UPDATE workers SET vu_le = ? WHERE id = ?', [time(), $wid]);
            $taches = q("SELECT * FROM taches WHERE livree = 0 AND statut = 'pending' AND $mienne", [$wid])->fetchAll();
            foreach ($taches as $t) q('UPDATE taches SET livree = 1, worker_id = COALESCE(worker_id, ?) WHERE id = ?', [$wid, $t['id']]);
            $decisions = q('SELECT v.id, v.decision FROM validations v JOIN taches t ON t.id = v.tache_id
                            WHERE v.decision IS NOT NULL AND v.decision_livree = 0 AND t.worker_id = ?', [$wid])->fetchAll();
            foreach ($decisions as $d) q('UPDATE validations SET decision_livree = 1 WHERE id = ?', [$d['id']]);
            $annulations = q('SELECT id FROM taches WHERE annulation = 1 AND worker_id = ?', [$wid])->fetchAll(PDO::FETCH_COLUMN);
            q('UPDATE taches SET annulation = 0 WHERE annulation = 1 AND worker_id = ?', [$wid]);
            $choix = q('SELECT id, choix_projet FROM taches WHERE choix_projet IS NOT NULL AND worker_id = ?', [$wid])->fetchAll();
            q('UPDATE taches SET choix_projet = NULL WHERE choix_projet IS NOT NULL AND worker_id = ?', [$wid]);
            $worktrees = q('SELECT id, projet, conversation, action_worktree FROM taches WHERE action_worktree IS NOT NULL AND worker_id = ?', [$wid])->fetchAll();
            q('UPDATE taches SET action_worktree = NULL WHERE action_worktree IS NOT NULL AND worker_id = ?', [$wid]);
            $recadrages = [];
            foreach (q('SELECT id, recadrages FROM taches WHERE recadrages IS NOT NULL AND worker_id = ? AND livree = 1', [$wid])->fetchAll() as $r) {
                foreach (json_decode($r['recadrages'], true) ?: [] as $texte) $recadrages[] = ['task_id' => $r['id'], 'text' => $texte];
            }
            q('UPDATE taches SET recadrages = NULL WHERE recadrages IS NOT NULL AND worker_id = ? AND livree = 1', [$wid]);
            $memoires = [];
            foreach (json_decode((string)q('SELECT projets FROM workers WHERE id = ?', [$wid])->fetchColumn(), true) ?: [] as $p) {
                if ($e = reglage('memoire_edit:' . $p['id'])) {
                    $memoires[] = ['project' => $p['id']] + json_decode($e, true);
                    q('DELETE FROM reglages WHERE cle = ?', ['memoire_edit:' . $p['id']]);
                }
            }
            db()->commit();
            if ($taches || $decisions || $annulations || $choix || $worktrees || $recadrages || $memoires) break;
            usleep(500000);
        } while (microtime(true) < $fin && !connection_aborted());
        repondre([
            'tasks' => array_map(fn($t) => ['id' => $t['id'], 'instruction' => $t['instruction'],
                'project' => $t['projet'], 'new_session' => (bool)$t['nouvelle_session'],
                'conversation' => $t['conversation'] ?? $t['id'], 'context' => $t['conversation'] ? contexte($t) : [],
                'resume_session' => $t['conversation'] ? session_conversation($t) : null, 'parent' => $t['parent'] ?? null,
                'action' => empty($t['action']) ? null : json_decode($t['action'], true),
                'model' => $t['modele'] ?? null, 'isolate' => !empty($t['isoler']), 'group' => $t['groupe'] ?? null], $taches),
            'steers' => $recadrages, 'memory_edits' => $memoires, 'trust' => (object)confiances(),
            'decisions' => $decisions, 'cancels' => $annulations,
            'choices' => array_map(fn($c) => ['task_id' => $c['id'], 'project' => $c['choix_projet']], $choix),
            'worktree_actions' => array_map(fn($w) => ['task_id' => $w['id'], 'project' => $w['projet'],
                'conversation' => $w['conversation'] ?? $w['id'], 'action' => $w['action_worktree']], $worktrees),
            // Liste globale : l'orchestrateur route les tâches sans projet parmi tous les projets.
            'all_projects' => $orchestrateur ? projets() : [],
        ]);

    case 'w_affecter': // l'orchestrateur confie une tâche routée au worker qui héberge le projet
        exiger_worker();
        $id = (string)($in['task_id'] ?? '');
        $projet = (string)($in['project'] ?? '');
        $cible = worker_du_projet($projet) ?? erreur('projet inconnu');
        $st = q("UPDATE taches SET projet = ?, worker_id = ?, livree = 0, statut = 'pending', maj_le = ?
                 WHERE id = ? AND statut NOT IN ('done','error','cancelled')", [$projet, $cible, time(), $id]);
        if (!$st->rowCount()) erreur('tâche introuvable ou terminée', 404);
        evenement_tache($id);
        repondre(['ok' => true, 'worker' => $cible]);

    case 'w_image':
        exiger_worker();
        $tacheId = (string)($in['task_id'] ?? '');
        $t = q('SELECT worker_id FROM taches WHERE id = ?', [$tacheId])->fetch() ?: erreur('tâche introuvable', 404);
        if ($t['worker_id'] !== (string)($in['worker_id'] ?? '')) erreur('tâche d\'un autre worker', 403);
        $jpeg = base64_decode((string)($in['data'] ?? ''), true);
        if ($jpeg === false || strlen($jpeg) > IMAGE_MAX || !str_starts_with($jpeg, "\xFF\xD8\xFF")) erreur('image JPEG invalide ou trop lourde');
        $dir = __DIR__ . '/data/images';
        if (!is_dir($dir)) mkdir($dir, 0700, true);
        $id = bin2hex(random_bytes(6));
        file_put_contents("$dir/$id.jpg", $jpeg);
        inserer_evenement($tacheId, ['kind' => 'message', 'task_id' => $tacheId, 'type' => 'image', 'image' => $id,
                                     'caption' => mb_substr((string)($in['caption'] ?? ''), 0, 200), 'ts' => time()]);
        repondre(['id' => $id]);

    case 'w_memoire':
        exiger_worker();
        $contenu = (string)($in['content'] ?? '');
        if (strlen($contenu) > MEMOIRE_MAX) erreur('mémoire trop longue');
        reglage('memoire:' . (string)($in['project'] ?? ''), json_encode(['content' => $contenu, 'hash' => (string)($in['hash'] ?? ''),
                'maj' => time(), 'worker' => (string)($in['worker_id'] ?? '')], JSON_UNESCAPED_UNICODE));
        repondre(['ok' => true]);

    case 'w_depots':
        exiger_worker();
        $depots = array_slice(is_array($in['depots'] ?? null) ? $in['depots'] : [], 0, 300);
        reglage('depots', json_encode($depots, JSON_UNESCAPED_UNICODE));
        reglage('depots_maj', (string)time());
        repondre(['ok' => true]);

    case 'w_taches': // tâches filles créées par un agent (lancer_en_parallele) : une conversation chacune
        exiger_worker();
        $parent = q('SELECT * FROM taches WHERE id = ?', [(string)($in['parent'] ?? '')])->fetch() ?: erreur('tâche parente introuvable', 404);
        $liste = $in['tasks'] ?? [];
        if (!is_array($liste) || !$liste || count($liste) > TACHES_FILLES_MAX) erreur('1 à ' . TACHES_FILLES_MAX . ' tâches');
        $connus = array_column(projets(), 'id');
        $differe = !empty($in['defer']);  // projet d'un pod qui démarre : la tâche attend que son worker se connecte
        foreach ($liste as $f) {
            $instruction = trim((string)($f['instruction'] ?? ''));
            if ($instruction === '' || mb_strlen($instruction) > 10000) erreur('instruction vide ou trop longue');
            $connu = in_array($f['project'] ?? null, $connus, true);
            if (!$connu && !($differe && preg_match('/^[a-z0-9](?:[a-z0-9-]{0,28}[a-z0-9])?$/', (string)($f['project'] ?? ''))))
                erreur('projet inconnu : ' . ($f['project'] ?? '?'));
        }
        $ids = [];
        db()->beginTransaction();
        foreach ($liste as $f) {
            $id = bin2hex(random_bytes(5));
            $wk = worker_du_projet($f['project']);
            q('INSERT INTO taches(id, instruction, projet, cree_le, maj_le, worker_id, conversation, parent, attente_projet) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)',
              [$id, trim((string)$f['instruction']), $f['project'], time(), time(), $wk, $id, $parent['id'], $wk ? null : 1]);
            evenement_tache($id);
            $ids[] = $id;
        }
        db()->commit();
        repondre(['ids' => $ids]);

    case 'w_evenements':
        exiger_worker();
        $notifier = false;
        $wid = (string)($in['worker_id'] ?? 'principal');
        db()->beginTransaction();
        foreach ($in['events'] ?? [] as $evt) {
            $tacheId = (string)($evt['task_id'] ?? '');
            $proprio = q('SELECT worker_id FROM taches WHERE id = ?', [$tacheId])->fetch();
            // Événement d'un worker qui n'est plus responsable de la tâche (réaffectée) : ignoré.
            if (!$proprio || ($proprio['worker_id'] !== null && $proprio['worker_id'] !== $wid)) continue;
            switch ($evt['kind'] ?? '') {
                case 'task':
                    $t = $evt['task'];
                    $avant = q('SELECT statut FROM taches WHERE id = ?', [$tacheId])->fetchColumn();
                    $extra = json_decode((string)q('SELECT extra FROM taches WHERE id = ?', [$tacheId])->fetchColumn(), true) ?: [];
                    foreach (['changes', 'worktree', 'duration_s', 'next_steps', 'tests'] as $k) if (isset($t[$k])) $extra[$k] = $t[$k];
                    q('UPDATE taches SET statut = ?, projet = COALESCE(?, projet), raison_routage = ?, resultat = ?, cout = ?, maj_le = ?,
                       session = COALESCE(?, session), extra = ? WHERE id = ?',
                      [$t['status'], $t['project'] ?? null, (string)($t['route_reason'] ?? ''), (string)($t['result'] ?? ''),
                       (float)($t['cost_usd'] ?? 0), time(), $t['session_id'] ?? null,
                       $extra ? json_encode($extra, JSON_UNESCAPED_UNICODE) : null, $tacheId]);
                    $notifier = $notifier || ($avant !== $t['status'] && in_array($t['status'], ['done', 'error', 'needs_project'], true));
                    evenement_tache($tacheId);
                    break;
                case 'approval':
                    $a = $evt['approval'];
                    $existe = q('SELECT decision FROM validations WHERE id = ?', [$a['id']])->fetch();
                    if (!$existe) {
                        q('INSERT INTO validations(id, tache_id, donnees, decision, decision_livree, cree_le) VALUES(?, ?, ?, ?, ?, ?)',
                          [$a['id'], $tacheId, json_encode($a, JSON_UNESCAPED_UNICODE), $a['decision'] ?? null, isset($a['decision']) ? 1 : 0, time()]);
                        $notifier = $notifier || !isset($a['decision']);
                    } elseif (isset($a['decision'])) {
                        // La décision finale vient du worker (approbation, refus ou délai dépassé).
                        q('UPDATE validations SET decision = ?, decision_livree = 1, donnees = ? WHERE id = ?',
                          [$a['decision'], json_encode($a, JSON_UNESCAPED_UNICODE), $a['id']]);
                    }
                    inserer_evenement($tacheId, $evt);
                    break;
                case 'message':
                    $notifier = $notifier || (($evt['type'] ?? '') === 'ci' && ($evt['state'] ?? '') === 'failure');
                    if (($evt['type'] ?? '') === 'worktree' && isset($evt['state'])) {
                        // Branche fusionnée ou jetée : l'état est conservé sur la tâche (tableau de bord, rechargement).
                        $extra = json_decode((string)q('SELECT extra FROM taches WHERE id = ?', [$tacheId])->fetchColumn(), true) ?: [];
                        if (isset($extra['worktree'])) {
                            $extra['worktree']['state'] = (string)$evt['state'];
                            q('UPDATE taches SET extra = ?, maj_le = ? WHERE id = ?', [json_encode($extra, JSON_UNESCAPED_UNICODE), time(), $tacheId]);
                            inserer_evenement($tacheId, $evt);
                            evenement_tache($tacheId);
                            break;
                        }
                    }
                    inserer_evenement($tacheId, $evt);
                    break;
            }
        }
        foreach ($in['audit'] ?? [] as $ligne) {
            q('INSERT INTO audit(donnees, ts) VALUES(?, ?)', [json_encode($ligne, JSON_UNESCAPED_UNICODE), time()]);
        }
        db()->commit();
        if ($notifier) notifier();
        repondre(['ok' => true]);

    default:
        erreur('route inconnue', 404);
}
