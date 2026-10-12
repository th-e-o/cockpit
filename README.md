# Cockpit Onyxia × Claude Code

PWA mobile (`https://deltastat.fr/cockpit/`) pour piloter des agents Claude Code qui tournent dans Onyxia,
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
- **Connexion sans mot de passe** : un QR code (téléphone) ou une clé d'appairage à saisir (ordinateur) une seule
  fois, puis Face ID / empreinte / Windows Hello (passkey). Session de 180 jours.
- **Validation** : carte avec la commande exacte, notification même écran verrouillé, refus après 15 min sans réponse.

## Mise en place (une seule fois)

### 1. GitHub : dépôt + 3 secrets
Dépôt `th-e-o/cockpit`. *Settings → Secrets and variables → Actions* : `FTP_SERVER`, `FTP_USERNAME`,
`FTP_PASSWORD`, avec les **mêmes valeurs que pour PLFMarket** (même hébergement OVH). Aucun autre secret : la clé du
worker n'est connue du relais que par son empreinte, versionnée dans `config/worker_key.sha256`.

Chaque `git push` sur `main` qui touche `web/` redéploie le relais (*Actions → Déploiement OVH*).

### 2. OVH : rien à faire
Le cockpit est déployé dans `www/cockpit/` et servi sur **https://deltastat.fr/cockpit/**, car la formule
d'hébergement n'autorise pas de domaine rattaché supplémentaire. Les deux projets restent indépendants : chaque
workflow ne met à jour ou ne supprime que les fichiers qu'il a lui-même envoyés (pas de `dangerous-clean-slate`).
Pour passer plus tard sur un sous-domaine dédié : variables GitHub `COCKPIT_HOTE`, `COCKPIT_BASE=/` et
`FTP_DOSSIER`, plus `COCKPIT_RELAY_URL` côté worker. Les passkeys sont liées au domaine : il faudra en recréer.

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
./run.sh pair     # QR code (téléphone) + clé d'appairage à saisir (ordinateur)
```
Sur un **ordinateur** sans appareil photo, ouvre `https://deltastat.fr/cockpit/` et saisis la clé d'appairage
affichée sous le QR code (`XXXXX-XXXXX`, insensible à la casse), puis crée une passkey (Windows Hello, Touch ID,
clé de sécurité ou ton téléphone).

Puis installe l'application et ouvre-la depuis son icône, puis ☰ → *Activer les notifications* :
- **Android** (Chrome) : ☰ → *📲 Installer l'application* (ou menu du navigateur → *Installer l'application*).
  Chrome en fait une vraie application Android (WebAPK) : icône dans le tiroir, fenêtre plein écran, notifications.
  Un appui long sur l'icône propose *Pods* (tableau de bord) et *Nouveau pod*.
- **PC** (Chrome, Edge) : icône d'installation dans la barre d'adresse, ou ☰ → *Installer l'application* :
  fenêtre dédiée, épinglable dans la barre des tâches. Fil et tableau de bord côte à côte ; `Ctrl+K` ou `/` pour
  écrire, `Alt+1` / `Alt+2` pour changer de vue sur un écran étroit.
- **iPhone** : Partager → *Sur l'écran d'accueil*.

## Au quotidien

- Puce **Auto** : le routeur choisit le projet. Sous 70 % de confiance, il te demande lequel.
  Une puce projet envoie directement ; **●** signale une conversation reprise, et **nouv.** repart de zéro.
- La pastille verte indique que le worker est connecté. Une instruction envoyée hors ligne part dès sa reconnexion.
- **Résumé des modifications** : à la fin de chaque tâche, le worker compare l'état git du projet avant et après
  (instantané avec un index temporaire, sans toucher au dépôt) : fichiers ajoutés/modifiés/supprimés, lignes `+/−`,
  commits, branche et état du push. Toucher un fichier affiche son diff (chargé à la demande). La réponse de Claude
  suit trois rubriques : **Fait**, **Choix**, **À faire**. La notification de fin reprend ce résumé.
- **Tableau de bord** (onglet *Pods* sur téléphone, à droite du fil sur un écran large) : Claude au travail,
  validations en attente, tâches et coût du jour ; pour chaque pod, CPU / mémoire (cgroup, comme `kubectl top`) /
  disque de `~/work`, agents actifs, état git de chaque projet et branches parallèles. Toucher une carte ouvre la tâche.
