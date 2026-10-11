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
SQL);
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
        'expires' => $expire, 'path' => '/', 'httponly' => true,
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
    return time() - (int)(reglage('worker_vu_le') ?? 0) < WORKER_EN_LIGNE;
}

function projets(): array {
    return json_decode(reglage('projets') ?? '[]', true) ?: [];
}

function inserer_evenement(string $tacheId, array $evt): void {
    q('INSERT INTO evenements(tache_id, donnees, ts) VALUES(?, ?, ?)',
      [$tacheId, json_encode($evt, JSON_UNESCAPED_UNICODE), time()]);
}

function tache_publique(array $t): array {
    return [
        'id' => $t['id'], 'instruction' => $t['instruction'], 'project' => $t['projet'], 'status' => $t['statut'],
        'route_reason' => $t['raison_routage'], 'result' => $t['resultat'], 'cost_usd' => (float)$t['cout'],
        'created' => (int)$t['cree_le'],
    ];
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

    case 'appairer': // lien QR généré par le worker
        limiter_echecs();
        $st = q('DELETE FROM appairages WHERE jeton_hash = ? AND expire_le >= ?', [h((string)($in['token'] ?? '')), time()]);
        if ($st->rowCount() !== 1) echec("lien d'appairage expiré ou déjà utilisé");
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
            'events' => array_map(fn($e) => json_decode($e['donnees'], true), $evts),
            'last_id' => $dernier,
            'projects' => projets(),
            'worker_online' => worker_en_ligne(),
            'pending' => (int)q('SELECT COUNT(*) FROM validations WHERE decision IS NULL')->fetchColumn(),
        ]);

    case 'resume': // appelé par le service worker à la réception d'une notification push
        exiger_session();
        $validations = array_map(function ($d) {
            $a = json_decode($d, true);
            return ['tool' => $a['tool'] ?? '', 'reason' => $a['reason'] ?? '', 'task_id' => $a['task_id'] ?? ''];
        }, q('SELECT donnees FROM validations WHERE decision IS NULL ORDER BY cree_le')->fetchAll(PDO::FETCH_COLUMN));
        $terminees = array_map('tache_publique', q("SELECT * FROM taches WHERE statut IN ('done','error','needs_project')
            AND maj_le > ? ORDER BY maj_le DESC LIMIT 3", [time() - 300])->fetchAll());
        repondre(['approvals' => $validations, 'finished' => $terminees]);

    case 'tache':
        exiger_session();
        $instruction = trim((string)($in['instruction'] ?? ''));
        if ($instruction === '' || mb_strlen($instruction) > 10000) erreur('instruction vide ou trop longue');
        $projet = $in['project'] ?? null;
        if ($projet !== null && !in_array($projet, array_column(projets(), 'id'), true)) erreur('projet inconnu');
        $id = bin2hex(random_bytes(5));
        q('INSERT INTO taches(id, instruction, projet, nouvelle_session, cree_le, maj_le) VALUES(?, ?, ?, ?, ?, ?)',
          [$id, $instruction, $projet, empty($in['new_session']) ? 0 : 1, time(), time()]);
        evenement_tache($id);
        repondre(tache_publique(q('SELECT * FROM taches WHERE id = ?', [$id])->fetch()));

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
        q('INSERT INTO appairages(jeton_hash, expire_le) VALUES(?, ?)', [h($jeton), time() + APPAIRAGE_DUREE]);
        repondre(['url' => origine() . '/#pair=' . $jeton, 'expires_in' => APPAIRAGE_DUREE]);

    case 'w_poll':
        exiger_worker();
        set_time_limit(LONG_POLL + 15);
        if (isset($in['projects'])) reglage('projets', json_encode($in['projects'], JSON_UNESCAPED_UNICODE));
        if (!empty($in['hello'])) {
            // Le worker vient de (re)démarrer : ce qu'il exécutait est perdu.
            foreach (q("SELECT id FROM taches WHERE livree = 1 AND statut NOT IN ('done','error','cancelled','needs_project')")->fetchAll(PDO::FETCH_COLUMN) as $id) {
                q("UPDATE taches SET statut = 'error', resultat = 'Interrompue : le worker a redémarré.', maj_le = ? WHERE id = ?", [time(), $id]);
                evenement_tache($id);
            }
            foreach (q('SELECT id, tache_id, donnees FROM validations WHERE decision IS NULL')->fetchAll() as $v) {
                $a = json_decode($v['donnees'], true);
                $a['decision'] = 'timeout';
                q("UPDATE validations SET decision = 'timeout', decision_livree = 1, donnees = ? WHERE id = ?",
                  [json_encode($a, JSON_UNESCAPED_UNICODE), $v['id']]);
                inserer_evenement($v['tache_id'], ['kind' => 'approval', 'task_id' => $v['tache_id'], 'approval' => $a]);
            }
        }
        $fin = microtime(true) + ($in['wait'] ?? LONG_POLL);
        do {
            reglage('worker_vu_le', (string)time());
            db()->beginTransaction();
            $taches = q("SELECT id, instruction, projet, nouvelle_session FROM taches WHERE livree = 0 AND statut = 'pending'")->fetchAll();
            q("UPDATE taches SET livree = 1 WHERE livree = 0 AND statut = 'pending'");
            $decisions = q('SELECT id, decision FROM validations WHERE decision IS NOT NULL AND decision_livree = 0')->fetchAll();
            q('UPDATE validations SET decision_livree = 1 WHERE decision IS NOT NULL AND decision_livree = 0');
            $annulations = q('SELECT id FROM taches WHERE annulation = 1')->fetchAll(PDO::FETCH_COLUMN);
            q('UPDATE taches SET annulation = 0 WHERE annulation = 1');
            $choix = q('SELECT id, choix_projet FROM taches WHERE choix_projet IS NOT NULL')->fetchAll();
            q('UPDATE taches SET choix_projet = NULL WHERE choix_projet IS NOT NULL');
            db()->commit();
            if ($taches || $decisions || $annulations || $choix) break;
            usleep(500000);
        } while (microtime(true) < $fin && !connection_aborted());
        repondre([
            'tasks' => array_map(fn($t) => ['id' => $t['id'], 'instruction' => $t['instruction'],
                'project' => $t['projet'], 'new_session' => (bool)$t['nouvelle_session']], $taches),
            'decisions' => $decisions, 'cancels' => $annulations,
            'choices' => array_map(fn($c) => ['task_id' => $c['id'], 'project' => $c['choix_projet']], $choix),
        ]);

    case 'w_evenements':
        exiger_worker();
        $notifier = false;
        db()->beginTransaction();
        foreach ($in['events'] ?? [] as $evt) {
            $tacheId = (string)($evt['task_id'] ?? '');
            if ($tacheId === '' || !q('SELECT 1 FROM taches WHERE id = ?', [$tacheId])->fetchColumn()) continue;
            switch ($evt['kind'] ?? '') {
                case 'task':
                    $t = $evt['task'];
                    $avant = q('SELECT statut FROM taches WHERE id = ?', [$tacheId])->fetchColumn();
                    q('UPDATE taches SET statut = ?, projet = COALESCE(?, projet), raison_routage = ?, resultat = ?, cout = ?, maj_le = ? WHERE id = ?',
                      [$t['status'], $t['project'] ?? null, (string)($t['route_reason'] ?? ''), (string)($t['result'] ?? ''),
                       (float)($t['cost_usd'] ?? 0), time(), $tacheId]);
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
