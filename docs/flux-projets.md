# Flux des projets : création, dépôts, délégation et suivi des pods

Ce document décrit le parcours complet d'une demande, de la création d'un projet jusqu'au suivi du travail
confié à son pod. Il s'appuie sur le code de `cockpit/worker.py`, `cockpit/agent.py`, `cockpit/services.py`,
`cockpit/github.py`, `cockpit/router.py`, `scripts/onyxia-init.sh` et `web/api.php` (relais).

## Acteurs

| Acteur | Rôle | Où |
|---|---|---|
| **Relais** | File des tâches, registre des workers et de leurs projets, validations, notifications | `web/api.php` (OVH, SQLite) |
| **Orchestrateur** | Worker du pod admin (`COCKPIT_ROLE` ≠ `projet`). Route les tâches sans projet, lance et arrête les services, publie la liste des dépôts GitHub | `cockpit/worker.py` (`ORCHESTRATOR`) |
| **Worker de projet** | Worker d'un pod lancé par l'orchestrateur (`COCKPIT_ROLE=projet`, rôle Kubernetes `view`). Il n'héberge qu'**un** projet | `scripts/onyxia-init.sh` |
| **Agent** | Session Claude Code lancée par un worker pour une tâche, avec les outils MCP `cockpit` (et `services` sur l'orchestrateur) | `cockpit/agent.py` (`Runner`) |

Les workers ne sont jamais joignables directement : chacun interroge le relais en long-polling (`w_poll`)
et s'y déclare avec un identifiant stable (`COCKPIT_WORKER_ID`, par défaut `HOSTNAME`) et sa liste de projets.

## Outils exposés aux agents

| Outil | Disponible | Validation | Effet |
|---|---|---|---|
| `lancer_service` | orchestrateur | oui (auto-approuvable, `trust.tools`) | `helm upgrade --install` du chart `vscode-python`, release `cockpit-<nom>` |
| `lister_services` | orchestrateur | non | releases `cockpit-*`, état des pods, quota du namespace |
| `arreter_service` | orchestrateur | toujours | `helm uninstall` (supprime aussi le stockage du pod) |
| `creer_depot` | tous | oui (auto-approuvable si privé) | dépôt GitHub vide ; avec `relier=true` (défaut), ajout d'`origin` et push de la branche courante |
| `chercher_depots` | tous | non | recherche dans les dépôts GitHub de l'utilisateur (nom, description, README, code) |
| `confier_au_projet` | tous | non | crée une tâche fille pour un autre projet (ou `"orchestrateur"`) |
| `etat_projet` | tous | non | état d'un projet vu du relais : pod en ligne, dernières tâches, PR, CI, question en attente |
| `lancer_en_parallele` | tous | oui (auto-approuvable) | 1 à 5 tâches filles, chacune dans un worktree git |

Les règles de validation sont dans `config/policy.yaml` (`allow_tools`, `ask_tools`, `trust`).

## Parcours de bout en bout

### 1. Arrivée d'une demande et routage

1. Le téléphone crée une tâche au relais (`statut = pending`), avec ou sans projet.
2. Une tâche **sans projet** (puce *Auto*) n'a pas de `worker_id` : seul l'orchestrateur la reçoit.
   Il la passe en `routing` et appelle `router.route()` sur la liste globale des projets (`all_projects`,
   fournie par le relais à chaque poll). Le routeur Haiku (repli : mots-clés) renvoie un projet et une confiance.
3. Sous le seuil de confiance (70 %), la tâche passe en `needs_project` : l'utilisateur choisit le projet sur le
   téléphone (`choisir_projet`), et le choix revient à l'orchestrateur au poll suivant.
4. Si le projet est hébergé par un autre pod, l'orchestrateur appelle `w_affecter` (`Worker.hand_over`) : le
   relais réaffecte la tâche au worker du projet (`worker_id`, `livree = 0`, `pending`), qui la recevra à son poll.
5. Sinon, l'agent tourne localement (`Runner.run`) : `queued`, puis `running`, éventuellement
   `waiting_approval`, enfin `done`, `error` ou `cancelled`.

Les demandes de nouveau projet, de nouveau pod ou de reprise d'un dépôt GitHub sont routées vers le projet
`onyxia` de l'orchestrateur (sa description dans `config/projects.yaml` le prévoit explicitement).

### 2. Création d'un projet

Deux chemins mènent au même résultat : un pod `cockpit-<nom>` dont le worker déclare le projet `<nom>`.

**a) Depuis le téléphone (*＋ Nouveau pod*, sans agent).**
1. `lancer_pod` (relais) vérifie le nom (`[a-z0-9-]`, 30 caractères), l'URL du dépôt et la branche, refuse un
   projet déjà connu et exige un orchestrateur en ligne (sinon HTTP 409).
