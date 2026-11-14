"""Pods de projet : demandes hors projet confiées à l'orchestrateur, mise à jour automatique du cockpit."""

import asyncio
import sqlite3
import subprocess
from pathlib import Path

import pytest
from test_relay import PHP, call, last_status, paired_phone, relay, worker_call, wpoll  # noqa: F401 (fixture)

from cockpit.agent import Runner
from cockpit.policy import Policy
from cockpit.router import Project
from cockpit.store import Store, Task

ROOT = Path(__file__).resolve().parent.parent


def sh(cwd, *args):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def test_agent_de_projet_renvoie_vers_l_orchestrateur(tmp_path):
    policy = Policy.load(ROOT / "config" / "policy.yaml")
    proj = Project(id="analyse-etp", name="analyse-etp", cwd=str(tmp_path))
    task = Task(id="t", instruction="nouveau projet")
    projet = Runner(Store(tmp_path / "a"), policy, services=False)
    projet.spawn_tasks = lambda *a, **k: None
    prompt = projet._options(task, proj, str(tmp_path), None, set()).system_prompt["append"]
    assert 'confier_au_projet(projet="orchestrateur")' in prompt and "« analyse-etp »" in prompt
    orch = Runner(Store(tmp_path / "b"), policy, services=True)
    orch.spawn_tasks = lambda *a, **k: None
    prompt = orch._options(task, proj, str(tmp_path), None, set()).system_prompt["append"]
    assert 'projet="orchestrateur"' not in prompt and "creer_depot(nom, relier=false)" in prompt


@pytest.fixture
def clone(tmp_path):
    """Un cockpit cloné (pod de projet) et son dépôt d'origine, qui prend de l'avance."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    dev = tmp_path / "dev"
    subprocess.run(["git", "clone", "-q", str(origin), str(dev)], check=True, capture_output=True)
    for args in (["config", "user.email", "t@x.fr"], ["config", "user.name", "T"], ["checkout", "-q", "-b", "main"]):
        sh(dev, *args)
    (dev / "requirements.txt").write_text("httpx\n")
    sh(dev, "add", "-A")
    sh(dev, "commit", "-qm", "v1")
    sh(dev, "push", "-q", "-u", "origin", "main")
    pod = tmp_path / "pod"
    subprocess.run(["git", "clone", "-q", str(origin), str(pod)], check=True, capture_output=True)
    (dev / "nouveau.py").write_text("x = 1\n")
    sh(dev, "add", "-A")
    sh(dev, "commit", "-qm", "v2")
    sh(dev, "push", "-q")
    return pod


def test_mise_a_jour_automatique(clone, tmp_path, monkeypatch):
    from cockpit import worker as worker_mod
    from cockpit.relay import Relay

    monkeypatch.setattr(worker_mod, "DATA", tmp_path / "data")
    monkeypatch.setattr(worker_mod, "ROOT", clone)
    monkeypatch.setattr(worker_mod, "UPDATE_EVERY", 0)
    w = worker_mod.Worker(Relay("http://relais.invalid", "k"))
    restarted, installed = [], []

    async def flush():
        pass

    async def install():
        installed.append(True)

    def execv(exe, args):
        restarted.append(args)
        raise SystemExit

    w.relay.flush = flush
    w.install_requirements = install
    monkeypatch.setattr(worker_mod.os, "execv", execv)
    assert asyncio.run(w.update_available()) == 1

    # Une tâche en cours : pas de mise à jour (vérifié une fois, puis on arrête la boucle)
    w.store.tasks["t"] = Task(id="t", instruction="x", status="running")
    calls = []

    async def once():
        calls.append(1)
        if len(calls) > 1:
            raise asyncio.CancelledError
        return 1

    w.update_available = once
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(w.auto_update())
    assert not (clone / "nouveau.py").exists() and not restarted

    # Libre : git pull, dépendances, redémarrage du worker
    w.store.tasks["t"].status = "done"
    del w.update_available
    with pytest.raises(SystemExit):
        asyncio.run(w.auto_update())
    assert (clone / "nouveau.py").exists() and installed and restarted[0][-3:] == ["-m", "cockpit.worker", "run"]


def test_dossier_du_projet_cree_si_absent(tmp_path, monkeypatch):
    """Pod lancé sur un dépôt vide : le dossier du projet n'existe pas ; le worker le crée, relié au dépôt."""
    from cockpit import worker as worker_mod
    from cockpit.relay import Relay

    monkeypatch.setattr(worker_mod, "DATA", tmp_path / "data")
    monkeypatch.setattr(worker_mod, "ORCHESTRATOR", False)
    monkeypatch.setenv("GIT_REPOSITORY", str(tmp_path / "inexistant.git"))  # clone impossible (dépôt vide)
    w = worker_mod.Worker(Relay("http://relais.invalid", "k"))
    cwd = tmp_path / "work" / "russe-telegram-cartes"
    w.projects = {"russe": Project(id="russe", name="russe", cwd=str(cwd))}
    w.ensure_project_dirs()
    assert (cwd / ".git").is_dir()
    remote = subprocess.run(["git", "remote", "get-url", "origin"], cwd=cwd, capture_output=True, text=True).stdout.strip()
    assert remote == str(tmp_path / "inexistant.git")


