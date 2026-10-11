<?php
// Génère web/config.php lors du déploiement. Aucun secret : seule l'empreinte de la clé du worker,
// versionnée dans config/worker_key.sha256 (générée par « ./run.sh init »), est nécessaire.
$racine = dirname(__DIR__);
$empreinte = trim((string)@file_get_contents("$racine/config/worker_key.sha256"));
if (!preg_match('/^[0-9a-f]{64}$/', $empreinte)) {
    fwrite(STDERR, "config/worker_key.sha256 absent ou invalide : lancer « ./run.sh init » puis versionner le fichier.\n");
    exit(1);
}
$hote = getenv('COCKPIT_HOTE') ?: 'cockpit.deltastat.fr';
$config = [
    'worker_cle_sha256' => $empreinte,
    'hote' => $hote,                       // identifiant des passkeys (rpId)
    'origine' => getenv('COCKPIT_ORIGINE') ?: "https://$hote",
    'debug' => false,
];
file_put_contents("$racine/web/config.php",
    "<?php\n// Généré par le déploiement : ne pas modifier sur le serveur.\nreturn " . var_export($config, true) . ";\n");
echo "web/config.php généré pour $hote.\n";
