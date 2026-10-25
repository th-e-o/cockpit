"""Mode full auto côté worker (usage du forfait, propositions) et détection des réinitialisations de tokens."""

import asyncio
import time

import pytest

from cockpit import usage
from cockpit.autopilote import Autopilot, parse_proposals, usage_blocks


def limits(util, resets_at, kind="five_hour", status=None):
    return {kind: usage.limit(kind, util, resets_at, status)}


def test_reinitialisation_signalee_quand_la_limite_etait_presque_epuisee():
    u = usage.Usage()
    now = time.time()
    u.update(limits(0.92, now - 30), "api")
    assert u.update(limits(0.03, now + 5 * 3600), "api") == ["five_hour"]
    assert u.update(limits(0.05, now + 5 * 3600), "api") == []  # pas de doublon à la lecture suivante


def test_pas_de_notification_si_la_limite_n_etait_pas_proche():
    u = usage.Usage()
    now = time.time()
    u.update(limits(0.30, now - 30), "api")
    assert u.update(limits(0.0, now + 5 * 3600), "api") == []


def test_pas_de_notification_avant_la_fin_de_la_fenetre():
    u = usage.Usage()
    now = time.time()
    u.update(limits(0.95, now + 3600), "api")
    assert u.update(limits(0.40, now + 3600), "api") == []  # baisse sans fenêtre écoulée : pas une réinitialisation


def test_reinitialisation_apres_limite_atteinte_sans_pourcentage():
    u = usage.Usage()
    now = time.time()
    u.update(limits(None, now - 30, status="rejected"), "agent")
    assert u.update(limits(0.0, now + 5 * 3600), "api") == ["five_hour"]


def test_prochaine_reinitialisation():
    u = usage.Usage()
    now = time.time()
    u.update({**limits(0.1, now + 600), **limits(0.1, now + 86400, "seven_day")}, "api")
    assert u.next_reset() == pytest.approx(now + 600, abs=2)


def test_le_forfait_bloque_le_full_auto():
    u = usage.Usage()
    assert usage_blocks(u, {}) is None  # usage inconnu : le plafond du jour du relais protège
    u.update(limits(0.50, None), "api")
    assert usage_blocks(u, {"threshold": 0.85}) is None
    u.update(limits(0.90, None), "api")
    assert "session" in usage_blocks(u, {"threshold": 0.85})
    assert usage_blocks(u, {"threshold": 0.95}) is None
    u.update(limits(0.1, None, status="rejected"), "agent")
    assert "limite atteinte" in usage_blocks(u, {"threshold": 0.95})
    u2 = usage.Usage()
    u2.update(limits(0.99, None, "seven_day"), "api")
    assert "semaine" in usage_blocks(u2, {"threshold": 0.85})


def test_propositions_generees_filtrees():
    text = ('Voici : {"cockpit": [{"titre": "Tests du relais", "instruction": "Ajoute des tests."}, {"titre": "", "instruction": "x"}],'
            ' "inconnu": [{"titre": "A", "instruction": "B"}], "onyxia": "pas une liste"}')
    out = parse_proposals(text, {"cockpit", "onyxia"})
    assert list(out) == ["cockpit"] and len(out["cockpit"]) == 1
    assert out["cockpit"][0]["instruction"].startswith("Ajoute des tests.") and "full auto" in out["cockpit"][0]["instruction"]
    with pytest.raises(ValueError):
        parse_proposals("rien", {"cockpit"})


class FakeRelay:
    def __init__(self, answers):
        self.answers, self.calls = list(answers), 0

    async def auto_tache(self):
        self.calls += 1
        return self.answers.pop(0)


def test_tick_ne_lance_rien_si_desarme_ou_si_le_forfait_est_plein():
    relay = FakeRelay([{"task_id": "t1", "titre": "x"}])
    u = usage.Usage()
    ap = Autopilot(relay, u)
    tick = lambda: asyncio.run(ap.tick())  # noqa: E731
    tick()
    assert relay.calls == 0  # désarmé
    ap.config = {"enabled": True, "threshold": 0.85}
    u.update(limits(0.9, None), "api")
    tick()
    assert relay.calls == 0 and "session" in ap.waiting
    u.update(limits(0.2, None), "api")
    tick()
    assert relay.calls == 1 and ap.waiting == ""


def test_plusieurs_projets_de_front_jusqu_au_maximum():
    """Le worker demande au relais une tâche par projet libre, jusqu'à max_parallel, puis s'arrête sur son refus."""
    relay = FakeRelay([{"task_id": "a", "titre": "A"}, {"task_id": "b", "titre": "B"}, {"skipped": "2 tâche(s) automatique(s) déjà en cours (maximum 3)"}])
    ap = Autopilot(relay, usage.Usage())
    ap.config = {"enabled": True, "max_parallel": 3}
    asyncio.run(ap.tick())
    assert relay.calls == 3 and "déjà en cours" in ap.waiting
    relay = FakeRelay([{"task_id": "a", "titre": "A"}] * 5)
    ap = Autopilot(relay, usage.Usage())
    ap.config = {"enabled": True, "max_parallel": 2}
    asyncio.run(ap.tick())
    assert relay.calls == 2  # jamais plus que le maximum demandé
