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
    assert v["init"] == {"personalInit": svc.INIT_SCRIPT.format(ref="main"), "personalInitArgs": "test main"}
    sha = "0123456789abcdef0123456789abcdef01234567"
    assert svc.helm_values(p, {}, ref=sha)["init"] == {
        "personalInit": f"https://raw.githubusercontent.com/th-e-o/cockpit/{sha}/scripts/onyxia-init.sh",
        "personalInitArgs": f"test {sha}"}
    assert v["resources"]["limits"]["nvidia.com/gpu"] == "1" and v["persistence"]["size"] == "10Gi"
    assert v["vault"]["secret"] == "Claude" and v["vault"]["token"] == "s.tok"
    assert "vault" not in svc.helm_values(p, {})
    g = svc.helm_values(p, {}, {"name": "Théo", "email": "t@x.fr", "token": "ghp_x", "repository": "pirate"})["git"]
    assert g["token"] == "ghp_x" and g["name"] == "Théo" and g["repository"] == GOOD["depot_git"]
    args = svc.helm_args(p, "ns", "/tmp/v.yaml")
    assert args[:4] == ["helm", "upgrade", "--install", "cockpit-test"] and "s.tok" not in " ".join(args)


def test_version_cockpit(monkeypatch):
    sha = "0123456789abcdef0123456789abcdef01234567"
    monkeypatch.setenv("COCKPIT_VERSION", sha)
    assert svc.version_cockpit() == sha
    monkeypatch.setenv("COCKPIT_VERSION", "dev")  # image construite à la main : on lit le clone, sinon main
    v = svc.version_cockpit()
    assert v == "main" or svc._SHA.match(v)
    monkeypatch.setattr(svc, "ROOT", Path("/nexiste/pas"))
    assert svc.version_cockpit() == "main"


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


def test_vscode_joignable_sur_les_nouveaux_pods():
    """Ingress VSCode (bouton « Ouvrir VSCode ») avec mot de passe aléatoire ; port d'aperçu seulement sur demande."""
    ing = {"ingressClassName": "onyxia", "hostname": "user-x-cockpit-etp.lab", "userHostname": "user-x-cockpit-etp-user.lab"}
    v = svc.helm_values(svc.Launch(nom="etp"), {}, None, ing)
    assert v["ingress"]["enabled"] and v["ingress"]["hostname"] == "user-x-cockpit-etp.lab"
    assert len(v["security"]["password"]) >= 20 and "networking" not in v
    assert svc.helm_values(svc.Launch(nom="etp"), {}, None, ing)["security"]["password"] != v["security"]["password"]
    assert svc.helm_values(svc.Launch(nom="etp", apercu=True), {}, None, ing)["networking"]["user"]["enabled"]
    assert "ingress" not in svc.helm_values(svc.Launch(nom="etp"), {}, None, None)


def test_reveiller_worker(monkeypatch):
    """Réveil d'un pod prêt dont le worker est mort : kubectl exec dans le bon conteneur, sans rien créer ni remplacer."""
    import asyncio
    import json

    from cockpit import services as svc

    calls = []
    pods = {"items": [
        {"metadata": {"name": "cockpit-lexipix-vscode-python-1"}, "spec": {"containers": [{"name": "vscode-python"}]},
         "status": {"phase": "Pending", "containerStatuses": [{"ready": False}]}},
        {"metadata": {"name": "cockpit-lexipix-vscode-python-0"}, "spec": {"containers": [{"name": "vscode-python"}]},
         "status": {"phase": "Running", "containerStatuses": [{"ready": True}]}}]}

    stdins = []

    async def run(*cmd, timeout=300, stdin=None):
        calls.append(cmd)
        stdins.append(stdin)
        return json.dumps(pods) if cmd[1] == "get" else "relancé"

    monkeypatch.setattr(svc, "_run", run)
    monkeypatch.setattr(svc, "namespace", lambda: "user-test")
    monkeypatch.setenv("COCKPIT_WORKER_KEY", "cle-secrete-du-worker")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "jeton-secret")
    assert asyncio.run(svc.reveiller_worker("lexipix")) == "relancé"
    get, ex = calls
    # Les secrets passent par l'entrée standard : jamais dans les arguments de la commande
    assert stdins[1] == b"cle-secrete-du-worker\njeton-secret\n" and "-i" in ex
    assert "cle-secrete" not in " ".join(ex) and "jeton-secret" not in " ".join(ex)
    assert "app.kubernetes.io/instance=cockpit-lexipix" in get and "user-test" in get
    assert ex[:5] == ("kubectl", "exec", "-i", "-n", "user-test") and "cockpit-lexipix-vscode-python-0" in ex and "PROJET=lexipix" in ex
    assert ex[ex.index("-c") + 1] == "vscode-python" and "helm" not in " ".join(ex)
    # Sans clé de worker côté orchestrateur : rien n'est lancé dans le pod
    calls.clear()
    monkeypatch.delenv("COCKPIT_WORKER_KEY")
    try:
        asyncio.run(svc.reveiller_worker("lexipix"))
        raise AssertionError("clé absente")
    except svc.ServiceError as e:
        assert "clé du worker" in str(e)
    assert all(c[1] != "exec" for c in calls)
    monkeypatch.setenv("COCKPIT_WORKER_KEY", "cle-secrete-du-worker")
    # Nom invalide : aucune commande lancée ; pod pas prêt : erreur claire
    calls.clear()
    for bad in ("Lexi pix", "a;rm -rf ~", ""):
        try:
            asyncio.run(svc.reveiller_worker(bad))
            raise AssertionError(bad)
        except svc.ServiceError:
            pass
    assert calls == []
    pods["items"] = pods["items"][:1]
    try:
        asyncio.run(svc.reveiller_worker("lexipix"))
        raise AssertionError("pod pas prêt")
    except svc.ServiceError as e:
        assert "aucun pod prêt" in str(e)


def test_git_values_cache_jamais_a_zero(monkeypatch):
    """Un cache de 0 s ferait oublier le token git aussitôt : on le remplace par la durée par défaut."""
    monkeypatch.setenv("GIT_PERSONAL_ACCESS_TOKEN", "x")
    for valeur in ("0", ""):
        monkeypatch.setenv("GIT_CREDENTIALS_CACHE_DURATION", valeur)
        assert svc.git_values()["cache"] == svc.GIT_CACHE_DEFAUT
    monkeypatch.setenv("GIT_CREDENTIALS_CACHE_DURATION", "600")
    assert svc.git_values()["cache"] == "600"
    monkeypatch.delenv("GIT_CREDENTIALS_CACHE_DURATION")
    assert svc.git_values()["cache"] == svc.GIT_CACHE_DEFAUT
