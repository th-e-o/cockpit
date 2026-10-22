"""Exécution d'une tâche par un agent Claude Code, avec validation humaine."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from pathlib import Path

from claude_agent_sdk import (
    RateLimitEvent,
    create_sdk_mcp_server,
    tool,
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

import base64

from . import apercu, github, gitstate, projet
from . import services as svc
from .policy import ALLOW, DENY, Policy
from .router import Project, choose_model
from .store import Approval, Store, Task, new_id

SYSTEM_APPEND = """
Tu es piloté à distance depuis un cockpit mobile. L'utilisateur lit tes réponses sur téléphone.
Sois autonome :
- mène la tâche jusqu'au bout sans t'arrêter aux étapes intermédiaires : explore, agis, vérifie ;
- quand un détail n'est pas précisé (nom, valeur par défaut, emplacement, choix technique courant),
  choisis toi-même l'option la plus raisonnable, puis signale ce choix dans ta réponse ;
- ne t'arrête pour poser une question que si l'information est indispensable et impossible à deviner.
  Tu ne peux pas poser de question en cours de tâche : formule-la dans ta réponse finale ; l'utilisateur
  répondra dans la suite de la conversation, et tu reprendras avec tout le contexte.
Pour une tâche en plusieurs étapes, tiens ta liste à jour avec TodoWrite : l'utilisateur la voit
se cocher en direct sur son téléphone.
Pour la réponse finale, sois concis et utilise ces trois rubriques (omets une rubrique vide) :
**Fait** — ce qui a été fait, en quelques puces ;
**Choix** — les décisions prises à la place de l'utilisateur ;
**À faire** — ce qui reste à décider ou ta question, s'il y en a.
Si tu as besoin d'une réponse de l'utilisateur pour continuer (choix, information manquante, confirmation),
appelle poser_question juste avant ta réponse finale, avec des choix courts quand c'est possible : il la voit en
évidence sur son téléphone et y répond d'un geste.
Juste avant cette réponse finale, appelle proposer_suite avec 1 à 4 prochaines étapes concrètes et utiles
(par exemple : tester, publier, fusionner, l'amélioration suivante) ; l'utilisateur les lance d'un geste.
Pour une simple question sans suite évidente, tu peux t'en passer.
Si le projet n'a pas de dépôt distant et qu'il faut publier le travail, crée-le avec creer_depot (validation).
Pour ouvrir une pull request, utilise ouvrir_pr (il pousse la branche si besoin).
Pour une interface web, vérifie le rendu : lance le serveur de développement en arrière-plan, puis appelle
capturer_ecran avec son adresse locale ; tu vois la capture, et l'utilisateur aussi sur son téléphone.
Si le projet a des tests, déclare leur commande dans .cockpit.yml (« test: pytest -q ») : le cockpit les lance
après chaque tâche et te signale les échecs.
Si l'utilisateur t'envoie un message pendant que tu travailles, tu es interrompu et le reçois : tiens-en compte
et poursuis.
Le cockpit affiche déjà la liste des fichiers modifiés et leurs diffs : inutile de les recopier.
Pour publier ton travail, crée une branche et pousse-la avec « git push -u origin <branche> » : c'est
automatique. Un push sur main ou master demande une validation sur le téléphone.
Certaines actions nécessitent une validation humaine ; si une action est refusée, ne tente pas de la
contourner par un autre moyen, explique plutôt ce qui était prévu.
"""


SERVICES_APPEND = """
Tu peux lancer, lister et arrêter des services Onyxia (chacun devient un projet pilotable) avec les outils
lancer_service, lister_services et arreter_service. N'utilise pas helm directement pour cela.
Si l'utilisateur parle d'un de ses dépôts ou de son code GitHub qui n'est pas encore un projet du cockpit, trouve-le
avec chercher_depots (plusieurs requêtes si besoin : synonymes, sigles), puis lance un service dessus avec
lancer_service (depot_git = son URL) pour qu'il devienne un projet ; dis à l'utilisateur ce que tu as trouvé.
Pour suivre un projet confié à un autre pod (avancement, PR, erreurs), utilise etat_projet.
Pour un NOUVEAU projet : assure-toi d'abord d'avoir l'essentiel pour en écrire le cahier des charges (sinon
poser_question, toutes les questions en une fois, et attends les réponses) ; puis crée son dépôt GitHub avec
creer_depot(nom, relier=false), lance un service dessus (lancer_service, depot_git = URL du dépôt créé) et confie-lui
un cahier des charges complet (objectif, fonctionnalités, choix techniques, réponses de l'utilisateur, déploiement)
avec confier_au_projet.
Tu ne peux pas lire le code d'un autre pod depuis ici : après avoir lancé un service pour travailler sur un dépôt,
transmets la demande de l'utilisateur au nouveau projet avec confier_au_projet (projet = nom du service) ; elle
partira toute seule dès que son pod sera prêt. Ne propose pas de cloner le dépôt ici.
Pour un nouveau projet sans dépôt git, lance le service sans dépôt (un dossier vide sera créé) ; choisis
toi-même un nom court à partir de la demande et garde les ressources par défaut sauf besoin explicite.
"""


PARALLEL_APPEND = """
Si la demande se découpe en sous-tâches indépendantes et longues (modules distincts, pistes à explorer
séparément), tu peux les confier à d'autres agents Claude avec l'outil lancer_en_parallele : chacune tourne
dans son propre worktree git (branche dédiée), partie du dernier commit — commite d'abord ce dont elles ont besoin.
Ne l'utilise pas pour des tâches courtes ou très liées : fais-les toi-même.
"""

FIRST_APPEND = """
C'est le premier message de cette conversation. Si la demande est importante (nouveau projet, fonctionnalité
conséquente, travail à confier à un autre pod) et qu'il te manque des informations essentielles pour bien la mener
(objectif et usage, fonctionnalités attendues, technologies imposées, données ou API externes, hébergement et
déploiement, accès et secrets, contraintes), NE commence PAS le travail : pose d'abord toutes tes questions en une
fois avec poser_question (une question par sujet, avec des choix courts), et arrête-toi là. Quand l'utilisateur aura
répondu, fais le travail (ou transmets un cahier des charges complet, réponses comprises). Si la demande est claire ou
simple, avance directement sans poser de question.
"""

PROJECT_APPEND = """
Tu travailles dans le pod du projet « {project} », piloté par le cockpit. Tu ne peux ni créer ni démarrer de pod :
c'est l'orchestrateur qui le fait. Pour un nouveau projet, un nouveau pod, ou une demande qui concerne un autre
dépôt, ne crée pas de dossier ici : transmets la demande complète avec confier_au_projet(projet="orchestrateur")
et dis-le à l'utilisateur.
"""

PREVIEW_APPEND = """
Le port {port} de ce pod est publié pour l'utilisateur à {url} : fais écouter le serveur de développement sur
0.0.0.0:{port} pour qu'il puisse ouvrir l'application lui-même.
"""

WORKTREE_APPEND = """
Un autre agent travaille en même temps dans le dossier principal du projet : tu travailles dans un worktree git
dédié ({cwd}, branche {branch}). Reste dans ce dossier et commite ton travail sur cette branche ; l'utilisateur
la fusionnera ou la jettera depuis le cockpit.
"""

MAX_AGENTS = int(os.environ.get("COCKPIT_MAX_AGENTS", "3"))
# Choix du téléphone -> alias de modèle de Claude Code (toujours la dernière version de chaque famille)
MODELS = {"rapide": "haiku", "standard": "sonnet", "approfondi": "opus"}
PARALLEL_TOOL = "mcp__cockpit__lancer_en_parallele"
REPO_TOOL = "mcp__cockpit__creer_depot"
SHOT_TOOL = "mcp__cockpit__capturer_ecran"
PR_TOOL = "mcp__cockpit__ouvrir_pr"
CI_POLL_S, CI_MAX_S = 45, 1200
NEXT_TOOL = "mcp__cockpit__proposer_suite"
FILE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
log = logging.getLogger("cockpit.agent")


def _name(path: str) -> str:
    return path.rstrip("/").rsplit("/", 1)[-1] or path


def _clip(s: str, n: int = 60) -> str:
    s = " ".join(str(s).split())
    return s if len(s) <= n else s[: n - 1] + "…"


def tool_label(tool: str, i: dict) -> str | None:
    """Étape lisible sur le téléphone (« 📖 Lit README.md ») ; None pour les outils sans intérêt à afficher."""
    short = tool.removeprefix(svc.TOOL_PREFIX).removeprefix("mcp__cockpit__")
    match short:
        case "ToolSearch":
            return None
        case "Read":
            return f"📖 Lit {_name(i.get('file_path', ''))}"
        case "Write":
            return f"📝 Crée {_name(i.get('file_path', ''))}"
        case "Edit" | "MultiEdit" | "NotebookEdit":
            return f"✏️ Modifie {_name(i.get('file_path') or i.get('notebook_path', ''))}"
        case "Grep":
            return f"🔎 Cherche « {_clip(i.get('pattern', ''), 40)} »"
        case "Glob":
            return f"🔎 Liste {_clip(i.get('pattern', ''), 40)}"
        case "Bash":
            return f"⚙️ {_clip(i.get('description') or i.get('command', ''))}"
        case "WebSearch":
            return f"🌐 Recherche « {_clip(i.get('query', ''), 50)} »"
        case "WebFetch":
            return f"🌐 Consulte {_clip(i.get('url', '').split('//')[-1].split('/')[0], 40)}"
        case "Task" | "Agent":
            return f"🤖 {_clip(i.get('description') or 'Sous-agent')}"
        case "TodoWrite":
            return "📋 Met à jour son plan"
        case "lancer_service":
            return f"🚀 Lance le service {i.get('nom', '')}"
        case "lister_services":
            return "📋 Liste les services"
        case "arreter_service":
            return f"🛑 Arrête le service {i.get('nom', '')}"
        case "proposer_suite":
            return None  # affiché comme « Prochaines étapes » sous la réponse
        case "confier_au_projet":
            return f"📨 Confie la demande au projet {i.get('projet', '')}"
        case "etat_projet":
            return f"📊 Consulte l'état du projet {i.get('projet') or '(tous)'}"
        case "poser_question":
            return "❓ Te pose une question"
        case "chercher_depots":
            return f"🔎 Cherche « {_clip(i.get('requete', ''), 40)} » dans tes dépôts GitHub"
        case "capturer_ecran":
            return "📸 Capture l'écran de l'application"
        case "ouvrir_pr":
            return f"🔀 Ouvre une pull request : {_clip(i.get('titre', ''), 50)}"
        case "creer_depot":
            return f"📦 Crée le dépôt GitHub {i.get('nom', '')}"
        case "lancer_en_parallele":
            return f"🔀 Lance {len(i.get('taches') or [])} Claude en parallèle"
    return f"🔧 {short}"


def _short(value, limit: int = 600) -> str:
    s = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return s if len(s) <= limit else s[:limit] + "…"


def _text(text: str, error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], **({"is_error": True} if error else {})}


_PLACEHOLDER = re.compile(r"<[^<>\n]{2,80}>|à compléter", re.I)


def clean_next_steps(items) -> list[dict]:
    """Prochaines étapes proposées par l'agent : 1 à 4, chacune {titre, instruction}, sans champ à compléter."""
    out = []
    for x in items if isinstance(items, list) else []:
        if isinstance(x, dict) and _PLACEHOLDER.search(str(x.get("instruction", ""))):
            continue  # une étape lancée d'un geste doit être complète
        if isinstance(x, dict) and str(x.get("titre", "")).strip() and str(x.get("instruction", "")).strip():
            out.append({"titre": " ".join(str(x["titre"]).split())[:90], "instruction": str(x["instruction"]).strip()[:2000]})
    return out[:4]


