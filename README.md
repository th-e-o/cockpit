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
  fois, puis Face ID / empreinte / Windows Hello (passkey). Session de 180 jours. Téléphone perdu : ☰ → *Passkeys et
  appareils* → *Déconnecter les autres appareils* (supprimer une passkey le fait aussi).
- **Validation** : carte avec la commande exacte, notification même écran verrouillé, refus après 15 min sans réponse.

## Mise en place (une seule fois)

### 1. GitHub : dépôt + 3 secrets
Dépôt `th-e-o/cockpit`. *Settings → Secrets and variables → Actions* : `FTP_SERVER`, `FTP_USERNAME`,
`FTP_PASSWORD`, avec les **mêmes valeurs que pour PLFMarket** (même hébergement OVH). Aucun autre secret : la clé du
worker n'est connue du relais que par son empreinte, versionnée dans `config/worker_key.sha256`.

Chaque `git push` sur `main` qui touche `web/` redéploie le relais (*Actions → Déploiement OVH*).

**Clés d'API (facultatif)** : un outil externe (ex. un bot Telegram) peut ajouter des idées avec
`POST api.php?r=proposition_api` (en-tête `Authorization: Bearer <clé>`, corps JSON `{instruction, titre?, project?, source?}`).
- `instruction` (ou `description`) : ce que Claude doit faire, 10 000 caractères au plus ; `titre` : facultatif, tiré des
  premiers mots sinon ; `project` : identifiant **ou nom** d'un projet (casse indifférente) ; absent : **idée de nouveau projet**
  (l'orchestrateur créera un dépôt privé et un pod, puis lui confiera l'idée).
- Réponse : `{ok, id, titre, project, kind}` (`kind` : `projet` ou `nouveau_projet`) ; 400 si l'idée est vide, déjà présente ou
  le projet inconnu (le message liste les projets).
- L'idée attend dans *Propositions* (marquée 🔌). Elle entre dans le plan du **full auto et du mode hard** (donc dans les tokens
  de fin de semaine) tant que l'option « 🔌 Inclure les idées envoyées par tes outils » reste cochée en les armant. La clé ne
  permet rien d'autre.
`./run.sh cle-api` crée une clé et ajoute son empreinte à `config/api_keys.sha256` (une par ligne) ; pour révoquer, retirer la ligne.

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

- **Ce qui t'attend d'abord** : les validations en attente sont en tête du tableau de bord, avec leurs boutons (plus besoin de
  chercher la carte dans le fil). Toucher une notification de validation ouvre la tâche concernée ; le badge jaune ⚠ de
  l'en-tête déplie le projet et descend jusqu'à la validation ou la question.
- **Fiabilité** : une instruction n'est jamais envoyée deux fois (Entrée répétée, dictée) et le brouillon de la saisie est
  gardé sur l'appareil. Relais injoignable : bandeau « Relais injoignable » et nouvel essai automatique. Une réponse perdue
  entre le relais et le worker (coupure réseau) est renvoyée jusqu'à son accusé de réception : une tâche part toujours, une
  validation approuvée n'expire plus en refus. Une nouvelle version déployée recharge l'app installée dès que tu n'écris pas.
  Le worker lancé par `./run.sh` est relancé automatiquement s'il plante (délai croissant, `./run.sh stop` pour l'arrêter).

- Puce **Auto** : le routeur choisit le projet. Sous 70 % de confiance, il te demande lequel.
  Une puce projet envoie directement ; **●** signale une conversation reprise, et **nouv.** repart de zéro.
- La pastille verte indique que le worker est connecté. Une instruction envoyée hors ligne part dès sa reconnexion.
- **Résumé des modifications** : à la fin de chaque tâche, le worker compare l'état git du projet avant et après
  (instantané avec un index temporaire, sans toucher au dépôt) : fichiers ajoutés/modifiés/supprimés, lignes `+/−`,
  commits, branche et état du push. Toucher un fichier affiche son diff (chargé à la demande). La réponse de Claude
  suit trois rubriques : **Fait**, **Choix**, **À faire**. La notification de fin reprend ce résumé.
