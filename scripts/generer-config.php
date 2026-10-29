<?php
// Génère web/config.php lors du déploiement. Aucun secret : seules l'empreinte de la clé du worker,
// versionnée dans config/worker_key.sha256 (générée par « ./run.sh init »), et celles des clés d'API
// (config/api_keys.sha256, facultatif) sont nécessaires.
$racine = dirname(__DIR__);
$empreinte = trim((string)@file_get_contents("$racine/config/worker_key.sha256"));
if (!preg_match('/^[0-9a-f]{64}$/', $empreinte)) {
    fwrite(STDERR, "config/worker_key.sha256 absent ou invalide : lancer « ./run.sh init » puis versionner le fichier.\n");
    exit(1);
}
// Clés d'API (outils externes autorisés à ajouter des propositions) : une empreinte SHA-256 par ligne, fichier facultatif.
$cles_api = array_values(array_filter(array_map('trim', @file("$racine/config/api_keys.sha256") ?: []),
                                      fn($l) => preg_match('/^[0-9a-f]{64}$/', $l)));
$hote = getenv('COCKPIT_HOTE') ?: 'deltastat.fr';
$base = '/' . trim(getenv('COCKPIT_BASE') ?: 'cockpit', '/') . '/';  // sous-dossier du site ('/' si sous-domaine dédié)
$base = $base === '//' ? '/' : $base;
$config = [
    'worker_cle_sha256' => $empreinte,
    'api_cles_sha256' => $cles_api,
    'hote' => $hote,                       // identifiant des passkeys (rpId)
    'origine' => getenv('COCKPIT_ORIGINE') ?: "https://$hote",
    'base' => $base,
    'debug' => false,
];
file_put_contents("$racine/web/config.php",
    "<?php\n// Généré par le déploiement : ne pas modifier sur le serveur.\nreturn " . var_export($config, true) . ";\n");
echo "web/config.php généré pour https://$hote$base\n";
