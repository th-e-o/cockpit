"""Robustesse du worker : annulation à tout moment, erreurs hors de l'agent, file d'envoi au relais jamais bloquée."""

import asyncio
from pathlib import Path

from claude_agent_sdk import ResultMessage

from cockpit import agent as agent_mod
from cockpit.agent import Runner
from cockpit.policy import Policy
from cockpit.relay import Relay, RelayError
from cockpit.router import Project
from cockpit.store import Store, Task

ROOT = Path(__file__).resolve().parent.parent


class SlowStartClient:
    """Client dont le démarrage prend du temps (instantané git, CLI qui se connecte) : l'annulation arrive pendant."""
    started: list[str] = []

    def __init__(self, options):
        self.options = options

    async def __aenter__(self):
        await asyncio.sleep(0.3)
        return self

    async def __aexit__(self, *exc):
        return False

    async def interrupt(self):
        raise RuntimeError("CLI déjà arrêtée")

    async def query(self, prompt):
        SlowStartClient.started.append(prompt)

    async def receive_response(self):
        yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1,
                            session_id="s", total_cost_usd=0, result="fait")


def runner(tmp_path):
    return Runner(Store(tmp_path / "data"), Policy.load(ROOT / "config" / "policy.yaml"))


def test_annulation_pendant_le_routage_n_est_pas_perdue(tmp_path):
    r = runner(tmp_path)
    t = Task(id="t1", instruction="x")
    r.store.tasks["t1"] = t
    asyncio.run(r.cancel(t))
    asyncio.run(r.run(t, Project(id="p", name="P", cwd=str(tmp_path))))
    assert t.status == "cancelled"


def test_annulation_pendant_le_demarrage_de_l_agent(tmp_path, monkeypatch):
    monkeypatch.setattr(agent_mod, "ClaudeSDKClient", SlowStartClient)
    SlowStartClient.started = []
    r = runner(tmp_path)
    t = Task(id="t2", instruction="démarre")
    r.store.tasks["t2"] = t

    async def scenario():
        job = asyncio.create_task(r.run(t, Project(id="p", name="P", cwd=str(tmp_path))))
        await asyncio.sleep(0.1)
        await r.cancel(t)  # le client n'est pas encore enregistré ; une interruption qui échoue ne gêne pas
        await job
        # Une validation demandée après l'annulation est refusée sans rien publier ni changer le statut
        assert not await r.request_approval(t, Project(id="p", name="P", cwd=str(tmp_path)), "Bash", {"command": "rm x"}, "rm")

    asyncio.run(scenario())
    assert SlowStartClient.started == []  # l'agent n'a jamais reçu l'instruction
    assert t.status == "cancelled" and not r.running


def test_erreur_hors_de_l_agent_ne_laisse_pas_la_tache_en_cours(tmp_path, monkeypatch):
    r = runner(tmp_path)
    t = Task(id="t3", instruction="x")

    async def boom(*a, **k):
        raise OSError("disque plein")

    monkeypatch.setattr(r, "_snapshot", boom)
    asyncio.run(r.run(t, Project(id="p", name="P", cwd=str(tmp_path))))
    assert t.status == "error" and "disque plein" in t.result and not r.running


def test_un_evenement_invalide_ne_bloque_pas_la_file():
    relay = Relay("http://relais", "k")
    sent = []

    async def call(route, body):
        if any(e.get("trop_gros") for e in body["events"]):
            raise RelayError(route, 413, "trop gros")
        sent.extend(body["events"])
        return {}

    relay.call = call
    relay.send({"texte": "emoji coupé \ud83d", "cout": float("nan")})
    relay.send({"trop_gros": True})
    relay.send({"kind": "approval", "id": "a1"})
    asyncio.run(relay.flush())
    assert sent == [{"texte": "emoji coupé ?", "cout": None}, {"kind": "approval", "id": "a1"}]
    assert relay.events == []


def test_relais_indisponible_garde_les_evenements():
    relay = Relay("http://relais", "k")

    async def call(route, body):
        raise RelayError(route, 503, "maintenance")

    relay.call = call
    relay.send({"kind": "approval", "id": "a1"})
    try:
        asyncio.run(relay.flush())
    except RelayError:
        pass
    assert relay.events == [{"kind": "approval", "id": "a1"}]  # réessayé plus tard, jamais abandonné
