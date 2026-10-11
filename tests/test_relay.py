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
b64u = lambda b: base64.urlsafe_b64encode(b).rstrip(b"=").decode()
b64u_dec = lambda s: base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


@pytest.fixture(scope="module")
def relay():
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
    yield {"base": base, "origin": origin, "app": origin + "/cockpit/"}
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


class Authenticator:
    """Authentificateur WebAuthn logiciel (ES256), comme Face ID / Touch ID."""

    def __init__(self, origin):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.id = b64u(os.urandom(32))
        self.origin = origin

    def client_data(self, type_, challenge, origin=None):
        return json.dumps({"type": type_, "challenge": challenge, "origin": origin or self.origin}).encode()

    def register(self, options):
        spki = self.key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        return {"id": self.id, "clientDataJSON": b64u(self.client_data("webauthn.create", options["challenge"])),
                "publicKey": b64u(spki), "alg": -7, "name": "Test"}

    def assert_(self, options, flags=0x05, origin=None, rp_id="localhost"):
        cd = self.client_data("webauthn.get", options["challenge"], origin)
        auth = hashlib.sha256(rp_id.encode()).digest() + bytes([flags]) + b"\0\0\0\1"
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
    assert call(p, "passkey_creer", auth.register(opts)).status_code == 200

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
