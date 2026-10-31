"""Tests d'intégration du relais PHP (web/api.php) avec un téléphone simulé et le vrai worker.

Nécessite PHP >= 8.1 avec pdo_sqlite et openssl (binaire `php`, ou variable COCKPIT_PHP).
Claude n'est pas appelé : l'exécution de l'agent est simulée.
"""

import asyncio
import base64
import hashlib
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

ROOT = Path(__file__).resolve().parent.parent
PHP = os.environ.get("COCKPIT_PHP") or shutil.which("php")
pytestmark = pytest.mark.skipif(not PHP, reason="PHP introuvable (définir COCKPIT_PHP)")

KEY = "cle-de-test-du-worker"
CLE_API = "cle-de-test-du-bot"
b64u = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()
b64u_dec = lambda s: base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


@pytest.fixture(scope="module")
def relay():
    yield from lancer_relais()


@pytest.fixture
def relay_neuf():
    """Relais vide, pour les tests qui dépendent de l'état global (full auto, mode hard) et ne doivent pas se gêner entre eux."""
    yield from lancer_relais()


def lancer_relais():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    # Comme en production : le cockpit est un sous-dossier du site (https://deltastat.fr/cockpit/)
    docroot = Path(tempfile.mkdtemp())
    web = docroot / "cockpit"
    shutil.copytree(ROOT / "web", web, ignore=shutil.ignore_patterns("*.db*", "config.php"))
    origin = f"http://localhost:{port}"
    (web / "config.php").write_text(
        f"<?php return ['worker_cle_sha256' => '{hashlib.sha256(KEY.encode()).hexdigest()}',"
        f" 'api_cles_sha256' => ['{hashlib.sha256(CLE_API.encode()).hexdigest()}'],"
        f" 'hote' => 'localhost', 'origine' => '{origin}', 'base' => '/cockpit/', 'debug' => true];"
    )
    proc = subprocess.Popen([PHP, "-S", f"127.0.0.1:{port}", "-t", str(docroot)],
                            env={**os.environ, "PHP_CLI_SERVER_WORKERS": "4"},
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}/cockpit"
    for _ in range(50):
        try:
            httpx.get(base + "/index.html")
            break
        except httpx.ConnectError:
            time.sleep(0.1)
    yield {"base": base, "origin": origin, "app": origin + "/cockpit/", "docroot": str(docroot)}
    proc.terminate()


def phone(relay):
    return httpx.Client(base_url=relay["base"], headers={"Host": relay["origin"].removeprefix("http://")}, timeout=30)


def call(client, route, body=None, **params):
    if body is None:
        return client.get("/api.php", params={"r": route, **params})
    return client.post("/api.php", params={"r": route, **params}, json=body)


def worker_call(relay, route, body, key=KEY):
    return httpx.post(relay["base"] + "/api.php", params={"r": route}, json=body,
                      headers={"Authorization": f"Bearer {key}"}, timeout=30)


def paired_phone(relay):
    url = worker_call(relay, "w_appairage", {}).json()["url"]
    p = phone(relay)
    assert call(p, "appairer", {"token": url.split("#pair=")[1]}).status_code == 200
    return p


def cbor(v):
    """Encodeur CBOR minimal (entiers, octets, chaînes, tables) pour fabriquer une attestation WebAuthn."""
    def head(major, n):
        if n < 24:
            return bytes([major << 5 | n])
        for info, size in ((24, 1), (25, 2), (26, 4), (27, 8)):
            if n < 256 ** size:
                return bytes([major << 5 | info]) + n.to_bytes(size, "big")
    if isinstance(v, int):
        return head(0, v) if v >= 0 else head(1, -1 - v)
    if isinstance(v, bytes):
        return head(2, len(v)) + v
    if isinstance(v, str):
        return head(3, len(v.encode())) + v.encode()
    return head(5, len(v)) + b"".join(cbor(k) + cbor(x) for k, x in v.items())


class Authenticator:
    """Authentificateur WebAuthn logiciel (ES256), comme Face ID / Touch ID."""

    def __init__(self, origin):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.id = b64u(os.urandom(32))
        self.origin = origin

    def client_data(self, type_, challenge, origin=None):
        return json.dumps({"type": type_, "challenge": challenge, "origin": origin or self.origin}).encode()

    def register(self, options, flags=0x45, rp_id="localhost", key=None):
        """Attestation « none » : authData avec l'identifiant et la clé COSE, comme un vrai authentificateur."""
        pub = (key or self.key).public_key()
        spki = self.key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        nums = pub.public_numbers()
        cred_id = base64.urlsafe_b64decode(self.id + "=" * (-len(self.id) % 4))
        cose = cbor({1: 2, 3: -7, -1: 1, -2: nums.x.to_bytes(32, "big"), -3: nums.y.to_bytes(32, "big")})
        auth = (hashlib.sha256(rp_id.encode()).digest() + bytes([flags]) + b"\0\0\0\0" + bytes(16)
                + len(cred_id).to_bytes(2, "big") + cred_id + cose)
        return {"id": self.id, "clientDataJSON": b64u(self.client_data("webauthn.create", options["challenge"])),
                "publicKey": b64u(spki), "alg": -7, "name": "Test",
                "attestationObject": b64u(cbor({"fmt": "none", "attStmt": {}, "authData": auth}))}

    def assert_(self, options, flags=0x05, origin=None, rp_id="localhost"):
        cd = self.client_data("webauthn.get", options["challenge"], origin)
        self.counter = getattr(self, "counter", 0) + 1
        auth = hashlib.sha256(rp_id.encode()).digest() + bytes([flags]) + self.counter.to_bytes(4, "big")
        sig = self.key.sign(auth + hashlib.sha256(cd).digest(), ec.ECDSA(hashes.SHA256()))
        return {"id": self.id, "clientDataJSON": b64u(cd), "authenticatorData": b64u(auth), "signature": b64u(sig)}


def test_acces_protege(relay):
    p = phone(relay)
    assert call(p, "etat").status_code == 401
    assert call(p, "moi").json()["authed"] is False
    assert worker_call(relay, "w_poll", {"wait": 0}, key="mauvaise").status_code == 401
    # Anti-CSRF : un POST sans JSON est refusé
    assert p.post("/api.php", params={"r": "tache"}, data={"instruction": "x"}).status_code == 415
    # Fichiers sensibles : non servis par Apache en production (.htaccess) ; ici on vérifie au moins l'existence des règles
    assert "config\\.php" in (ROOT / "web" / ".htaccess").read_text()


def test_appairage_usage_unique(relay):
    url = worker_call(relay, "w_appairage", {}).json()["url"]
    assert url.startswith(relay["app"] + "#pair=")  # lien vers le sous-dossier
    token = url.split("#pair=")[1]
    p = phone(relay)
    assert call(p, "appairer", {"token": token}).status_code == 200
    assert call(p, "moi").json()["authed"] is True
    assert all(c.path == "/cockpit/" for c in p.cookies.jar)  # cookie limité au cockpit
    assert call(phone(relay), "appairer", {"token": token}).status_code == 401


def test_passkey(relay):
    p = paired_phone(relay)
    auth = Authenticator(relay["origin"])
    opts = call(p, "passkey_options_creation", {}).json()
    assert opts["rp"]["id"] == "localhost"
    # Attestation : sans vérification biométrique, autre rpId, clé annoncée différente de la clé attestée
    assert call(p, "passkey_creer", auth.register(opts, flags=0x41)).status_code == 401
    other = ec.generate_private_key(ec.SECP256R1())
    assert call(p, "passkey_creer", auth.register(call(p, "passkey_options_creation", {}).json(), key=other)).status_code == 401
    assert call(p, "passkey_creer", auth.register(call(p, "passkey_options_creation", {}).json(), rp_id="evil.example")).status_code == 401
    sans = auth.register(call(p, "passkey_options_creation", {}).json())
    del sans["attestationObject"]
    assert call(p, "passkey_creer", sans).status_code == 401
    assert call(p, "passkey_creer", auth.register(call(p, "passkey_options_creation", {}).json())).status_code == 200
    # Une passkey existante ne peut pas être remplacée
    assert call(p, "passkey_creer", auth.register(call(p, "passkey_options_creation", {}).json())).status_code == 409

    # Nouvel appareil / PWA installée sans cookie : connexion par passkey seule
    q = phone(relay)
    assert call(q, "moi").json() == {"authed": False, "has_passkey": True, "worker_online": call(q, "moi").json()["worker_online"]}
    opts = call(q, "passkey_options_connexion", {}).json()
    assert call(q, "passkey_connexion", auth.assert_(opts)).status_code == 200
    assert call(q, "etat").status_code == 200

    # Rejeu du même challenge, mauvaise origine, sans vérification biométrique, autre clé
    r = phone(relay)
    assert call(r, "passkey_connexion", auth.assert_(opts)).status_code == 401
    assert call(r, "passkey_connexion", auth.assert_(call(r, "passkey_options_connexion", {}).json(), origin="https://evil.example")).status_code == 401
    assert call(r, "passkey_connexion", auth.assert_(call(r, "passkey_options_connexion", {}).json(), flags=0x01)).status_code == 401
    assert call(r, "passkey_connexion", auth.assert_(call(r, "passkey_options_connexion", {}).json(), rp_id="evil.example")).status_code == 401
    pirate = Authenticator(relay["origin"])
    pirate.id = auth.id
    assert call(r, "passkey_connexion", pirate.assert_(call(r, "passkey_options_connexion", {}).json())).status_code == 401
    assert call(r, "etat").status_code == 401
    # Compteur de signatures qui recule : clé clonée
    auth.counter = 0
    assert call(r, "passkey_connexion", auth.assert_(call(r, "passkey_options_connexion", {}).json())).status_code == 401
    auth.counter = 10

    # Fermer les autres sessions : q est déconnecté, p reste connecté
    assert call(p, "sessions_fermer", {}).json()["closed"] >= 1
    assert call(q, "etat").status_code == 401
    assert call(p, "etat").status_code == 200
    s = phone(relay)
    assert call(s, "passkey_connexion", auth.assert_(call(s, "passkey_options_connexion", {}).json())).status_code == 200
    q = s

    # Les routes qui modifient quelque chose refusent le GET (un <img src> d'une autre page du domaine)
    assert call(q, "autopilote").status_code == 405
    assert call(q, "mode_hard").status_code == 405

    # Déconnexion
    assert call(q, "deconnexion", {}).status_code == 200
    assert call(q, "etat").status_code == 401


def test_vapid(relay):
    p = paired_phone(relay)
    key = b64u_dec(call(p, "vapid").json()["key"])
    assert len(key) == 65 and key[0] == 4  # point P-256 non compressé
    assert call(p, "push_abonner", {"endpoint": "pas une url"}).status_code == 400