2. Le relais crée une tâche « 🚀 Nouveau pod » portant une `action` `lancer_service`, adressée à l'orchestrateur.
3. `Worker.run_action` vérifie qu'aucune release `cockpit-<nom>` n'existe (`verifier_nouveau`), **puis**
   crée le dépôt GitHub si *＋ Nouveau dépôt* est coché (`github.creer_depot`), et lance le service
   (`svc.lancer_nouveau`). La tâche passe en `done` (ou `error` avec la cause).

**b) Par un agent de l'orchestrateur (*Nouveau projet* sur *Auto*).**
Les consignes de `SERVICES_APPEND` imposent l'ordre suivant :
1. Questions préalables si l'essentiel manque (`poser_question`, puis arrêt jusqu'à la réponse).
2. `creer_depot(nom, relier=false)` : le dépôt est créé sans être relié au dossier de l'orchestrateur.
3. `lancer_service(nom, depot_git=<URL du dépôt>)`, après validation (carte avec ressources et quota restant).
4. `confier_au_projet(projet=<nom>, instruction=<cahier des charges complet>)`.

Pour un dépôt existant, l'agent le retrouve avec `chercher_depots`, puis enchaîne `lancer_service` et
`confier_au_projet`. Sans dépôt, le service démarre sur un dossier vide initialisé avec git.

### 3. Démarrage du pod de projet

1. Helm installe le chart avec `kubernetes.role=view`, l'identité et le token git de l'orchestrateur, le secret
   Vault `Claude` et `init.personalInit = scripts/onyxia-init.sh <nom>` (`services.helm_values`). Un ingress
   VSCode avec mot de passe aléatoire est ajouté quand l'ingress de l'orchestrateur sert de modèle ; `apercu=true`
   publie en plus le port 5000.
2. `onyxia-init.sh` clone le dépôt (ou `git init` relié à `origin` s'il est vide, ou dossier
   `~/work/<nom>` sans dépôt), clone le cockpit dans `~/work/.cockpit`, écrit un `config/projects.yaml` à un
   seul projet (`id = <nom>`) et lance `./run.sh` avec `COCKPIT_ROLE=projet` et `COCKPIT_AUTO_UPDATE=1`.
3. Au démarrage, `Worker.ensure_project_dirs` recrée le dossier du projet s'il manque encore.
4. Premier `w_poll` avec `hello` : le relais enregistre le worker, envoie la notification « Pod connecté »,
   et **rattache à ce worker les tâches qui attendaient ce projet** (voir ci-dessous).

Comptez 1 à 3 minutes entre le lancement et la connexion du worker.

### 4. Délégation : `confier_au_projet`

1. L'agent appelle `confier_au_projet(projet, instruction)`. L'instruction doit être autonome : l'agent cible ne
   voit pas la conversation d'origine.
