"""Validations auto-approuvées (trust.default) : grande liberté, sauf ce qui détruit sans retour."""

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
    ("Bash", {"command": "git push origin main"}, True),
    ("Bash", {"command": "helm upgrade x y"}, True),
    ("Bash", {"command": "kubectl apply -f deploy.yaml"}, True),
    ("Bash", {"command": "sudo apt install x"}, True),
    ("Bash", {"command": "git reset --hard HEAD~3"}, True),
    ("Bash", {"command": "mc cp data.csv s3/bucket/"}, True),
    ("Bash", {"command": "curl -X POST https://api.exemple.fr"}, True),
    ("Bash", {"command": "git push --force origin feat"}, False),    # réécrit l'historique distant
    ("Bash", {"command": "git push origin --delete feat"}, False),
    ("Bash", {"command": "helm uninstall mlflow"}, False),            # supprime un service et son stockage
    ("Bash", {"command": "echo $(kubectl delete pod x)"}, False),    # caché dans une substitution
    ("Bash", {"command": "mc rm -r s3/bucket"}, False),
    ("Bash", {"command": "vault kv get onyxia-kv/x"}, False),
    ("Bash", {"command": "rm -rf ~"}, False),                        # refus (deny) : jamais
    ("Write", {"file_path": "/opt/x"}, False),                       # hors de ~/work et /tmp
    ("Write", {"file_path": "/home/onyxia/work/autre/a.py"}, True),  # autre dossier de l'espace de travail
    ("Write", {"file_path": "/tmp/clone/x"}, True),                  # fichiers temporaires : sans risque
    ("Write", {"file_path": CWD + "/.github/workflows/ci.yml"}, True),
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


def test_hors_de_l_espace_de_travail_demande():
    """Par défaut, tout ~/work est libre ; le reste du système de fichiers est demandé (ou refusé)."""
    assert POLICY.trust_default is True
    assert POLICY.trusted("Write", {"file_path": "/home/onyxia/work/autre-projet/a.py"}, CWD) is True
    assert POLICY.trusted("Write", {"file_path": "/home/onyxia/.bashrc"}, CWD) is False


def hook(runner, task, cwd):
    opts = runner._options(task, Project(id="p", name="P", cwd=cwd), cwd, None, set())
    return opts.hooks["PreToolUse"][0].hooks[0]