def test_circuit_complet(relay, monkeypatch, tmp_path):
    """Téléphone -> relais -> worker -> validation sur le téléphone -> résultat."""
    monkeypatch.setenv("COCKPIT_DATA", str(tmp_path))
    from cockpit import worker as worker_mod
    from cockpit.relay import Relay

    monkeypatch.setattr(worker_mod, "DATA", tmp_path)
    w = worker_mod.Worker(Relay(relay["base"], KEY))
    w.relay.http.headers["Host"] = relay["origin"].removeprefix("http://")

    async def fake_run(task, project, resume=True):
        w.store.task_update(task, status="running", project=project.id)
        w.store.publish("message", {"task_id": task.id, "type": "tool_use", "tool": "Bash", "text": "helm upgrade x"})
        ok = await w.runner.request_approval(task, project, "Bash", {"command": "helm upgrade x"}, "opération sensible")
        w.store.audit("tool", task=task.id, tool="Bash", policy="ask", allowed=ok)
        w.store.task_update(task, status="done", result="Mise à jour effectuée." if ok else "Refusé.", cost_usd=0.01)

    monkeypatch.setattr(w.runner, "run", fake_run)
    p = paired_phone(relay)

    async def scenario():
        loop_task = asyncio.create_task(w.run())
        try:
            for _ in range(50):  # le worker publie ses projets au premier poll
                if call(p, "etat").json()["projects"]:
                    break
                await asyncio.sleep(0.1)
            s = call(p, "etat").json()
            assert s["worker_online"] and {x["id"] for x in s["projects"]} == {"cockpit", "onyxia"}

            t = call(p, "tache", {"instruction": "mets à jour mlflow", "project": "onyxia"}).json()
            approval = None
            for _ in range(100):
                await asyncio.sleep(0.1)
                evts = call(p, "etat").json()["events"]
                pending = [e["approval"] for e in evts if e["kind"] == "approval" and not e["approval"].get("decision")]
                if pending:
                    approval = pending[0]
                    break
            assert approval and approval["tool"] == "Bash" and approval["task_id"] == t["id"]
            assert call(p, "resume").json()["approvals"][0]["tool"] == "Bash"
            assert call(p, "decider", {"approval_id": approval["id"], "decision": "approve"}).status_code == 200
            assert call(p, "decider", {"approval_id": approval["id"], "decision": "deny"}).status_code == 409

            for _ in range(100):
                await asyncio.sleep(0.1)
                tasks = [e["task"] for e in call(p, "etat").json()["events"] if e["kind"] == "task" and e["task_id"] == t["id"]]
                if tasks and tasks[-1]["status"] == "done":
                    break
            assert tasks[-1]["status"] == "done" and tasks[-1]["result"] == "Mise à jour effectuée."
            for _ in range(30):
                audit = call(p, "audit").json()
                if any(a.get("event") == "approval" and a.get("decision") == "approve" for a in audit):
                    break
                await asyncio.sleep(0.1)
            assert any(a.get("event") == "approval" and a.get("decision") == "approve" for a in audit)

            # Incrémental : rien de nouveau depuis le dernier identifiant
            last = call(p, "etat").json()["last_id"]
            assert call(p, "etat", since=last).json()["events"] == []

            # Projet inconnu refusé ; annulation d'une tâche jamais prise en charge
            assert call(p, "tache", {"instruction": "x", "project": "nope"}).status_code == 400
        finally:
            loop_task.cancel()
            await w.relay.http.aclose()

    asyncio.run(scenario())


def test_redemarrage_et_annulation(relay):
    p = paired_phone(relay)
    W = {"instance": "test"}
    worker_call(relay, "w_poll", {**W, "wait": 0, "hello": True, "projects": [{"id": "onyxia", "name": "Onyxia"}]})
    # Annulée avant prise en charge : annulée directement par le relais
    t1 = call(p, "tache", {"instruction": "a", "project": "onyxia"}).json()
    call(p, "annuler", {"task_id": t1["id"]})
    # Prise en charge puis worker redémarré : marquée en erreur, validation en attente expirée
    t2 = call(p, "tache", {"instruction": "b", "project": "onyxia"}).json()
    got = worker_call(relay, "w_poll", {**W, "wait": 0}).json()
    assert [t["id"] for t in got["tasks"]] == [t2["id"]]
    worker_call(relay, "w_evenements", {"events": [{"kind": "approval", "task_id": t2["id"],
        "approval": {"id": "a1", "task_id": t2["id"], "tool": "Bash", "tool_input": {}, "reason": "r", "expires": 0}}]})
    # Un nouveau worker remplace l'ancien : l'ancien ne reçoit plus rien
    worker_call(relay, "w_poll", {"instance": "nouveau", "wait": 0, "hello": True})
    assert worker_call(relay, "w_poll", {**W, "wait": 0}).json() == {"superseded": True}
    evts = call(p, "etat").json()["events"]
    last = {}
    for e in evts:
        if e["kind"] == "task":
            last[e["task_id"]] = e["task"]["status"]
    assert last[t1["id"]] == "cancelled" and last[t2["id"]] == "error"
    assert [e["approval"].get("decision") for e in evts if e["kind"] == "approval" and e["approval"]["id"] == "a1"][-1] == "timeout"


def wpoll(relay, wid, orchestrateur, projects=None, hello=False, instance=None, **extra):
    body = {"worker_id": wid, "instance": instance or f"inst-{wid}", "orchestrateur": orchestrateur, "wait": 0,
            "hello": hello, "name": wid, **extra}
    if projects is not None:
        body["projects"] = projects
    return worker_call(relay, "w_poll", body).json()


def last_status(p):
    out = {}
    for e in call(p, "etat").json()["events"]:
        if e["kind"] == "task":
            out[e["task_id"]] = e["task"]
    return out


def test_multi_workers(relay):
    """Deux workers : affectation par projet, routage via l'orchestrateur, zombies et hors ligne par worker."""
    p = paired_phone(relay)
    wpoll(relay, "orch", True, [{"id": "mp-orch", "name": "Orch"}], hello=True)
    wpoll(relay, "pod-a", False, [{"id": "mp-a", "name": "Projet A"}], hello=True)

    s = call(p, "etat").json()
    by_id = {x["id"]: x for x in s["projects"]}
    assert by_id["mp-a"]["worker"] == "pod-a" and by_id["mp-a"]["online"] is True
    assert by_id["mp-orch"]["worker"] == "orch"
    # L'orchestrateur voit tous les projets, le pod de projet non
    assert {x["id"] for x in wpoll(relay, "orch", True)["all_projects"]} >= {"mp-a", "mp-orch"}
    assert wpoll(relay, "pod-a", False)["all_projects"] == []

    # Tâche avec projet : livrée au seul worker concerné
    ta = call(p, "tache", {"instruction": "pour A", "project": "mp-a"}).json()
    assert wpoll(relay, "orch", True)["tasks"] == []
    assert [t["id"] for t in wpoll(relay, "pod-a", False)["tasks"]] == [ta["id"]]

    # Tâche sans projet : à l'orchestrateur, qui la réaffecte au pod A
    tr = call(p, "tache", {"instruction": "route-moi"}).json()
    assert wpoll(relay, "pod-a", False)["tasks"] == []
    assert [t["id"] for t in wpoll(relay, "orch", True)["tasks"]] == [tr["id"]]
    assert worker_call(relay, "w_affecter", {"task_id": tr["id"], "project": "mp-inconnu"}).status_code == 400
    assert worker_call(relay, "w_affecter", {"task_id": tr["id"], "project": "mp-a"}).json()["worker"] == "pod-a"
    assert [t["id"] for t in wpoll(relay, "pod-a", False)["tasks"]] == [tr["id"]]
    # Événement tardif de l'orchestrateur sur la tâche réaffectée : ignoré
    worker_call(relay, "w_evenements", {"worker_id": "orch", "events": [
        {"kind": "task", "task_id": tr["id"], "task": {"status": "routing", "project": None}}]})
    assert last_status(p)[tr["id"]]["status"] == "pending"
    worker_call(relay, "w_evenements", {"worker_id": "pod-a", "events": [
        {"kind": "task", "task_id": tr["id"], "task": {"status": "running", "project": "mp-a"}}]})
    assert last_status(p)[tr["id"]]["status"] == "running"

    # Un redémarrage de l'orchestrateur ne touche pas aux tâches du pod A
    wpoll(relay, "orch", True, hello=True, instance="inst-orch-2")
    assert last_status(p)[tr["id"]]["status"] == "running"
    assert last_status(p)[ta["id"]]["status"] != "error"
    # Zombie par worker : l'ancienne instance de l'orchestrateur est remplacée, pas celle du pod A
    assert wpoll(relay, "orch", True) == {"superseded": True}
    assert "superseded" not in wpoll(relay, "pod-a", False)

    # Annulation routée vers le worker propriétaire
    call(p, "annuler", {"task_id": tr["id"]})
    assert wpoll(relay, "orch", True, instance="inst-orch-2")["cancels"] == []
    assert wpoll(relay, "pod-a", False)["cancels"] == [tr["id"]]


def test_worker_hors_ligne(relay):
    import sqlite3

    p = paired_phone(relay)
    wpoll(relay, "pod-off", False, [{"id": "mp-off", "name": "Hors ligne"}], hello=True)
    db = sqlite3.connect(next(Path(relay["docroot"]).rglob("cockpit.db")))
    db.execute("UPDATE workers SET vu_le = vu_le - 3600 WHERE id = 'pod-off'")
    db.commit()
    projects = {x["id"]: x for x in call(p, "etat").json()["projects"]}
    assert projects["mp-off"]["online"] is False  # grisé dans la PWA, mais toujours listé
    # Une tâche pour ce projet attend la reconnexion du pod
    t = call(p, "tache", {"instruction": "x", "project": "mp-off"}).json()
    assert wpoll(relay, "orch", True, instance="inst-orch-2")["tasks"] == []
    assert [x["id"] for x in wpoll(relay, "pod-off", False)["tasks"]] == [t["id"]]


def test_conversation(relay):
    """Une réponse continue la conversation : même projet, session Claude reprise, contexte transmis."""
    p = paired_phone(relay)
    wpoll(relay, "pod-c", False, [{"id": "mp-c", "name": "Conv"}], hello=True)
    t1 = call(p, "tache", {"instruction": "lance un service pour les drones", "project": "mp-c"}).json()
    assert t1["conversation"] == t1["id"] and t1["reply_to"] is None
    assert wpoll(relay, "pod-c", False)["tasks"][0]["context"] == []
    worker_call(relay, "w_evenements", {"worker_id": "pod-c", "events": [{"kind": "task", "task_id": t1["id"], "task": {
        "status": "done", "project": "mp-c", "result": "Quel dépôt git ?", "session_id": "sess-1"}}]})

    # Réponse sans projet choisi : hérite du projet de la conversation
    t2 = call(p, "tache", {"instruction": "aucun, crée un dossier vide", "reply_to": t1["id"]}).json()
    assert t2["conversation"] == t1["id"] and t2["reply_to"] == t1["id"] and t2["project"] == "mp-c"
    got = wpoll(relay, "pod-c", False)["tasks"]
    assert [t["id"] for t in got] == [t2["id"]]
    assert got[0]["resume_session"] == "sess-1" and got[0]["conversation"] == t1["id"]
    assert got[0]["context"] == [{"instruction": "lance un service pour les drones", "result": "Quel dépôt git ?", "project": "mp-c"}]

    # Nouvelle conversation : ni contexte ni session
    t3 = call(p, "tache", {"instruction": "autre chose", "project": "mp-c"}).json()
    got = wpoll(relay, "pod-c", False)["tasks"]
    assert got[0]["id"] == t3["id"] and got[0]["context"] == [] and got[0]["resume_session"] is None
    assert call(p, "tache", {"instruction": "x", "reply_to": "inexistante"}).status_code == 400


