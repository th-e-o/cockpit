"""Résumé des modifications (git), worktrees et agents en parallèle ; le client Claude est simulé."""

import asyncio
import subprocess
from pathlib import Path

import pytest
from claude_agent_sdk import ResultMessage

from cockpit import agent as agent_mod
from cockpit import gitstate
from cockpit.agent import Runner
from cockpit.policy import Policy
from cockpit.router import Project
from cockpit.store import Store, Task

ROOT = Path(__file__).resolve().parent.parent


def sh(cwd, *args):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setattr(gitstate, "WORKTREES", tmp_path / "worktrees")
    r = tmp_path / "projet"
    r.mkdir()
    sh(r, "init", "-q", "-b", "main")
    sh(r, "config", "user.email", "t@x.fr")
    sh(r, "config", "user.name", "Test")
    (r / "a.py").write_text("un\ndeux\ntrois\n")
    (r / ".gitignore").write_text("*.log\n")
    sh(r, "add", "-A")
    sh(r, "commit", "-q", "-m", "départ")
    return r


def run(coro):
    return asyncio.run(coro)


def test_modifications_de_l_agent_seulement(repo):
    (repo / "a.py").write_text("un\ndeux\ntrois\nmodif d'avant la tâche\n")  # dossier déjà sale : à ignorer
    (repo / "brouillon.txt").write_text("déjà là\n")
    before = run(gitstate.snapshot(repo))
    (repo / "a.py").write_text("UN\ndeux\ntrois\nmodif d'avant la tâche\n")
    (repo / "nouveau.py").write_text("x = 1\ny = 2\n")
    (repo / "bruit.log").write_text("ignoré\n")
    (repo / "gros.bin").write_bytes(b"0" * (gitstate.BIG_FILE + 1))
    sh(repo, "add", "nouveau.py")
    sh(repo, "commit", "-q", "-m", "Ajoute nouveau.py")
    summary, patches = run(gitstate.changes(before, run(gitstate.snapshot(repo))))
    files = {f["path"]: f for f in summary["files"]}
    assert set(files) == {"a.py", "nouveau.py"}
    assert files["nouveau.py"] == {"path": "nouveau.py", "status": "A", "added": 2, "removed": 0}
    assert (files["a.py"]["added"], files["a.py"]["removed"]) == (1, 1)
    assert summary["added"] == 3 and summary["file_count"] == 2
    assert [c["subject"] for c in summary["commits"]] == ["Ajoute nouveau.py"]
    assert summary["branch"] == "main" and "upstream" not in summary
    assert "-un" in patches["a.py"] and "+UN" in patches["a.py"]
    # L'instantané ne touche pas au dépôt de l'utilisateur
    assert sh(repo, "diff", "--cached", "--name-only") == ""
    assert run(gitstate.snapshot(repo.parent)) is None  # hors dépôt


def test_etat_projet(repo):
    (repo / "a.py").write_text("changé\n")
    st = run(gitstate.repo_status(repo))
    assert st["dirty"] == 1 and st["branch"] == "main" and st["last_commit"]["subject"] == "départ"


def test_worktree_fusion_et_rejet(repo):
    cwd = run(gitstate.add_worktree(str(repo), "p", "conv1"))
    assert cwd == str(gitstate.worktree_path("p", "conv1"))
    assert run(gitstate.add_worktree(str(repo), "p", "conv1")) == cwd  # idempotent
    Path(cwd, "b.py").write_text("b\n")  # non commité : le cockpit le commite avant la fusion
    assert "fusionnée dans main" in run(gitstate.merge_worktree(str(repo), "p", "conv1"))
    assert (repo / "b.py").exists() and not Path(cwd).exists()
    assert "cockpit/conv1" not in sh(repo, "branch")

    cwd = run(gitstate.add_worktree(str(repo), "p", "conv2"))
    Path(cwd, "c.py").write_text("c\n")
    run(gitstate.remove_worktree(str(repo), "p", "conv2"))
    assert not (repo / "c.py").exists() and not Path(cwd).exists()


def test_session_liee_au_dossier(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path))
    d = tmp_path / "projects" / "-home-x-work-p"
    d.mkdir(parents=True)
    (d / "s1.jsonl").write_text("{}")
    assert gitstate.session_usable("/home/x/work/p", "s1")
    assert not gitstate.session_usable("/home/x/work/.cockpit-worktrees/p-c", "s1")
    assert not gitstate.session_usable("/home/x/work/p", "inconnue")