async def create_repo(cwd: str, args: dict) -> str:
    """Crée le dépôt GitHub, le relie au projet courant (origin) et y pousse la branche courante s'il y a des commits."""
    repo = await github.creer_depot(args.get("nom", ""), bool(args.get("prive", True)), args.get("description", ""))
    lines = [f"Dépôt {'privé ' if repo['private'] else ''}{repo['name']} créé : {repo['url']}"]
    if args.get("relier", True) and await gitstate.toplevel(cwd):
        rc, _ = await gitstate.git(cwd, "remote", "get-url", "origin")
        if rc == 0:
            lines.append("Le projet a déjà un dépôt distant « origin » : il n'a pas été modifié.")
        else:
            await gitstate.git(cwd, "remote", "add", "origin", repo["url"] + ".git")
            if await gitstate.head(cwd):
                rc, out = await gitstate.git(cwd, "push", "-u", "origin", "HEAD", timeout=120)
                lines.append("Branche courante poussée (origin)." if rc == 0 else f"Push impossible : {out[-300:]}")
            else:
                lines.append("Dépôt relié (origin) ; aucun commit à pousser pour l'instant.")
    return "\n".join(lines)


async def open_pr(cwd: str, args: dict) -> dict:
    """Pousse la branche courante si besoin et ouvre une pull request vers la branche par défaut."""
    rc, remote = await gitstate.git(cwd, "remote", "get-url", "origin")
    target = github.owner_repo(remote) if rc == 0 else None
    if not target:
        raise github.GitHubError("pas de dépôt GitHub « origin » : crée-le d'abord avec creer_depot")
    rc, branch = await gitstate.git(cwd, "rev-parse", "--abbrev-ref", "HEAD")
    if rc or branch in ("HEAD", "main", "master"):
        raise github.GitHubError("place-toi sur une branche de travail (pas main/master) avant d'ouvrir une PR")
    info = await gitstate.branch_info(cwd)
    if not info.get("upstream") or info.get("ahead"):
        rc, out = await gitstate.git(cwd, "push", "-u", "origin", branch, timeout=120)
        if rc:
            raise github.GitHubError(f"push de {branch} impossible : {out[-300:]}")
    return await github.creer_pr(*target, branch, args.get("titre") or branch, args.get("description", ""),
                                 args.get("base", ""), bool(args.get("brouillon")))