- **Prochaines étapes** : à la fin d'une tâche, Claude propose 1 à 4 suites concrètes (outil `proposer_suite`).
  Sous la réponse, toucher une étape la prépare dans la saisie (modifiable) ; ▶ la lance tout de suite dans la
  conversation. Le tableau de bord les regroupe (*Prochaines étapes*) tant qu'elles ne sont ni lancées ni ignorées.
- **Notifications** (une par nouveauté, jamais répétée une fois fermée) :
  validation requise avec la commande et les boutons *Approuver* / *Refuser* ; tâche terminée avec le résumé des
  modifications, les étapes proposées et un bouton *▶* pour lancer les deux premières sans ouvrir l'app (Android,
  PC ; l'iPhone n'affiche pas les boutons) ; pod de projet connecté. Toucher la notification ouvre la tâche.
- **Dépôts GitHub** : Claude peut créer un dépôt (outil `creer_depot`, privé par défaut, soumis à validation),
  le relier au projet (`origin`) et y pousser la branche courante. *＋ Nouveau pod* → *＋ Nouveau dépôt* crée le
  dépôt puis lance un pod qui le clone. Il faut que le token git d'Onyxia puisse créer des dépôts : droit
  *Administration : écriture* (token à grain fin) ou portée `repo` (token classique).