class FakeClient:
    """Agent simulé : écrit un fichier dans son dossier de travail, attend le feu vert du test, puis termine."""
    gates: dict[str, asyncio.Event] = {}
    cwds: dict[str, str] = {}

    def __init__(self, options):
        self.options = options

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def query(self, prompt):
        self.prompt = prompt
        FakeClient.cwds[prompt] = str(self.options.cwd)
        Path(self.options.cwd, f"{prompt}.txt").write_text(prompt + "\n")

    async def receive_response(self):
        await FakeClient.gates.setdefault(self.prompt, asyncio.Event()).wait()
        yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1,
                            session_id=f"sess-{self.prompt}", total_cost_usd=0.01, result=f"**Fait** — {self.prompt}")


def test_deux_conversations_en_parallele(repo, tmp_path, monkeypatch):
    monkeypatch.setattr(agent_mod, "ClaudeSDKClient", FakeClient)
    store = Store(tmp_path / "data")
    runner = Runner(store, Policy.load(ROOT / "config" / "policy.yaml"), max_agents=3)
    proj = Project(id="p", name="P", cwd=str(repo))

    async def scenario():
        t1 = Task(id="t1", instruction="premier", conversation="c1")
        t2 = Task(id="t2", instruction="second", conversation="c2")
        store.tasks.update(t1=t1, t2=t2)
        r1 = asyncio.create_task(runner.run(t1, proj))
        await asyncio.sleep(0.3)
        r2 = asyncio.create_task(runner.run(t2, proj))
        for _ in range(100):  # les deux agents tournent en même temps
            if len(runner.running) == 2:
                break
            await asyncio.sleep(0.05)
        assert runner.running == {"t1", "t2"}
        FakeClient.gates.setdefault("premier", asyncio.Event()).set()
        FakeClient.gates.setdefault("second", asyncio.Event()).set()
        await asyncio.gather(r1, r2)
        return t1, t2

    FakeClient.gates = {}
    t1, t2 = asyncio.run(scenario())
    assert FakeClient.cwds["premier"] == str(repo)  # dossier principal
    assert FakeClient.cwds["second"] == str(gitstate.worktree_path("p", "c2"))  # worktree dédié
    assert t1.worktree is None and t2.worktree["branch"] == "cockpit/c2"
    assert t1.status == t2.status == "done" and t2.result == "**Fait** — second"
    assert [f["path"] for f in t1.changes["files"]] == ["premier.txt"]
    assert [f["path"] for f in t2.changes["files"]] == ["second.txt"]
    evts = [e for e in t2.events if e.get("type") == "changes"]
    assert evts and "+second" in evts[0]["patches"]["second.txt"]
    assert not (repo / "second.txt").exists()

    # Fusion depuis le téléphone : le travail du worktree arrive dans le projet
    text = asyncio.run(runner.worktree_action(t2, proj, "merge"))
    assert "fusionnée" in text and (repo / "second.txt").exists()
    assert not gitstate.worktree_path("p", "c2").exists()


def test_limite_d_agents(repo, tmp_path, monkeypatch):
    monkeypatch.setattr(agent_mod, "ClaudeSDKClient", FakeClient)
    other = tmp_path / "autre"
    other.mkdir()
    store = Store(tmp_path / "data")
    runner = Runner(store, Policy.load(ROOT / "config" / "policy.yaml"), max_agents=1)

    async def scenario():
        a = Task(id="a", instruction="alpha", conversation="a")
        b = Task(id="b", instruction="beta", conversation="b")
        ra = asyncio.create_task(runner.run(a, Project(id="p", name="P", cwd=str(repo))))
        rb = asyncio.create_task(runner.run(b, Project(id="q", name="Q", cwd=str(other))))
        await asyncio.sleep(0.5)
        assert runner.running == {"a"} and b.status == "queued"  # projet différent, mais un seul agent permis
        FakeClient.gates.setdefault("alpha", asyncio.Event()).set()
        await ra
        for _ in range(100):
            if "beta" in FakeClient.gates:
                break
            await asyncio.sleep(0.05)
        FakeClient.gates["beta"].set()
        await rb
        return b

    FakeClient.gates = {}
    b = asyncio.run(scenario())
    assert b.status == "done"
    assert b.changes is None or b.changes.get("git") is False  # hors git : pas de résumé git


def test_outil_parallele_soumis_a_validation():
    p = Policy.load(ROOT / "config" / "policy.yaml")
    assert p.evaluate(agent_mod.PARALLEL_TOOL, {"taches": []}, "/").action == "ask"
    assert agent_mod.tool_label(agent_mod.PARALLEL_TOOL, {"taches": [{}, {}]}) == "🔀 Lance 2 Claude en parallèle"