def detect_question(result: str) -> dict | None:
    """Question restée dans la réponse finale (si l'agent n'a pas appelé poser_question) : dernières phrases en « ? »."""
    tail = re.sub(r"```.*?```", " ", (result or "").strip()[-800:], flags=re.S)
    tail = re.sub(r"\*\*[^*\n]+\*\*\s*[—:-]?\s*", "", tail)  # rubriques (**À faire** —) et gras
    found = [q.strip(" *-•\n") for q in re.findall(r"[^.!?\n]*\?", tail)
             if len(q.strip()) > 8 and "://" not in q and "`" not in q]
    return {"text": " ".join(found[-3:])[:600], "options": [], "detected": True} if found else None


def cockpit_server(cwd: str, on_next, spawn=None, pub=None, upload=None, delegate=None, on_question=None, status=None):
    """Outils propres au cockpit : prochaines étapes, dépôts et PR GitHub, captures d'écran, agents en parallèle."""

    @tool("proposer_suite",
          "Propose à l'utilisateur 1 à 4 prochaines étapes concrètes, qu'il pourra lancer d'un geste depuis son "
          "téléphone (ou depuis la notification de fin). titre : quelques mots ; instruction : la consigne complète "
          "que tu recevras s'il la choisit, envoyée telle quelle : JAMAIS de champ à compléter (« <…> », « à "
          "compléter ») ; s'il te manque une information, pose plutôt la question avec poser_question. "
          "À appeler une fois, juste avant ta réponse finale.",
          {"type": "object", "properties": {"etapes": {"type": "array", "minItems": 1, "maxItems": 4, "items": {
              "type": "object", "properties": {"titre": {"type": "string"}, "instruction": {"type": "string"}},
              "required": ["titre", "instruction"]}}}, "required": ["etapes"]})
    async def proposer_suite(args: dict) -> dict:
        steps = clean_next_steps(args.get("etapes"))
        if not steps:
            return _text("etapes : 1 à 4 objets {titre, instruction}", error=True)
        on_next(steps)
        return _text(f"{len(steps)} prochaine(s) étape(s) proposée(s) à l'utilisateur. Termine par ta réponse finale.")

    @tool("creer_depot",
          "Crée un dépôt GitHub vide sur le compte de l'utilisateur (privé par défaut), puis le relie au projet courant "
          "(remote origin) et y pousse la branche courante si le projet a des commits. À utiliser quand un projet "
          "n'a pas encore de dépôt distant ou quand l'utilisateur en demande un. Nécessite une validation.",
          {"type": "object", "properties": {"nom": {"type": "string"}, "prive": {"type": "boolean"},
                                            "description": {"type": "string"}, "relier": {"type": "boolean"}},
           "required": ["nom"]})
    async def creer_depot(args: dict) -> dict:
        try:
            return _text(await create_repo(cwd, args))
        except github.GitHubError as e:
            return _text(str(e), error=True)

    @tool("capturer_ecran",
          "Capture d'écran d'une page servie dans ce pod (http://localhost:<port>/…), en vue téléphone par défaut "
          "(mobile=false : vue ordinateur ; page_entiere=true : toute la hauteur). Tu reçois l'image, et "
          "l'utilisateur la voit sur son téléphone avec la légende.",
          {"type": "object", "properties": {"url": {"type": "string"}, "mobile": {"type": "boolean"},
                                            "page_entiere": {"type": "boolean"}, "legende": {"type": "string"}},
           "required": ["url"]})
    async def capturer_ecran(args: dict) -> dict:
        try:
            data = await apercu.capture(args.get("url", ""), args.get("mobile", True), bool(args.get("page_entiere")))
        except apercu.ApercuError as e:
            return _text(str(e), error=True)
        b64 = base64.b64encode(data).decode()
        sent = ""
        if upload:
            try:
                await upload(b64, str(args.get("legende") or args.get("url"))[:200])
                sent = " Capture envoyée à l'utilisateur."
            except Exception as e:
                sent = f" (envoi au téléphone impossible : {e})"
        return {"content": [{"type": "image", "data": b64, "mimeType": "image/jpeg"},
                            {"type": "text", "text": f"Capture de {args.get('url')}.{sent}"}]}

    @tool("ouvrir_pr",
          "Ouvre une pull request GitHub depuis la branche courante (poussée si besoin) vers la branche par défaut "
          "(ou base). titre : court et explicite ; description : contexte, changements, tests, points d'attention.",
          {"type": "object", "properties": {"titre": {"type": "string"}, "description": {"type": "string"},
                                            "base": {"type": "string"}, "brouillon": {"type": "boolean"}},
           "required": ["titre"]})
    async def ouvrir_pr(args: dict) -> dict:
        try:
            pr = await open_pr(cwd, args)
        except github.GitHubError as e:
            return _text(str(e), error=True)
        if pub:
            pub(type="pr", **pr)
        return _text(f"Pull request #{pr['number']} ouverte : {pr['url']} ({pr['head']} → {pr['base']}).")

    @tool("chercher_depots",
          "Cherche dans les dépôts GitHub de l'utilisateur (nom, description, README et code) ceux qui correspondent "
          "à une requête courte (mots-clés, éventuellement plusieurs essais : synonymes, sigles). Renvoie le nom, "
          "l'URL, la branche par défaut et la raison de chaque résultat.",
          {"type": "object", "properties": {"requete": {"type": "string"}}, "required": ["requete"]})
    async def chercher_depots(args: dict) -> dict:
        try:
            found = await github.chercher(args.get("requete", ""))
        except github.GitHubError as e:
            return _text(str(e), error=True)
        if not found:
            return _text("Aucun dépôt ne correspond : essaie d'autres mots-clés (synonymes, sigles, anglais).")
        return _text(json.dumps([{k: x.get(k) for k in ("name", "url", "branch", "private", "description", "pushed", "why")}
                                 for x in found], ensure_ascii=False))

    @tool("poser_question",
          "Pose une ou plusieurs questions à l'utilisateur quand tu as besoin de ses réponses pour bien faire le travail. "
          "Elles s'affichent en évidence sur son téléphone (et dans la notification) : un choix par question en "
          "boutons, plus un champ libre ; il répond en une fois. question : introduction ou question unique ; "
          "questions : 1 à 6 questions {question, choix (2 à 4 réponses courtes, facultatif)}. Regroupe TOUTES tes "
          "questions dans un seul appel, juste avant ta réponse finale ; les réponses arrivent comme suite.",
          {"type": "object", "properties": {
              "question": {"type": "string"},
              "choix": {"type": "array", "items": {"type": "string"}, "maxItems": 4},
              "questions": {"type": "array", "maxItems": 6, "items": {"type": "object", "properties": {
                  "question": {"type": "string"}, "choix": {"type": "array", "items": {"type": "string"}, "maxItems": 4}},
                  "required": ["question"]}}}})
    async def poser_question(args: dict) -> dict:
        clean = lambda s, n: " ".join(str(s or "").split())[:n]  # noqa: E731
        opts = lambda c: [clean(o, 80) for o in (c or []) if str(o).strip()][:4]  # noqa: E731
        items = [{"question": clean(q.get("question"), 400), "options": opts(q.get("choix"))}
                 for q in (args.get("questions") or []) if isinstance(q, dict) and clean(q.get("question"), 400)][:6]
        text = clean(args.get("question"), 1000)
        if len(items) == 1 and not text:  # une seule question : forme simple
            text, options, items = items[0]["question"], items[0]["options"], []
        else:
            options = opts(args.get("choix"))
        if not text and not items:
            return _text("question vide", error=True)
        if on_question:
            on_question({"text": text or "Quelques questions avant de commencer :", "options": [] if items else options,
                         **({"items": items} if items else {})})
        n = len(items) or 1
        return _text(f"{n} question(s) transmise(s) à l'utilisateur. Termine par ta réponse finale, courte, sans les "
                     "répéter : tu reprendras quand il aura répondu.")

    tools = [proposer_suite, creer_depot, capturer_ecran, ouvrir_pr, chercher_depots, poser_question]
    if status:
        @tool("etat_projet",
              "État d'un projet du cockpit, vu du relais : pod en ligne ou non, dernières tâches (statut, résultat, "
              "fichiers modifiés, commits, branche poussée, tests, CI, pull requests, question en attente). Sans projet : "
              "liste des projets. À utiliser pour suivre un projet confié à un autre pod, au lieu de deviner.",
              {"type": "object", "properties": {"projet": {"type": "string"}}})
        async def etat_projet(args: dict) -> dict:
            try:
                return _text(json.dumps(await status(str(args.get("projet") or "")), ensure_ascii=False))
            except Exception as e:
                return _text(f"état indisponible : {e}", error=True)

        tools.append(etat_projet)
    if spawn:
        @tool("lancer_en_parallele",
              "Lance jusqu'à 5 autres agents Claude en parallèle, chacun avec une instruction autonome et complète (il ne "
              "voit pas cette conversation). Chaque agent travaille dans son propre worktree git, parti du dernier commit, "
              "et l'utilisateur suit et fusionne chaque branche depuis le cockpit. projet : identifiant du projet "
              "(par défaut, le projet courant). Nécessite une validation sur le téléphone.",
              {"type": "object", "properties": {"taches": {"type": "array", "minItems": 1, "maxItems": 5, "items": {
                  "type": "object", "properties": {"instruction": {"type": "string"}, "projet": {"type": "string"}},
                  "required": ["instruction"]}}}, "required": ["taches"]})
        async def lancer(args: dict) -> dict:
            items = args.get("taches")
            if not isinstance(items, list) or not 1 <= len(items) <= 5 or not all(
                    isinstance(t, dict) and str(t.get("instruction", "")).strip() for t in items):
                return _text("taches : 1 à 5 objets avec une instruction", error=True)
            try:
                ids = await spawn(items)
            except Exception as e:
                return _text(f"lancement impossible : {e}", error=True)
            return _text(f"{len(ids)} agent(s) lancé(s) en parallèle (tâches {', '.join(ids)}). "
                         "L'utilisateur suit leur avancement dans le cockpit ; tu n'as pas à les attendre.")

        tools.append(lancer)
    if delegate:
        @tool("confier_au_projet",
              "Confie une consigne à un autre projet du cockpit (son agent travaille dans son propre pod, sur son code). "
              "Le projet peut être un service que tu viens de lancer : la consigne partira dès que son pod sera connecté. "
              "projet=\"orchestrateur\" : la consigne va à l'orchestrateur, qui crée les pods et les nouveaux projets. "
              "instruction : complète et autonome (l'autre agent ne voit pas cette conversation), en reprenant la demande "
              "de l'utilisateur.",
              {"type": "object", "properties": {"projet": {"type": "string"}, "instruction": {"type": "string"}},
               "required": ["projet", "instruction"]})
        async def confier(args: dict) -> dict:
            projet, instruction = str(args.get("projet", "")).strip(), str(args.get("instruction", "")).strip()
            if not projet or not instruction:
                return _text("projet et instruction obligatoires", error=True)
            try:
                ids = await delegate(projet, instruction)
            except Exception as e:
                return _text(f"impossible de confier la consigne : {e}", error=True)
            return _text(f"Consigne confiée au projet {projet} (tâche {ids[0]}) : elle partira dès que son pod sera "
                         "connecté, et l'utilisateur la suit dans le cockpit. Tu n'as pas à l'attendre.")

        tools.append(confier)
    return create_sdk_mcp_server("cockpit", tools=tools)