- **Tableau de bord** (onglet *Pods* sur téléphone, à droite du fil sur un écran large) : l'essentiel d'abord, ce qui
  attend ta réponse (questions, PR, variantes), l'autonomie (full auto, mode hard), l'usage, Claude au travail, les
  propositions, les pods (CPU / mémoire / disque, état git de chaque projet) et les tâches récentes. Le reste (chiffres
  du jour, tâches gourmandes, prochaines étapes, mémoire des projets, bilan, compte rendu) est replié sous *Plus de
  détails*. Toucher une carte ouvre la tâche.
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
  - *Modèle* : 🤖 *Modèle auto* (par défaut) fait choisir le modèle à chaque message par un sélecteur (Haiku) :
    Opus pour tout ce qui écrit ou modifie du code et en cas de doute, Sonnet pour les tâches courantes sans code
    (tests, doc, commit), Haiku pour les questions et lectures ; Opus si le sélecteur échoue. Un choix manuel
    (⚡ Rapide, ● Standard, ◆ Approfondi) vaut pour toute la conversation.
    Chaque carte affiche le modèle qui a réellement écrit (« ◆ Opus 5.5 », plus ceux des sous-agents), dès le
    premier message de Claude.
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
  - *Grande liberté par défaut* (`trust` de `config/policy.yaml`) : installations, `sudo`, suppressions et écritures
    partout dans `~/work`, workflows de CI, push (main compris), `helm install/upgrade`, `kubectl apply`, S3 en lecture
    et écriture, requêtes HTTP, lancement d'un pod, dépôt GitHub privé, agents en parallèle : sans attendre. Restent
    demandés seulement ce qui détruit sans retour : refus (`deny`), `.git/` à la main, écritures hors de `~/work` et
    `/tmp`, arrêt d'un service (stockage perdu), `helm uninstall`, `kubectl delete`, push forcé ou suppression de
    branche distante, suppression de données S3, `vault`, dépôt public. Chaque auto-approbation est journalisée et
    signalée sur la carte (« 🛡 N validations passées automatiquement »). Le *mode hard* lève aussi ces dernières.
  - *Questions avant le travail* : au premier message d'une conversation importante, Claude pose d'abord, en une
    fois, les questions essentielles (`poser_question`, un choix par question + précisions) puis travaille ou
    transmet un cahier des charges complet.
  - *Mini-fenêtre toujours visible* (🪟 ou Alt+P, Chrome et Edge sur ordinateur) : par-dessus les autres fenêtres,
    validations à approuver, questions de Claude, tâches en cours et terminées ; elle clignote quand quelque chose
    t'attend.
  - *Commande vocale* (🎤 à côté de la saisie, ou Alt+M) : tu parles, l'instruction s'écrit dans la saisie ; termine
    par « envoie » pour l'envoyer sans toucher l'écran, « annule » pour effacer la dictée. Reconnaissance vocale du
    navigateur (Chrome, Edge, Safari) ; le bouton n'apparaît pas si le navigateur ne la propose pas.
  - *Commandes rapides* (puces au-dessus de la saisie, ✎ pour les modifier, communes ou par projet) et *mémoire du
    projet* (🧠 : le `CLAUDE.md`, lu par Claude au début de chaque tâche, modifiable depuis le téléphone).
- **Pods de projet** : leur agent transmet à l'orchestrateur ce qui sort de son projet (nouveau projet, nouveau
  pod : `confier_au_projet(projet="orchestrateur")`). Le cockpit de ces pods tourne sur le **même commit que
  l'orchestrateur** qui les a lancés (`scripts/onyxia-init.sh <projet> <commit>`), et non sur le `main` du moment : un commit
  poussé sur le dépôt public ne s'exécute pas tout seul dans les pods. `COCKPIT_AUTO_UPDATE=1` rétablit la mise à jour
  automatique depuis `main` (git pull puis redémarrage du worker, toutes les 10 min, entre deux tâches).