def test_tableau_de_bord_modifications_et_parallele(relay):
    """État des pods, résumé des modifications (diffs à la demande), fusion d'un worktree, tâches filles."""
    p = paired_phone(relay)
    state = {"resources": {"cpu": 0.42, "cpu_cores": 2, "mem_used": 3 * 1024**3, "mem_limit": 8 * 1024**3},
             "projects": {"tb": {"branch": "main", "dirty": 2}}, "running": 1, "max_agents": 3, "worktrees": []}
    wpoll(relay, "pod-tb", False, [{"id": "tb", "name": "Tableau"}], hello=True, state=state)
    w = {x["id"]: x for x in call(p, "etat").json()["workers"]}["pod-tb"]
    assert w["online"] and w["state"]["resources"]["cpu"] == 0.42 and w["projects"] == ["tb"]
    assert w["orchestrator"] is False and w["tasks_today"] == 0

    t = call(p, "tache", {"instruction": "ajoute une page", "project": "tb"}).json()
    assert [x["id"] for x in wpoll(relay, "pod-tb", False)["tasks"]] == [t["id"]]
    changes = {"files": [{"path": "web/page.html", "status": "A", "added": 12, "removed": 0}], "file_count": 1,
               "added": 12, "removed": 0, "commits": [{"sha": "abc1234", "subject": "Ajoute la page"}], "branch": "cockpit/x"}
    worktree = {"branch": "cockpit/x", "path": "/w/tb-x", "state": "active"}
    worker_call(relay, "w_evenements", {"worker_id": "pod-tb", "events": [
        {"kind": "message", "task_id": t["id"], "type": "changes", "changes": changes,
         "patches": {"web/page.html": "@@ -0,0 +1 @@\n+<h1>Page</h1>"}},
        {"kind": "task", "task_id": t["id"], "task": {"status": "done", "project": "tb", "result": "**Fait** — page",
                                                      "cost_usd": 0.12, "changes": changes, "worktree": worktree}}]})
    evts = call(p, "etat").json()["events"]
    ch = [e for e in evts if e.get("type") == "changes"][-1]
    assert "patches" not in ch and ch["has_patches"] is True  # le fil reste léger
    task = last_status(p)[t["id"]]
    assert task["changes"]["added"] == 12 and task["worktree"]["state"] == "active" and task["worker"] == "pod-tb"
    assert call(p, "diff", task=t["id"]).json()["patches"]["web/page.html"].endswith("<h1>Page</h1>")
    fini = {x["id"]: x for x in call(p, "resume").json()["finished"]}
    assert fini[t["id"]]["changes"]["file_count"] == 1  # notification de fin
    assert {x["id"]: x for x in call(p, "etat").json()["workers"]}["pod-tb"]["cost_today"] == 0.12

    # Fusion demandée par le téléphone -> livrée au worker -> état conservé sur la tâche
    assert call(p, "worktree", {"task_id": t["id"], "action": "rm -rf"}).status_code == 400
    assert call(p, "worktree", {"task_id": t["id"], "action": "merge"}).json()["ok"]
    acts = wpoll(relay, "pod-tb", False)["worktree_actions"]
    assert acts == [{"task_id": t["id"], "project": "tb", "conversation": t["id"], "action": "merge"}]
    assert wpoll(relay, "pod-tb", False)["worktree_actions"] == []  # livrée une seule fois
    worker_call(relay, "w_evenements", {"worker_id": "pod-tb", "events": [
        {"kind": "message", "task_id": t["id"], "type": "worktree", "state": "merged", "ok": True, "text": "Fusionnée."}]})
    assert last_status(p)[t["id"]]["worktree"]["state"] == "merged"

    # Tâches filles (lancer_en_parallele) : une conversation chacune, livrées au worker du projet
    assert worker_call(relay, "w_taches", {"parent": t["id"], "tasks": [{"instruction": "x", "project": "inconnu"}]}).status_code == 400
    assert worker_call(relay, "w_taches", {"parent": t["id"], "tasks": [{"instruction": "x", "project": "tb"}] * 6}).status_code == 400
    ids = worker_call(relay, "w_taches", {"parent": t["id"], "tasks": [
        {"instruction": "module A", "project": "tb"}, {"instruction": "module B", "project": "tb"}]}).json()["ids"]
    livrees = wpoll(relay, "pod-tb", False)["tasks"]
    assert [x["id"] for x in livrees] == ids and all(x["parent"] == t["id"] and x["conversation"] == x["id"] for x in livrees)
    assert last_status(p)[ids[0]]["parent"] == t["id"]


def test_nouveau_pod_et_bilan(relay):
    """Dépôts publiés par l'orchestrateur, lancement d'un pod depuis le téléphone, bilan par projet."""
    p = paired_phone(relay)
    depots = [{"name": "th-e-o/drones", "url": "https://github.com/th-e-o/drones", "private": True, "branch": "main",
               "description": "Vols", "pushed": "2026-10-12T10:00:00Z", "archived": False}]
    assert worker_call(relay, "w_depots", {"depots": depots}).json()["ok"]
    wpoll(relay, "orch-np", True, [{"id": "np-onyxia", "name": "Onyxia"}], hello=True)
    r = call(p, "depots").json()
    assert r["depots"] == depots and r["orchestrateur"] is True

    for bad in [{"nom": "Pas Bon"}, {"nom": "x", "depot_git": "http://github.com/a/b"}, {"nom": "x", "depot_git": "https://u:p@h/a"},
                {"nom": "np-onyxia"}, {"nom": "x", "branche": "a b"}]:
        assert call(p, "lancer_pod", bad).status_code == 400, bad
    t = call(p, "lancer_pod", {"nom": "drones", "depot_git": "https://github.com/th-e-o/drones", "branche": "dev", "cpu": "4"}).json()
    assert t["kind"] == "service" and t["instruction"] == "🚀 Nouveau pod « drones » : th-e-o/drones (dev)"
    livrees = [x for x in wpoll(relay, "orch-np", True)["tasks"] if x["id"] == t["id"]]
    assert livrees[0]["action"] == {"type": "lancer_service", "nom": "drones", "depot_git": "https://github.com/th-e-o/drones",
                                    "branche": "dev", "cpu": "4", "memoire": "8Gi", "disque": "10Gi", "gpu": False,
                                    "apercu": False}

    # Bilan : tâches, durée, fichiers distincts, lignes, commits, activité du jour
    t1 = call(p, "tache", {"instruction": "un", "project": "np-onyxia"}).json()["id"]
    t2 = call(p, "tache", {"instruction": "deux", "project": "np-onyxia"}).json()["id"]
    ch = lambda files, a, c: {"files": [{"path": f, "status": "M", "added": a, "removed": 1} for f in files],
                              "file_count": len(files), "added": a * len(files), "removed": len(files), "commits": [{"sha": "x", "subject": "s"}] * c}
    worker_call(relay, "w_evenements", {"worker_id": "orch-np", "events": [
        {"kind": "task", "task_id": t1, "task": {"status": "done", "project": "np-onyxia", "duration_s": 120, "changes": ch(["a.py", "b.py"], 3, 1)}},
        {"kind": "task", "task_id": t2, "task": {"status": "error", "project": "np-onyxia", "duration_s": 30, "changes": ch(["a.py"], 5, 2)}}]})
    b = {x["project"]: x for x in call(p, "bilan", jours=7).json()["projects"]}["np-onyxia"]
    assert (b["tasks"], b["done"], b["error"], b["duration_s"]) == (2, 1, 1, 150)
    assert (b["file_count"], b["added"], b["removed"], b["commits"]) == (2, 11, 3, 3)
    assert b["top_files"][0] == {"path": "a.py", "count": 2} and b["days"][-1] == 2 and len(b["days"]) == 7
    assert [x["id"] for x in b["recent"]] == [t2, t1]
    assert len(call(p, "bilan", jours=30).json()["projects"][0]["days"]) == 30
    assert call(p, "bilan", jours=99).json()["days"] == 7


def test_nouveau_pod_apercu(relay):
    """Case « Aperçu » du formulaire « + Nouveau pod » : transmise en booléen à l'action lancer_service, false si absente."""
    p = paired_phone(relay)
    wpoll(relay, "orch-ap", True, [{"id": "ap-onyxia", "name": "Onyxia"}], hello=True)
    for nom, extra, attendu in [("ap-vrai", {"apercu": True}, True), ("ap-faux", {"apercu": False}, False),
                                ("ap-absent", {}, False), ("ap-texte", {"apercu": "false"}, False)]:
        t = call(p, "lancer_pod", {"nom": nom, **extra}).json()
        wid = last_status(p)[t["id"]]["worker"]
        livrees = wpoll(relay, wid, True, instance="inst-orch-2" if wid == "orch" else None)["tasks"]
        act = [x for x in livrees if x["id"] == t["id"]][0]["action"]
        assert act["apercu"] is attendu, (nom, act)


def test_suite_et_notifications(relay):
    """Prochaines étapes conservées et ignorables, contenu des notifications, nouveau pod avec nouveau dépôt."""
    p = paired_phone(relay)
    wpoll(relay, "orch-ns", True, [{"id": "ns-p", "name": "P"}], hello=True)
    t = call(p, "tache", {"instruction": "ajoute l'export CSV", "project": "ns-p"}).json()
    steps = [{"titre": "Lancer les tests", "instruction": "Lance pytest"}, {"titre": "Publier", "instruction": "Pousse la branche"}]
    worker_call(relay, "w_evenements", {"worker_id": "orch-ns", "events": [
        {"kind": "approval", "task_id": t["id"], "approval": {"id": "ap-ns", "task_id": t["id"], "tool": "Bash",
         "tool_input": {"command": "rm -rf build"}, "reason": "opération sensible : rm", "expires": time.time() + 60}},
        {"kind": "task", "task_id": t["id"], "task": {"status": "done", "project": "ns-p", "result": "ok", "next_steps": steps}}]})
    task = last_status(p)[t["id"]]
    assert task["next_steps"] == steps and task["next_steps_dismissed"] is False
    r = call(p, "resume").json()
    ap = [a for a in r["approvals"] if a["id"] == "ap-ns"][0]
    assert ap["preview"] == "rm -rf build" and ap["task_id"] == t["id"]
    assert [x for x in r["finished"] if x["id"] == t["id"]][0]["next_steps"] == steps

    # La notification lance l'étape : une suite de la conversation, sur le même projet
    n = call(p, "tache", {"instruction": steps[0]["instruction"], "reply_to": t["id"]}).json()
    assert n["reply_to"] == t["id"] and n["project"] == "ns-p" and n["conversation"] == t["conversation"]
    assert call(p, "ignorer_suite", {"task_id": t["id"]}).json()["ok"]
    assert last_status(p)[t["id"]]["next_steps_dismissed"] is True

    # Nouveau pod de projet : signalé une fois connecté (orchestrateurs exclus)
    wpoll(relay, "pod-ns-neuf", False, [{"id": "ns-neuf", "name": "neuf"}], hello=True)
    pods = call(p, "resume").json()["new_pods"]
    assert "pod-ns-neuf" in [x["id"] for x in pods] and "orch-ns" not in [x["id"] for x in pods]

    # Nouveau pod sur un nouveau dépôt GitHub
    assert call(p, "lancer_pod", {"nom": "drones", "creer_depot": {"nom": "a b"}}).status_code == 400
    t2 = call(p, "lancer_pod", {"nom": "drones", "depot_git": "https://ignoré", "creer_depot": {"nom": "Drones.vols", "prive": False}}).json()
    assert t2["instruction"] == "🚀 Nouveau pod « drones » : nouveau dépôt GitHub public « Drones.vols »"
    wid = last_status(p)[t2["id"]]["worker"]  # orchestrateur en ligne le plus récent (d'autres tests en ont lancé)
    livrees = wpoll(relay, wid, True, instance="inst-orch-2" if wid == "orch" else None)["tasks"]
    act = [x for x in livrees if x["id"] == t2["id"]][0]["action"]
    assert act["creer_depot"] == {"nom": "Drones.vols", "prive": False} and act["depot_git"] == ""


