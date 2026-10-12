"""Réglages communs aux tests : aucun appel réel à Claude (sélecteur de modèle, routeur)."""

import pytest


@pytest.fixture(autouse=True)
def pas_de_claude_reel(monkeypatch):
    """Le sélecteur de modèle n'interroge jamais Haiku pendant les tests : il prend son repli (Opus) aussitôt."""
    from cockpit import router

    async def indisponible(system, prompt):
        raise RuntimeError("pas d'appel à Claude pendant les tests")

    monkeypatch.setattr(router, "_ask_haiku", indisponible)
