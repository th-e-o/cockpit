"""Atelier de décision (lecture seule, definir_plan) et relecture du travail par un second agent ; client Claude simulé."""

import asyncio
import subprocess
from pathlib import Path

from claude_agent_sdk import ResultMessage

from cockpit import agent as agent_mod
from cockpit.agent import Runner, atelier_interdit, parse_review
from cockpit.policy import Policy
from cockpit.router import Project
from cockpit.store import Store, Task

ROOT = Path(__file__).resolve().parent.parent
POLICY = Policy.load(ROOT / "config" / "policy.yaml")


def test_atelier_en_lecture_seule(tmp_path):
    cwd = str(tmp_path)
    ok = lambda tool, i: not atelier_interdit(tool, POLICY.evaluate(tool, i, cwd))  # noqa: E731
    assert ok("Read", {"file_path": f"{cwd}/a.py"}) and ok("Grep", {"pattern": "x"}) and ok("Bash", {"command": "git log --oneline"})
    assert not ok("Write", {"file_path": f"{cwd}/a.py"}) and not ok("Edit", {"file_path": f"{cwd}/a.py"})
    assert not ok("Bash", {"command": "rm -rf build"}) and not ok("Bash", {"command": "npm install x"})
    assert not ok("mcp__cockpit__creer_depot", {"nom": "x"}) and not ok("mcp__cockpit__confier_au_projet", {})
    assert ok("mcp__cockpit__poser_question", {}) and ok("mcp__cockpit__etat_projet", {})


def test_options_de_l_atelier(tmp_path):
    """Consigne de brainstorm, outil definir_plan relié au relais, écritures refusées par le hook."""
    runner = Runner(Store(tmp_path / "data"), POLICY)
    plans = []

    async def submit(task, resume, etapes):
        plans.append((task.objective, resume, etapes))
    runner.submit_plan = submit
    task = Task(id="a1", instruction="objectif", mode="atelier", objective="o1")
    opts = runner._options(task, Project(id="p", name="P", cwd=str(tmp_path)), str(tmp_path), None, set())
    assert "ATELIER DE DÉCISION" in opts.system_prompt["append"] and "premier message" not in opts.system_prompt["append"]
    hook = opts.hooks["PreToolUse"][0].hooks[0]
    out = asyncio.run(hook({"tool_name": "Write", "tool_input": {"file_path": f"{tmp_path}/x.py", "content": "x"}}, "u1", None))
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny" and "lecture seule" in out["hookSpecificOutput"]["permissionDecisionReason"]
    # L'outil appelle le relais avec l'objectif de la tâche
    server = agent_mod.cockpit_server(str(tmp_path), lambda s: None, plan=lambda r, e: submit(task, r, e))
    assert server["type"] == "sdk"
    asyncio.run(submit(task, "Option B", [{"titre": "x", "instruction": "y"}]))
    assert plans == [("o1", "Option B", [{"titre": "x", "instruction": "y"}])]


def test_lecture_du_bloc_de_relecture():
    text = 'Relu.\n{"remarques": [{"gravite": "important", "fichier": "a.py", "texte": "cas vide", "corrigee": true},' \
           ' {"gravite": "énorme", "texte": "nom"}], "avis": "Bon à garder."}'
    remarques, avis = parse_review(text)
    assert remarques == [{"gravite": "important", "fichier": "a.py", "texte": "cas vide", "corrigee": True},
                         {"gravite": "mineur", "fichier": "", "texte": "nom", "corrigee": False}]
    assert avis == "Bon à garder." and parse_review("pas de JSON") == ([], "pas de JSON")


class Client:
    """Premier client : l'auteur écrit 20 lignes ; second client : le relecteur corrige une ligne et rend son avis."""
    prompts: list[str] = []

    def __init__(self, options):
        self.options = options

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def query(self, prompt):
        Client.prompts.append(prompt)
        self.prompt = prompt

    async def receive_response(self):
        f = Path(self.options.cwd, "module.py")
        if "relecteur" in self.prompt:
            f.write_text(f.read_text().replace("x0 = 0", "x0 = 1"))
            result = '{"remarques": [{"gravite": "important", "fichier": "module.py", "texte": "valeur initiale", "corrigee": true}], "avis": "OK."}'
        else:
            f.write_text("".join(f"x{i} = {i}\n" for i in range(20)))
            result = "**Fait** — module écrit."
        yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1, session_id="s",
                            total_cost_usd=0.01, result=result, usage={"input_tokens": 100, "output_tokens": 10})


def test_relecture_par_un_second_agent(tmp_path, monkeypatch):
    repo = tmp_path / "projet"
    repo.mkdir()
    for args in (["init", "-q", "-b", "main"], ["config", "user.email", "t@x.fr"], ["config", "user.name", "T"],
                 ["commit", "-q", "--allow-empty", "-m", "départ"]):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    monkeypatch.setattr(agent_mod, "ClaudeSDKClient", Client)
    Client.prompts = []
    runner = Runner(Store(tmp_path / "data"), POLICY)
    task = Task(id="t1", instruction="écris le module", conversation="t1")
    runner.store.tasks["t1"] = task
    asyncio.run(runner.run(task, Project(id="p", name="P", cwd=str(repo))))
    assert len(Client.prompts) == 2 and "relecteur" in Client.prompts[1] and "module.py" in Client.prompts[1]
    review = next(e for e in task.events if e.get("type") == "relecture")
    assert review["remarques"][0]["corrigee"] and review["avis"] == "OK."
    assert task.status == "done" and task.result == "**Fait** — module écrit."  # la réponse de l'auteur reste la réponse
    assert "x0 = 1" in (repo / "module.py").read_text() and task.tokens["input"] == 200  # correction prise en compte, tokens cumulés
    # Petit changement : pas de relecture
    Client.prompts = []
    monkeypatch.setattr(agent_mod, "REVIEW_MIN_LINES", 1000)
    t2 = Task(id="t2", instruction="petit", conversation="t2")
    asyncio.run(runner.run(t2, Project(id="p", name="P", cwd=str(repo))))
    assert len(Client.prompts) == 1
