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
    ("mcp__services__lancer_service", {"nom": "x"}, False),
    ("mcp__cockpit__creer_depot", {"nom": "x"}, False),
    ("mcp__cockpit__lancer_en_parallele", {"taches": []}, False),
    ("WebFetch", {"url": "https://docs.python.org"}, True),
])
def test_regles(tool, inp, trusted):
    assert POLICY.trusted(tool, inp, CWD) is trusted


def hook(runner, task, cwd):
    opts = runner._options(task, Project(id="p", name="P", cwd=cwd), cwd, None, set())
    return opts.hooks["PreToolUse"][0].hooks[0]


def test_decision_de_l_agent(tmp_path):
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
