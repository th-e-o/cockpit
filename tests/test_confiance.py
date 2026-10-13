"""Mode confiance : validations de routine auto-approuvées pour une conversation, jamais les opérations sensibles."""

import asyncio
import time
from pathlib import Path

import pytest

from cockpit.agent import Runner
from cockpit.policy import Policy
from cockpit.router import Project
from cockpit.store import Store, Task

ROOT = Path(__file__).resolve().parent.parent
POLICY = Policy.load(ROOT / "config" / "policy.yaml")
CWD = "/home/onyxia/work/projet"


@pytest.mark.parametrize("tool,inp,trusted", [
    ("Bash", {"command": "pip install requests"}, True),
    ("Bash", {"command": "rm -rf build && npm install"}, True),
    ("Bash", {"command": "make build"}, True),                       # commande non répertoriée
    ("Bash", {"command": "pytest > rapport.txt"}, True),             # redirection
    ("Write", {"file_path": CWD + "/.env"}, True),                   # fichier sensible, mais dans le projet
    ("Bash", {"command": "git push origin main"}, False),
    ("Bash", {"command": "git push --force origin feat"}, False),
    ("Bash", {"command": "helm upgrade x y"}, False),
    ("Bash", {"command": "echo $(kubectl delete pod x)"}, False),    # caché dans une substitution
    ("Bash", {"command": "sudo apt install x"}, False),
    ("Bash", {"command": "git reset --hard HEAD~3"}, False),
    ("Bash", {"command": "mc rm -r s3/bucket"}, False),
    ("Bash", {"command": "rm -rf ~"}, False),                        # refus (deny) : jamais
    ("Write", {"file_path": "/tmp/x"}, False),                       # hors du projet
    ("Write", {"file_path": CWD + "/.github/workflows/ci.yml"}, False),
    ("Edit", {"file_path": CWD + "/.git/config"}, False),
    ("mcp__services__lancer_service", {"nom": "x"}, True),           # lancer un pod : routine
    ("mcp__services__arreter_service", {"nom": "x"}, False),         # supprime le stockage : toujours demandé
    ("mcp__cockpit__creer_depot", {"nom": "x"}, True),               # dépôt privé
    ("mcp__cockpit__creer_depot", {"nom": "x", "prive": False}, False),  # dépôt public : toujours demandé
    ("mcp__cockpit__lancer_en_parallele", {"taches": []}, True),
    ("WebFetch", {"url": "https://docs.python.org"}, True),
])
def test_regles(tool, inp, trusted):
    assert POLICY.trusted(tool, inp, CWD) is trusted


def test_mode_confiance_etendu():
    """Par défaut, la routine est automatique ; le mode 🛡 permet en plus d'écrire hors du projet, dans ~/work."""
    assert POLICY.trust_default is True
    autre = {"file_path": "/home/onyxia/work/autre-projet/a.py"}
    assert POLICY.trusted("Write", autre, CWD) is False
    assert POLICY.trusted("Write", autre, CWD, extended=True) is True
    assert POLICY.trusted("Write", {"file_path": "/tmp/x"}, CWD, extended=True) is False
    assert POLICY.trusted("Write", {"file_path": "/home/onyxia/work/autre/.git/config"}, CWD, extended=True) is False
    assert POLICY.trusted("Bash", {"command": "git push origin main"}, CWD, extended=True) is False


def hook(runner, task, cwd):
    opts = runner._options(task, Project(id="p", name="P", cwd=cwd), cwd, None, set())
    return opts.hooks["PreToolUse"][0].hooks[0]


def test_decision_de_l_agent(tmp_path):
    store = Store(tmp_path / "data")
    policy = Policy.load(ROOT / "config" / "policy.yaml")
    policy.trust_default = False  # routine demandée hors mode confiance (comportement de « default: false »)
    runner = Runner(store, policy)
    asked = []

    async def request_approval(task, project, tool, tool_input, reason):
        asked.append(tool_input.get("command"))
        return False

    runner.request_approval = request_approval
    task = Task(id="t", instruction="x", conversation="c")
    store.tasks["t"] = task
    pre = hook(runner, task, str(tmp_path))
    call = lambda cmd: asyncio.run(pre({"tool_name": "Bash", "tool_input": {"command": cmd}, "tool_use_id": cmd}, cmd, None))

    # Sans confiance : validation demandée
    assert call("pip install x")["hookSpecificOutput"]["permissionDecision"] == "deny" and asked == ["pip install x"]
    # Confiance active sur la conversation : routine auto-approuvée, signalée, journalisée
    runner.trust = {"c": time.time() + 600}
    assert call("pip install y")["hookSpecificOutput"]["permissionDecision"] == "allow" and asked == ["pip install x"]
    assert [e["type"] for e in task.events][-1] == "trusted"
    assert '"policy": "trust"' in (tmp_path / "data" / "audit.jsonl").read_text()
    # Opération sensible : toujours demandée
    assert call("git push origin main")["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert asked[-1] == "git push origin main"
    # Autre conversation, ou confiance expirée : demandée
    runner.trust = {"autre": time.time() + 600, "c": time.time() - 1}
    call("pip install z")
    assert asked[-1] == "pip install z"


def test_routine_automatique_par_defaut(tmp_path):
    """« default: true » : routine auto-approuvée dans toutes les conversations ; opérations sensibles demandées."""
    store = Store(tmp_path / "data")
    runner = Runner(store, POLICY)
    asked = []

    async def request_approval(task, project, tool, tool_input, reason):
        asked.append(tool_input.get("command"))
        return False

    runner.request_approval = request_approval
    task = Task(id="t", instruction="x", conversation="c")
    store.tasks["t"] = task
    pre = hook(runner, task, str(tmp_path))
    call = lambda cmd: asyncio.run(pre({"tool_name": "Bash", "tool_input": {"command": cmd}, "tool_use_id": cmd}, cmd, None))
    assert call("pip install rich")["hookSpecificOutput"]["permissionDecision"] == "allow" and asked == []
    assert call("helm upgrade x y")["hookSpecificOutput"]["permissionDecision"] == "deny" and asked == ["helm upgrade x y"]
