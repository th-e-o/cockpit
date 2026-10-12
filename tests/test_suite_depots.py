"""Prochaines étapes proposées par l'agent et création de dépôts GitHub (API GitHub simulée)."""

import asyncio
import subprocess

import httpx
import pytest

from cockpit import agent as agent_mod
from cockpit import github
from cockpit import services as svc
from cockpit.policy import Policy
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent



def handlers(cwd, captured, spawn=None):
    """Gestionnaires des outils du serveur « cockpit » (appelés comme le ferait le SDK)."""
    created = {}
    real = agent_mod.create_sdk_mcp_server
    agent_mod.create_sdk_mcp_server = lambda name, tools: created.update({t.name: t.handler for t in tools}) or {}
    try:
        agent_mod.cockpit_server(cwd, captured.extend, spawn)
    finally:
        agent_mod.create_sdk_mcp_server = real
    return created


def test_proposer_suite(tmp_path):
    captured = []
    h = handlers(str(tmp_path), captured)
    # lancer_en_parallele seulement avec un worker
    assert set(h) == {"proposer_suite", "creer_depot", "capturer_ecran", "ouvrir_pr"}
    r = asyncio.run(h["proposer_suite"]({"etapes": [
        {"titre": "  Lancer   les tests ", "instruction": "Lance pytest et corrige"}, {"titre": "", "instruction": "x"},
        {"titre": "Publier", "instruction": "Pousse la branche"}] + [{"titre": "t", "instruction": "i"}] * 5}))
    assert "is_error" not in r
    assert captured[0] == {"titre": "Lancer les tests", "instruction": "Lance pytest et corrige"} and len(captured) == 4
    assert asyncio.run(h["proposer_suite"]({"etapes": []}))["is_error"]


def test_politique_et_libelles():
    p = Policy.load(ROOT / "config" / "policy.yaml")
    assert p.evaluate(agent_mod.NEXT_TOOL, {}, "/").action == "allow"
    assert p.evaluate(agent_mod.REPO_TOOL, {"nom": "x"}, "/").action == "ask"
    assert agent_mod.tool_label(agent_mod.NEXT_TOOL, {}) is None
    assert agent_mod.tool_label(agent_mod.REPO_TOOL, {"nom": "drones"}) == "📦 Crée le dépôt GitHub drones"


def fake_github(monkeypatch, handler):
    monkeypatch.setenv("GIT_PERSONAL_ACCESS_TOKEN", "ghp_test")
    real = httpx.AsyncClient
    monkeypatch.setattr(github, "_client", lambda: real(base_url=github.API, transport=httpx.MockTransport(handler)))


def test_creer_depot_api(monkeypatch):
    calls = []

    def handler(req):
        calls.append(req)
        body = __import__("json").loads(req.content)
        if body["name"] == "existe":
            return httpx.Response(422, json={"message": "name already exists"})
        if body["name"] == "interdit":
            return httpx.Response(403, json={"message": "Resource not accessible by personal access token"})
        return httpx.Response(201, json={"full_name": f"th-e-o/{body['name']}", "html_url": f"https://github.com/th-e-o/{body['name']}",
                                         "private": body["private"], "default_branch": "main"})

    fake_github(monkeypatch, handler)
    r = asyncio.run(github.creer_depot("drones", description="Vols"))
    assert r == {"name": "th-e-o/drones", "url": "https://github.com/th-e-o/drones", "private": True, "branch": "main"}
    assert calls[0].url.path == "/user/repos"
    for nom, msg in [("existe", "existe déjà"), ("interdit", "Administration"), ("a b", "invalide"), ("x.git", "invalide")]:
        with pytest.raises(github.GitHubError, match=msg):
            asyncio.run(github.creer_depot(nom))


def test_sans_token(monkeypatch):
    monkeypatch.delenv("GIT_PERSONAL_ACCESS_TOKEN", raising=False)
    with pytest.raises(github.GitHubError, match="token"):
        asyncio.run(github.creer_depot("ok"))
    assert asyncio.run(github.depots()) == []


def test_creer_depot_relie_et_pousse(tmp_path, monkeypatch):
    """L'outil relie le projet au nouveau dépôt (origin) et y pousse la branche courante."""
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(remote)], check=True)
    proj = tmp_path / "projet"
    proj.mkdir()
    for args in (["init", "-q", "-b", "main"], ["config", "user.email", "t@x.fr"], ["config", "user.name", "T"]):
        subprocess.run(["git", *args], cwd=proj, check=True)
    (proj / "a.txt").write_text("a\n")
    subprocess.run(["git", "add", "-A"], cwd=proj, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=proj, check=True)

    async def creer(nom, prive=True, description=""):
        return {"name": f"th-e-o/{nom}", "url": str(tmp_path / "remote"), "private": prive, "branch": "main"}

    monkeypatch.setattr(github, "creer_depot", creer)
    h = handlers(str(proj), [])
    text = asyncio.run(h["creer_depot"]({"nom": "projet"}))["content"][0]["text"]
    assert "Dépôt privé th-e-o/projet créé" in text and "poussée" in text
    assert subprocess.run(["git", "log", "--oneline", "main"], cwd=remote, capture_output=True, text=True).stdout.strip().endswith("init")
    # Un projet qui a déjà un origin n'est pas modifié
    text = asyncio.run(h["creer_depot"]({"nom": "autre"}))["content"][0]["text"]
    assert "déjà un dépôt distant" in text


def test_nouveau_pod_avec_nouveau_depot(tmp_path, monkeypatch):
    from cockpit import worker as worker_mod
    from cockpit.relay import Relay
    from cockpit.store import Task

    monkeypatch.setattr(worker_mod, "DATA", tmp_path)
    created, launched = [], []

    async def creer(nom, prive=True, description=""):
        created.append(nom)
        return {"name": f"th-e-o/{nom}", "url": f"https://github.com/th-e-o/{nom}", "private": prive, "branch": "main"}

    async def lister():
        return [{"nom": "existant"}]

    async def lancer(p):
        launched.append(p)
        return f"Service {p.release} lancé."

    monkeypatch.setattr(github, "creer_depot", creer)
    monkeypatch.setattr(svc, "lister", lister)
    monkeypatch.setattr(svc, "lancer", lancer)

    async def scenario():
        w = worker_mod.Worker(Relay("http://relais.invalid", "k"))
        ok, dup = Task(id="a", instruction="pod"), Task(id="b", instruction="pod")
        await w.run_action(ok, {"type": "lancer_service", "nom": "drones", "creer_depot": {"nom": "drones", "prive": True}})
        await w.run_action(dup, {"type": "lancer_service", "nom": "existant", "creer_depot": {"nom": "existant"}})
        return ok, dup

    ok, dup = asyncio.run(scenario())
    assert ok.status == "done" and ok.result.startswith("Dépôt privé th-e-o/drones créé")
    assert launched[0].depot_git == "https://github.com/th-e-o/drones"
    assert dup.status == "error" and created == ["drones"]  # service existant : aucun dépôt orphelin
