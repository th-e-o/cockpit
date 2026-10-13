"""État en mémoire des tâches, événements et journal d'audit JSONL."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path


def new_id() -> str:
    return uuid.uuid4().hex[:10]


@dataclass
class Task:
    id: str
    instruction: str
    project: str | None = None
    route_reason: str = ""
    status: str = "pending"  # pending|routing|queued|running|waiting_approval|done|error|cancelled|needs_project
    result: str = ""
    session_id: str | None = None
    cost_usd: float = 0.0
    created: float = field(default_factory=time.time)
    conversation: str | None = None  # id de la première tâche de la conversation
    events: list[dict] = field(default_factory=list)
    context: list[dict] = field(default_factory=list)  # échanges précédents (fournis par le relais)
    resume_session: str | None = None  # session de la conversation connue du relais
    changes: dict | None = None  # résumé des modifications (fichiers, lignes, commits, branche)
    worktree: dict | None = None  # {"branch", "path", "state": active|merged|discarded} si exécutée en parallèle
    parent: str | None = None  # tâche qui l'a lancée (lancer_en_parallele)
    duration_s: int | None = None  # temps de travail de l'agent (bilan par projet)
    next_steps: list[dict] | None = None  # prochaines étapes proposées par l'agent [{titre, instruction}]
    model: str | None = None  # rapide | standard | approfondi (None : choisi automatiquement avant l'exécution)
    model_auto: dict | None = None  # choix automatique du modèle {choice, reason}
    isolate: bool = False  # variante : toujours dans un worktree dédié
    group: str | None = None  # variantes d'une même demande (id de la première)
    tests: dict | None = None  # résultat des tests lancés après la tâche {ok, command, duration_s}
    model_used: str | None = None  # modèle qui a réellement écrit les réponses (ex. claude-opus-5-5)
    models: list[str] = field(default_factory=list)  # tous les modèles utilisés, sous-agents compris
    question: dict | None = None  # question(s) posée(s) à l'utilisateur {text, options, items?, detected?}
    reply_to_task: str | None = None  # tâche à laquelle répond celle-ci (suite de conversation)
    tokens: dict | None = None  # {input, output, cache_read, cache_write, total} consommés par la tâche
    session_delta: float | None = None  # part de la session de 5 h consommée pendant la tâche (0-1, approximative)


@dataclass
class Approval:
    id: str
    task_id: str
    project: str
    tool: str
    tool_input: dict
    reason: str
    created: float = field(default_factory=time.time)
    expires: float = 0.0
    decision: str | None = None  # approve|deny|timeout
    future: asyncio.Future | None = field(default=None, repr=False)

    def public(self) -> dict:
        return {k: v for k, v in vars(self).items() if k != "future"}


class Store:
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        data_dir.mkdir(parents=True, exist_ok=True)
        self.audit_path = data_dir / "audit.jsonl"
        self.tasks: dict[str, Task] = {}
        self.approvals: dict[str, Approval] = {}
        self.sessions: dict[str, str] = self._load_sessions()  # projet -> session_id
        self.conversations: dict[str, str] = {}  # "conversation:projet" -> session_id

    # --- sessions Claude par projet (persistées pour survivre aux redémarrages)
    def _load_sessions(self) -> dict[str, str]:
        p = self.data_dir / "sessions.json"
        return json.loads(p.read_text()) if p.exists() else {}

    def save_session(self, project: str, session_id: str | None) -> None:
        if session_id:
            self.sessions[project] = session_id
        else:
            self.sessions.pop(project, None)
        (self.data_dir / "sessions.json").write_text(json.dumps(self.sessions, indent=2))

    # --- événements (relayés vers le téléphone par RelayStore)
    def publish(self, kind: str, payload: dict) -> None:
        evt = {"kind": kind, "ts": time.time(), **payload}
        task_id = payload.get("task_id")
        if task_id and task_id in self.tasks and kind == "message":
            self.tasks[task_id].events.append(evt)
        self.forward(evt)

    def forward(self, evt: dict) -> None:
        pass

    def task_update(self, task: Task, **changes) -> None:
        for k, v in changes.items():
            setattr(task, k, v)
        self.publish("task", {"task_id": task.id, "task": self.task_public(task)})

    @staticmethod
    def task_public(task: Task) -> dict:
        d = asdict(task)
        for k in ("events", "context", "resume_session"):
            d.pop(k)
        return d

    # --- audit
    def audit(self, event: str, **fields) -> dict:
        line = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "event": event, **fields}
        with self.audit_path.open("a") as f:
            f.write(json.dumps(line, ensure_ascii=False, default=str) + "\n")
        return line

    def read_audit(self, limit: int = 200) -> list[dict]:
        if not self.audit_path.exists():
            return []
        lines = self.audit_path.read_text().splitlines()[-limit:]
        return [json.loads(x) for x in reversed(lines)]
