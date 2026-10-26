# Mise en production : ce qu'il reste à faire

Audit du 2026-10-26. Cases à cocher au fur et à mesure. Ordre de priorité : de haut en bas.

## 1. Bloquants

- [ ] **CI de tests** : ajouter un job `pytest` obligatoire avant `deploiement.yml` et `image.yml` (aujourd'hui un push sur `main` déploie sans test).
- [ ] **Corriger les 13 échecs de `tests/test_ui.py`** (Playwright, `TargetClosed`). Cause : Chromium ne démarre pas faute de dépendances système, pas un bug du code. Lancer `playwright install-deps chromium` dans le pod et dans la CI, puis revérifier.
- [ ] **Protéger `main`** : PR obligatoire, CI verte requise (le worker se met à jour seul, `COCKPIT_AUTO_UPDATE=1` : un commit cassé se propage à tous les pods).
- [ ] **Image reproductible** : remplacer `:latest` + `imagePullPolicy: Always` par un tag SHA ; épingler la version de `@anthropic-ai/claude-code` dans le Dockerfile.
- [ ] **Dépendances Python épinglées** : lockfile (`uv lock`), séparer `requirements-dev.txt` (pytest, playwright) de `requirements.txt`.
- [ ] **Déploiement du relais** : route `/health`, contrôle après déploiement dans le workflow, procédure de retour arrière (le FTP n'est pas atomique).

## 2. Sécurité

- [ ] Limitation de débit sur l'appairage, les passkeys et les routes worker (`web/api.php`).
- [ ] Rotation de la clé du worker, du token Claude et du token GitHub ; révocation de session / passkey depuis l'interface (session actuelle : 180 jours).
- [ ] Token GitHub à portée limitée (fine-grained par dépôt ou GitHub App) au lieu d'un token partagé par tous les pods.
- [ ] Isolation des pods : `NetworkPolicy` (sortie réseau limitée), pas d'accès Vault / `kubectl`, système de fichiers en lecture seule hors du projet. Les regex de `config/policy.yaml` seules sont contournables (`bash -c`, `python -c`, encodage).
- [ ] Journal d'audit (`data/audit.jsonl`) envoyé vers un stockage externe en ajout seul.
- [ ] Rotation de `data/worker.log` (1,3 Mo) et vérification qu'aucun secret n'y figure.
- [ ] Scan de secrets (gitleaks) et Dependabot / Renovate (PHP, Python, Actions, image de base).
- [ ] Revue de sécurité de `web/api.php` (frontière d'authentification).

## 3. Fiabilité et exploitation

- [ ] Sauvegardes SQLite **exportées hors du serveur OVH** + restauration testée (aujourd'hui : 3 sauvegardes dans `web/data/`, sur la même machine).
- [ ] Mettre à jour SQLite côté hébergement (3.27.2, antérieur au correctif « WAL reset » 3.51.3), ou migrer vers MySQL / un VPS avec PostgreSQL.
- [ ] Supervision externe : sonde sur `/health`, alerte si le worker ne donne plus signe de vie, alerte sur coût et budget de tokens.
- [ ] Journaux structurés avec identifiant de tâche ; remontée centralisée des erreurs PHP.
- [ ] `StatefulSet` : sondes `liveness` / `readiness` ; vérifier la reprise d'une tâche après redémarrage du pod (`sessions.json` local au volume).
- [ ] Limites de coût et de durée par tâche (kill switch).
- [ ] Documenter les points uniques de défaillance (relais OVH, orchestrateur unique) et la reprise.

## 4. Qualité du code

- [ ] Découper `web/api.php` (~1950 lignes) et `web/static/app.js` (~2300 lignes) en modules.
- [ ] Linters et typage dans la CI : ruff, mypy, phpstan, eslint.
- [ ] Ajouter `.pytest_cache/` au `.gitignore`.
- [ ] Commiter ou écarter les modifications en cours (`cockpit/agent.py`, `cockpit/worker.py`, `tests/test_confiance.py`).

## 5. Documentation et gouvernance

- [ ] Runbook : relais en panne, base corrompue, fuite de token, retour arrière.
- [ ] `SECURITY.md`, licence, CHANGELOG, tags de version.
- [ ] Environnement de recette séparé (les passkeys sont liées au domaine `deltastat.fr`).

## Ordre proposé

1. CI avec tests obligatoires, correction des 13 échecs, protection de `main`.
2. Versions épinglées (image, dépendances).
3. Limitation de débit, `/health`, supervision externe.
4. Sauvegardes externalisées, restauration testée.
5. Isolation réseau des pods, tokens à portée limitée.
6. Runbook, puis découpage progressif du code.
