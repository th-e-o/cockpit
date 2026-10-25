"""Barre d'usage du forfait Claude et recherche dans les dépôts GitHub de l'utilisateur (API simulées)."""

import asyncio

import httpx
import pytest
from claude_agent_sdk import RateLimitEvent
from claude_agent_sdk.types import RateLimitInfo

from cockpit import github, usage
from cockpit.agent import Runner
from cockpit.policy import Policy
from cockpit.store import Store, Task
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_lecture_de_l_usage():
    body = {"five_hour": {"utilization": 87.0, "resets_at": "2026-10-12T18:00:00+00:00"},
            "seven_day": {"utilization": 41.5, "resets_at": None}, "seven_day_opus": None, "autre": {"utilization": 1}}
    limits = usage.parse(body)
    assert set(limits) == {"five_hour", "seven_day"}
    from datetime import datetime, timezone

    assert limits["five_hour"]["utilization"] == pytest.approx(0.87)
    assert limits["five_hour"]["resets_at"] == int(datetime(2026, 10, 12, 18, tzinfo=timezone.utc).timestamp())
    assert limits["seven_day"]["resets_at"] is None


def test_interface_d_usage(monkeypatch):
    real = httpx.AsyncClient
    seen = []

    def handler(req):
        seen.append(req.headers)
        if req.headers["authorization"] == "Bearer bon":
            return httpx.Response(200, json={"five_hour": {"utilization": 12, "resets_at": "2026-10-12T18:00:00Z"}})
        return httpx.Response(403, json={"error": "scope"})

    monkeypatch.setattr(usage.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))
    assert asyncio.run(usage.fetch("bon"))["five_hour"]["utilization"] == pytest.approx(0.12)
    assert seen[0]["anthropic-beta"] == "oauth-2025-04-20"
    with pytest.raises(usage.UsageError, match="403"):
        asyncio.run(usage.fetch("refuse"))
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    with pytest.raises(usage.UsageError, match="jeton"):
        asyncio.run(usage.fetch())


def test_evenements_pendant_les_taches(tmp_path, monkeypatch):
    from cockpit import worker as worker_mod
    from cockpit.relay import Relay

    monkeypatch.setattr(worker_mod, "DATA", tmp_path)
    w = worker_mod.Worker(Relay("http://relais.invalid", "k"))
    w.usage.update({"seven_day": usage.limit("seven_day", 0.5, 1800000000)}, "compte")
    w.rate_limit(RateLimitInfo(status="allowed_warning", resets_at=1800000500, rate_limit_type="five_hour", utilization=0.82))
    w.rate_limit(RateLimitInfo(status="rejected", rate_limit_type="seven_day"))  # sans pourcentage : garde le dernier
    w.rate_limit(RateLimitInfo(status="allowed", rate_limit_type="overage", utilization=0.1))  # type ignoré
    pub = w.usage.public()
    by = {x["type"]: x for x in pub["limits"]}
    assert [x["type"] for x in pub["limits"]] == ["five_hour", "seven_day"] and pub["source"] == "agent"
    assert by["five_hour"]["utilization"] == 0.82 and by["seven_day"] == {"type": "seven_day", "utilization": 0.5,
                                                                          "resets_at": None, "status": "rejected"}
    # Limite atteinte pendant une tâche : signalée sur la carte
    store = Store(tmp_path / "data")
    r = Runner(store, Policy.load(ROOT / "config" / "policy.yaml"))
    t = Task(id="t", instruction="x")
    store.tasks["t"] = t
    r._relay(t, RateLimitEvent(rate_limit_info=RateLimitInfo(status="rejected", resets_at=1800000000,
                                                             rate_limit_type="five_hour"), uuid="u", session_id="s"))
    assert t.events[-1]["type"] == "rate_limited" and t.events[-1]["resets_at"] == 1800000000


def test_recherche_dans_les_depots(monkeypatch):
    monkeypatch.setenv("GIT_PERSONAL_ACCESS_TOKEN", "ghp_test")
    real = httpx.AsyncClient
    repo = lambda name, desc="": {"full_name": name, "html_url": f"https://github.com/{name}", "private": True,
                                  "default_branch": "main", "description": desc, "pushed_at": "2026-10-01T00:00:00Z"}

    def handler(req):
        path, q = req.url.path, req.url.params.get("q", "")
        if path == "/user/repos":
            return httpx.Response(200, json=[repo("th-e-o/analyse_etp", "Effectifs ETP"), repo("th-e-o/cockpit")])
        if path == "/user":
            return httpx.Response(200, json={"login": "th-e-o"})
        if path == "/search/repositories":
            assert "user:th-e-o" in q and "in:name,description,readme" in q
            return httpx.Response(200, json={"items": [repo("th-e-o/analyse_etp")] if "fonctionnaires" in q else []})
        if path == "/search/code":
            return httpx.Response(200, json={"items": [{"path": "src/compte_agents.py", "repository": repo("th-e-o/rh_stats")}]})
        return httpx.Response(404)

    monkeypatch.setattr(github, "_client", lambda: real(base_url=github.API, transport=httpx.MockTransport(handler)))
    found = asyncio.run(github.chercher("nombre de fonctionnaires"))
    by = {x["name"]: x["why"] for x in found}
    assert by == {"th-e-o/analyse_etp": "README", "th-e-o/rh_stats": "code : src/compte_agents.py"}
    assert {x["name"] for x in asyncio.run(github.chercher("ETP"))} >= {"th-e-o/analyse_etp"}  # nom ou description
    with pytest.raises(github.GitHubError):
        asyncio.run(github.chercher("   "))


def test_routage_vers_l_orchestrateur():
    from cockpit.router import keyword_route, load_projects

    projects = load_projects(ROOT / "config" / "projects.yaml")
    r = keyword_route("Est-ce que tu peux reprendre le code sur mon github qui cherche le nombre de fonctionnaires ?", projects)
    assert r.project == "onyxia"
    assert Policy.load(ROOT / "config" / "policy.yaml").evaluate("mcp__cockpit__chercher_depots", {}, "/").action == "allow"