class Runner:
    def __init__(self, store: Store, policy: Policy, services: bool = False, max_agents: int = MAX_AGENTS):
        self.services = services  # orchestrateur : outils de gestion des services
        self.store = store
        self.policy = policy
        self.max_agents = max_agents
        self.slots = asyncio.Semaphore(max_agents)  # agents simultanés dans ce pod
        self.conv_locks: dict[str, asyncio.Lock] = {}  # une tâche à la fois par conversation (session partagée)
        self.main_locks: dict[str, asyncio.Lock] = {}  # une tâche à la fois dans le dossier principal d'un projet
        self.main_users: dict[str, int] = {}  # tâches affectées au dossier principal, par projet
        self.workspaces: dict[str, str] = {}  # "conversation:projet" -> worktree ("" : dossier principal)
        self.running: set[str] = set()  # tâches dont l'agent tourne
        self.clients: dict[str, ClaudeSDKClient] = {}  # task_id -> client actif
        # Création de tâches filles (fournie par le worker) : async (parent, [{"instruction", "projet"}]) -> [ids]
        self.spawn_tasks = None
        self.upload_image = None  # async (task_id, jpeg_base64, légende) -> id, fourni par le worker
        self.preview: dict | None = None  # URL publique du port utilisateur du pod {url, port}
        self.steers: dict[str, list[str]] = {}  # messages de l'utilisateur pendant une tâche
        self.hard = False  # mode hard (téléphone) : plus aucune validation, seuls les refus (deny) de la politique restent
        self.trust: dict[str, float] = {}  # mode confiance : conversation -> fin (activé depuis le téléphone)
        self.on_rate_limit = None  # RateLimitInfo -> None (barre d'usage), fourni par le worker
        self.project_status = None  # async (projet) -> état vu du relais (outil etat_projet), fourni par le worker
        self.session_usage = None  # () -> utilisation de la session de 5 h (0-1) ou None, fourni par le worker
        self.refresh_usage = None  # async () -> utilisation relue juste après une tâche, fourni par le worker
        self._background: set[asyncio.Task] = set()

    @staticmethod
    def _lock(locks: dict[str, asyncio.Lock], key: str) -> asyncio.Lock:
        return locks.setdefault(key, asyncio.Lock())

    async def cancel(self, task: Task) -> None:
        client = self.clients.get(task.id)
        if client:
            await client.interrupt()
        for a in self.store.approvals.values():
            if a.task_id == task.id and a.future and not a.future.done():
                a.future.set_result("deny")
        self.store.task_update(task, status="cancelled")

    async def request_approval(self, task: Task, project: Project, tool: str, tool_input: dict, reason: str) -> bool:
        loop = asyncio.get_running_loop()
        approval = Approval(
            id=new_id(), task_id=task.id, project=project.id, tool=tool,
            tool_input=tool_input, reason=reason,
            expires=time.time() + self.policy.timeout, future=loop.create_future(),
        )
        self.store.approvals[approval.id] = approval
        self.store.task_update(task, status="waiting_approval")
        self.store.publish("approval", {"task_id": task.id, "approval": approval.public()})
        try:
            decision = await asyncio.wait_for(approval.future, timeout=self.policy.timeout)
        except asyncio.TimeoutError:
            decision = "timeout"
        approval.decision = decision
        self.store.publish("approval", {"task_id": task.id, "approval": approval.public()})
        self.store.audit("approval", task=task.id, project=project.id, tool=tool,
                         input=tool_input, reason=reason, decision=decision)
        if task.status == "waiting_approval":
            self.store.task_update(task, status="running")
        return decision == "approve"

    def _options(self, task: Task, project: Project, cwd: str, session: str | None,
                 edited: set[str]) -> ClaudeAgentOptions:
        approved: set[str] = set()

        async def pre_tool(input_data, tool_use_id, context):
            tool = input_data["tool_name"]
            tool_input = input_data.get("tool_input", {})
            d = self.policy.evaluate(tool, tool_input, cwd)
            if d.action != DENY and tool.startswith(svc.TOOL_PREFIX):
                d.reason = await svc.describe(tool, tool_input) or d.reason
            if d.action != DENY and tool == REPO_TOOL:
                d.reason = (f"Créer le dépôt GitHub {'privé' if tool_input.get('prive', True) else 'PUBLIC'} "
                            f"« {tool_input.get('nom', '?')} »" + (" et y publier ce projet" if tool_input.get("relier", True) else ""))
            if d.action != DENY and tool == PARALLEL_TOOL:
                n = len(tool_input.get("taches") or [])
                d.reason = f"Lancer {n} agent(s) Claude en parallèle, chacun dans son worktree git"
            if d.action == ALLOW:
                ok = True
            elif d.action == DENY:
                ok = False
            elif self.hard:
                ok, d.action = True, "hard"
                self.store.publish("message", {"task_id": task.id, "type": "trusted", "tool": tool, "text": d.reason})
            elif rule := self.policy.user_allowed(tool, tool_input, cwd):
                # Règle « Toujours autoriser » créée depuis le téléphone
                ok, d.action, d.reason = True, "rule", f"règle « {rule} » : {d.reason}"
                self.store.publish("message", {"task_id": task.id, "type": "trusted", "tool": tool, "text": d.reason})
            elif ((self.policy.trust_default or self.trusted(task))
                  and self.policy.trusted(tool, tool_input, cwd, extended=self.trusted(task))):
                # Validation de routine auto-approuvée (journalisée et signalée sur la carte) : par défaut, ou mode 🛡
                ok, d.action = True, "trust"
                self.store.publish("message", {"task_id": task.id, "type": "trusted", "tool": tool, "text": d.reason})
            else:
                ok = await self.request_approval(task, project, tool, tool_input, d.reason)
            self.store.audit("tool", task=task.id, project=project.id, tool=tool,
                             input=_short(tool_input, 2000), policy=d.action, reason=d.reason, allowed=ok)
            if ok:
                approved.add(input_data.get("tool_use_id") or tool_use_id or "")
                if tool in FILE_TOOLS:
                    edited.add(tool_input.get("file_path") or tool_input.get("notebook_path") or "")
                decision, why = "allow", d.reason
            else:
                why = d.reason if d.action == DENY else f"refusé par l'utilisateur : {d.reason}"
                self.store.publish("message", {"task_id": task.id, "type": "blocked", "tool": tool, "text": why})
                decision = "deny"
            return {"hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": decision,
                "permissionDecisionReason": why,
            }}

        async def can_use_tool(tool, tool_input, context):
            # Filet de sécurité : n'autorise que ce que le hook a déjà approuvé.
            if context.tool_use_id in approved:
                return PermissionResultAllow()
            return PermissionResultDeny(message="non approuvé par la politique du cockpit")

        append = SYSTEM_APPEND + (SERVICES_APPEND if self.services else "")
        servers = {svc.SERVER_NAME: svc.server()} if self.services else {}
        spawn = (lambda items: self.spawn_tasks(task, items)) if self.spawn_tasks else None
        if spawn:
            append += PARALLEL_APPEND
        upload = (lambda b64, caption: self.upload_image(task.id, b64, caption)) if self.upload_image else None
        delegate = ((lambda projet, instruction: self.spawn_tasks(task, [{"instruction": instruction, "projet": projet}],
                                                                  defer=True)) if self.spawn_tasks else None)
        servers["cockpit"] = cockpit_server(cwd, lambda steps: setattr(task, "next_steps", steps), spawn,
                                            pub=lambda **kw: self.store.publish("message", {"task_id": task.id, **kw}),
                                            upload=upload, delegate=delegate,
                                            on_question=lambda q: setattr(task, "question", q), status=self.project_status)
        if self.preview:
            append += PREVIEW_APPEND.format(**self.preview)
        if not self.services and delegate:
            append += PROJECT_APPEND.format(project=project.id)
        if not task.context and not task.parent and not task.reply_to_task:  # première demande d'une conversation
            append += FIRST_APPEND
        if task.worktree:
            append += WORKTREE_APPEND.format(cwd=cwd, branch=task.worktree["branch"])
        return ClaudeAgentOptions(
            cwd=cwd,
            system_prompt={"type": "preset", "preset": "claude_code", "append": append},
            mcp_servers=servers,
            permission_mode="default",
            setting_sources=["project"],  # CLAUDE.md du projet, sans les permissions utilisateur
            hooks={"PreToolUse": [HookMatcher(matcher=None, hooks=[pre_tool],
                                              timeout=self.policy.timeout + 60)]},
            can_use_tool=can_use_tool,
            resume=session,
            model=MODELS.get(task.model or ""),
        )

    def _session(self, task: Task, project: Project, cwd: str | None = None) -> str | None:
        """Session Claude à reprendre : celle de la conversation pour ce projet (jamais une autre conversation).

        Une session ne se reprend que depuis son dossier d'origine : sinon, l'historique passe par le prompt.
        """
        if not task.conversation:
            return None
        session = self.store.conversations.get(f"{task.conversation}:{project.id}") or task.resume_session
        if session and cwd and not gitstate.session_usable(cwd, session):
            return None
        return session

    @staticmethod
    def _prompt(task: Task, session: str | None) -> str:
        """Sans session à reprendre, les échanges précédents de la conversation sont rappelés dans l'instruction."""
        if session or not task.context:
            return task.instruction
        history = "\n\n".join(f"Utilisateur : {c['instruction']}\nRéponse : {c.get('result') or '(aucune)'}"
                              for c in task.context)
        return f"Échanges précédents de cette conversation :\n\n{history}\n\nNouvelle instruction : {task.instruction}"

    async def _workspace(self, task: Task, project: Project, conv: str) -> str | None:
        """Worktree de la conversation, ou None pour le dossier principal.

        Une conversation reste dans l'espace où elle a commencé. Une nouvelle conversation part dans un worktree
        si un autre agent occupe (ou attend) le dossier principal du projet et que celui-ci est un dépôt git.
        """
        key = f"{conv}:{project.id}"
        if key not in self.workspaces:
            busy = self.main_users.get(project.id, 0) > 0
            if gitstate.worktree_path(project.id, conv).exists() or busy or task.isolate:
                self.workspaces[key] = "?"  # réservé pendant la création (pas de second worktree en parallèle)
                try:
                    self.workspaces[key] = await gitstate.add_worktree(project.cwd, project.id, conv)
                except Exception as e:
                    log.info("pas de worktree pour %s (%s) : dossier principal", key, e)
                    self.workspaces[key] = ""
            else:
                self.workspaces[key] = ""
        return self.workspaces[key] or None

    async def choose_model(self, task: Task, project: Project) -> None:
        """« Modèle auto » : le sélecteur (Haiku) choisit le niveau de modèle selon la demande (Opus pour le code)."""
        if task.model:
            return
        context = "\n".join(f"- {c['instruction'][:300]}" + (f" → {c['result'][:200]}" if c.get("result") else "")
                            for c in task.context[-4:])
        choice, reason = await choose_model(task.instruction, context, f"{project.name} — {project.description}"[:300])
        task.model = choice
        self.store.task_update(task, model_auto={"choice": choice, "reason": reason})
        self.store.audit("model_choice", task=task.id, project=project.id, model=choice, reason=reason)

    async def run(self, task: Task, project: Project) -> None:
        self.store.task_update(task, status="queued", project=project.id)
        await self.choose_model(task, project)
        conv = task.conversation or task.id
        async with self._lock(self.conv_locks, f"{conv}:{project.id}"):  # la tâche précédente de la conversation est terminée
            if task.status == "cancelled":
                return
            # Réservation immédiate du dossier principal (avant tout await) : la conversation suivante le voit occupé.
            key = f"{conv}:{project.id}"
            main = not task.isolate and (self.workspaces.get(key) == "" or (
                key not in self.workspaces and self.main_users.get(project.id, 0) == 0
                and not gitstate.worktree_path(project.id, conv).exists()))
            if main:
                self.workspaces[key] = ""
                self.main_users[project.id] = self.main_users.get(project.id, 0) + 1
                try:
                    async with self._lock(self.main_locks, project.id):
                        await self._execute(task, project, project.cwd)
                finally:
                    self.main_users[project.id] -= 1
                return
            cwd = await self._workspace(task, project, conv)
            if cwd is None:  # création du worktree impossible : file d'attente du dossier principal
                self.main_users[project.id] = self.main_users.get(project.id, 0) + 1
                try:
                    async with self._lock(self.main_locks, project.id):
                        await self._execute(task, project, project.cwd)
                finally:
                    self.main_users[project.id] -= 1
                return
            branch = gitstate.branch_name(conv)
            self.store.task_update(task, worktree={"branch": branch, "path": cwd, "state": "active"})
            await self._execute(task, project, cwd)

    async def _execute(self, task: Task, project: Project, cwd: str) -> None:
        async with self.slots:  # au plus max_agents agents simultanés dans ce pod
            if task.status == "cancelled":
                return
            session = self._session(task, project, cwd)
            self.running.add(task.id)
            self.store.task_update(task, status="running")
            self.store.audit("task_start", task=task.id, project=project.id, instruction=task.instruction,
                             **({"worktree": task.worktree["branch"]} if task.worktree else {}))
            before = await self._snapshot(cwd)
            edited: set[str] = set()
            final: dict = {}
            duration, tokens = 0, {}
            session_before = self.session_usage() if self.session_usage else None
            try:
                async with ClaudeSDKClient(self._options(task, project, cwd, session, edited)) as client:
                    self.clients[task.id] = client
                    prompt = self._prompt(task, session)
                    if early := self.steers.pop(task.id, None):  # précisions envoyées avant le démarrage
                        prompt += "\n\nPrécisions de l'utilisateur :\n" + "\n".join(early)
                    await client.query(prompt)
                    while True:
                        async for msg in client.receive_response():
                            if r := self._relay(task, msg):
                                duration += r.pop("duration_s", 0)
                                for k, v in r.pop("tokens", {}).items():  # plusieurs tours (recadrage) : cumul
                                    tokens[k] = tokens.get(k, 0) + v
                                final = r
                        # Message de l'utilisateur pendant le travail : l'agent a été interrompu, il reprend avec.
                        pending = self.steers.pop(task.id, None)
                        if not pending or task.status == "cancelled":
                            break
                        await client.query("Message de l'utilisateur pendant ton travail (tu as été interrompu) :\n"
                                           + "\n".join(pending) + "\nTiens-en compte et poursuis la tâche.")
            except Exception as e:
                final = {"status": "error", "result": f"{type(e).__name__}: {e}"}
            finally:
                self.clients.pop(task.id, None)
                self.running.discard(task.id)
                if late := self.steers.pop(task.id, None):  # arrivé après la dernière réponse de l'agent
                    self.store.publish("message", {"task_id": task.id, "type": "steer_late", "text": "\n".join(late)})
            final["duration_s"] = duration
            if tokens:
                final["tokens"] = {**tokens, "total": sum(tokens.values())}
            changes = await self._changes(task, cwd, before, edited)
            if task.status == "cancelled":
                final = {k: v for k, v in final.items() if k != "status"}
            elif final.get("status") == "done" and changes and changes.get("file_count"):
                final |= await self._tests(task, project, cwd)
            if final.get("status") == "done" and not task.question:
                if q := detect_question(final.get("result", "")):
                    final["question"] = q
            # Statut final et modifications publiés ensemble : la notification de fin peut les résumer.
            self.store.task_update(task, **final, changes=changes)
            self.store.audit("task_end", task=task.id, project=project.id, status=task.status,
                             cost_usd=task.cost_usd, result=_short(task.result, 2000),
                             files=(changes or {}).get("file_count", 0))
            if changes and changes.get("commits") and changes.get("upstream") and not changes.get("ahead"):
                self._spawn(self._watch_ci(task, cwd))
            if self.refresh_usage and session_before is not None:
                self._spawn(self._session_delta(task, session_before))

    async def _session_delta(self, task: Task, before: float) -> None:
        """Part de la session de 5 h consommée par la tâche : usage relu juste après (approximatif si plusieurs
        tâches tournent en même temps ; nulle si la session a été réinitialisée entre-temps)."""
        try:
            after = await self.refresh_usage()
        except Exception:
            return
        if after is not None:
            self.store.task_update(task, session_delta=round(max(0.0, after - before), 4))

    def trusted(self, task: Task) -> bool:
        """La conversation de la tâche est-elle en mode confiance (activé depuis le téléphone, non expiré) ?"""
        return self.trust.get(task.conversation or task.id, 0) > time.time()

    def _spawn(self, coro) -> None:
        t = asyncio.create_task(coro)
        self._background.add(t)
        t.add_done_callback(self._background.discard)

    async def steer(self, task: Task, text: str) -> None:
        """Message de l'utilisateur pendant la tâche : l'agent est interrompu et reprend avec ce message."""
        self.steers.setdefault(task.id, []).append(text)
        for a in self.store.approvals.values():  # une validation en attente est caduque : l'agent reprend
            if a.task_id == task.id and a.future and not a.future.done():
                a.future.set_result("deny")
        if client := self.clients.get(task.id):
            await client.interrupt()

    async def _tests(self, task: Task, project: Project, cwd: str) -> dict:
        """Tests du projet après une tâche qui a modifié des fichiers ; un échec devient la première étape proposée."""
        cmd, why = projet.test_command(cwd, self.policy, getattr(project, "test", ""))
        if not cmd:
            if "non lancée" in why:
                self.store.publish("message", {"task_id": task.id, "type": "tests", "skipped": why})
            return {}
        self.store.publish("message", {"task_id": task.id, "type": "tool_use", "tool": "tests", "text": cmd,
                                       "label": f"🧪 Lance les tests ({_clip(cmd, 40)})"})
        result = await projet.run_tests(cwd, cmd)
        self.store.publish("message", {"task_id": task.id, "type": "tests", **result})
        self.store.audit("tests", task=task.id, project=project.id, command=cmd, ok=result["ok"])
        out = {"tests": {k: result[k] for k in ("ok", "command", "duration_s")}}
        if not result["ok"]:
            out["next_steps"] = [projet.fix_tests_step(result)] + (task.next_steps or [])[:3]
        return out

    async def _watch_ci(self, task: Task, cwd: str) -> None:
        """Suit la CI GitHub du commit poussé et publie chaque changement d'état (en cours, réussie, en échec)."""
        rc, remote = await gitstate.git(cwd, "remote", "get-url", "origin")
        target = github.owner_repo(remote) if rc == 0 else None
        sha = await gitstate.head(cwd)
        if not target or not sha or not os.environ.get("GIT_PERSONAL_ACCESS_TOKEN"):
            return
        last, waited = None, 0
        await asyncio.sleep(15)  # le temps que GitHub déclenche les workflows
        while waited < CI_MAX_S:
            try:
                ci = await github.etat_ci(*target, sha)
            except Exception as e:
                log.info("état de la CI indisponible : %s", e)
                ci = None
            if ci and ci["state"] != last and not (ci["state"] == "none" and waited < 120):
                last = ci["state"]
                self.store.publish("message", {"task_id": task.id, "type": "ci", **ci})
            if ci and ci["state"] in ("success", "failure") or (ci and ci["state"] == "none" and waited >= 120):
                return
            await asyncio.sleep(CI_POLL_S)
            waited += CI_POLL_S

    @staticmethod
    async def _snapshot(cwd: str):
        try:
            return await gitstate.snapshot(cwd)
        except Exception as e:
            log.warning("instantané git impossible dans %s : %s", cwd, e)
            return None

    async def _changes(self, task: Task, cwd: str, before, edited: set[str]) -> dict | None:
        """Ce que l'agent a réellement changé (git), ou à défaut les fichiers qu'il a écrits."""
        summary, patches = None, {}
        after = await self._snapshot(cwd) if before else None
        if before and after:
            try:
                summary, patches = await gitstate.changes(before, after)
            except Exception as e:
                log.warning("résumé des modifications impossible : %s", e)
        if summary is None and edited:
            root = Path(cwd).resolve()
            files = sorted({os.path.relpath(Path(p).resolve(), root) for p in edited if p})
            summary = {"files": [{"path": f, "status": "M"} for f in files], "file_count": len(files),
                       "commits": [], "git": False}
        if summary and (summary["file_count"] or summary["commits"]):
            self.store.publish("message", {"task_id": task.id, "type": "changes", "changes": summary,
                                           "patches": patches})
        return summary

    def _relay(self, task: Task, msg) -> dict | None:
        """Relaie un message de l'agent ; renvoie les champs finaux de la tâche à la fin (ResultMessage)."""
        pub = lambda **kw: self.store.publish("message", {"task_id": task.id, **kw})
        if isinstance(msg, AssistantMessage):
            # Modèle réellement utilisé : celui de l'agent principal, et ceux de ses sous-agents
            if msg.model and msg.model not in task.models:
                changes = {"models": task.models + [msg.model]}
                if not msg.parent_tool_use_id and task.model_used != msg.model:
                    changes["model_used"] = msg.model
                self.store.task_update(task, **changes)
            elif msg.model and not msg.parent_tool_use_id and task.model_used != msg.model:
                self.store.task_update(task, model_used=msg.model)
            for b in msg.content:
                if isinstance(b, TextBlock) and b.text.strip():
                    pub(type="text", text=b.text)
                elif isinstance(b, ToolUseBlock):
                    extra = {"todos": b.input.get("todos", [])[:30]} if b.name == "TodoWrite" else {}
                    pub(type="tool_use", tool=b.name, text=_short(b.input, 300),
                        label=tool_label(b.name, b.input), **extra)
        elif isinstance(msg, UserMessage) and isinstance(msg.content, list):
            for b in msg.content:
                if isinstance(b, ToolResultBlock) and b.is_error:
                    pub(type="tool_error", text=_short(b.content, 300))
        elif isinstance(msg, RateLimitEvent):
            info = msg.rate_limit_info
            if self.on_rate_limit:
                self.on_rate_limit(info)
            if info.status == "rejected":  # limite du forfait atteinte : la tâche attend ou échoue
                pub(type="rate_limited", resets_at=info.resets_at, kind=info.rate_limit_type)
        elif isinstance(msg, ResultMessage):
            self.store.save_session(task.project, msg.session_id)
            if task.conversation and msg.session_id:
                self.store.conversations[f"{task.conversation}:{task.project}"] = msg.session_id
            u = msg.usage or {}
            tokens = {"input": int(u.get("input_tokens") or 0), "output": int(u.get("output_tokens") or 0),
                      "cache_read": int(u.get("cache_read_input_tokens") or 0),
                      "cache_write": int(u.get("cache_creation_input_tokens") or 0)}
            return {"status": "error" if msg.is_error else "done", "result": msg.result or "",
                    "session_id": msg.session_id, "cost_usd": msg.total_cost_usd or 0.0,
                    "duration_s": round((msg.duration_ms or 0) / 1000), "tokens": tokens}
        return None

    async def worktree_action(self, task: Task, project: Project, action: str) -> str:
        """Fusionner ou jeter le worktree d'une conversation (action de l'utilisateur sur le téléphone)."""
        conv = task.conversation or task.id
        key = f"{conv}:{project.id}"
        if self._lock(self.conv_locks, key).locked():
            raise gitstate.GitError("un agent travaille encore dans ce worktree")
        if action == "merge":
            if self._lock(self.main_locks, project.id).locked():
                raise gitstate.GitError("un agent travaille dans le dossier principal : réessaie quand il aura fini")
            async with self._lock(self.main_locks, project.id):
                text = await gitstate.merge_worktree(project.cwd, project.id, conv)
        else:
            text = await gitstate.remove_worktree(project.cwd, project.id, conv)
        self.workspaces.pop(key, None)
        self.store.conversations.pop(key, None)  # la session était liée au worktree
        return text
