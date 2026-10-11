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
import secrets
import sys
from pathlib import Path

from .agent import Runner
from .policy import Policy
from .relay import Relay, RelayStore
from .router import Project, load_projects, route
from .store import Task

ROOT = Path(__file__).resolve().parent.parent
CONFIG = Path(os.environ.get("COCKPIT_CONFIG", ROOT / "config"))
DATA = Path(os.environ.get("COCKPIT_DATA", ROOT / "data"))
RELAY_URL = os.environ.get("COCKPIT_RELAY_URL", "https://deltastat.fr/cockpit")
KEY_FILE = DATA / "worker_key"
KEY_HASH_FILE = CONFIG / "worker_key.sha256"  # versionné : seule l'empreinte est publique

log = logging.getLogger("cockpit.worker")


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
    relay = Relay(RELAY_URL, worker_key())
    url = await relay.pairing_url()
    try:
        import qrcode

        qr = qrcode.QRCode(border=1)
        qr.add_data(url)
        qr.print_ascii(invert=True)
    except ImportError:
        pass
    print(f"\nScanne ce QR code avec ton téléphone (valable 10 minutes, usage unique) :\n{url}\n")


class Worker:
    def __init__(self, relay: Relay):
        self.relay = relay
        self.projects: dict[str, Project] = load_projects(CONFIG / "projects.yaml")
        self.store = RelayStore(DATA, relay)
        self.runner = Runner(self.store, Policy.load(CONFIG / "policy.yaml"))
        self._background: set[asyncio.Task] = set()

    def spawn(self, coro) -> None:
        t = asyncio.create_task(coro)
        self._background.add(t)
        t.add_done_callback(self._background.discard)

    def projects_payload(self) -> list[dict]:
        return [
            {"id": p.id, "name": p.name, "description": p.description, "has_session": p.id in self.store.sessions}
            for p in self.projects.values()
        ]

    async def dispatch(self, task: Task, project_id: str | None, new_session: bool) -> None:
        if project_id not in self.projects:
            self.store.task_update(task, status="routing")
            r = await route(task.instruction, self.projects)
            self.store.audit("route", task=task.id, project=r.project, confidence=r.confidence, reason=r.reason)
            if r.project is None:
                self.store.task_update(task, status="needs_project", route_reason=r.reason)
                return
            self.store.task_update(task, route_reason=f"{r.reason} ({r.confidence:.0%})")
            project_id = r.project
        await self.runner.run(task, self.projects[project_id], resume=not new_session)

    def handle(self, msg: dict) -> None:
        for t in msg.get("tasks", []):
            task = Task(id=t["id"], instruction=t["instruction"], project=t.get("project"))
            self.store.tasks[task.id] = task
            self.spawn(self.dispatch(task, t.get("project"), t.get("new_session", False)))
        for d in msg.get("decisions", []):
            a = self.store.approvals.get(d["id"])
            if a and a.future and not a.future.done():
                a.future.set_result(d["decision"])
        for task_id in msg.get("cancels", []):
            if task := self.store.tasks.get(task_id):
                self.spawn(self.runner.cancel(task))
        for c in msg.get("choices", []):
            task = self.store.tasks.get(c["task_id"])
            if task and c["project"] in self.projects:
                self.store.audit("route_manual", task=task.id, project=c["project"])
                self.spawn(self.runner.run(task, self.projects[c["project"]]))

    async def run(self) -> None:
        self.spawn(self.relay.flush_forever())
        hello, delay = True, 2
        log.info("connecté au relais %s — projets : %s", RELAY_URL, ", ".join(self.projects))
        while True:
            try:
                msg = await self.relay.poll(self.projects_payload(), hello=hello)
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
    cmd = sys.argv[1] if len(sys.argv) > 1 else "run"
    if cmd == "init":
        init_key()
    elif cmd == "pair":
        asyncio.run(pair())
    elif cmd == "run":
        asyncio.run(Worker(Relay(RELAY_URL, worker_key())).run())
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
