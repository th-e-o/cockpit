# Plan : lancer des services Onyxia depuis le cockpit

> Document de reprise, écrit le 11/10/2026 à la fin de la session de conception initiale.
> But : pouvoir dire depuis le téléphone « crée un projet X à partir du dépôt Y » ; le cockpit lance alors
> un service Onyxia dédié, qui devient un projet pilotable.

## État actuel (commit de référence : voir `git log`)

**En production** sur https://deltastat.fr/cockpit/ :
- `web/` : PWA + relais PHP/SQLite, déployés par GitHub Actions (FTP) dans `www/cockpit/` de l'hébergement OVH.
  Indépendant de PLFMarket (`www/`), car chaque workflow ne gère que ses propres fichiers.
- Authentification : appairage par QR code (`./run.sh pair`), puis passkey. Push Web sans contenu (VAPID).
- `cockpit/` : worker Python qui interroge le relais en long-polling (`w_poll`), route les instructions
  (`router.py`) et lance les agents via Claude Agent SDK (`agent.py`). La politique est appliquée dans un hook
  `PreToolUse` (`policy.py`, `config/policy.yaml`), et les cas `ask` partent en validation sur le téléphone.
- Tests : `COCKPIT_PHP=<php> .venv/bin/python -m pytest -q`. Ils couvrent la politique, le relais PHP réel et
  la PWA dans Chromium avec WebAuthn virtuel. PHP n'est pas installé dans les images Onyxia : utiliser le binaire
  statique de `https://dl.static-php.dev/static-php-cli/common/` (php-8.3.x-cli-linux-x86_64). Pour Chromium :
  `.venv/bin/playwright install chromium && sudo .venv/bin/playwright install-deps chromium`.

**Pas encore vérifié avec un vrai agent Claude** : il faut `CLAUDE_CODE_OAUTH_TOKEN` (obtenu par `claude setup-token`)
dans l'environnement du worker. L'extension VSCode utilise sa propre session, que le CLI ne voit pas.

## Constats qui motivent ce plan

1. Les services VSCode lancés avec le rôle Kubernetes par défaut (`view`) ne peuvent ni créer de déploiements ni
   lister les secrets. `helm install` y est donc impossible : il faut un pod lancé avec le rôle **admin**.
2. Le relais n'accepte qu'**un seul worker** : `w_poll` compare `instance` à `reglages.worker_instance`, et un
   second worker « remplace » le premier (réponse `superseded`). Les projets sont statiques (`config/projects.yaml`).
3. Le chart officiel `vscode-python` (InseeFrLab, v2.6.6, dépôt helm
   `https://inseefrlab.github.io/helm-charts-interactive-services`) expose tout le nécessaire :
   - `git.repository`, `git.branch` : clone du projet ;
   - `init.personalInit` (+ `personalInitArgs`) : URL d'un script exécuté au démarrage ;
   - `vault.secret` (+ `mount`, `directory`) : injection d'un secret Onyxia en variables d'environnement ;
   - `kubernetes.role` : `view` par défaut, **à garder pour les pods de projets** ;
   - `resources`, `persistence.size`, `security.password`.
   Les modèles du chart sont surtout dans sa library-chart : vérifier les clés exactes avec `helm show values`.
4. Quota du namespace `user-theof` au 11/10 : 5 pods sur 100, 0 GPU sur 1, 98 Gi de stockage sur 2 Ti.

## Architecture cible

```
📱 ──▶ relais OVH (multi-workers) ◀── worker « orchestrateur » (pod admin) : outils services + projets locaux
                                  ◀── worker projet A (pod view, lancé par l'orchestrateur)
                                  ◀── worker projet B …
```

- **Orchestrateur** : seul pod avec le rôle admin. Il expose aux agents des outils dédiés (serveur MCP en processus
  via `create_sdk_mcp_server` du SDK) plutôt que du `helm` brut en Bash, pour contrôler les paramètres et
  présenter une validation lisible.
- **Pods de projets** : rôle `view`. Le worker démarre seul grâce au script `init.personalInit`.

## Étapes

### 1. Relais multi-workers (`web/api.php`, `cockpit/relay.py`, `cockpit/worker.py`)
- Nouvelle table `workers(id, nom, instance, projets JSON, vu_le)` ; `id` stable par pod (ex. `HOSTNAME`).
  L'instance (aléatoire par processus) protège toujours contre les long-polls zombies, **par worker**.
- Colonne `taches.worker_id` : affectée à la création si le projet est connu, sinon au routage.
- Routage automatique : aujourd'hui fait par le worker, ce qui ne marche plus quand il y a plusieurs workers.
  Option retenue : l'orchestrateur route (il reçoit les tâches sans projet et connaît la liste globale des
  projets, que le relais lui renvoie), puis réaffecte la tâche au bon worker via une route `w_affecter`.
- `etat` renvoie les projets de tous les workers en ligne, avec leur statut ; la PWA grise les projets hors ligne.
- `hello` ne doit marquer en erreur que les tâches **de ce worker**.
- Tests : étendre `tests/test_relay.py` (deux workers, affectation, zombie par worker, hors ligne).

### 2. Outils de gestion des services (`cockpit/services.py`, branchés dans `agent.py`)
- `lancer_service(nom, depot_git, branche, cpu, memoire, disque, gpu=False)` : `helm upgrade --install` du chart
  `vscode-python` avec `kubernetes.role=view`, `vault.secret=Claude`,
  `init.personalInit=https://raw.githubusercontent.com/th-e-o/cockpit/main/scripts/onyxia-init.sh` et
  `personalInitArgs` = identifiant du projet. Nom de release préfixé (`cockpit-<nom>`) pour ne gérer que les siens.
- `lister_services()` : releases helm du namespace, avec leur état (pods prêts) et le quota.
- `arreter_service(nom)` : `helm uninstall`, limité aux releases `cockpit-*`.
- Politique : lancement et arrêt toujours en `ask`. La validation affiche le dépôt, les ressources et le quota
  restant. Ajouter les outils MCP à `config/policy.yaml`, car les outils `mcp__*` sont en `ask` par défaut.
- À vérifier au premier lancement : la release apparaît-elle dans l'interface Onyxia ? Faut-il des labels
  ou annotations particuliers ?

### 3. Script de démarrage `scripts/onyxia-init.sh`
- Clone `th-e-o/cockpit` dans `~/work/.cockpit`, crée le venv, puis écrit `config/projects.yaml` avec un projet
  unique (`cwd` = dossier cloné par `git.repository`) et lance `./run.sh`.
- Prérequis : le secret Onyxia `Claude` contient `CLAUDE_CODE_OAUTH_TOKEN` **et** `COCKPIT_WORKER_KEY`.
  Une clé commune à tous les workers suffit au départ ; l'identité vient de l'id du pod.

### 4. PWA
- Projets groupés par pod, avec une pastille en ligne ou hors ligne.
- Carte de validation dédiée au lancement de service (ressources, quota).
- Plus tard : actions rapides « arrêter le service » sur un projet.

### 5. Vérification réelle
1. Pod admin + token Claude → `./run.sh` → instruction « liste mes services » (lecture seule, sans validation).
2. « Crée un projet test à partir de https://github.com/th-e-o/PLFMarket » → validation sur le téléphone →
   le pod démarre → le projet apparaît dans les puces → instruction de lecture sur ce projet.
3. « Arrête le service test » → validation → release supprimée.

## Questions ouvertes
- Ressources par défaut d'un service (proposition : 2 CPU, 8 Go, 10 Gi).
- Arrêt automatique des services inactifs, pour préserver le quota ?
- Clé de worker unique ou une clé par pod (révocation fine) ?