2. Le worker crée la tâche fille par `w_taches` avec `defer=true` (`Worker.spawn_children`), avec la tâche
   courante pour parent. Le relais accepte trois cas :
   - **projet connu** (un worker l'a déclaré, même hors ligne) : `worker_id` = ce worker ; la tâche part à son
     prochain poll ;
   - **`"orchestrateur"`** : tâche sans projet ni worker, routée par l'orchestrateur (étape 1) ;
   - **projet inconnu au nom valide** (pod en cours de démarrage) : `attente_projet = 1`, sans worker. Le
     téléphone affiche « ⏳ En attente du pod ». La tâche est rattachée au premier worker qui déclare ce projet,
     ou passe en `error` si aucun ne l'a déclaré au bout de 30 min (`ATTENTE_PROJET_MAX`).
3. L'agent n'attend pas : il termine sa réponse ; l'utilisateur suit la tâche fille dans le cockpit, et un
   agent peut la suivre avec `etat_projet`.

Un worker de projet utilise le même outil dans l'autre sens : tout ce qui sort de son projet (nouveau projet,
nouveau pod, autre dépôt) part à l'orchestrateur avec `confier_au_projet(projet="orchestrateur")`
(consigne `PROJECT_APPEND`).

### 5. Suivi des pods et des workers

- **Présence** : chaque poll met à jour `workers.vu_le`. Un worker est *en ligne* s'il a interrogé le relais dans
  les 45 dernières secondes (`WORKER_EN_LIGNE`). Le tableau de bord affiche les pods vus dans les 7 derniers
  jours (`WORKERS_VISIBLES`).
- **Projets** : `projets()` fusionne les projets déclarés par tous les workers ; si deux workers déclarent le même
  identifiant, celui qui est en ligne l'emporte.
- **État du pod** : à chaque poll, le worker publie `pod_state()` (CPU, mémoire, disque, agents actifs, état git
  de chaque projet, worktrees, URL VSCode, aperçu, usage), mis en cache 20 s.
- **Services** : `lister_services` (orchestrateur) donne l'état helm et l'état des pods Kubernetes de chaque
  release `cockpit-*`, avec le quota du namespace.
- **Tâches** : `etat_projet` (`w_projet_etat`) renvoie les 8 dernières tâches d'un projet avec statut, résultat,
  modifications, commits, push, tests, CI, PR et question en attente.
- **Redémarrage** : un `hello` d'un worker connu passe en `error` ses tâches livrées non terminées
  (« Interrompue : le worker a redémarré ») et expire ses validations en attente. Un nouveau processus avec le même
  identifiant remplace l'ancien (`superseded`).
- **Mise à jour** : les pods de projet font `git pull` du cockpit toutes les 10 minutes, seulement entre deux
  tâches, puis redémarrent leur worker.
- **Dépôts GitHub** : l'orchestrateur republie la liste des dépôts toutes les 10 minutes, et aussitôt après une
  création de dépôt.

## États

### États d'un projet

| État | Signe | Passage |
|---|---|---|
| **Inexistant** | aucun worker ne déclare l'identifiant | — |
| **Demandé** | release `cockpit-<nom>` installée, pod en démarrage ; tâches confiées en `attente_projet` | `lancer_service` / `lancer_pod` |
| **En ligne** | worker connecté (poll de moins de 45 s), projet présent dans les puces | premier `w_poll` du worker |
| **Hors ligne** | worker connu mais silencieux ; les tâches qui lui sont confiées attendent son retour | pod arrêté, worker planté ou redémarrage |
| **Arrêté** | release supprimée par `arreter_service` ; le relais garde la dernière déclaration du worker, donc le projet reste connu, hors ligne | `helm uninstall` |
| **Échec de lancement** | tâche de lancement en `error` (nom pris, quota, helm) ; aucun pod | erreur de `lancer` |

### États d'une tâche (`store.Task.status`)

`pending` → (`routing` → `needs_project`) → `queued` → `running` ⇄ `waiting_approval` → `done` | `error` | `cancelled`.
Le drapeau `waiting_project` (colonne `attente_projet`) s'ajoute à `pending` tant que le pod cible n'est pas connecté,
pendant 30 min au plus : passé ce délai, la tâche passe en `error`.