def test_cle_d_appairage(relay):
    """Clé à saisir sur un ordinateur : usage unique, liée au lien QR, saisie tolérante, échecs comptés."""
    import re

    r = worker_call(relay, "w_appairage", {}).json()
    assert re.fullmatch(r"[0-9A-HJKMNP-TV-Z]{5}-[0-9A-HJKMNP-TV-Z]{5}", r["code"])
    pc = phone(relay)
    assert call(pc, "appairer", {"code": "ABC"}).status_code == 401  # format invalide
    assert call(pc, "appairer", {"code": "00000-00000"}).status_code == 401  # inconnue
    # Saisie en minuscules, sans tiret, avec O / I à la place de 0 / 1
    saisie = r["code"].replace("-", " ").lower().replace("0", "o").replace("1", "i")
    assert call(pc, "appairer", {"code": saisie}).json()["ok"]
    assert call(pc, "moi").json()["authed"] is True
    assert call(phone(relay), "appairer", {"code": r["code"]}).status_code == 401  # usage unique
    # Le lien QR de la même génération est invalidé lui aussi
    assert call(phone(relay), "appairer", {"token": r["url"].split("#pair=")[1]}).status_code == 401


def test_vibe_coding(relay):
    """Modèle, variantes, recadrage, captures, mémoire, commandes rapides, garder une variante, CI en échec."""
    import base64

    p = paired_phone(relay)
    wpoll(relay, "pod-vc", False, [{"id": "vc", "name": "VC"}], hello=True)

    # Modèle : choisi puis hérité par la suite de la conversation
    assert call(p, "tache", {"instruction": "x", "project": "vc", "model": "énorme"}).status_code == 400
    t = call(p, "tache", {"instruction": "page d'accueil", "project": "vc", "model": "rapide"}).json()
    suite = call(p, "tache", {"instruction": "et le pied de page", "reply_to": t["id"]}).json()
    assert t["model"] == suite["model"] == "rapide"
    livrees = {x["id"]: x for x in wpoll(relay, "pod-vc", False)["tasks"]}
    assert livrees[t["id"]]["model"] == "rapide" and livrees[t["id"]]["isolate"] is False

    # Recadrage : seulement pendant la tâche, affiché dans le fil, livré une fois au worker
    assert call(p, "recadrer", {"task_id": t["id"], "message": "plutôt en bleu"}).json()["ok"]
    evts = call(p, "etat").json()["events"]
    assert any(e.get("type") == "steer" and e["text"] == "plutôt en bleu" for e in evts)
    assert wpoll(relay, "pod-vc", False)["steers"] == [{"task_id": t["id"], "text": "plutôt en bleu"}]
    assert wpoll(relay, "pod-vc", False)["steers"] == []
    worker_call(relay, "w_evenements", {"worker_id": "pod-vc", "events": [
        {"kind": "task", "task_id": t["id"], "task": {"status": "done", "project": "vc", "tests": {"ok": False, "command": "pytest", "duration_s": 3}}}]})
    assert call(p, "recadrer", {"task_id": t["id"], "message": "trop tard"}).status_code == 409
    assert last_status(p)[t["id"]]["tests"]["ok"] is False

    # Captures : envoyées par le worker propriétaire, JPEG uniquement, servies au téléphone connecté
    jpeg = b"\xff\xd8\xff\xe0" + b"0" * 100
    img = {"task_id": t["id"], "data": base64.b64encode(jpeg).decode(), "caption": "Accueil", "worker_id": "pod-vc"}
    assert worker_call(relay, "w_image", {**img, "worker_id": "autre"}).status_code == 403
    assert worker_call(relay, "w_image", {**img, "data": base64.b64encode(b"<svg/>").decode()}).status_code == 400
    iid = worker_call(relay, "w_image", img).json()["id"]
    r = call(p, "image", id=iid)
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg" and r.content == jpeg
    assert call(phone(relay), "image", id=iid).status_code == 401
    assert call(p, "image", id="../cockpit").status_code == 404
    assert any(e.get("type") == "image" and e["image"] == iid for e in call(p, "etat").json()["events"])

    # Mémoire (CLAUDE.md) : publiée par le worker, modifiée sur le téléphone, livrée au worker
    assert call(p, "memoire", project="vc").json()["absent"] is True
    worker_call(relay, "w_memoire", {"project": "vc", "content": "# Règles", "hash": "h1", "worker_id": "pod-vc"})
    m = call(p, "memoire", project="vc").json()
    assert m["content"] == "# Règles" and m["hash"] == "h1"
    assert call(p, "memoire_maj", {"project": "vc", "content": "x", "hash": "ancien"}).status_code == 409
    assert call(p, "memoire_maj", {"project": "vc", "content": "# Règles\n- pytest", "hash": "h1"}).json()["ok"]
    assert wpoll(relay, "pod-vc", False)["memory_edits"][0]["content"] == "# Règles\n- pytest"
    assert wpoll(relay, "pod-vc", False)["memory_edits"] == []

    # Commandes rapides
    assert call(p, "commandes_maj", {"project": "vc", "commandes": [{"titre": "", "instruction": "x"}]}).status_code == 400
    assert call(p, "commandes_maj", {"project": "vc", "commandes": [{"titre": "🧪 Tests", "instruction": "Lance pytest"}]}).json()["ok"]
    assert call(p, "commandes").json()["vc"] == [{"titre": "🧪 Tests", "instruction": "Lance pytest"}]

    # Variantes : une conversation chacune, isolées, regroupées ; on en garde une, les autres sont jetées
    assert call(p, "tache", {"instruction": "x", "project": "vc", "variants": 4}).status_code == 400
    assert call(p, "tache", {"instruction": "x", "reply_to": t["id"], "variants": 2}).status_code == 400
    v = call(p, "tache", {"instruction": "logo", "project": "vc", "variants": 2, "model": "approfondi"}).json()
    ids = v["variants"]
    assert len(ids) == 2 and ids[0] == v["id"]
    livrees = {x["id"]: x for x in wpoll(relay, "pod-vc", False)["tasks"]}
    assert all(livrees[i]["isolate"] and livrees[i]["group"] == ids[0] and livrees[i]["model"] == "approfondi" for i in ids)
    assert "Variante 2/2" in livrees[ids[1]]["instruction"] and livrees[ids[1]]["conversation"] == ids[1]
    assert call(p, "garder_variante", {"task_id": ids[1]}).status_code == 409  # encore en cours
    worker_call(relay, "w_evenements", {"worker_id": "pod-vc", "events": [
        {"kind": "task", "task_id": i, "task": {"status": "done", "project": "vc",
         "worktree": {"branch": f"cockpit/{i}", "path": "/w", "state": "active"}}} for i in ids]})
    assert call(p, "garder_variante", {"task_id": ids[1]}).json()["ok"]
    actions = {a["task_id"]: a["action"] for a in wpoll(relay, "pod-vc", False)["worktree_actions"]}
    assert actions == {ids[1]: "merge", ids[0]: "discard"}

    # CI en échec : notification avec les vérifications en échec
    worker_call(relay, "w_evenements", {"worker_id": "pod-vc", "events": [{"kind": "message", "task_id": t["id"], "type": "ci",
        "state": "failure", "sha": "abc1234", "url": "https://gh/run", "checks": [
            {"name": "tests", "conclusion": "failure"}, {"name": "lint", "conclusion": "success"}]}]})
    fail = [c for c in call(p, "resume").json()["ci_failures"] if c["task_id"] == t["id"]][0]
    assert fail["checks"] == ["tests"] and fail["sha"] == "abc1234"


def test_etat_ne_renvoie_que_les_blocs_modifies(relay):
    """Le téléphone renvoie les empreintes reçues : seuls les blocs du tableau de bord qui ont changé repartent."""
    p = paired_phone(relay)
    wpoll(relay, "pod-h", False, [{"id": "eh", "name": "EH"}], hello=True)
    s1 = call(p, "etat").json()
    assert {"projects", "workers", "memory", "hard"} <= s1.keys() and len(s1["h"].split(".")) == 7
    s2 = call(p, "etat", since=s1["last_id"], h=s1["h"]).json()
    assert not {"projects", "memory", "autopilot", "hard", "week", "deleted_projects"} & s2.keys()
    assert {"events", "last_id", "worker_online", "pending", "h"} <= s2.keys()
    # Un nouveau projet renvoie les blocs qui le listent (projets, pods, mémoire), pas les autres
    wpoll(relay, "pod-h2", False, [{"id": "eh2", "name": "EH2"}], hello=True)
    s3 = call(p, "etat", since=s2["last_id"], h=s2["h"]).json()
    assert "eh2" in {x["id"] for x in s3["projects"]}
    assert not {"autopilot", "hard", "deleted_projects"} & s3.keys()
    # Empreintes inconnues ou absentes : tout est renvoyé
    assert "memory" in call(p, "etat", h="x.y").json()


def test_schema_migre_une_seule_fois(relay):
    """La version du schéma est enregistrée dans la base : les requêtes suivantes ne refont pas les migrations."""
    import sqlite3
    call(paired_phone(relay), "etat")
    db = sqlite3.connect(Path(relay["docroot"]) / "cockpit" / "data" / "cockpit.db")
    assert db.execute("PRAGMA user_version").fetchone()[0] >= 2
    assert not db.execute("SELECT 1 FROM sqlite_master WHERE name = 'confiances'").fetchone()
    assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert "modele_auto" in {r[1] for r in db.execute("PRAGMA table_info(taches)")}


def test_modele_choisi_au_routage_transmis_au_pod(relay):
    """L'orchestrateur choisit le modèle en routant : le pod du projet le reçoit avec la tâche, sans le refaire."""
    p = paired_phone(relay)
    wpoll(relay, "orch-m", True, [{"id": "mo-orch", "name": "Orch"}], hello=True)
    wpoll(relay, "pod-m", False, [{"id": "mo-a", "name": "A"}], hello=True)
    t = call(p, "tache", {"instruction": "route-moi"}).json()
    assert [x["id"] for x in wpoll(relay, "orch-m", True)["tasks"]] == [t["id"]]
    hint = {"choice": "rapide", "reason": "simple lecture"}
    assert worker_call(relay, "w_affecter", {"task_id": t["id"], "project": "mo-a", "model_hint": hint}).status_code == 200
    [livree] = wpoll(relay, "pod-m", False)["tasks"]
    assert livree["model_hint"] == hint and livree["model"] is None
    # Indice invalide : ignoré
    t2 = call(p, "tache", {"instruction": "route-moi aussi"}).json()
    wpoll(relay, "orch-m", True)
    worker_call(relay, "w_affecter", {"task_id": t2["id"], "project": "mo-a", "model_hint": {"choice": "max"}})
    assert wpoll(relay, "pod-m", False)["tasks"][0]["model_hint"] is None