- **Nouveau projet** (dans une *Nouvelle conversation*, sur *Auto*) : l'orchestrateur crée le dépôt GitHub, lance
  le pod dessus et lui confie la demande.
- **Du lancement au projet prêt** (*＋ Nouveau pod*, section *Projets et pods*) : une carte « démarrage » apparaît aussitôt et
  suit le pod étape par étape (ressources du cluster, image, installation, connexion du worker) ; elle se termine quand le projet
  est prêt, ou dit **pourquoi** il ne démarre pas (image introuvable, conteneur qui plante, avec la fin de son journal ; rien
  après 15 min). Sur un dépôt existant sans `CLAUDE.md`, Claude écrit aussitôt la **mémoire du projet** ; sur un dépôt neuf, la
  première instruction commence par là. Chaque projet de la carte du pod montre l'état de sa mémoire : *🧠 Mémoire* (voir,
  modifier), *⚠ Sans mémoire · Créer*, ou « en cours d'écriture ». Ressources du pod (CPU, mémoire, disque, GPU) repliées dans
  le formulaire, 2 CPU · 8 Go · 10 Go par défaut.
- **Ouvrir un pod** : bouton *🖥 Ouvrir VSCode* sur la carte du pod (adresse trouvée par son worker dans l'ingress
  Onyxia) et *🔑 Mot de passe*, qui le copie sans l'afficher. Le mot de passe transite par le relais avec l'état du
  pod (`COCKPIT_VSCODE_PASSWORD=0` pour ne pas le publier). Les pods lancés par le cockpit ont désormais un
  ingress VSCode avec un mot de passe aléatoire.
- **« Toujours autoriser »** : troisième bouton de chaque validation ; il approuve et crée une règle ciblée (une
  commande comme « docker build … », un dossier, un outil) appliquée par tous les pods. ☰ → *Règles d'autorisation*
  pour les voir et les supprimer. Jamais pour un refus définitif, une commande masquée (`$(…)`) ni `.git`.
- **Synchro des pull requests** : le worker interroge GitHub toutes les 2 min et fait sortir de « à valider » les PR fusionnées
  ou fermées ailleurs. L'orchestrateur suit aussi les PR des pods hors ligne (même token) et *Fusionner* passe par lui quand le
  pod du projet est hors ligne, au lieu d'attendre indéfiniment.