def test_decision_de_l_agent(tmp_path):
    store = Store(tmp_path / "data")
    policy = Policy.load(ROOT / "config" / "policy.yaml")
    policy.trust_default = False  # « default: false » : toute la routine est demandée
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

    assert call("pip install x")["hookSpecificOutput"]["permissionDecision"] == "deny" and asked == ["pip install x"]
    # « default: true » : routine auto-approuvée, signalée, journalisée ; opération sensible toujours demandée
    policy.trust_default = True
    assert call("pip install y")["hookSpecificOutput"]["permissionDecision"] == "allow" and asked == ["pip install x"]
    assert [e["type"] for e in task.events][-1] == "trusted"
    assert '"policy": "trust"' in (tmp_path / "data" / "audit.jsonl").read_text()
    assert call("git push --force origin feat")["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert asked[-1] == "git push --force origin feat"


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
    assert call("helm uninstall x")["hookSpecificOutput"]["permissionDecision"] == "deny" and asked == ["helm uninstall x"]


def test_regles_toujours_autoriser():
    """Règles créées depuis le téléphone : ciblées ; jamais un refus, une commande masquée ou .git."""
    p = Policy.load(ROOT / "config" / "policy.yaml")
    p.set_user_rules([{"tool": "Bash", "pattern": r"^helm\s+upgrade\b"}, {"tool": "files", "pattern": "^/home/onyxia/work/autre/"},
                      {"tool": "mcp__services__arreter_service", "pattern": ""}, {"tool": "Bash", "pattern": "(invalide"}])
    assert p.user_allowed("Bash", {"command": "helm upgrade x y"}, CWD) == r"^helm\s+upgrade\b"
    assert p.user_allowed("Bash", {"command": "helm upgrade x y && helm uninstall z"}, CWD) is None  # chaque segment
    assert p.user_allowed("Bash", {"command": "echo $(helm upgrade x y)"}, CWD) is None
    assert p.user_allowed("Bash", {"command": "rm -rf ~"}, CWD) is None
    assert p.user_allowed("Write", {"file_path": "/home/onyxia/work/autre/a.py"}, CWD) == "^/home/onyxia/work/autre/"
    assert p.user_allowed("Write", {"file_path": "/home/onyxia/work/autre/.git/config"}, CWD) is None
    assert p.user_allowed("mcp__services__arreter_service", {"nom": "x"}, CWD) == "mcp__services__arreter_service"
    assert Policy.load(ROOT / "config" / "policy.yaml").user_allowed("Bash", {"command": "helm upgrade x"}, CWD) is None
    assert p.trusted("Write", {"file_path": "/tmp/clone/README.md"}, CWD)  # /tmp : sans validation


def test_regle_appliquee_par_l_agent(tmp_path):
    store = Store(tmp_path / "data")
    runner = Runner(store, Policy.load(ROOT / "config" / "policy.yaml"))
    runner.policy.set_user_rules([{"tool": "Bash", "pattern": r"^helm\s+upgrade\b"}])
    asked = []

    async def request_approval(task, project, tool, tool_input, reason):
        asked.append(tool_input.get("command"))
        return False

    runner.request_approval = request_approval
    task = Task(id="t", instruction="x", conversation="c")
    store.tasks["t"] = task
    pre = hook(runner, task, str(tmp_path))
    out = asyncio.run(pre({"tool_name": "Bash", "tool_input": {"command": "helm upgrade x y"}, "tool_use_id": "1"}, "1", None))
    assert out["hookSpecificOutput"]["permissionDecision"] == "allow" and asked == []
    assert "règle" in task.events[-1]["text"] and '"policy": "rule"' in (tmp_path / "data" / "audit.jsonl").read_text()


def test_mode_hard_aucune_validation_sauf_refus(tmp_path):
    """Mode hard : tout ce qui demandait une validation est approuvé et journalisé ; les refus (deny) restent."""
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
    assert call("git push --force origin feat")["hookSpecificOutput"]["permissionDecision"] == "deny" and asked == ["git push --force origin feat"]
    runner.hard = True
    for cmd in ("git push --force origin feat", "helm uninstall x", "mc rm -r s3/b"):
        assert call(cmd)["hookSpecificOutput"]["permissionDecision"] == "allow", cmd
    assert asked == ["git push --force origin feat"]  # plus aucune demande
    assert '"policy": "hard"' in (tmp_path / "data" / "audit.jsonl").read_text()
    assert call("rm -rf ~")["hookSpecificOutput"]["permissionDecision"] == "deny"  # refus absolu de la politique


def test_mode_hard_aucune_question_a_l_utilisateur(tmp_path):
    """Mode hard : l'agent ne pose plus de question (l'outil lui dit de trancher seul) et le prompt le lui interdit."""
    from cockpit import agent as agent_mod

    created, asked = {}, []
    real = agent_mod.create_sdk_mcp_server
    agent_mod.create_sdk_mcp_server = lambda name, tools: created.update({t.name: t.handler for t in tools}) or {}
    hard = [False]
    try:
        agent_mod.cockpit_server(str(tmp_path), lambda s: None, on_question=asked.append, hard=lambda: hard[0])
    finally:
        agent_mod.create_sdk_mcp_server = real
    ask = lambda: asyncio.run(created["poser_question"]({"question": "Quel format ?", "choix": ["CSV", "JSON"]}))
    ask()
    assert len(asked) == 1  # hors mode hard : la question part sur le téléphone
    hard[0] = True
    r = ask()
    assert len(asked) == 1 and "Mode hard" in r["content"][0]["text"]  # plus de question : l'agent tranche

    store = Store(tmp_path / "data")
    runner = Runner(store, POLICY)
    task = Task(id="t", instruction="x", conversation="c")
    opts = lambda: runner._options(task, Project(id="p", name="P", cwd=str(tmp_path)), str(tmp_path), None, set())
    assert "MODE HARD" not in opts().system_prompt["append"] and "poser_question" in opts().system_prompt["append"]
    runner.hard = True
    append = opts().system_prompt["append"]
    assert "MODE HARD" in append and "AUCUNE question" in append and "premier message de cette conversation" not in append


def test_mode_hard_limite_aux_projets_choisis(tmp_path):
    """Le périmètre du mode hard : un projet hors périmètre garde ses validations, un projet du périmètre n'en a plus."""
    store = Store(tmp_path / "data")
    runner = Runner(store, POLICY)
    asked = []

    async def request_approval(task, project, tool, tool_input, reason):
        asked.append(tool_input.get("command"))
        return False

    runner.request_approval = request_approval
    task = Task(id="t", instruction="x", conversation="c")
    store.tasks["t"] = task
    pre = hook(runner, task, str(tmp_path))  # projet « p »
    call = lambda cmd: asyncio.run(pre({"tool_name": "Bash", "tool_input": {"command": cmd}, "tool_use_id": cmd}, cmd, None))
    runner.hard, runner.hard_projects = True, {"autre"}
    assert runner.is_hard("autre") and not runner.is_hard("p")
    assert call("git push --force origin feat")["hookSpecificOutput"]["permissionDecision"] == "deny" and asked == ["git push --force origin feat"]
    runner.hard_projects = {"p", "autre"}
    assert call("git push --force origin feat")["hookSpecificOutput"]["permissionDecision"] == "allow" and len(asked) == 1
    runner.hard_projects = set()  # sans périmètre : tous les projets
    assert runner.is_hard("n'importe-lequel")


def test_memoire_du_projet_premiere_chose_a_faire(tmp_path):
    """Sans CLAUDE.md (ou presque vide), l'agent doit d'abord créer la mémoire du projet ; avec, il la tient à jour."""
    from cockpit.agent import MEMORY_MIN, memory_missing

    projet = tmp_path / "projet"
    projet.mkdir()
    assert memory_missing(str(projet)) and memory_missing(str(tmp_path / "absent"))
    (projet / "CLAUDE.md").write_text("# Projet\n")
    assert memory_missing(str(projet))  # trop court : considérée comme absente
    runner = Runner(Store(tmp_path / "data"), POLICY)
    task = Task(id="t", instruction="x", conversation="c")
    opts = lambda: runner._options(task, Project(id="p", name="P", cwd=str(projet)), str(projet), None, set()).system_prompt["append"]  # noqa: E731
    sans = opts()
    assert "MÉMOIRE DU PROJET ABSENTE" in sans and "PREMIÈRE chose à faire" in sans and "Prochaines étapes" in sans
    assert "mets à jour CLAUDE.md" not in sans
    (projet / "CLAUDE.md").write_text("# Projet\n" + "Une ligne utile sur le projet. " * (MEMORY_MIN // 20))
    avec = opts()
    assert not memory_missing(str(projet)) and "MÉMOIRE DU PROJET ABSENTE" not in avec and "mets à jour CLAUDE.md" in avec
    # Une tâche lancée dans un worktree vérifie la mémoire du dossier principal du projet, pas celle du worktree
    wt = tmp_path / "wt"
    wt.mkdir()
    task.worktree = {"branch": "cockpit/c", "path": str(wt), "state": "active"}
    assert "MÉMOIRE DU PROJET ABSENTE" not in runner._options(task, Project(id="p", name="P", cwd=str(projet)), str(wt), None, set()).system_prompt["append"]