def test_consigne_pour_un_pod_qui_demarre(relay):
    """confier_au_projet : la tâche attend le worker du nouveau pod, puis lui est livrée (jamais à l'orchestrateur)."""
    p = paired_phone(relay)
    wpoll(relay, "orch-df", True, [{"id": "df-onyxia", "name": "Onyxia"}], hello=True)
    parent = call(p, "tache", {"instruction": "reprends mon code sur les fonctionnaires", "project": "df-onyxia"}).json()
    body = {"parent": parent["id"], "tasks": [{"instruction": "Analyse le code", "project": "df-etp"}], "worker_id": "orch-df"}
    assert worker_call(relay, "w_taches", body).status_code == 400  # projet inconnu sans « defer »
    assert worker_call(relay, "w_taches", {**body, "tasks": [{"instruction": "x", "project": "Pas Bon"}], "defer": True}).status_code == 400
    tid = worker_call(relay, "w_taches", {**body, "defer": True}).json()["ids"][0]
    t = last_status(p)[tid]
    assert t["waiting_project"] is True and t["status"] == "pending" and t["parent"] == parent["id"]
    assert tid not in [x["id"] for x in wpoll(relay, "orch-df", True)["tasks"]]  # l'orchestrateur ne la prend pas
    premier = wpoll(relay, "pod-df-etp", False, [{"id": "df-etp", "name": "df-etp"}], hello=True)  # le pod est prêt
    assert [x["id"] for x in premier["tasks"]] == [tid]  # livrée dès sa première connexion
    assert last_status(p)[tid]["waiting_project"] is False


def test_consigne_vers_l_orchestrateur(relay):
    """Un pod de projet confie une demande hors projet (nouveau pod…) à l'orchestrateur : tâche sans projet, routée."""
    p = paired_phone(relay)
    wpoll(relay, "orch-vo", True, [{"id": "vo-onyxia", "name": "Onyxia"}], hello=True)
    wpoll(relay, "pod-vo", False, [{"id": "vo-etp", "name": "etp"}], hello=True)
    parent = call(p, "tache", {"instruction": "nouveau projet anki", "project": "vo-etp"}).json()
    tid = worker_call(relay, "w_taches", {"parent": parent["id"], "worker_id": "pod-vo", "defer": True,
                                          "tasks": [{"instruction": "Crée un pod pour le bot anki", "project": "orchestrateur"}]}).json()["ids"][0]
    t = last_status(p)[tid]
    assert t["project"] is None and t["waiting_project"] is False and t["parent"] == parent["id"]
    livree = [x for x in wpoll(relay, "orch-vo", True)["tasks"] if x["id"] == tid]
    assert livree and livree[0]["project"] is None  # l'orchestrateur la route


def test_etat_d_un_projet_pour_les_agents(relay):
    """etat_projet : tâches, résultat, modifications, PR, CI et question d'un projet, vus du relais."""
    p = paired_phone(relay)
    wpoll(relay, "pod-et", False, [{"id": "et-russe", "name": "russe"}], hello=True)
    t = call(p, "tache", {"instruction": "prépare le déploiement", "project": "et-russe"}).json()
    worker_call(relay, "w_evenements", {"worker_id": "pod-et", "events": [
        {"kind": "message", "task_id": t["id"], "type": "pr", "url": "https://github.com/a/b/pull/3", "title": "Déploiement", "number": 3},
        {"kind": "message", "task_id": t["id"], "type": "ci", "state": "success", "sha": "abc"},
        {"kind": "task", "task_id": t["id"], "task": {"status": "done", "project": "et-russe", "result": "Prêt.",
         "changes": {"file_count": 3, "added": 40, "removed": 2, "commits": [{"sha": "a", "subject": "Déploiement OVH"}],
                     "branch": "feat/deploy", "upstream": "origin/feat/deploy", "ahead": 0},
         "question": {"text": "Quelle offre OVH ?", "options": []}}}]})
    r = worker_call(relay, "w_projet_etat", {"project": "et-russe"}).json()
    assert r["known"] and r["online"] and r["pod"] == "pod-et"
    x = r["tasks"][0]
    assert x["status"] == "done" and x["changes"]["files"] == 3 and x["changes"]["pushed"] is True
    assert x["pull_requests"] == [{"url": "https://github.com/a/b/pull/3", "title": "Déploiement"}] and x["ci"] == "success"
    assert x["question"] == "Quelle offre OVH ?"
    assert "et-russe" in [p["id"] for p in worker_call(relay, "w_projet_etat", {}).json()["projects"]]
    assert worker_call(relay, "w_projet_etat", {"project": "inconnu"}).json()["known"] is False


def test_regles_d_autorisation(relay):
    p = paired_phone(relay)
    for bad in [{"tool": "Bash", "pattern": "", "label": "tout"}, {"tool": "x y", "pattern": "a", "label": "l"},
                {"tool": "Bash", "pattern": "(", "label": "l"}, {"tool": "files", "pattern": "", "label": "l"}]:
        assert call(p, "regle_ajouter", bad).status_code == 400, bad
    assert call(p, "regle_ajouter", {"tool": "Bash", "pattern": r"^docker\s+build\b", "label": "les commandes « docker build … »",
                                     "example": "docker build -t x ."}).json()["ok"]
    call(p, "regle_ajouter", {"tool": "Bash", "pattern": r"^docker\s+build\b", "label": "doublon"})  # ignoré
    rules = call(p, "regles").json()
    assert len([r for r in rules if r["pattern"] == r"^docker\s+build\b"]) == 1
    wpoll(relay, "pod-rg", False, [{"id": "rg", "name": "rg"}], hello=True)
    assert {"tool": "Bash", "pattern": r"^docker\s+build\b"} in wpoll(relay, "pod-rg", False)["rules"]
    call(p, "regle_supprimer", {"id": rules[0]["id"]})
    assert {"tool": "Bash", "pattern": r"^docker\s+build\b"} not in wpoll(relay, "pod-rg", False)["rules"]


def test_memoire_propositions_et_full_auto(relay_neuf):
    p = paired_phone(relay_neuf)
    wpoll(relay_neuf, "pod-fa", True, [{"id": "fa", "name": "Full auto"}], hello=True)
    # Une tâche terminée met le projet en mémoire et ses prochaines étapes deviennent des propositions
    t = call(p, "tache", {"instruction": "premier travail", "project": "fa"}).json()
    got = wpoll(relay_neuf, "pod-fa", True)
    assert [x["id"] for x in got["tasks"]] == [t["id"]]
    steps = [{"titre": "Ajouter des tests", "instruction": "Ajoute des tests."}, {"titre": "Documenter", "instruction": "Documente."}]
    worker_call(relay_neuf, "w_evenements", {"worker_id": "pod-fa", "events": [
        {"kind": "task", "task_id": t["id"], "task": {"status": "done", "project": "fa", "result": "Fait.", "next_steps": steps}}]})
    mem = call(p, "etat").json()["memory"]
    assert [x["id"] for x in mem["projects"]] == ["fa"] and mem["projects"][0]["status"] == "en_cours"
    assert {x["titre"] for x in mem["proposals"]} == {"Ajouter des tests", "Documenter"}
    # Ignorer, ajouter à la main (doublon refusé), lancer
    ids = {x["titre"]: x["id"] for x in mem["proposals"]}
    assert call(p, "proposition", {"action": "ignorer", "id": ids["Documenter"]}).json()["ok"]
    assert call(p, "proposition", {"action": "ajouter", "titre": "Idée", "instruction": "Fais ceci.", "project": "fa"}).json()["ok"]
    assert call(p, "proposition", {"action": "ajouter", "titre": "Idée", "instruction": "Fais ceci.", "project": "fa"}).status_code == 400
    lance = call(p, "proposition", {"action": "lancer", "id": ids["Ajouter des tests"]}).json()
    assert call(p, "proposition", {"action": "lancer", "id": ids["Ajouter des tests"]}).status_code == 404  # déjà traitée
    assert [x["id"] for x in wpoll(relay_neuf, "pod-fa", True)["tasks"]] == [lance["task_id"]]
    # Full auto : refusé tant qu'il n'est pas armé, puis une seule tâche automatique à la fois
    assert worker_call(relay_neuf, "w_auto_tache", {}).json() == {"skipped": "désarmé"}
    assert call(p, "autopilote", {"enabled": True, "days": 3, "max_per_day": 5}).json()["enabled"]
    assert wpoll(relay_neuf, "pod-fa", True)["autopilot"]["max_per_day"] == 5
    auto = worker_call(relay_neuf, "w_auto_tache", {}).json()
    assert auto["titre"] == "Idée"
    assert "déjà en cours" in worker_call(relay_neuf, "w_auto_tache", {}).json()["skipped"]
    assert last_status(p)[auto["task_id"]]["auto"] is True
    # Trois erreurs de suite désarment le mode
    for i in range(3):
        if i:
            worker_call(relay_neuf, "w_memoire_projets", {"proposals": {"fa": [{"titre": f"Idée {i}", "instruction": "Encore."}]}})
            auto = worker_call(relay_neuf, "w_auto_tache", {}).json()
        worker_call(relay_neuf, "w_evenements", {"worker_id": "pod-fa", "events": [
            {"kind": "task", "task_id": auto["task_id"], "task": {"status": "error", "project": "fa", "result": "raté"}}]})
    assert call(p, "etat").json()["autopilot"]["enabled"] is False
    assert any("Full auto" in a["titre"] for a in call(p, "resume").json()["alerts"])


def test_proposition_par_cle_api(relay_neuf):
    p = paired_phone(relay_neuf)
    idee = {"titre": "POC Telegram", "instruction": "Code ceci.", "source": "telegram"}
    # Sans clé, avec une mauvaise clé, ou avec la clé du worker : refusé
    assert httpx.post(relay_neuf["base"] + "/api.php", params={"r": "proposition_api"}, json=idee).status_code == 401
    assert worker_call(relay_neuf, "proposition_api", idee, key="mauvaise").status_code == 401
    assert worker_call(relay_neuf, "proposition_api", idee).status_code == 401
    # La clé d'API ne donne accès à rien d'autre
    assert worker_call(relay_neuf, "w_poll", {"wait": 0}, key=CLE_API).status_code == 401
    r = worker_call(relay_neuf, "proposition_api", idee, key=CLE_API)
    assert r.status_code == 200 and r.json()["ok"]
    mem = call(p, "etat").json()["memory"]
    assert [(x["id"], x["titre"], x["source"], x["project"]) for x in mem["proposals"]] == [(r.json()["id"], "POC Telegram", "telegram", None)]
    # Doublon et projet inconnu refusés ; rien n'est lancé
    assert worker_call(relay_neuf, "proposition_api", idee, key=CLE_API).status_code == 400
    assert worker_call(relay_neuf, "proposition_api", {**idee, "titre": "Autre", "project": "nope"}, key=CLE_API).status_code == 400
    assert last_status(p) == {}


def test_alerte_reinitialisation_des_tokens(relay):
    p = paired_phone(relay)
    assert worker_call(relay, "w_alerte", {"kind": "inconnu"}).status_code == 400
    assert worker_call(relay, "w_alerte", {"kind": "five_hour"}).json()["ok"]
    worker_call(relay, "w_alerte", {"kind": "five_hour"})  # un autre pod signale la même réinitialisation : une seule alerte
    alerts = [a for a in call(p, "resume").json()["alerts"] if "Tokens" in a["titre"]]
    assert len(alerts) == 1 and "5 h" in alerts[0]["corps"]


