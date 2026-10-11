# Cockpit Onyxia × Claude Code

PWA mobile (`https://cockpit.deltastat.fr`) pour piloter des agents Claude Code qui tournent dans Onyxia,
avec une forte autonomie et une validation humaine pour les opérations sensibles.

```
📱 PWA  ──HTTPS──▶  OVH : web/ (PHP + SQLite)  ◀──long-polling HTTPS──  Worker (pod Onyxia)
   passkey (Face ID)    relais : tâches, validations,        clé worker           │
   notifications push   audit, notifications                                      ├─ routeur (Haiku / mots-clés)
                                                                                  └─ agents Claude Code (Agent SDK)
                                                                                       └─ hook PreToolUse → politique
                                                                                          allow / ask (téléphone) / deny
```

- **Le pod Onyxia n'est jamais exposé** : le worker ouvre une connexion sortante vers le relais.
  L'adresse du téléphone ne change pas quand tu relances ton service Onyxia.
- **Connexion sans code** : un QR code une seule fois, puis Face ID / empreinte (passkey). Session de 180 jours.
- **Validation** : carte avec la commande exacte, notification même écran verrouillé, refus après 15 min sans réponse.

## Mise en place (une seule fois)

### 1. GitHub : dépôt privé + 3 secrets
Créer le dépôt **privé** `th-e-o/cockpit`, puis y pousser ce dossier :
```bash
git remote add origin https://github.com/th-e-o/cockpit.git && git push -u origin main
```
*Settings → Secrets and variables → Actions* : `FTP_SERVER`, `FTP_USERNAME`, `FTP_PASSWORD`, avec les
**mêmes valeurs que pour PLFMarket** (même hébergement OVH). Aucun autre secret : la clé du worker n'est
connue du relais que par son empreinte, versionnée dans `config/worker_key.sha256`.

Chaque `git push` sur `main` qui touche `web/` redéploie le relais (*Actions → Déploiement OVH*).
Il est envoyé dans le dossier `cockpit/`, **à côté de** `www/` (PLFMarket) : il n'est donc pas accessible
par `deltastat.fr/cockpit`.

### 2. OVH : le sous-domaine
Espace client → Hébergements → onglet **Multisite** → *Ajouter un domaine ou un sous-domaine* :
- domaine : `cockpit.deltastat.fr`, dossier racine : `cockpit` ;
- cocher **SSL** (Let's Encrypt). Si la zone DNS de deltastat.fr est chez OVH, l'enregistrement est créé automatiquement.

Le certificat peut mettre jusqu'à une heure. Les passkeys et les notifications exigent HTTPS.

### 3. Onyxia : identifiant Claude
Le CLI du pod n'est pas connecté (l'extension VSCode utilise sa propre session). Dans un terminal :
```bash
claude setup-token
```
Range le token dans **Mes secrets** Onyxia, secret `Claude`, variable `CLAUDE_CODE_OAUTH_TOKEN`. Ce secret
est déjà injecté dans tes services VSCode (`VAULT_RELATIVE_PATH=Claude`). Pour une persistance de la clé
du worker d'un service à l'autre, ajoute aussi `COCKPIT_WORKER_KEY` avec le contenu de `data/worker_key`.

### 4. Lancer et connecter le téléphone
```bash
./run.sh          # démarre le worker en arrière-plan
./run.sh pair     # QR code → scan avec l'appareil photo → « Activer Face ID »
```
Puis, sur iPhone : Partager → *Sur l'écran d'accueil*. Ouvre le cockpit depuis l'icône, puis
☰ → *Activer les notifications*. Sur Android : menu → *Installer l'application*.

## Au quotidien

- Puce **Auto** : le routeur choisit le projet. Sous 70 % de confiance, il te demande lequel.
  Une puce projet envoie directement ; **●** signale une conversation reprise, et **nouv.** repart de zéro.
- La pastille verte indique que le worker est connecté. Une instruction envoyée hors ligne part dès sa reconnexion.
- ☰ : journal d'audit, notifications, passkeys (ajout/suppression), déconnexion.
- `./run.sh logs | stop | fg` côté Onyxia.

## Configuration

- `config/projects.yaml` : projets (dossier de travail, description et mots-clés pour le routage).
- `config/policy.yaml` : règles `deny > ask > allow > défaut (ask)` par outil, chemin de fichier et commande shell.
  Les commandes composées (`&&`, `|`, `;`) sont évaluées segment par segment. Les substitutions et les redirections
  `>` passent toujours en validation.
- Variables : `COCKPIT_RELAY_URL` (défaut `https://cockpit.deltastat.fr`), `COCKPIT_WORKER_KEY`, et la variable
  GitHub `COCKPIT_HOTE` pour changer de sous-domaine.

## Sécurité : ce qu'il faut savoir

- **La politique est un garde-fou, pas une frontière de sécurité.** Un agent autorisé à lancer `python`
  ou `pytest` peut exécuter du code arbitraire. La vraie frontière, ce sont le RBAC du namespace Kubernetes et
  l'isolation des pods.
- **Le relais OVH voit passer** les instructions, les commandes soumises à validation et les résultats (base SQLite
  dans `cockpit/data/`, interdite en lecture web). Quelqu'un qui contrôlerait l'hébergement pourrait envoyer des
  instructions au worker, toujours bornées par la politique.
- **Authentification** : passkey ES256 vérifiée côté serveur (origine, rpId, challenge à usage unique, présence
  et vérification biométrique). Cookie `HttpOnly; Secure; SameSite=Strict`. Les requêtes d'écriture exigent du JSON
  (anti-CSRF), avec une limite de 20 échecs par IP sur 15 min. Liens d'appairage : 10 min, usage unique.
- **Révoquer un appareil** : ☰ → Passkeys → Supprimer. **Révoquer le worker** : supprimer `data/worker_key`,
  puis `./run.sh init` et pousser la nouvelle empreinte.

## Limites connues

- Les agents tournent dans le pod où le worker est lancé : les projets doivent être sur son disque.
  Le worker s'arrête avec le pod ; il faut le relancer avec `./run.sh` (ou via le script d'initialisation du service Onyxia).
- Une tâche en cours est perdue si le worker redémarre : elle passe en erreur et ses validations expirent.

## Tests

```bash
COCKPIT_PHP=/chemin/vers/php .venv/bin/python -m pytest -q
```
- `test_policy.py` : règles de sécurité.
- `test_relay.py` : relais PHP réel, avec appairage, passkeys (signatures ECDSA) et circuit complet téléphone ↔ worker.
- `test_ui.py` : la PWA dans Chromium (vue iPhone, authentificateur WebAuthn virtuel), de l'appairage à la
  validation. Nécessite `playwright install chromium`.
