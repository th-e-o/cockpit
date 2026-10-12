"""Outils de gestion des services Onyxia : validation des paramètres, valeurs helm, politique."""

import asyncio
from pathlib import Path

import pytest

from cockpit import services as svc
from cockpit.policy import ALLOW, ASK, Policy

ROOT = Path(__file__).resolve().parent.parent
GOOD = dict(nom="test", depot_git="https://github.com/th-e-o/PLFMarket")


def test_validation():
    svc.Launch(**GOOD).validate()
    svc.Launch(**GOOD, branche="feat/x", cpu="500m", memoire="512Mi", disque="20Gi").validate()
    svc.Launch(nom="drones").validate()  # sans dépôt : dossier vide
    with pytest.raises(svc.ServiceError):
        svc.Launch(nom="drones", branche="main").validate()
    for bad in [dict(nom="Bad_Name"), dict(nom="-x"), dict(nom="a" * 40), dict(nom="x; rm -rf"),
                dict(depot_git="http://github.com/a/b"), dict(depot_git="https://u:p@github.com/a/b"),
                dict(depot_git="https://github.com/a/b,init.x=1"), dict(depot_git="https://github.com/a/../b"),
                dict(cpu="2; ls"), dict(memoire="8GB"), dict(disque="-1Gi"), dict(branche="a b")]:
        with pytest.raises(svc.ServiceError):
            svc.Launch(**{**GOOD, **bad}).validate()


def test_valeurs_helm():
    p = svc.Launch(**GOOD, gpu=True)
    v = svc.helm_values(p, {"url": "https://vault", "token": "s.tok", "mount": "onyxia-kv", "directory": "user-x"})
    assert v["kubernetes"]["role"] == "view"  # jamais admin pour un projet
    assert v["git"]["repository"] == GOOD["depot_git"]
    assert v["init"] == {"personalInit": svc.INIT_SCRIPT, "personalInitArgs": "test"}
    assert v["resources"]["limits"]["nvidia.com/gpu"] == "1" and v["persistence"]["size"] == "10Gi"
    assert v["vault"]["secret"] == "Claude" and v["vault"]["token"] == "s.tok"
    assert "vault" not in svc.helm_values(p, {})
    g = svc.helm_values(p, {}, {"name": "Théo", "email": "t@x.fr", "token": "ghp_x", "repository": "pirate"})["git"]
    assert g["token"] == "ghp_x" and g["name"] == "Théo" and g["repository"] == GOOD["depot_git"]
    args = svc.helm_args(p, "ns", "/tmp/v.yaml")
    assert args[:4] == ["helm", "upgrade", "--install", "cockpit-test"] and "s.tok" not in " ".join(args)


def test_arret_limite_aux_releases_du_cockpit(monkeypatch):
    monkeypatch.setattr(svc, "namespace", lambda: "ns")

    async def fake_lister():
        return [{"release": "cockpit-test"}]

    monkeypatch.setattr(svc, "lister", fake_lister)
    ran = []

    async def fake_run(*cmd, timeout=0):
        ran.append(cmd)
        return ""

    monkeypatch.setattr(svc, "_run", fake_run)
    with pytest.raises(svc.ServiceError):
        asyncio.run(svc.arreter("mlflow-563237"))
    assert ran == []
    asyncio.run(svc.arreter("test"))
    assert ran == [("helm", "uninstall", "cockpit-test", "-n", "ns")]


def test_politique_et_validation(monkeypatch):
    policy = Policy.load(ROOT / "config" / "policy.yaml")
    assert policy.evaluate(svc.LIST_TOOL, {}, "/x").action == ALLOW
    assert policy.evaluate(svc.LAUNCH_TOOL, GOOD, "/x").action == ASK
    assert policy.evaluate(svc.STOP_TOOL, {"nom": "test"}, "/x").action == ASK

    monkeypatch.setattr(svc, "namespace", lambda: "ns")

    async def fake_quota(ns):
        return {"pods": {"used": "5", "hard": "100"}, "requests.cpu": {"used": "4", "hard": "40"}}

    monkeypatch.setattr(svc, "quota", fake_quota)
    text = asyncio.run(svc.describe(svc.LAUNCH_TOOL, GOOD))
    assert "cockpit-test" in text and "PLFMarket" in text and "2 CPU" in text and "pods 5/100" in text
    assert asyncio.run(svc.describe("Bash", {})) is None
    assert "sans dépôt" in asyncio.run(svc.describe(svc.LAUNCH_TOOL, {"nom": "drones"}))


def test_nom_depuis_depot():
    assert svc.nom_depuis_depot("https://github.com/th-e-o/PLFMarket") == "plfmarket"
    assert svc.nom_depuis_depot("https://github.com/AIML4OS/WP7_landcover.git") == "wp7-landcover"
    assert svc.nom_depuis_depot("https://github.com/a/" + "x" * 40) == "x" * 30
    assert svc.nom_depuis_depot("https://github.com/a/__") == "projet"


def test_lancement_depuis_le_telephone(tmp_path, monkeypatch):
    """Action « lancer_service » : exécutée par l'orchestrateur sans agent, sans écraser un service existant."""
    from cockpit import worker as worker_mod
    from cockpit.relay import Relay
    from cockpit.store import Task

    monkeypatch.setattr(worker_mod, "DATA", tmp_path)
    launched = []

    async def lister():
        return [{"nom": "existant"}]

    async def lancer(p):
        launched.append(p)
        return f"Service {p.release} lancé."

    monkeypatch.setattr(svc, "lister", lister)
    monkeypatch.setattr(svc, "lancer", lancer)

    async def scenario():
        w = worker_mod.Worker(Relay("http://relais.invalid", "k"))
        ok = Task(id="t1", instruction="pod")
        await w.run_action(ok, {"type": "lancer_service", "nom": "drones", "depot_git": "https://github.com/a/drones"})
        dup = Task(id="t2", instruction="pod")
        await w.run_action(dup, {"type": "lancer_service", "nom": "existant"})
        bad = Task(id="t3", instruction="pod")
        await w.run_action(bad, {"type": "lancer_service", "nom": "Pas Bon"})
        return ok, dup, bad

    ok, dup, bad = asyncio.run(scenario())
    assert ok.status == "done" and ok.result == "Service cockpit-drones lancé."
    assert launched[0].depot_git == "https://github.com/a/drones" and launched[0].cpu == "2"
    assert dup.status == "error" and "existe déjà" in dup.result
    assert bad.status == "error" and "nom invalide" in bad.result and len(launched) == 1


def test_carte_sans_gpu(monkeypatch):
    """La carte de validation dit « sans GPU » et n'affiche le quota GPU que si un GPU est demandé."""
    q = {"count/pods": {"used": "6", "hard": "100"}, "requests.nvidia.com/gpu": {"used": "0", "hard": "1"}}

    async def quota(ns):
        return q

    monkeypatch.setattr(svc, "quota", quota)
    monkeypatch.setattr(svc, "namespace", lambda: "ns")
    cpu = asyncio.run(svc.describe(svc.LAUNCH_TOOL, {"nom": "etp", "depot_git": "https://github.com/a/b"}))
    assert "2 CPU, 8Gi, 10Gi, sans GPU" in cpu and "GPU 0/1" not in cpu and "nvidia" not in cpu
    gpu = asyncio.run(svc.describe(svc.LAUNCH_TOOL, {"nom": "etp", "gpu": True}))
    assert "+ 1 GPU" in gpu and "GPU 0/1" in gpu