def test_outils_interdits_et_etapes_completes():
    from cockpit.agent import clean_next_steps

    policy = Policy.load(ROOT / "config" / "policy.yaml")
    for tool in ("ListAgents", "SendMessage", "RemoteTrigger", "CronCreate"):
        assert policy.evaluate(tool, {}, "/").action == "deny"  # jamais d'accès aux autres sessions Claude
    steps = clean_next_steps([{"titre": "Infos OVH", "instruction": "Mon offre est : <mutualisé / VPS>."},
                              {"titre": "Hébergement", "instruction": "Voici l'hébergement : à compléter"},
                              {"titre": "Tests", "instruction": "Lance pytest"}])
    assert steps == [{"titre": "Tests", "instruction": "Lance pytest"}]


@pytest.mark.skipif(not PHP, reason="PHP introuvable (définir COCKPIT_PHP)")
def test_attente_projet_expire(relay):  # noqa: F811 (fixture importée)
    """Une tâche confiée à un pod qui ne se connecte pas passe en erreur au bout de 30 min, avec un message clair."""
    p = paired_phone(relay)
    wpoll(relay, "orch-ex", True, [{"id": "ex-onyxia", "name": "Onyxia"}], hello=True)
    parent = call(p, "tache", {"instruction": "lance un pod ex-bot", "project": "ex-onyxia"}).json()
    body = {"parent": parent["id"], "worker_id": "orch-ex", "defer": True}
    ancienne, recente = worker_call(relay, "w_taches", {**body, "tasks": [
        {"instruction": "Code le bot", "project": "ex-bot"},
        {"instruction": "Code l'autre bot", "project": "ex-autre"}]}).json()["ids"]
    db = Path(relay["docroot"]) / "cockpit" / "data" / "cockpit.db"
    with sqlite3.connect(db) as c:
        c.execute("UPDATE taches SET cree_le = cree_le - 1801 WHERE id = ?", (ancienne,))
    etats = last_status(p)
    assert etats[ancienne]["status"] == "error" and etats[ancienne]["waiting_project"] is False
    assert "ne s'est pas connecté au bout de 30 min" in etats[ancienne]["result"]
    assert etats[recente]["status"] == "pending" and etats[recente]["waiting_project"] is True
    # Le pod finit par se connecter : seule la tâche encore en attente lui est livrée
    premier = wpoll(relay, "pod-ex", False, [{"id": "ex-bot", "name": "ex-bot"}, {"id": "ex-autre", "name": "ex-autre"}], hello=True)
    assert [t["id"] for t in premier["tasks"]] == [recente]