def test_mode_hard(relay):
    p = paired_phone(relay)
    wpoll(relay, "pod-hd", True, [{"id": "hd", "name": "Hard"}], hello=True)
    assert wpoll(relay, "pod-hd", True)["hard"] is False
    assert call(p, "mode_hard", {"enabled": True, "hours": 2}).json()["enabled"]
    assert wpoll(relay, "pod-hd", True)["hard"] is True and call(p, "etat").json()["hard"]["enabled"]
    assert call(p, "mode_hard", {"enabled": False}).json()["enabled"] is False
    assert wpoll(relay, "pod-hd", True)["hard"] is False


def test_mode_hard_pleine_autonomie(relay_neuf):
    """Mode hard : full auto armé sur plusieurs projets, questions de Claude tranchées automatiquement, compte rendu."""
    p = paired_phone(relay_neuf)
    for wid, proj in (("pod-h1", "h1"), ("pod-h2", "h2"), ("pod-h3", "h3")):
        wpoll(relay_neuf, wid, wid == "pod-h1", [{"id": proj, "name": proj}], hello=True)
    t = call(p, "tache", {"instruction": "travail initial", "project": "h1"}).json()
    wpoll(relay_neuf, "pod-h1", True)
    state = call(p, "mode_hard", {"enabled": True, "hours": 4, "max_parallel": 2}).json()
    assert state["enabled"]
    auto = call(p, "etat").json()["autopilot"]
    assert auto["enabled"] and auto["max_parallel"] == 2 and auto["max_per_day"] == 40
    # Deux projets de front, pas davantage ; un projet à la fois
    for proj in ("h1", "h2", "h3"):
        call(p, "proposition", {"action": "ajouter", "titre": f"Idée {proj}", "instruction": f"Avance sur {proj}.", "project": proj})
        call(p, "proposition", {"action": "ajouter", "titre": f"Autre {proj}", "instruction": f"Autre chose sur {proj}.", "project": proj})
    first = worker_call(relay_neuf, "w_auto_tache", {}).json()
    second = worker_call(relay_neuf, "w_auto_tache", {}).json()
    assert first["task_id"] and second["task_id"] and first["titre"] != second["titre"]
    assert {first["titre"][-2:], second["titre"][-2:]} == {"h1", "h2"}  # deux projets différents
    assert "déjà en cours" in worker_call(relay_neuf, "w_auto_tache", {}).json()["skipped"]
    # Une question posée par Claude reçoit une réponse automatique dans la même conversation
    worker_call(relay_neuf, "w_evenements", {"worker_id": "pod-h1", "events": [
        {"kind": "task", "task_id": t["id"], "task": {"status": "done", "project": "h1", "result": "Quel format ?",
                                                       "question": {"text": "Quel format ?", "options": ["CSV", "JSON"]}}}]})
    reply = [x for x in wpoll(relay_neuf, "pod-h1", True)["tasks"] if "Mode hard" in x["instruction"]]
    assert len(reply) == 1 and reply[0]["conversation"] == t["id"] and reply[0]["project"] == "h1"
    # Compte rendu : projets, tâches et question tranchée
    rapport = call(p, "compte_rendu").json()
    assert rapport["enabled"] and rapport["totals"]["tasks"] >= 3 and rapport["totals"]["questions"] == 1
    assert "h1" in [x["project"] for x in rapport["projects"]]
    assert call(p, "mode_hard", {"enabled": False}).json()["enabled"] is False
    assert any("Mode hard terminé" in a["titre"] for a in call(p, "resume").json()["alerts"])
    assert call(p, "compte_rendu").json()["since"] > 0  # le compte rendu reste consultable après l'arrêt


def test_pr_etat_et_fusion_par_l_orchestrateur(relay):
    """N'importe quel pod peut publier la fusion ou la fermeture d'une PR (le propriétaire de la tâche peut être hors ligne)."""
    p = paired_phone(relay)
    url = "https://github.com/a/b/pull/7"
    wpoll(relay, "orch-pr", True, [{"id": "orchpr", "name": "orch"}], hello=True)
    wpoll(relay, "pod-off", False, [{"id": "offpr", "name": "off"}], hello=True)
    t = call(p, "tache", {"instruction": "x", "project": "offpr"}).json()
    wpoll(relay, "pod-off", False)
    worker_call(relay, "w_evenements", {"worker_id": "pod-off", "events": [
        {"kind": "message", "task_id": t["id"], "type": "pr", "url": url, "title": "PR"},
        {"kind": "task", "task_id": t["id"], "task": {"status": "done", "project": "offpr", "result": f"PR : {url}"}}]})
    # (le pod « off » n'est plus interrogé : il est considéré hors ligne après quelques secondes ; l'orchestrateur reste actif)
    assert worker_call(relay, "w_pr_etat", {"task_id": t["id"], "url": url, "state": "merged"}).json() == {"ok": True, "deja": False}
    assert worker_call(relay, "w_pr_etat", {"task_id": t["id"], "url": url, "state": "merged"}).json()["deja"] is True
    assert worker_call(relay, "w_pr_etat", {"task_id": t["id"], "url": url, "state": "bizarre"}).status_code == 400
    assert any(e.get("type") == "pr_merged" and e.get("url") == url for e in call(p, "etat").json()["events"])


def test_mode_hard_perimetre_apercu_et_reprise(relay_neuf):
    """Mode hard : projets choisis, aperçu de ce qu'il compte faire, tâches interrompues reprises du plus ancien au plus récent."""
    p = paired_phone(relay_neuf)
    wpoll(relay_neuf, "pod-hp", True, [{"id": "hp1", "name": "Un"}, {"id": "hp2", "name": "Deux"}], hello=True)
    # Deux tâches interrompues par un redémarrage (la plus ancienne d'abord), sur hp1
    ids = []
    for texte in ("première tâche", "deuxième tâche"):
        t = call(p, "tache", {"instruction": texte, "project": "hp1"}).json()
        ids.append(t["id"])
    got = wpoll(relay_neuf, "pod-hp", True)
    assert {x["id"] for x in got["tasks"]} >= set(ids)
    wpoll(relay_neuf, "pod-hp", True, [{"id": "hp1", "name": "Un"}, {"id": "hp2", "name": "Deux"}], hello=True)  # redémarrage : tâches interrompues
    call(p, "proposition", {"action": "ajouter", "titre": "Idée deux", "instruction": "Avance sur hp2.", "project": "hp2"})
    ap = call(p, "hard_apercu", projects="hp1").json()
    assert [x["kind"] for x in ap["plan"]][:2] == ["reprise", "reprise"] and ap["plan"][0]["titre"].startswith("première")
    assert all(x["project"] == "hp1" for x in ap["plan"])  # hp2 hors périmètre : sa proposition n'apparaît pas
    assert [x["id"] for x in ap["projects"]] == ["hp1"]
    # Armement limité à hp1 : reprises dans l'ordre, jamais hp2
    assert call(p, "mode_hard", {"enabled": True, "hours": 2, "max_parallel": 3, "projects": ["hp1"]}).json()["projects"] == ["hp1"]
    assert wpoll(relay_neuf, "pod-hp", True)["hard_projects"] == ["hp1"]
    premiere = worker_call(relay_neuf, "w_auto_tache", {}).json()
    assert premiere["titre"].startswith("Reprise : première")
    assert "déjà en cours" in worker_call(relay_neuf, "w_auto_tache", {}).json()["skipped"] or worker_call(relay_neuf, "w_auto_tache", {}).json()["skipped"] == "aucune proposition"
    call(p, "mode_hard", {"enabled": False})


def test_full_auto_meme_perimetre_que_le_mode_hard(relay_neuf):
    """Le full auto se règle comme le mode hard (projets, durée en heures, parallélisme) ; le mode hard l'arme par défaut."""
    p = paired_phone(relay_neuf)
    wpoll(relay_neuf, "pod-fp", True, [{"id": "fp1", "name": "Un"}, {"id": "fp2", "name": "Deux"}], hello=True)
    auto = call(p, "autopilote", {"enabled": True, "hours": 6, "max_parallel": 2, "max_per_day": 7, "threshold": 0.7, "projects": ["fp2", "inconnu"]}).json()
    assert auto["enabled"] and auto["projects"] == ["fp2"] and auto["max_parallel"] == 2 and auto["max_per_day"] == 7
    assert 5 * 3600 < auto["until"] - time.time() <= 6 * 3600 + 5 and abs(auto["threshold"] - 0.7) < 1e-9
    assert wpoll(relay_neuf, "pod-fp", True)["hard"] is False  # le full auto seul ne retire aucune validation
    call(p, "autopilote", {"enabled": False})
    # Mode hard sans full auto : seules les validations disparaissent
    assert call(p, "mode_hard", {"enabled": True, "hours": 2, "auto": False}).json()["enabled"]
    assert call(p, "etat").json()["autopilot"]["enabled"] is False
    call(p, "mode_hard", {"enabled": False})
    # Mode hard par défaut : full auto armé avec les mêmes projets et réglages
    call(p, "mode_hard", {"enabled": True, "hours": 2, "projects": ["fp1"], "max_parallel": 4, "max_per_day": 9, "threshold": 0.9})
    auto = call(p, "etat").json()["autopilot"]
    assert auto["enabled"] and auto["projects"] == ["fp1"] and auto["max_parallel"] == 4 and auto["max_per_day"] == 9
    assert abs(auto["threshold"] - 0.9) < 1e-9


def test_incitation_mode_hard_fin_de_semaine(relay_neuf):
    """Il reste de la semaine à l'approche de sa réinitialisation : estimation en tokens et alerte proposant le mode hard."""
    p = paired_phone(relay_neuf)
    now = int(time.time())
    usage = {"limits": [{"type": "seven_day", "utilization": 0.4, "resets_at": now + 30 * 3600}], "ts": now}
    wpoll(relay_neuf, "pod-iw", True, [{"id": "iw", "name": "iw"}], hello=True)
    t = call(p, "tache", {"instruction": "gros travail", "project": "iw"}).json()
    wpoll(relay_neuf, "pod-iw", True)
    worker_call(relay_neuf, "w_evenements", {"worker_id": "pod-iw", "events": [
        {"kind": "task", "task_id": t["id"], "task": {"status": "done", "project": "iw", "result": "ok", "tokens": {"total": 4_000_000}}}]})
    assert call(p, "etat").json()["week"] is None  # aucun usage publié pour l'instant
    wpoll(relay_neuf, "pod-iw", True, state={"usage": usage})  # publie l'usage et déclenche la vérification
    alerts = call(p, "resume").json()["alerts"]
    msg = [a for a in alerts if "Il reste 60 %" in a["titre"]]
    assert len(msg) == 1 and "6 M" in msg[0]["corps"] and "mode hard" in msg[0]["corps"]
    wpoll(relay_neuf, "pod-iw", True, state={"usage": usage})  # vérification limitée à une toutes les 10 min : pas de doublon
    assert len([a for a in call(p, "resume").json()["alerts"] if "Il reste 60 %" in a["titre"]]) == 1