- **Vibe coding** :
  - *Modèle* par conversation (⚡ Rapide = Haiku, ● Standard = Sonnet, ◆ Approfondi = Opus), hérité par ses suites.
  - *Recadrer* : pendant que Claude travaille, un message dans la conversation l'interrompt et il reprend avec
    (une validation en attente est alors refusée).
  - *Captures* : Claude photographie l'application servie dans le pod (`capturer_ecran`, localhost seulement) ; il
    voit l'image et toi aussi, sur la carte. Navigateur installé par `./run.sh` (`playwright install chromium`).
    *Aperçu en direct* : option du formulaire *Nouveau pod* qui publie le port 5000 (URL **publique**, lien dans
    le tableau de bord ; VSCode reçoit alors un mot de passe aléatoire).
  - *Tests* après chaque tâche qui modifie des fichiers : commande `test` de `projects.yaml` ou du `.cockpit.yml`
    du dépôt (lancée seulement si la politique l'autorise sans validation). Un échec devient la première étape
    proposée (« Corriger les tests »). *CI GitHub* suivie après un push : notification en cas d'échec avec ▶ Corriger.
  - *Variantes* (×2, ×3) : plusieurs Claude traitent la même demande, chacun dans son worktree ; le tableau de bord
    les compare (modifications, tests, capture) et *Garder* fusionne la choisie et jette les autres.
  - *Revue* : toucher une ligne d'un diff pour la commenter, puis *Envoyer la revue à Claude*. *Ouvrir une pull
    request* demande à Claude de la rédiger (`ouvrir_pr`, jamais depuis main/master).
  - *Mode confiance* (🛡 dans la barre de conversation : 15 min à 2 h) : les validations de routine de cette
    conversation passent sans toi (installations, suppressions dans le projet, commandes non répertoriées,
    fichiers sensibles du projet). Restent toujours demandés : refus (`deny`), écritures hors du projet, `.git/`
    et workflows de CI, services, dépôts GitHub, agents en parallèle, cluster, push sur main ou forcé, `sudo`
    (section `trust` de `config/policy.yaml`). Chaque auto-approbation est journalisée et signalée sur la carte.
  - *Commandes rapides* (puces au-dessus de la saisie, ✎ pour les modifier, communes ou par projet) et *mémoire du
    projet* (🧠 : le `CLAUDE.md`, lu par Claude au début de chaque tâche, modifiable depuis le téléphone).
- **Bilan par projet** (tableau de bord, aujourd'hui / 7 / 30 jours) : tâches réussies ou en erreur, temps de travail
  des agents, fichiers distincts touchés, lignes `+/−`, commits, activité par jour, fichiers les plus modifiés,
  dernières tâches et état git actuel du projet.
- **Nouveau pod sur un dépôt git** (tableau de bord → *＋ Nouveau pod*, ou ☰) : choix parmi tes dépôts GitHub
  (publics et privés, listés par l'orchestrateur avec le token git d'Onyxia, rafraîchis toutes les 10 min) ou URL
  collée, nom, branche et ressources. L'orchestrateur lance le service directement, sans agent ni validation
  supplémentaire (c'est toi qui le demandes), et refuse de remplacer un service existant. Le projet apparaît dans le
  cockpit dès que le worker du pod est connecté (1 à 3 min).
- **Plusieurs Claude en parallèle** : chaque conversation a son agent. Des conversations sur des projets différents
  tournent ensemble ; sur le même projet, la première travaille dans le dossier principal et les suivantes dans un
  **worktree git** dédié (`~/work/.cockpit-worktrees/<projet>-<conversation>`, branche `cockpit/<conversation>`).
  À la fin, *Fusionner* (commit du travail restant puis `merge --no-ff` dans la branche courante du projet) ou
  *Jeter*. Claude peut aussi découper lui-même une grosse demande avec l'outil `lancer_en_parallele`
  (jusqu'à 5 sous-tâches, soumis à validation). Au plus `COCKPIT_MAX_AGENTS` agents simultanés par pod (3 par défaut).
- ☰ : journal d'audit, notifications, passkeys (ajout/suppression), déconnexion.
- `./run.sh logs | stop | fg` côté Onyxia.

## Configuration

- `config/projects.yaml` : projets (dossier de travail, description et mots-clés pour le routage).
- `config/policy.yaml` : règles `deny > ask > allow > défaut (ask)` par outil, chemin de fichier et commande shell.
  Les commandes composées (`&&`, `|`, `;`) sont évaluées segment par segment. Les substitutions et les redirections
  `>` passent toujours en validation.
- Variables : `COCKPIT_RELAY_URL` (défaut `https://deltastat.fr/cockpit`), `COCKPIT_WORKER_KEY`,
  `COCKPIT_MAX_AGENTS` (agents simultanés par pod, défaut 3) et `COCKPIT_WORKTREES` (dossier des worktrees).

## Sécurité : ce qu'il faut savoir

- **Le mode confiance élargit ce que l'agent fait sans toi** : à n'activer que sur une conversation que tu suis.
- **La politique est un garde-fou, pas une frontière de sécurité.** Un agent autorisé à lancer `python`
  ou `pytest` peut exécuter du code arbitraire. La vraie frontière, ce sont le RBAC du namespace Kubernetes et
  l'isolation des pods.
- **Le relais OVH voit passer** les instructions, les commandes soumises à validation et les résultats (base SQLite
  dans `cockpit/data/`, interdite en lecture web). Quelqu'un qui contrôlerait l'hébergement pourrait envoyer des
  instructions au worker, toujours bornées par la politique.
- **Authentification** : passkey ES256 vérifiée côté serveur (origine, rpId, challenge à usage unique, présence
  et vérification biométrique). Cookie `HttpOnly; Secure; SameSite=Strict`. Les requêtes d'écriture exigent du JSON
  (anti-CSRF), avec une limite de 20 échecs par IP sur 15 min. Liens et clés d'appairage : 10 min, usage
  unique (le lien et la clé d'une même génération s'invalident mutuellement), clé de 50 bits, échecs comptés dans la
  limite par IP.
- **Révoquer un appareil** : ☰ → Passkeys → Supprimer. **Révoquer le worker** : supprimer `data/worker_key`,
  puis `./run.sh init` et pousser la nouvelle empreinte.

## Limites connues

- **Même origine web que PLFMarket** (`deltastat.fr`). Une faille XSS dans PLFMarket permettrait à un script
  d'utiliser ta session cockpit, toujours bornée par les validations. Une relecture de PLFMarket (octobre 2026)
  n'a rien montré d'évident : saisies échappées, liens d'articles limités à `http(s)`. À garder en tête quand
  PLFMarket évolue.

- Les agents tournent dans le pod où le worker est lancé : les projets doivent être sur son disque.
  Le worker s'arrête avec le pod ; il faut le relancer avec `./run.sh` (ou via le script d'initialisation du service Onyxia).
- Une tâche en cours est perdue si le worker redémarre : elle passe en erreur et ses validations expirent.
- Un worktree part du dernier commit : les modifications non commitées du dossier principal n'y sont pas.
  Les nouveaux fichiers de plus de 5 Mo (données, modèles) sont ignorés par le résumé des modifications.

## Tests

```bash
COCKPIT_PHP=/chemin/vers/php .venv/bin/python -m pytest -q
```
- `test_policy.py` : règles de sécurité.
- `test_parallel.py` : résumé git des modifications, worktrees (fusion, rejet), deux agents simulés en parallèle.
- `test_relay.py` : relais PHP réel, avec appairage, passkeys (signatures ECDSA) et circuit complet téléphone ↔ worker.
- `test_ui.py` : la PWA dans Chromium (vue iPhone, authentificateur WebAuthn virtuel), de l'appairage à la
  validation, et le tableau de bord sur téléphone et sur ordinateur (captures dans `COCKPIT_SHOTS`). Nécessite `playwright install chromium`.
