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
    """Premier client : l'auteur modifie un fichier sensible ; second client : le relecteur (lecture seule) rend ses remarques."""
    prompts: list[str] = []
    options: list = []

    def __init__(self, options):
        self.options = options
        Client.options.append(options)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def query(self, prompt):
        Client.prompts.append(prompt)
        self.prompt = prompt

    async def receive_response(self):
        if "relis, en lecture seule" in self.prompt:
            result = ('{"remarques": [{"gravite": "bloquant", "fichier": "auth.py", "texte": "mot de passe comparé en clair"},'
                      ' {"gravite": "mineur", "fichier": "auth.py", "texte": "nom"}], "avis": "À corriger."}')
        else:
            Path(self.options.cwd, self.prompt).write_text("".join(f"x{i} = {i}\n" for i in range(6)))
            result = "**Fait** — écrit."
        yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1, session_id="s",
                            total_cost_usd=0.01, result=result, usage={"input_tokens": 100, "output_tokens": 10})


def test_relecture_ciblee_et_bon_marche(tmp_path, monkeypatch):
    repo = tmp_path / "projet"
    repo.mkdir()
    for args in (["init", "-q", "-b", "main"], ["config", "user.email", "t@x.fr"], ["config", "user.name", "T"],
                 ["commit", "-q", "--allow-empty", "-m", "départ"]):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    monkeypatch.setattr(agent_mod, "ClaudeSDKClient", Client)
    Client.prompts, Client.options = [], []
    runner = Runner(Store(tmp_path / "data"), POLICY)
    proj = Project(id="p", name="P", cwd=str(repo))
    # 6 lignes dans un fichier ordinaire : pas de relecture
    t0 = Task(id="t0", instruction="utils.py", conversation="t0")
    asyncio.run(runner.run(t0, proj))
    assert len(Client.prompts) == 1 and not [e for e in t0.events if e.get("type") == "relecture"]
    # 6 lignes dans un fichier sensible : relu, en lecture seule, sur le diff, avec un modèle intermédiaire et peu de tours
    Client.prompts, Client.options = [], []
    task = Task(id="t1", instruction="auth.py", conversation="t1")
    runner.store.tasks["t1"] = task
    asyncio.run(runner.run(task, proj))
    assert len(Client.prompts) == 2 and "+x5 = 5" in Client.prompts[1] and "auth.py" in Client.prompts[1]
    rv = Client.options[1]
    assert rv.model == "sonnet" and rv.max_turns == agent_mod.REVIEW_MAX_TURNS
    hook = rv.hooks["PreToolUse"][0].hooks[0]
    out = asyncio.run(hook({"tool_name": "Edit", "tool_input": {"file_path": f"{repo}/auth.py"}}, "u", None))
    assert out["hookSpecificOutput"]["permissionDecision"] == "deny"  # le relecteur ne modifie rien
    review = next(e for e in task.events if e.get("type") == "relecture")
    assert "sensible" in review["raison"] and len(review["remarques"]) == 2
    # Remarque sérieuse : sa correction devient la première étape proposée ; la réponse reste celle de l'auteur
    assert task.next_steps[0]["titre"] == "Corriger les remarques de relecture" and "mot de passe" in task.next_steps[0]["instruction"]
    assert task.status == "done" and task.result == "**Fait** — écrit." and task.tokens["input"] == 200


def test_decision_de_relecture():
    f = lambda path, n: {"path": path, "added": n, "removed": 0}  # noqa: E731
    d = agent_mod.review_decision
    assert d({"files": [f("README.md", 400), f("uv.lock", 900)]}) == (False, "aucun code modifié")
    assert d({"files": [f("app/util.py", 40)]})[0] is False                       # changement limité
    assert d({"files": [f("app/util.py", 40)]}, autonomous=False)[0] is False
    assert d({"files": [f("app/util.py", 60)]}, autonomous=True)[0] is True       # fait sans l'utilisateur
    assert d({"files": [f("app/core.py", 200)]})[0] is True                        # gros changement
    assert d({"files": [f("tests/test_core.py", 300)]})[0] is False               # des tests : un tiers du poids
    assert d({"files": [f("app/auth/login.py", 4)]})[1].startswith("fichier(s) sensible(s)")
    assert d({"files": [f(".github/workflows/ci.yml", 5)]})[0] is True
    assert d({"files": [f("app/auth.py", 50)]}, tests={"ok": False})[0] is False   # tests rouges : déjà à corriger
    assert d({"files": [f("app/auth.py", 50)]}, session=0.9)[0] is False           # session presque pleine
    assert d({"files": [f("app/auth.py", 50)]}, done_today=99)[0] is False         # plafond du jour


def test_diff_du_relecteur_cible():
    changes = {"files": [{"path": "README.md", "added": 50}, {"path": "app/big.py", "added": 80}, {"path": "app/auth.py", "added": 2}]}
    patches = {"README.md": "doc", "app/big.py": "x" * 500, "app/auth.py": "+check()"}
    diff = agent_mod.review_diff(changes, patches, limit=300)
    assert diff.index("app/auth.py") < diff.index("Non inclus") and "README.md" not in diff and "app/big.py" in diff