def test_reveil_des_pods_hors_ligne_avec_du_travail(relay_neuf):
    """Le relais désigne les projets du périmètre dont le pod est hors ligne et qui ont du travail (tâche en attente, proposition)."""
    import sqlite3

    p = paired_phone(relay_neuf)
    wpoll(relay_neuf, "orch-rv", True, [{"id": "orchrv", "name": "orch"}], hello=True)
    for wid, proj in (("pod-rv1", "rv1"), ("pod-rv2", "rv2"), ("pod-rv3", "rv3")):
        wpoll(relay_neuf, wid, False, [{"id": proj, "name": proj}], hello=True)
    t = call(p, "tache", {"instruction": "à faire", "project": "rv1"}).json()
    call(p, "proposition", {"action": "ajouter", "titre": "Idée rv2", "instruction": "Avance.", "project": "rv2"})
    # rv3 n'a aucun travail ; rv1, rv2 et rv3 passent hors ligne (aucun poll depuis longtemps)
    db = sqlite3.connect(Path(relay_neuf["docroot"]) / "cockpit" / "data" / "cockpit.db")
    db.execute("UPDATE workers SET vu_le = 0 WHERE id LIKE 'pod-rv%'")
    db.commit()
    db.close()
    # Autonomie désarmée : seuls les pods qui ont des tâches en attente sont à réveiller (rv2 n'a qu'une proposition)
    got = {x["id"]: x for x in worker_call(relay_neuf, "w_reveil", {}).json()["projects"]}
    assert set(got) == {"rv1"} and got["rv1"]["waiting"] == 1
    call(p, "autopilote", {"enabled": True, "hours": 2})
    got = {x["id"]: x for x in worker_call(relay_neuf, "w_reveil", {}).json()["projects"]}
    assert set(got) == {"rv1", "rv2"} and got["rv1"]["waiting"] == 1 and got["rv2"]["proposals"] == 1
    call(p, "autopilote", {"enabled": True, "hours": 2, "projects": ["rv2"]})  # périmètre : rv1 hors périmètre
    assert [x["id"] for x in worker_call(relay_neuf, "w_reveil", {}).json()["projects"]] == ["rv2"]
    assert t["id"]


def test_supprimer_et_restaurer_un_projet(relay_neuf):
    """Retirer un projet du cockpit : il disparaît partout (liste, mémoire, propositions, périmètres) et reste masqué, sauf demande."""
    p = paired_phone(relay_neuf)
    wpoll(relay_neuf, "orch-sp", True, [{"id": "orchsp", "name": "Orchestrateur"}], hello=True)
    wpoll(relay_neuf, "pod-sp", False, [{"id": "sp1", "name": "Un"}, {"id": "sp2", "name": "Deux"}], hello=True)
    t = call(p, "tache", {"instruction": "travail", "project": "sp1"}).json()
    got = wpoll(relay_neuf, "pod-sp", False)
    assert [x["id"] for x in got["tasks"]] == [t["id"]]
    # Tâche en cours : refus ; projet de l'orchestrateur : refus ; inconnu : 404
    assert call(p, "projet_supprimer", {"project": "sp1"}).status_code == 409
    assert call(p, "projet_supprimer", {"project": "orchsp"}).status_code == 409
    assert call(p, "projet_supprimer", {"project": "nexiste-pas"}).status_code == 404
    worker_call(relay_neuf, "w_evenements", {"worker_id": "pod-sp", "events": [
        {"kind": "task", "task_id": t["id"], "task": {"status": "done", "project": "sp1", "result": "Fait.",
                                                      "next_steps": [{"titre": "Suite sp1", "instruction": "Continue."}]}}]})
    call(p, "proposition", {"action": "ajouter", "titre": "Idée sp2", "instruction": "Avance.", "project": "sp2"})
    call(p, "autopilote", {"enabled": True, "hours": 2, "projects": ["sp1"]})
    etat = call(p, "etat").json()
    assert {"sp1", "sp2"} <= {x["id"] for x in etat["projects"]} and "sp1" in [x["id"] for x in etat["memory"]["projects"]]
    # Suppression sans l'historique
    assert call(p, "projet_supprimer", {"project": "sp1"}).json() == {"ok": True, "history": False}
    etat = call(p, "etat").json()
    assert "sp1" not in [x["id"] for x in etat["projects"]] and "sp2" in [x["id"] for x in etat["projects"]]
    assert "sp1" not in [x["id"] for x in etat["memory"]["projects"]]
    assert [x["titre"] for x in etat["memory"]["proposals"]] == ["Idée sp2"]  # la proposition de sp1 est partie
    assert etat["deleted_projects"]["sp1"]["name"] == "Un"
    assert etat["autopilot"]["enabled"] is False  # son périmètre était sp1 seul : désarmé, pas « tous les projets »
    assert t["id"] in {e["task_id"] for e in etat["events"] if e["kind"] == "task"}  # l'historique est conservé
    assert call(p, "tache", {"instruction": "encore", "project": "sp1"}).status_code == 400  # plus routable
    wpoll(relay_neuf, "pod-sp", False, [{"id": "sp1", "name": "Un"}, {"id": "sp2", "name": "Deux"}])  # le pod l'annonce toujours
    assert "sp1" not in [x["id"] for x in call(p, "etat").json()["projects"]]
    # Restauration
    assert call(p, "projet_restaurer", {"project": "sp1"}).json()["ok"]
    assert "sp1" in [x["id"] for x in call(p, "etat").json()["projects"]]
    assert call(p, "projet_restaurer", {"project": "sp1"}).status_code == 404
    # Suppression avec l'historique
    assert call(p, "projet_supprimer", {"project": "sp1", "history": True}).json()["history"] is True
    etat = call(p, "etat", since=0).json()
    assert t["id"] not in {e["task_id"] for e in etat["events"]}


def test_ecoute_longue_sans_verrou_d_ecriture(relay_neuf):
    """La boucle d'écoute lit sans verrou, livre un travail arrivé pendant l'attente et n'écrit « vu_le » qu'à intervalles espacés."""
    import sqlite3
    import threading

    p = paired_phone(relay_neuf)
    wpoll(relay_neuf, "pod-ll", False, [{"id": "ll", "name": "ll"}], hello=True)
    base = Path(relay_neuf["docroot"]) / "cockpit" / "data"
    vu = lambda: sqlite3.connect(base / "cockpit.db").execute("SELECT vu_le FROM workers WHERE id = 'pod-ll'").fetchone()[0]  # noqa: E731
    avant = vu()
    got = {}

    def ecoute():
        got["rep"] = worker_call(relay_neuf, "w_poll", {"worker_id": "pod-ll", "instance": "inst-pod-ll", "wait": 6, "orchestrateur": False}).json()
        got["fin"] = time.time()

    t0 = time.time()
    th = threading.Thread(target=ecoute)
    th.start()
    time.sleep(1.2)
    tache = call(p, "tache", {"instruction": "arrivée pendant l'attente", "project": "ll"}).json()
    th.join(10)
    assert [x["id"] for x in got["rep"]["tasks"]] == [tache["id"]]
    assert got["fin"] - t0 < 4  # livrée dans la seconde qui suit, sans attendre la fin du délai
    assert vu() >= avant
    # Un « superseded » se détecte aussi sans verrou
    assert worker_call(relay_neuf, "w_poll", {"worker_id": "pod-ll", "instance": "autre", "wait": 0, "orchestrateur": False}).json() == {"superseded": True}


def test_sauvegarde_quotidienne_de_la_base(relay_neuf):
    """Une sauvegarde cohérente par jour (VACUUM INTO), lisible et saine ; seules les 3 dernières sont gardées."""
    import sqlite3

    p = paired_phone(relay_neuf)
    base = Path(relay_neuf["docroot"]) / "cockpit" / "data"
    # Anciennes sauvegardes factices : seules les plus récentes doivent rester
    for jour in ("20200101", "20200102", "20200103"):
        (base / f"cockpit.db.auto-{jour}").write_bytes(b"x")
    wpoll(relay_neuf, "pod-sv", False, [{"id": "sv", "name": "sv"}], hello=True)  # premier poll : entretien
    sauvegardes = sorted(base.glob("cockpit.db.auto-*"))
    assert len(sauvegardes) == 3 and sauvegardes[-1].name == f"cockpit.db.auto-{time.strftime('%Y%m%d')}"
    copie = sqlite3.connect(sauvegardes[-1])
    assert copie.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert copie.execute("SELECT COUNT(*) FROM workers").fetchone()[0] >= 1
    assert call(p, "moi").json()["worker_online"] is True


def test_memoire_de_projet_premiere_chose_a_faire(relay_neuf):
    """Un projet sans mémoire (CLAUDE.md vide ou trop court) : l'autonomie la crée en premier, une fois par jour, avant tout le reste."""
    p = paired_phone(relay_neuf)
    wpoll(relay_neuf, "pod-me", False, [{"id": "me1", "name": "Un"}, {"id": "me2", "name": "Deux"}, {"id": "me3", "name": "Trois"}], hello=True)
    longue = "# Projet\n" + "Une ligne utile sur le projet. " * 10
    pub = lambda projet, contenu: worker_call(relay_neuf, "w_memoire", {"project": projet, "content": contenu, "hash": "h", "worker_id": "pod-me"})  # noqa: E731
    pub("me1", "")        # absente
    pub("me2", "# Deux")  # trop courte : absente
    pub("me3", longue)    # présente
    mem = {x["id"]: x for x in call(p, "etat").json()["memory"]["projects"]}
    assert set(mem) == {"me1", "me2", "me3"}  # tous les projets ont une entrée, même sans tâche terminée
    assert [mem[i]["memory"]["state"] for i in ("me1", "me2", "me3")] == ["absente", "absente", "ok"]
    assert mem["me3"]["memory"]["lines"] >= 2 and mem["me1"]["status"] == "nouveau"
    # Aperçu : la mémoire d'abord, seulement pour les projets qui en manquent
    call(p, "proposition", {"action": "ajouter", "titre": "Idée me3", "instruction": "Avance.", "project": "me3"})
    plan = call(p, "hard_apercu").json()["plan"]
    assert [x["kind"] for x in plan][:2] == ["memoire", "memoire"] and {x["project"] for x in plan[:2]} == {"me1", "me2"}
    assert plan[2]["kind"] == "proposition"
    # À la demande : refus si la mémoire existe, création sinon, pas de doublon pendant qu'elle se crée
    assert call(p, "memoire_init", {"project": "me3"}).status_code == 409
    assert call(p, "memoire_init", {"project": "inconnu"}).status_code == 404
    manuel = call(p, "memoire_init", {"project": "me2"}).json()
    assert manuel["ok"] and call(p, "memoire_init", {"project": "me2"}).status_code == 409
    tache = [x for x in wpoll(relay_neuf, "pod-me", False)["tasks"] if x["id"] == manuel["task_id"]][0]
    assert "CLAUDE.md" in tache["instruction"] and "Prochaines étapes" in tache["instruction"] and tache["project"] == "me2"
    # Autonomie : la mémoire de me1 passe avant la proposition de me3 ; me2 vient d'être tentée (une fois par jour)
    call(p, "autopilote", {"enabled": True, "hours": 2, "max_parallel": 3})
    premiere = worker_call(relay_neuf, "w_auto_tache", {}).json()
    assert premiere["titre"] == "Mémoire du projet : Un"
    suivante = worker_call(relay_neuf, "w_auto_tache", {}).json()
    assert suivante["titre"] == "Idée me3"  # me2 : déjà tentée aujourd'hui ; me1 : sa tâche est en cours
    assert [x["project"] for x in call(p, "hard_apercu").json()["plan"] if x["kind"] == "memoire"] == []
    # Une fois la mémoire écrite par l'agent (publiée par le worker), l'état passe à ok
    pub("me1", longue)
    assert {x["id"]: x["memory"]["state"] for x in call(p, "etat").json()["memory"]["projects"]}["me1"] == "ok"