## Limites connues

- **Une session cockpit ne peut pas écrire de code dans le pod d'un autre projet.** Chaque pod a son propre disque :
  un agent ne voit que le système de fichiers de son pod, et la politique demande une validation pour toute
  écriture hors du dossier du projet (même le mode confiance 🛡 ne l'étend qu'à `~/work` **du même pod**).
  Pour modifier un autre projet, il faut **lui déléguer la tâche** avec `confier_au_projet(projet=<id>)` ; pour
  un projet qui n'existe pas encore, avec `confier_au_projet(projet="orchestrateur")`. L'orchestrateur ne lit
  pas non plus le code des autres pods : ses consignes lui interdisent de cloner le dépôt chez lui.
- **Pas de lancement de pod hors de l'orchestrateur.** Seul le pod admin peut installer des releases helm ;
  si l'orchestrateur est hors ligne, *＋ Nouveau pod* est refusé (409) et les tâches *Auto* ou confiées à
  `"orchestrateur"` attendent qu'il revienne.
- **Une tâche en `attente_projet` expire au bout de 30 min.** Si le pod ne se connecte pas à temps (quota
  atteint, échec helm, script d'init en erreur, démarrage très lent) ou si l'identifiant confié ne correspond pas
  au nom du service, la tâche passe en `error` (« Expirée : le pod du projet ne s'est pas connecté au bout de
  30 min… ») et une notification est envoyée ; il faut la relancer une fois le pod en ligne. L'expiration est
  vérifiée à chaque poll de worker et à chaque rafraîchissement du téléphone (constante `ATTENTE_PROJET_MAX`
  de `web/api.php`). Le nom passé à `confier_au_projet` doit être exactement celui de `lancer_service`.
- **Pas d'accusé de réception entre agents.** La délégation est « tire et oublie » : l'agent d'origine ne reçoit
  ni le résultat ni les questions de l'agent cible. Le suivi passe par le téléphone ou par `etat_projet`, à la
  demande.
- **Le contexte ne suit pas la délégation.** L'agent cible ne voit ni la conversation d'origine, ni ses fichiers,
  ni ses captures : tout doit tenir dans l'instruction (10 000 caractères au plus).
- **Dépôts orphelins possibles.** Côté téléphone, l'existence de la release est vérifiée avant de créer le dépôt,
  mais un échec de `helm` après la création laisse un dépôt GitHub vide. Côté agent, `creer_depot` puis un
  refus de `lancer_service` laissent aussi un dépôt sans pod.
- **`lancer_service` appelé par un agent ne vérifie pas l'existence de la release** (`helm upgrade --install`) :
  relancer un nom existant met à jour et redémarre le service au lieu d'échouer. Seul le chemin du téléphone
  (`lancer_nouveau`) le refuse. La carte de validation est le garde-fou.
- **L'arrêt détruit le stockage.** `arreter_service` supprime la release et son volume : le travail non poussé est
  perdu. Le relais ne retire pas le worker de son registre : le projet reste listé, hors ligne.
- **Un projet par pod de projet, liste statique.** `config/projects.yaml` est écrit une fois par le script
  d'init ; deux services ne peuvent pas porter le même identifiant de projet.
- **Tâches perdues au redémarrage.** Un worker qui redémarre (plantage, mise à jour automatique, pod relancé)
  passe en erreur les tâches qu'il exécutait ; il faut les relancer.
- **Pas d'arrêt automatique des pods inactifs** : le quota du namespace se consomme jusqu'à `arreter_service`.
- **Token GitHub requis** : `creer_depot`, `chercher_depots`, `ouvrir_pr` et la fusion de PR utilisent
  `GIT_PERSONAL_ACCESS_TOKEN` ; pour créer un dépôt, il faut le droit *Administration : écriture* (token à grain
  fin) ou la portée `repo`.
