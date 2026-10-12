"""Exécution d'une tâche par un agent Claude Code, avec validation humaine."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path

from claude_agent_sdk import (
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

from . import gitstate
from . import services as svc
from .policy import ALLOW, DENY, Policy
from .router import Project
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
**À faire** — ce qui reste, ou ta question.
Le cockpit affiche déjà la liste des fichiers modifiés et leurs diffs : inutile de les recopier.
Pour publier ton travail, crée une branche et pousse-la avec « git push -u origin <branche> » : c'est
automatique. Un push sur main ou master demande une validation sur le téléphone.
Certaines actions nécessitent une validation humaine ; si une action est refusée, ne tente pas de la
contourner par un autre moyen, explique plutôt ce qui était prévu.
"""


SERVICES_APPEND = """
Tu peux lancer, lister et arrêter des services Onyxia (chacun devient un projet pilotable) avec les outils
lancer_service, lister_services et arreter_service. N'utilise pas helm directement pour cela.
Pour un nouveau projet sans dépôt git, lance le service sans dépôt (un dossier vide sera créé) ; choisis
toi-même un nom court à partir de la demande et garde les ressources par défaut sauf besoin explicite.
"""


PARALLEL_APPEND = """
Si la demande se découpe en sous-tâches indépendantes et longues (modules distincts, pistes à explorer
séparément), tu peux les confier à d'autres agents Claude avec l'outil lancer_en_parallele : chacune tourne
dans son propre worktree git (branche dédiée), partie du dernier commit — commite d'abord ce dont elles ont besoin.
Ne l'utilise pas pour des tâches courtes ou très liées : fais-les toi-même.
"""

WORKTREE_APPEND = """
Un autre agent travaille en même temps dans le dossier principal du projet : tu travailles dans un worktree git
dédié ({cwd}, branche {branch}). Reste dans ce dossier et commite ton travail sur cette branche ; l'utilisateur
la fusionnera ou la jettera depuis le cockpit.
"""

MAX_AGENTS = int(os.environ.get("COCKPIT_MAX_AGENTS", "3"))
PARALLEL_TOOL = "mcp__cockpit__lancer_en_parallele"
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
        case "lancer_en_parallele":
            return f"🔀 Lance {len(i.get('taches') or [])} Claude en parallèle"
    return f"🔧 {short}"


def _short(value, limit: int = 600) -> str:
    s = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    return s if len(s) <= limit else s[:limit] + "…"


def parallel_server(spawn):
    """Outil « lancer_en_parallele » : confie des sous-tâches indépendantes à d'autres agents (une conversation chacune)."""

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
            return {"content": [{"type": "text", "text": "taches : 1 à 5 objets avec une instruction"}], "is_error": True}
        try:
            ids = await spawn(items)
        except Exception as e:
            return {"content": [{"type": "text", "text": f"lancement impossible : {e}"}], "is_error": True}
        return {"content": [{"type": "text", "text": f"{len(ids)} agent(s) lancé(s) en parallèle (tâches {', '.join(ids)}). "
                             "L'utilisateur suit leur avancement dans le cockpit ; tu n'as pas à les attendre."}]}

    return create_sdk_mcp_server("cockpit", tools=[lancer])


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
            if d.action != DENY and tool == PARALLEL_TOOL:
                n = len(tool_input.get("taches") or [])
                d.reason = f"Lancer {n} agent(s) Claude en parallèle, chacun dans son worktree git"
            if d.action == ALLOW:
                ok = True
            elif d.action == DENY:
                ok = False
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
        if self.spawn_tasks:
            append += PARALLEL_APPEND
            servers["cockpit"] = parallel_server(lambda items: self.spawn_tasks(task, items))
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
            if gitstate.worktree_path(project.id, conv).exists() or busy:
                self.workspaces[key] = "?"  # réservé pendant la création (pas de second worktree en parallèle)
                try:
                    self.workspaces[key] = await gitstate.add_worktree(project.cwd, project.id, conv)
                except Exception as e:
                    log.info("pas de worktree pour %s (%s) : dossier principal", key, e)
                    self.workspaces[key] = ""
            else:
                self.workspaces[key] = ""
        return self.workspaces[key] or None

    async def run(self, task: Task, project: Project) -> None:
        self.store.task_update(task, status="queued", project=project.id)
        conv = task.conversation or task.id
        async with self._lock(self.conv_locks, f"{conv}:{project.id}"):  # la tâche précédente de la conversation est terminée
            if task.status == "cancelled":
                return
            # Réservation immédiate du dossier principal (avant tout await) : la conversation suivante le voit occupé.
            key = f"{conv}:{project.id}"
            main = self.workspaces.get(key) == "" or (
                key not in self.workspaces and self.main_users.get(project.id, 0) == 0
                and not gitstate.worktree_path(project.id, conv).exists())
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
            try:
                async with ClaudeSDKClient(self._options(task, project, cwd, session, edited)) as client:
                    self.clients[task.id] = client
                    await client.query(self._prompt(task, session))
                    async for msg in client.receive_response():
                        final = self._relay(task, msg) or final
            except Exception as e:
                final = {"status": "error", "result": f"{type(e).__name__}: {e}"}
            finally:
                self.clients.pop(task.id, None)
                self.running.discard(task.id)
            changes = await self._changes(task, cwd, before, edited)
            if task.status == "cancelled":
                final = {k: v for k, v in final.items() if k != "status"}
            # Statut final et modifications publiés ensemble : la notification de fin peut les résumer.
            self.store.task_update(task, **final, changes=changes)
            self.store.audit("task_end", task=task.id, project=project.id, status=task.status,
                             cost_usd=task.cost_usd, result=_short(task.result, 2000),
                             files=(changes or {}).get("file_count", 0))

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
        elif isinstance(msg, ResultMessage):
            self.store.save_session(task.project, msg.session_id)
            if task.conversation and msg.session_id:
                self.store.conversations[f"{task.conversation}:{task.project}"] = msg.session_id
            return {"status": "error" if msg.is_error else "done", "result": msg.result or "",
                    "session_id": msg.session_id, "cost_usd": msg.total_cost_usd or 0.0}
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