def test_reprise_manuelle_d_une_tache_en_pause(relay_neuf):
    """Limite du forfait : la tâche en pause ne bloque pas le projet ; « Reprendre » crée la suite et annule l'ancienne."""
    p = paired_phone(relay_neuf)
    wpoll(relay_neuf, "pod-rp", True, [{"id": "rp1", "name": "Un"}], hello=True)
    t = call(p, "tache", {"instruction": "gros chantier", "project": "rp1"}).json()
    wpoll(relay_neuf, "pod-rp", True)
    assert call(p, "reprendre", {"task_id": t["id"]}).status_code == 409  # pas encore en pause
    worker_call(relay_neuf, "w_evenements", {"worker_id": "pod-rp", "events": [
        {"kind": "task", "task_id": t["id"], "task": {"status": "paused", "project": "rp1", "resume_at": int(time.time()) + 3600,
                                                      "result": "⏸ Mise en pause"}}]})
    st = last_status(p)[t["id"]]
    assert st["status"] == "paused" and st["resume_at"] > time.time()
    r = call(p, "reprendre", {"task_id": t["id"]}).json()
    assert r["task_id"] and r["task_id"] != t["id"]
    st = last_status(p)
    assert st[t["id"]]["status"] == "cancelled" and st[r["task_id"]]["status"] == "pending"
    assert call(p, "reprendre", {"task_id": t["id"]}).status_code == 409  # déjà reprise


def test_mode_hard_limite_a_son_perimetre(relay_neuf):
    """Armer le mode hard sur un projet n'approuve pas les validations en attente des autres ; un périmètre disparu est refusé."""
    p = paired_phone(relay_neuf)
    for wid, proj in (("pod-pa", "pa"), ("pod-pb", "pb")):
        wpoll(relay_neuf, wid, wid == "pod-pa", [{"id": proj, "name": proj}], hello=True)
    ids = {}
    for wid, proj in (("pod-pa", "pa"), ("pod-pb", "pb")):
        t = call(p, "tache", {"instruction": f"travail {proj}", "project": proj}).json()
        wpoll(relay_neuf, wid, wid == "pod-pa")
        worker_call(relay_neuf, "w_evenements", {"worker_id": wid, "events": [
            {"kind": "task", "task_id": t["id"], "task": {"status": "waiting_approval", "project": proj}},
            {"kind": "approval", "task_id": t["id"], "approval": {"id": f"ap-{proj}", "task_id": t["id"], "tool": "Bash",
             "tool_input": {"command": "rm -rf /tmp/x"}, "reason": "opération sensible : rm", "expires": 9999999999}}]})
        ids[proj] = f"ap-{proj}"
    assert call(p, "mode_hard", {"enabled": True, "hours": 1, "projects": ["inconnu"]}).status_code == 400
    assert call(p, "mode_hard", {"enabled": True, "hours": 1, "projects": ["pa"], "auto": False}).json()["projects"] == ["pa"]
    attente = [a["id"] for a in call(p, "resume").json()["approvals"]]
    assert attente == ["ap-pb"]  # celle de pa est approuvée, celle de pb attend toujours ta réponse
    call(p, "mode_hard", {"enabled": False})


def test_idees_externes_selon_le_choix_fait_en_armant(relay_neuf):
    """Une idée arrivée par la clé d'API est marquée externe ; le full auto ne la lance que si l'option a été laissée cochée."""
    p = paired_phone(relay_neuf)
    wpoll(relay_neuf, "pod-ext", True, [{"id": "ext", "name": "Ext"}], hello=True)
    r = worker_call(relay_neuf, "proposition_api", {"titre": "Bot", "instruction": "Ajoute un export CSV.", "project": "ext"}, key=CLE_API)
    assert r.status_code == 200
    prop = call(p, "etat").json()["memory"]["proposals"][0]
    assert prop["externe"] is True
    # Désarmé de l'option : rien n'est lancé, et l'aperçu ne la compte pas
    call(p, "autopilote", {"enabled": True, "hours": 2, "external": False})
    assert worker_call(relay_neuf, "w_auto_tache", {}).json() == {"skipped": "aucune proposition"}
    apercu = call(p, "hard_apercu", external="0").json()
    assert apercu["counts"]["external"] == 1 and not [x for x in apercu["plan"] if x["kind"] == "proposition"]
    # Option laissée (par défaut) : elle entre dans le plan et part
    assert [x["titre"] for x in call(p, "hard_apercu").json()["plan"] if x.get("external")] == ["Bot"]
    call(p, "autopilote", {"enabled": True, "hours": 2})
    assert worker_call(relay_neuf, "w_auto_tache", {}).json()["titre"] == "Bot"

def test_livraison_avec_accuse_de_reception(relay_neuf):
    """Une réponse de w_poll perdue (coupure réseau) est renvoyée au poll suivant tant que le worker ne l'a pas confirmée."""
    p = paired_phone(relay_neuf)
    wpoll(relay_neuf, "pod-ack", True, [{"id": "ack", "name": "Ack"}], hello=True, ack=None)
    t = call(p, "tache", {"instruction": "à ne pas perdre", "project": "ack"}).json()
    first = wpoll(relay_neuf, "pod-ack", True, ack=None)
    assert [x["id"] for x in first["tasks"]] == [t["id"]] and first["lot"]
    # Réponse perdue : le worker n'a rien reçu et redemande sans accusé
    again = wpoll(relay_neuf, "pod-ack", True, ack=None)
    assert [x["id"] for x in again["tasks"]] == [t["id"]] and again["lot"] == first["lot"]
    # Accusé reçu : plus rien à livrer
    done = wpoll(relay_neuf, "pod-ack", True, ack=first["lot"])
    assert done["tasks"] == [] and done["lot"] is None
    # Un ancien worker (sans « ack ») garde la livraison simple, sans renvoi
    t2 = call(p, "tache", {"instruction": "ancien worker", "project": "ack"}).json()
    assert [x["id"] for x in wpoll(relay_neuf, "pod-ack", True)["tasks"]] == [t2["id"]]
    assert wpoll(relay_neuf, "pod-ack", True)["tasks"] == []



def test_idee_de_nouveau_projet_lancee_par_l_orchestrateur(relay_neuf):
    """Une idée sans projet (bot, saisie sur Auto) entre dans le plan du mode hard et part chez l'orchestrateur, qui crée le projet."""
    p = paired_phone(relay_neuf)
    wpoll(relay_neuf, "orch-idee", True, [{"id": "onyxia", "name": "Onyxia", "description": "On y crée un NOUVEAU projet."},
                                          {"id": "cockpit", "name": "Cockpit", "description": "Le cockpit."}], hello=True)
    # Sans titre : tiré de l'instruction ; projet donné par son nom, sans tenir compte de la casse
    r = worker_call(relay_neuf, "proposition_api", {"instruction": "Un bot Telegram qui révise les cartes Anki chaque matin"}, key=CLE_API).json()
    assert r["kind"] == "nouveau_projet" and r["titre"].startswith("Un bot Telegram")
    assert worker_call(relay_neuf, "proposition_api", {"instruction": "Corrige le thème", "project": "COCKPIT"}, key=CLE_API).json()["project"] == "cockpit"
    assert worker_call(relay_neuf, "proposition_api", {"instruction": "x", "project": "inconnu"}, key=CLE_API).status_code == 400
    plan = call(p, "hard_apercu").json()
    assert [x["kind"] for x in plan["plan"]].count("idee") == 1 and plan["counts"]["ideas"] == 1
    assert not [x for x in call(p, "hard_apercu", ideas="0").json()["plan"] if x["kind"] == "idee"]
    call(p, "mode_hard", {"enabled": True, "hours": 2, "max_parallel": 3})
    lances = [worker_call(relay_neuf, "w_auto_tache", {}).json() for _ in range(2)]
    taches = {t["id"]: t for t in wpoll(relay_neuf, "orch-idee", True)["tasks"]}
    idee = next(taches[x["task_id"]] for x in lances if x["task_id"] in taches and "Nouveau projet" in taches[x["task_id"]]["instruction"])
    assert idee["project"] == "onyxia" and "Anki" in idee["instruction"]
    call(p, "mode_hard", {"enabled": False})


def test_prochaines_etapes_suivent_la_conversation(relay_neuf):
    """Une étape lancée depuis la carte ferme la proposition correspondante ; une proposition d'étape continue sa conversation."""
    p = paired_phone(relay_neuf)
    wpoll(relay_neuf, "pod-st", True, [{"id": "st", "name": "St"}], hello=True)
    t = call(p, "tache", {"instruction": "écris le module", "project": "st"}).json()
    wpoll(relay_neuf, "pod-st", True)
    steps = [{"titre": "Ajouter les tests", "instruction": "Ajoute les tests du module."},
             {"titre": "Documenter", "instruction": "Documente le module."}]
    worker_call(relay_neuf, "w_evenements", {"worker_id": "pod-st", "events": [
        {"kind": "task", "task_id": t["id"], "task": {"status": "done", "project": "st", "result": "ok", "next_steps": steps}}]})
    props = call(p, "etat").json()["memory"]["proposals"]
    assert sorted(x["titre"] for x in props) == ["Ajouter les tests", "Documenter"]
    # Lancer la proposition « Documenter » : suite de la même conversation (contexte et session repris)
    doc = next(x for x in props if x["titre"] == "Documenter")
    lancee = call(p, "proposition", {"action": "lancer", "id": doc["id"]}).json()["task_id"]
    suite = next(x for x in wpoll(relay_neuf, "pod-st", True)["tasks"] if x["id"] == lancee)
    assert suite["conversation"] == t["id"] and suite["reply_to"] == t["id"]
    # La conversation a avancé : l'autre étape (dépassée) n'est plus proposée au full auto
    assert call(p, "etat").json()["memory"]["proposals"] == []

    # Depuis la carte : même instruction que l'étape -> proposition marquée lancée, pas relancée plus tard
    t2 = call(p, "tache", {"instruction": "autre module", "project": "st"}).json()
    wpoll(relay_neuf, "pod-st", True)
    worker_call(relay_neuf, "w_evenements", {"worker_id": "pod-st", "events": [
        {"kind": "task", "task_id": t2["id"], "task": {"status": "done", "project": "st", "result": "ok", "next_steps": steps[:1]}}]})
    assert len(call(p, "etat").json()["memory"]["proposals"]) == 1
    call(p, "tache", {"instruction": "Ajoute les tests du module.", "reply_to": t2["id"]})
    assert call(p, "etat").json()["memory"]["proposals"] == []
    # Ignorer les étapes d'une tâche les retire aussi des propositions
    t3 = call(p, "tache", {"instruction": "troisième", "project": "st"}).json()
    wpoll(relay_neuf, "pod-st", True)
    worker_call(relay_neuf, "w_evenements", {"worker_id": "pod-st", "events": [
        {"kind": "task", "task_id": t3["id"], "task": {"status": "done", "project": "st", "result": "ok", "next_steps": steps[1:]}}]})
    assert len(call(p, "etat").json()["memory"]["proposals"]) == 1
    call(p, "ignorer_suite", {"task_id": t3["id"]})
    assert call(p, "etat").json()["memory"]["proposals"] == []
