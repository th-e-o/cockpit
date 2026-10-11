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