- **Pull requests** : encadré sur la carte (titre, branches, *Voir sur GitHub*, *Fusionner*), section *Pull requests
  à valider* du tableau de bord, bouton *Voir la PR* dans la notification. *Fusionner* demande au pod du projet de
  fusionner la PR avec son token GitHub (refus clair si la CI, une revue ou un conflit l'empêche).
- **Affichage** : thème 🌓 auto, ☀️ clair ou 🌙 sombre (☰) ; sur ordinateur, la séparation entre le fil et le tableau
  de bord se règle en la faisant glisser (double-clic : par défaut), et ⇥ (ou Alt+D) masque le tableau de bord.
- **Économiser les tokens** : chaque carte affiche sa consommation (« ≈ 45 k tokens · +2 % session », en orange
  au-delà de 5 % de la session de 5 h) ; le tableau de bord montre les tokens du jour, les tâches les plus
  gourmandes et les tokens par projet. Sous la saisie, un conseil quand il s'applique : avancement visible
  gratuitement dans le tableau de bord, puce du projet plutôt qu'Auto, « Modèle auto » plutôt qu'Opus pour une
  courte question, nouvelle conversation quand la conversation suivie est longue ou que le cache a expiré.
  Le sélecteur de modèle envoie le suivi d'avancement et la transmission de consignes sur Haiku.
- **Usage de Claude** : barre fine sous l'en-tête (session de 5 h, orange au-delà de 80 %) et détail dans le tableau
  de bord (session, semaine, heure de réinitialisation). Le jeton de `claude setup-token` n'a pas accès à
  l'interface d'usage du compte : le worker lit donc toutes les 10 min les en-têtes d'usage de l'API
  (`anthropic-ratelimit-unified-5h/7d-*`), par un comptage de tokens gratuit ou, à défaut, une mini-requête Haiku
  d'un token (`COCKPIT_USAGE_SONDE=0` pour la couper). Diagnostic : `./run.sh usage`.
- **Mémoire, propositions et full auto** (tableau de bord, section *Propositions*) : le relais garde
  l'état de chaque projet suivi (dernier résultat, activité, tâches en cours, « en pause » après 14 jours) et une liste
  de propositions de ce qui peut être fait : prochaines étapes des tâches terminées, idées générées par Claude quand la
  file est vide (toutes les 3 h au plus), ou ajoutées à la main (*＋ Idée* : un seul champ, pour un projet ou pour un
  **nouveau projet**) ou par un outil (clé d'API) ; ▶ Lancer ou Ignorer. Une prochaine étape lancée reprend **sa
  conversation** (même session Claude) ; quand une conversation avance, ses anciennes étapes sont retirées (la tâche
  suivante propose les siennes) : rien n'est fait deux fois. Les idées de nouveaux projets partent chez l'orchestrateur,
  une à la fois et 2 par jour au plus en automatique (chacune crée un pod). *Armer le full auto* (1 à 14 jours,
  tâches par jour) fait enchaîner ces propositions à l'orchestrateur, une tâche à la fois, tant que la session reste sous
  le seuil d'usage (85 %) ; il reprend après chaque réinitialisation et se désarme après 3 erreurs de suite. Même
  politique que le reste : une validation sans réponse est refusée au bout de 15 min, jamais approuvée d'office.
- **Mémoire de projet toujours présente** : chaque projet a un `CLAUDE.md` (lu par Claude au début de chaque tâche). S'il manque
  (absent ou de moins de 150 caractères), **c'est la première chose faite** : tout agent qui démarre sur le projet doit d'abord
  explorer le dépôt et écrire la mémoire (objectif, stack et structure, commandes, conventions, état actuel, prochaines étapes pour
  que Cockpit sache par où repartir), puis traiter la demande ; le full auto et le mode hard lancent en priorité absolue une tâche
  « Mémoire du projet » (une tentative par jour et par projet), avant les reprises et les propositions, et l'aperçu la montre en tête
  du plan. Quand la mémoire existe, l'agent la tient à jour si son travail change l'objectif, les commandes ou l'état d'avancement.
  Dans le tableau de bord, l'état de la mémoire figure sur chaque projet de la carte de son pod ; *Plus de détails → Projets
  suivis* reprend le dernier résultat de chaque projet (et la mémoire de ceux dont le pod n'est plus affiché).
- **Gérer les projets** (☰ → *🗂 Gérer les projets*) : *Supprimer* retire un projet du cockpit : liste, état de mémoire, propositions,
  mémoire (CLAUDE.md publié) et commandes rapides ; un périmètre du full auto ou du mode hard réduit à ce seul projet est désarmé.
  Le pod, le dépôt git et le service Onyxia **ne sont pas touchés** (le pod continue d'annoncer le projet, qui reste masqué).
  Refusé tant qu'une tâche tourne dessus, et pour les projets de l'orchestrateur (à retirer de `config/projects.yaml`). La case
  « Supprimer aussi son historique » efface en plus ses tâches et événements (définitif) ; sinon l'historique reste. Les projets
  retirés sont listés en bas, avec *Restaurer*. Arrêter le service Onyxia lui-même n'est pas proposé ici (le stockage du pod serait détruit).
- **Fil par projet** : le chat est regroupé en menus déroulants, un par projet (le plus récemment actif en bas), avec le nombre
  de tâches, celles en cours (⚙) et les validations en attente (⚠, le menu s'ouvre alors tout seul). Ouverts au départ : ceux qui
  travaillent et le plus récent ; ton choix est mémorisé, *Tout replier / Tout déplier* en haut du fil. Suivre une conversation ou
  toucher une tâche dans le tableau de bord ouvre son menu.
- **Réveil des pods** : quand le full auto ou le mode hard est armé, l'orchestrateur relance (`kubectl exec`) le worker des pods
  de projet **prêts mais muets** (un worker s'arrête définitivement quand le relais le croit remplacé, et rien ne le relançait)
  qui ont du travail (tâches en attente, propositions), au plus une fois toutes les 10 min par projet. Il ne crée ni ne remplace
  aucun service : un pod absent ou arrêté n'est pas relancé (l'erreur s'affiche) ; les secrets dont le worker a besoin (clé du worker, jeton Claude), absents de
  l'environnement du pod après son démarrage, sont transmis par l'orchestrateur sur l'entrée standard de `kubectl exec` (jamais en
  argument, jamais écrits sur disque), et le réveil ne répond « relancé » qu'après avoir vérifié que le worker tourne. La carte *Autonomie* montre en direct les
  tâches automatiques en cours, la raison d'une attente et les pods relancés.
- **Reste de la semaine et incitation** (tableau de bord, *⏳ Reste de la semaine*) : jauge hebdomadaire, temps avant la
  réinitialisation et **estimation en tokens** de ce qu'il reste à dépenser (budget déduit des tokens des tâches du cockpit
  cette semaine divisés par la part d'usage correspondante : estimation prudente, l'usage hors cockpit n'est pas compté ;
  nombre de tâches moyennes équivalent). Quand il reste au moins 20 % à 48 h de la réinitialisation (puis 10 % à 12 h), une
  notification « ☠ Il reste N % de la semaine » propose le mode hard, avec le bouton *Utiliser le reste* qui préremplit la
  durée jusqu'à la réinitialisation. L'aperçu du dialogue chiffre aussi les tokens du plan affiché.
- **Une seule carte *Autonomie*** (tableau de bord) et un seul dialogue (⚡ *Armer l'autonomie…*) : projets au choix (● pod en
  ligne / ○ hors ligne), durée, projets de front, idées à inclure, réglages avancés (tâches par jour, seuil de session) repliés,
  et l'**aperçu** de ce que Claude compte faire (travail resté en plan d'abord, puis propositions et idées) chiffré en tokens.
  Sans autre option, c'est le **full auto** : il garde les rares validations restantes (refusées au bout de 15 min sans
  réponse). Cocher **☠ Sans validations** en fait le **mode hard** (réglages par défaut plus rapides). Armée, la carte montre ce
  qui tourne, *Arrêter* (tout) et, en full auto, *☠ Passer sans validations…*.
- **Mode hard** (option *Sans validations*) : pleine autonomie, pour une durée choisie (1 h à 14 jours) :
  - plus aucune validation (écriture hors du projet, push sur main, cluster, `sudo`…), chaque action reste journalisée
    (`policy: hard`) ; les validations en attente sont approuvées à l'activation ;
  - plus aucune question : Claude tranche seul (l'outil `poser_question` lui dit de décider), et s'il en a posé une quand même,
    le relais lui répond automatiquement de choisir l'option la plus prudente et de continuer (4 fois au plus par conversation) ;
  - **projets au choix** : un dialogue liste les projets (● pod en ligne / ○ hors ligne) ; seuls ceux cochés sont concernés
    (validations, questions, propositions). Sous la liste, un **aperçu** montre ce que Claude compte faire, dans l'ordre :
    d'abord le travail resté en plan, du plus ancien au plus récent (tâches interrompues par un redémarrage, ou transmises
    à un pod en ligne sans jamais avancer : elles sont reprises dans leur conversation, les bloquées sont annulées puis
    relancées), puis les propositions de l'état de mémoire. Les projets dont le pod est hors ligne restent à l'arrêt (le
    cockpit ne sait pas relancer un pod) : l'aperçu les signale avec leurs tâches en attente ;
  - les propositions sont enchaînées par défaut (case « Enchaîner aussi… » à décocher), pour la même durée et sur les mêmes projets : il mène
    **plusieurs projets de front** (jusqu'à 6, un projet à la fois, un pod par projet), en enchaînant le travail resté en plan
    puis les propositions de l'état de mémoire ;
  - **compte rendu** (tableau de bord, *📋 Compte rendu*) : par projet, ce qui a été fait (résultat, fichiers, commits, branche,
    tests), les questions tranchées, le nombre de validations passées automatiquement et les opérations sensibles ; une
    notification annonce la fin du mode, et le compte rendu reste consultable ensuite.
  - **Limite du forfait** : le full auto et le mode hard ne lancent une tâche que si elle tient dans la session de 5 h restante
    (coût = médiane de la part de session des 10 dernières tâches terminées, 5 % sans mesure ; plafond 98 %, additionné pour
    les projets de front) et attendent sinon la réinitialisation. Une tâche déjà en cours que la limite atteint est
    interrompue et **mise en pause** (⏸, pas une erreur : elle ne compte pas dans les 3 échecs de suite) ; après la
    réinitialisation, elle est reprise dans sa conversation avec les travaux restés en plan (ou à la main, avec le bouton *▶ Reprendre* de la tâche).
  Seuls les outils en `deny` de `config/policy.yaml` restent bloqués. À n'activer que sur des pods dont tu acceptes les risques.
- **Réinitialisation des tokens** : notification « 🔄 Tokens réinitialisés » quand la session de 5 h ou une limite
  hebdomadaire se renouvelle après avoir dépassé 80 % (`COCKPIT_RESET_NOTIFY_MIN`).
- **Tes dépôts GitHub** : une demande qui parle d'un dépôt ou d'un code qui n'est pas encore un projet (« reprends
  mon code sur … ») va à l'orchestrateur, qui le cherche (outil `chercher_depots` : nom, description, README,
  code) puis ouvre un pod dessus (validation), et lui confie ta demande (`confier_au_projet`) : elle part toute
  seule dès que le worker du nouveau pod est connecté (« ⏳ En attente du pod » en attendant).
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

- **Le mode hard lève toutes les validations** sauf les refus : à n'activer que sur des pods dont tu acceptes les risques.
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

## Robustesse du relais et des workers

Une panne de la base SQLite du relais (21 octobre 2026 : 500 pendant des heures, puis un « superseded » reçu par tous les workers
en même temps, qui les a arrêtés pour toujours) a conduit à ces garde-fous :
- **Moins d'écritures** : la boucle d'écoute de `w_poll` lit sans verrou et ne prend le verrou d'écriture que pour livrer du travail ;
  `vu_le` n'est écrit qu'au plus toutes les 10 s (au lieu de deux écritures par seconde et par pod).
- **Entretien toutes les 30 min** : checkpoint non bloquant du WAL, contrôle rapide d'intégrité (notification « ⚠ Base du relais
  corrompue » au plus une fois par jour) et **une sauvegarde par jour** (`data/cockpit.db.auto-AAAAMMJJ`, les 3 dernières ; jamais
  d'une base abîmée). Réparation : route `w_reparer` (clé du worker), voir `w_integrite` pour le diagnostic.
- **Worker tolérant** : le premier `superseded` n'arrête plus le worker, il se réannonce après 30 à 60 s ; un second en moins de
  5 min (vrai doublon) l'arrête. Un pod muet qui a des tâches en attente est relancé même sans autonomie armée
  (`COCKPIT_WAKE_PODS=0` pour le couper).
- Le serveur d'hébergement utilise SQLite 3.27.2, antérieur au correctif du bug « WAL reset » (3.51.3) : à faire mettre à jour.

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
