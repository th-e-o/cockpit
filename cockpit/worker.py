"""Worker Onyxia : récupère les instructions sur le relais et les confie aux agents Claude Code.

    python -m cockpit.worker          # boucle principale
    python -m cockpit.worker pair     # QR code pour connecter un téléphone
    python -m cockpit.worker init     # génère la clé du worker
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import secrets
import subprocess
import sys
import time
from pathlib import Path

from . import apercu, github, gitstate, usage
from . import services as svc
from .agent import Runner
from .autopilote import Autopilot
from .podstate import PodState
from .policy import Policy
from .relay import Relay, RelayStore
from .router import Project, load_projects, route
from .store import Task

ROOT = Path(__file__).resolve().parent.parent
CONFIG = Path(os.environ.get("COCKPIT_CONFIG", ROOT / "config"))
DATA = Path(os.environ.get("COCKPIT_DATA", ROOT / "data"))
RELAY_URL = os.environ.get("COCKPIT_RELAY_URL", "https://deltastat.fr/cockpit")
WORKER_ID = os.environ.get("COCKPIT_WORKER_ID") or os.environ.get("HOSTNAME") or "principal"
WORKER_NAME = os.environ.get("COCKPIT_WORKER_NAME") or WORKER_ID
# « orchestrateur » : route les instructions sans projet et gère les services ; « projet » : pod d'un seul projet.
ORCHESTRATOR = os.environ.get("COCKPIT_ROLE", "orchestrateur") != "projet"
KEY_FILE = DATA / "worker_key"
MEMORY_FILE, MEMORY_MAX = "CLAUDE.md", 50_000
# Pods de projet (scripts/onyxia-init.sh) : le cockpit se met à jour seul, entre deux tâches
AUTO_UPDATE = os.environ.get("COCKPIT_AUTO_UPDATE") == "1"
UPDATE_EVERY = int(os.environ.get("COCKPIT_UPDATE_EVERY", "600"))
PR_SYNC_EVERY = int(os.environ.get("COCKPIT_PR_SYNC_EVERY", "120"))
PR_TASKS_SEEN = 40  # dernières tâches de chaque projet examinées (le tableau de bord en affiche jusqu'à 30)
PR_URL = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+")
ACTIVE = {"pending", "routing", "queued", "running", "waiting_approval"}
KEY_HASH_FILE = CONFIG / "worker_key.sha256"  # versionné : seule l'empreinte est publique

log = logging.getLogger("cockpit.worker")


def make_relay() -> Relay:
    return Relay(RELAY_URL, worker_key(), WORKER_ID, WORKER_NAME, ORCHESTRATOR)


def worker_key() -> str:
    if key := os.environ.get("COCKPIT_WORKER_KEY"):
        return key.strip()
    if KEY_FILE.exists():
        return KEY_FILE.read_text().strip()
    sys.exit("Clé du worker absente : lance `./run.sh init` (ou définis COCKPIT_WORKER_KEY).")


def init_key() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    if os.environ.get("COCKPIT_WORKER_KEY"):
        key = os.environ["COCKPIT_WORKER_KEY"].strip()
    elif KEY_FILE.exists():
        key = KEY_FILE.read_text().strip()
    else:
        key = secrets.token_urlsafe(32)
        KEY_FILE.write_text(key)
        KEY_FILE.chmod(0o600)
    KEY_HASH_FILE.write_text(hashlib.sha256(key.encode()).hexdigest() + "\n")
    print(f"Clé du worker : {KEY_FILE}")
    print(f"Empreinte (à versionner, déployée sur le relais) : {KEY_HASH_FILE}")


async def pair() -> None:
    relay = make_relay()
    pairing = await relay.pairing()
    url, code = pairing["url"], pairing.get("code")
    try:
        import qrcode

        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.print_ascii(invert=True)
    except ImportError:
        pass
    print(f"\nTéléphone : scanne ce QR code avec l'appareil photo.\n{url}\n")
    if code:
        base = url.split("#", 1)[0]
        print(f"Ordinateur : ouvre {base} et saisis la clé d'appairage :\n\n    {code}\n")
    print("Valable 10 minutes, pour un seul appareil.")


class Worker:
    def __init__(self, relay: Relay):
        self.relay = relay
        self.projects: dict[str, Project] = load_projects(CONFIG / "projects.yaml")
        self.store = RelayStore(DATA, relay)
        self.runner = Runner(self.store, Policy.load(CONFIG / "policy.yaml"), services=ORCHESTRATOR)
        self.all_projects: dict[str, Project] = {}  # projets de tous les workers (orchestrateur)
        self._background: set[asyncio.Task] = set()
        self.runner.spawn_tasks = self.spawn_children
        self.pod = PodState()
        self.started = time.time()
        self._state: dict | None = None
        self._state_ts = 0.0
        self._repos_dirty = False  # dépôt créé : liste à republier
        self._pr_done: set[str] = set()  # pull requests dont la fusion / fermeture a déjà été publiée
        self.runner.upload_image = self.relay.image
        self.runner.project_status = self.relay.projet_etat
        self.runner.session_usage = self.session_usage
        self.runner.refresh_usage = self.refresh_usage
        self._memory: dict[str, str] = {}  # projet -> empreinte du CLAUDE.md publié
        self.usage = usage.Usage()
        self.autopilot = Autopilot(relay, self.usage)
        self.vscode: dict | None = None  # {"url", "password"} : bouton « Ouvrir VSCode » du tableau de bord
        self.runner.on_rate_limit = self.rate_limit

    def spawn(self, coro) -> None:
        t = asyncio.create_task(coro)
        self._background.add(t)
        t.add_done_callback(self._background.discard)

    def projects_payload(self) -> list[dict]:
        return [
            {"id": p.id, "name": p.name, "description": p.description, "has_session": p.id in self.store.sessions}
            for p in self.projects.values()
        ]

    async def pod_state(self) -> dict:
        """État du pod pour le tableau de bord : ressources, agents, état git des projets, worktrees actifs."""
        if self._state and time.monotonic() - self._state_ts < 20:
            return self._state
        projects = {}
        for p in self.projects.values():
            try:
                projects[p.id] = await gitstate.repo_status(p.cwd)
            except Exception:
                projects[p.id] = None
        worktrees = []
        if gitstate.WORKTREES.is_dir():
            for d in sorted(gitstate.WORKTREES.iterdir()):
                pid = next((p for p in self.projects if d.name.startswith(p + "-")), None)
                if pid:
                    conv = d.name.removeprefix(pid + "-")
                    worktrees.append({"project": pid, "conversation": conv, "branch": gitstate.branch_name(conv)})
        tasks = self.store.tasks.values()
        await self.publish_memory()
        self._state = {
            "usage": self.usage.public(),
            "autopilot": self.autopilot.public() if ORCHESTRATOR else None,
            "vscode": self.vscode,
            "preview": self.runner.preview,
            "resources": self.pod.resources(), "projects": projects, "worktrees": worktrees,
            "running": len(self.runner.running), "max_agents": self.runner.max_agents,
            "queued": sum(1 for t in tasks if t.status == "queued"), "started": self.started,
        }
        self._state_ts = time.monotonic()
        return self._state

    def session_usage(self) -> float | None:
        lim = self.usage.limits.get("five_hour")
        return lim.get("utilization") if lim else None

    async def refresh_usage(self) -> float | None:
        """Relit l'usage (en-têtes de l'API) après une tâche, pour la part de session qu'elle a consommée."""
        if not self.usage.limits:
            return None
        limits, _ = await usage.probe()
        self.apply_usage(limits, "api")
        self._state = None
        return self.session_usage()

    def rate_limit(self, info) -> None:
        """Événement d'usage émis pendant une tâche : la barre d'usage est mise à jour au prochain poll."""
        if lim := usage.from_event(info):
            self.apply_usage({lim["type"]: lim}, "agent")
            self._state = None

    def apply_usage(self, limits: dict[str, dict], source: str) -> None:
        """Met l'usage à jour ; une limite qui vient de se réinitialiser est signalée au téléphone (notification)."""
        for kind in self.usage.update(limits, source):
            log.info("limite d'usage réinitialisée : %s", kind)
            self.spawn(self.notify_reset(kind))

    async def notify_reset(self, kind: str) -> None:
        try:
            await self.relay.alerte(kind)
        except Exception as e:
            log.warning("réinitialisation non signalée : %s", e)

    async def poll_usage(self) -> None:
        """Usage du forfait : interface du compte toutes les 5 min, sinon en-têtes de l'API toutes les 10 min.

        Si aucune ne répond, la barre n'est alimentée que par les événements émis pendant les tâches.
        """
        account, reported = True, set()
        while True:
            how = None
            if account:
                try:
                    self.apply_usage(await usage.fetch(), "compte")
                    how = "compte"
                except usage.UsageError as e:
                    account = False  # refus définitif (jeton de « claude setup-token ») : on passe à la sonde
                    log.info("interface d'usage du compte indisponible (%s) : lecture des en-têtes de l'API", e)
                except Exception as e:
                    log.info("interface d'usage du compte : %s", e)
            if not how:
                try:
                    limits, how = await usage.probe()
                    self.apply_usage(limits, "api")
                except usage.UsageError as e:
                    log.info("usage du forfait indisponible (%s) : barre alimentée par les tâches seulement", e)
                    return
                except Exception as e:
                    log.info("usage du forfait : %s", e)
            if how and how not in reported:
                reported.add(how)
                log.info("usage du forfait lu par : %s", how)
            self._state = None
            # Relecture juste après une réinitialisation prévue, pour la signaler sans attendre le prochain cycle
            nxt = self.usage.next_reset()
            await asyncio.sleep(min(300 if account else 600, max(30, nxt - time.time() + 20)) if nxt else (300 if account else 600))

    def ensure_project_dirs(self) -> None:
        """Pod de projet : dossier du projet absent (dépôt vide, que le chart n'a pas cloné) -> clone, sinon git init
        relié au dépôt (origin), pour que les agents puissent travailler et pousser."""
        if ORCHESTRATOR:
            return
        repo = os.environ.get("GIT_REPOSITORY", "")
        for p in self.projects.values():
            cwd = Path(p.cwd)
            if cwd.exists():
                continue
            cwd.parent.mkdir(parents=True, exist_ok=True)
            run = lambda *a, **k: subprocess.run(a, capture_output=True, text=True, timeout=120, **k)  # noqa: E731
            if repo and run("git", "clone", "-q", repo, str(cwd)).returncode == 0 and cwd.exists():
                log.info("projet %s : dépôt cloné dans %s", p.id, cwd)
                continue
            cwd.mkdir(parents=True, exist_ok=True)
            run("git", "init", "-q", "-b", "main", cwd=cwd)
            if repo:
                run("git", "remote", "add", "origin", repo, cwd=cwd)
            log.info("projet %s : dossier %s créé (git init%s)", p.id, cwd, ", origin " + repo if repo else "")

    def busy(self) -> bool:
        return bool(self.runner.running) or any(t.status in ACTIVE for t in self.store.tasks.values())

    async def update_available(self) -> int:
        """Nombre de commits du cockpit en avance sur GitHub (0 si à jour ou indéterminé)."""
        if (await gitstate.git(ROOT, "fetch", "-q", "origin", timeout=60))[0]:
            return 0
        rc, behind = await gitstate.git(ROOT, "rev-list", "--count", "HEAD..@{u}")
        return int(behind) if rc == 0 and behind.isdigit() else 0

    @staticmethod
    async def install_requirements() -> None:
        proc = await asyncio.create_subprocess_exec(
            "uv", "pip", "install", "-q", "--python", sys.executable, "-r", str(ROOT / "requirements.txt"))
        await proc.wait()

    async def known_prs(self) -> dict[str, str]:
        """Pull requests des projets de ce pod (adresse -> tâche) : tâches en mémoire, puis dernières tâches vues du relais
        (la mémoire du worker est vide après un redémarrage)."""
        found: dict[str, str] = {}
        for t in self.store.tasks.values():
            for e in t.events:
                if e.get("type") == "pr" and e.get("url"):
                    found[e["url"]] = t.id
            for url in PR_URL.findall(t.result or ""):
                found.setdefault(url, t.id)
        # L'orchestrateur suit aussi les pull requests des pods hors ligne (même token GitHub) ; un pod de projet, les siennes.
        projects = list(self.projects)
        if ORCHESTRATOR:
            try:
                projects = [p["id"] for p in (await self.relay.projet_etat()).get("projects", [])] or projects
            except Exception as e:
                log.info("liste des projets : relais injoignable (%s)", e)
        for project in projects:
            try:
                etat = await self.relay.projet_etat(project, limit=PR_TASKS_SEEN)
            except Exception as e:
                log.info("pull requests de %s : relais injoignable (%s)", project, e)
                continue
            for t in etat.get("tasks", []):
                for pr in t.get("pull_requests", []):
                    if pr.get("url"):
                        found.setdefault(pr["url"], t["id"])
                for url in PR_URL.findall(t.get("result") or ""):
                    found.setdefault(url, t["id"])
        return found

    async def sync_prs_once(self) -> None:
        """Publie la fusion ou la fermeture des pull requests faites directement sur GitHub : sans cela, elles restent
        « à valider » dans le tableau de bord (seule la fusion depuis le cockpit le signalait)."""
        for url, task_id in (await self.known_prs()).items():
            if url in self._pr_done:
                continue
            try:
                state = await github.etat_pr(url)
            except github.GitHubError:
                continue  # dépôt hors de portée du token de ce pod : on ne touche à rien
            if state == "open":
                continue
            task = self.store.tasks.get(task_id)
            try:
                if task or not ORCHESTRATOR:
                    self.store.publish("message", {"task_id": task_id, "type": "pr_merged" if state == "merged" else "pr_closed",
                                                   "url": url})
                else:  # tâche d'un autre pod : seul le relais peut la mettre à jour (ce pod n'en est pas le propriétaire)
                    await self.relay.pr_etat(task_id, url, state)
            except Exception as e:
                log.info("pull request %s non publiée : %s", url, e)
                continue
            self._pr_done.add(url)

    async def sync_prs(self) -> None:
        while True:
            await asyncio.sleep(PR_SYNC_EVERY)
            try:
                await self.sync_prs_once()
            except Exception as e:
                log.warning("état des pull requests : %s", e)

    async def auto_update(self) -> None:
        """Met le cockpit à jour (git pull) puis redémarre le worker, seulement quand aucune tâche n'est en cours."""
        while True:
            await asyncio.sleep(UPDATE_EVERY)
            try:
                behind = await self.update_available()
                if not behind or self.busy():
                    continue
                rc, out = await gitstate.git(ROOT, "pull", "-q", "--ff-only", timeout=120)
                if rc:
                    log.warning("mise à jour du cockpit impossible : %s", out[-300:])
                    continue
                await self.install_requirements()
                log.info("cockpit mis à jour (%d commit(s)) : redémarrage du worker", behind)
                await self.relay.flush()
                os.execv(sys.executable, [sys.executable, "-m", "cockpit.worker", "run"])
            except Exception as e:
                log.warning("mise à jour du cockpit : %s", e)

    async def publish_memory(self) -> None:
        """CLAUDE.md de chaque projet (mémoire) publié au relais quand il change."""
        for p in self.projects.values():
            path = Path(p.cwd, MEMORY_FILE)
            try:
                content = path.read_text()[:MEMORY_MAX] if path.exists() else ""
            except OSError:
                continue
            digest = hashlib.sha256(content.encode()).hexdigest()[:16]
            if self._memory.get(p.id) != digest:
                try:
                    await self.relay.memoire(p.id, content, digest)
                    self._memory[p.id] = digest
                except Exception as e:
                    log.info("mémoire de %s non publiée : %s", p.id, e)

    def apply_memory_edit(self, edit: dict) -> None:
        """Mémoire modifiée sur le téléphone : CLAUDE.md réécrit dans le dossier du projet (lu par les agents)."""
        p = self.projects.get(edit.get("project", ""))
        if not p:
            return
        content = str(edit.get("content", ""))[:MEMORY_MAX]
        Path(p.cwd, MEMORY_FILE).write_text(content if content.endswith("\n") or not content else content + "\n")
        self.store.audit("memory_edit", project=p.id, size=len(content))
        self._memory.pop(p.id, None)
        self._state = None  # republiée au prochain poll

    async def steer(self, s: dict) -> None:
        task = self.store.tasks.get(s.get("task_id", ""))
        if task and task.status in ("pending", "routing", "queued", "running", "waiting_approval"):
            await self.runner.steer(task, str(s.get("text", ""))[:4000])
            self.store.audit("steer", task=task.id, project=task.project)
        elif task:
            self.store.publish("message", {"task_id": task.id, "type": "steer_late", "text": s.get("text", "")})

    async def spawn_children(self, parent: Task, items: list[dict], defer: bool = False) -> list[str]:
        """Tâches filles (lancer_en_parallele, confier_au_projet) : chacune devient une conversation, sur son projet."""
        payload = [{"instruction": str(t["instruction"]).strip()[:10000], "project": t.get("projet") or parent.project}
                   for t in items]
        ids = await self.relay.creer_taches(parent.id, payload, defer)
        self.store.audit("spawn", task=parent.id, project=parent.project, children=ids)
        return ids

    async def worktree_action(self, a: dict) -> None:
        task = self.store.tasks.get(a["task_id"]) or Task(id=a["task_id"], instruction="", project=a.get("project"),
                                                         conversation=a.get("conversation"))
        project = self.projects.get(a.get("project") or task.project or "")
        try:
            if not project:
                raise gitstate.GitError("projet inconnu de ce worker")
            text, state = await self.runner.worktree_action(task, project, a["action"]), (
                "merged" if a["action"] == "merge" else "discarded")
        except Exception as e:
            text, state = f"{e}", "active"
        self.store.audit("worktree", task=task.id, project=task.project, action=a["action"], result=text)
        self.store.publish("message", {"task_id": task.id, "type": "worktree", "state": state, "text": text,
                                       "ok": state != "active"})
        self._state = None  # tableau de bord à jour au prochain poll

    async def run_action(self, task: Task, action: dict) -> None:
        """Action directe demandée depuis le téléphone (sans agent) : lancement d'un pod sur un dépôt git."""
        self.store.task_update(task, status="running")
        if action.get("type") == "fusionner_pr":
            try:
                r = await github.fusionner_pr(action.get("url", ""))
                text, status = f"✅ Pull request fusionnée ({r['message'] or (r['sha'] or '')[:7]}) : {action.get('url')}", "done"
                self.store.publish("message", {"task_id": task.id, "type": "pr_merged", "url": action.get("url")})
            except Exception as e:
                text, status = f"Fusion impossible : {e}", "error"
            self.store.audit("pr_merge", task=task.id, url=action.get("url"), result=text)
            self.store.task_update(task, status=status, result=text)
            return
        try:
            if action.get("type") != "lancer_service":
                raise svc.ServiceError(f"action inconnue : {action.get('type')}")
            p = svc.Launch(nom=action.get("nom", ""), depot_git=action.get("depot_git", ""), branche=action.get("branche", ""),
                           cpu=str(action.get("cpu", "2")), memoire=action.get("memoire", "8Gi"),
                           disque=action.get("disque", "10Gi"), gpu=bool(action.get("gpu")), apercu=bool(action.get("apercu")))
            await svc.verifier_nouveau(p)  # avant de créer un dépôt qui resterait orphelin
            prefix = ""
            if nouveau := action.get("creer_depot"):
                # Nouveau dépôt GitHub (vide), cloné par le pod : le projet démarre sous git avec son dépôt distant.
                repo = await github.creer_depot(nouveau.get("nom", ""), bool(nouveau.get("prive", True)),
                                                nouveau.get("description", ""))
                p.depot_git, p.branche = repo["url"], ""
                prefix = f"Dépôt {'privé ' if repo['private'] else ''}{repo['name']} créé : {repo['url']}\n"
                self._repos_dirty = True
            text, status = prefix + await svc.lancer_nouveau(p), "done"
        except Exception as e:
            text, status = f"Lancement impossible : {e}", "error"
        self.store.audit("service_launch", task=task.id, action=action, result=text)
        self.store.task_update(task, status=status, result=text)

    async def publish_repos(self) -> None:
        """Orchestrateur : liste des dépôts GitHub envoyée au relais au démarrage, puis toutes les 10 minutes."""
        while True:
            self._repos_dirty = False
            try:
                await self.relay.depots(await github.depots())
            except Exception as e:
                log.warning("liste des dépôts git indisponible : %s", e)
            for _ in range(60):  # 10 min, ou tout de suite après la création d'un dépôt
                if self._repos_dirty:
                    break
                await asyncio.sleep(10)

    @staticmethod
    def routing_text(task: Task) -> str:
        """Instruction à router, précédée des échanges de la conversation (« ce projet », « oui », …)."""
        if not task.context:
            return task.instruction
        prev = "\n".join(f"- {c['instruction'][:300]}" + (f" (projet {c['project']})" if c.get("project") else "")
                         for c in task.context)
        return f"{task.instruction}\n\nMessages précédents de la conversation :\n{prev}"

    async def dispatch(self, task: Task, project_id: str | None) -> None:
        if project_id not in self.projects and project_id in self.all_projects:
            await self.hand_over(task, project_id)
            return
        if project_id not in self.projects:
            self.store.task_update(task, status="routing")
            r = await route(self.routing_text(task), self.all_projects or self.projects)
            self.store.audit("route", task=task.id, project=r.project, confidence=r.confidence, reason=r.reason)
            if r.project is None:
                self.store.task_update(task, status="needs_project", route_reason=r.reason)
                return
            self.store.task_update(task, route_reason=f"{r.reason} ({r.confidence:.0%})")
            project_id = r.project
            if project_id not in self.projects:
                await self.hand_over(task, project_id)
                return
        await self.runner.run(task, self.projects[project_id])

    async def hand_over(self, task: Task, project_id: str) -> None:
        """Projet hébergé par un autre worker : le relais lui réaffecte la tâche."""
        try:
            target = await self.relay.affecter(task.id, project_id)
        except Exception as e:
            self.store.task_update(task, status="error", result=f"Réaffectation impossible : {e}")
            return
        self.store.audit("hand_over", task=task.id, project=project_id, worker=target)

    def handle(self, msg: dict) -> None:
        if ORCHESTRATOR:
            self.all_projects = {
                p["id"]: self.projects.get(p["id"]) or Project(
                    id=p["id"], name=p.get("name", p["id"]), cwd="", description=p.get("description", ""))
                for p in msg.get("all_projects", [])
            } | self.projects
        for t in msg.get("tasks", []):
            task = Task(id=t["id"], instruction=t["instruction"], project=t.get("project"),
                        conversation=t.get("conversation"), context=t.get("context") or [],
                        resume_session=t.get("resume_session"), parent=t.get("parent"), model=t.get("model"),
                        isolate=bool(t.get("isolate")), group=t.get("group"), reply_to_task=t.get("reply_to"))
            self.store.tasks[task.id] = task
            if t.get("action"):
                self.spawn(self.run_action(task, t["action"]))
            else:
                self.spawn(self.dispatch(task, t.get("project")))
        for d in msg.get("decisions", []):
            a = self.store.approvals.get(d["id"])
            if a and a.future and not a.future.done():
                a.future.set_result(d["decision"])
        for task_id in msg.get("cancels", []):
            if task := self.store.tasks.get(task_id):
                self.spawn(self.runner.cancel(task))
        if "hard" in msg:
            if bool(msg["hard"]) != self.runner.hard:
                self.store.audit("mode_hard", enabled=bool(msg["hard"]))
            self.runner.hard = bool(msg["hard"])
            self.runner.hard_projects = set(msg.get("hard_projects") or [])
        if "autopilot" in msg:
            self.autopilot.config = msg["autopilot"] or {}
        if "rules" in msg:
            self.runner.policy.set_user_rules(msg["rules"] or [])
        if "trust" in msg:
            self.runner.trust = {str(k): float(v) for k, v in (msg["trust"] or {}).items()}
        for s in msg.get("steers", []):
            self.spawn(self.steer(s))
        for e in msg.get("memory_edits", []):
            try:
                self.apply_memory_edit(e)
            except OSError as err:
                log.warning("mémoire non écrite : %s", err)
        for a in msg.get("worktree_actions", []):
            self.spawn(self.worktree_action(a))
        for c in msg.get("choices", []):
            task = self.store.tasks.get(c["task_id"])
            if not task:
                continue
            self.store.audit("route_manual", task=task.id, project=c["project"])
            if c["project"] in self.projects:
                self.spawn(self.runner.run(task, self.projects[c["project"]]))
            elif c["project"] in self.all_projects:
                self.spawn(self.hand_over(task, c["project"]))

    async def run(self) -> None:
        try:
            self.ensure_project_dirs()
        except Exception as e:
            log.warning("dossier du projet : %s", e)
        self.spawn(self.relay.flush_forever())
        self.spawn(self.poll_usage())
        self.spawn(self.sync_prs())
        if AUTO_UPDATE:
            self.spawn(self.auto_update())
        if ORCHESTRATOR:
            self.spawn(self.publish_repos())
            self.spawn(self.autopilot.run())
        try:
            urls = await apercu.pod_urls()
            self.runner.preview = urls["preview"]
            if urls["vscode"]:
                self.vscode = {"url": urls["vscode"], **({"password": pw} if (pw := apercu.vscode_password()) else {})}
        except Exception as e:
            log.info("adresses du pod indisponibles : %s", e)
        hello, delay = True, 2
        log.info("connecté au relais %s — projets : %s", RELAY_URL, ", ".join(self.projects))
        while True:
            try:
                msg = await self.relay.poll(self.projects_payload(), hello=hello, state=await self.pod_state())
                hello, delay = False, 2
            except Exception as e:
                log.warning("relais injoignable (%s), nouvel essai dans %ss", e, delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60)
                continue
            if msg.get("superseded"):
                log.error("un autre worker s'est connecté au relais : arrêt de celui-ci")
                return
            try:
                self.handle(msg)
            except Exception:
                log.exception("message du relais non traité : %s", msg)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # une ligne par long-poll sinon
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "init":
        init_key()
    elif cmd == "pair":
        asyncio.run(pair())
    elif cmd == "run":
        asyncio.run(Worker(make_relay()).run())
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
