"""Barre d'usage : lecture des en-têtes d'usage de l'API (session 5 h et semaine), API simulée."""

import asyncio

import httpx
import pytest

from cockpit import usage

HEADERS = {"anthropic-ratelimit-unified-5h-utilization": "0.34", "anthropic-ratelimit-unified-5h-reset": "1800000000",
           "anthropic-ratelimit-unified-7d-utilization": "0.61", "anthropic-ratelimit-unified-7d-reset": "1800500000",
           "anthropic-ratelimit-unified-status": "allowed"}


def test_en_tetes():
    limits = usage.from_headers(HEADERS)
    assert limits["five_hour"] == {"type": "five_hour", "utilization": 0.34, "resets_at": 1800000000, "status": "allowed"}
    assert limits["seven_day"]["utilization"] == 0.61 and limits["seven_day"]["resets_at"] == 1800500000
    assert usage.from_headers({"anthropic-ratelimit-unified-5h-utilization": "34"})["five_hour"]["utilization"] == 0.34
    assert usage.from_headers({}) == {}


def mock(monkeypatch, handler):
    real = httpx.AsyncClient
    monkeypatch.setattr(usage.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **kw))


def test_sonde_par_comptage_gratuit(monkeypatch):
    calls = []

    def handler(req):
        calls.append(req.url.path)
        return httpx.Response(200, headers=HEADERS, json={"input_tokens": 3})

    mock(monkeypatch, handler)
    limits, how = asyncio.run(usage.probe("tok"))
    assert how == "comptage de tokens" and calls == ["/v1/messages/count_tokens"] and set(limits) == {"five_hour", "seven_day"}


def test_sonde_par_mini_requete(monkeypatch):
    calls = []

    def handler(req):
        calls.append((req.url.path, req.content and __import__("json").loads(req.content).get("model")))
        if req.url.path.endswith("count_tokens"):
            return httpx.Response(200, json={"input_tokens": 3})  # sans en-têtes d'usage
        if b"claude-haiku-4-5" in req.content:
            return httpx.Response(404, json={"error": "model"})
        return httpx.Response(200, headers=HEADERS, json={"content": []})

    mock(monkeypatch, handler)
    monkeypatch.delenv("COCKPIT_USAGE_MODEL", raising=False)
    limits, how = asyncio.run(usage.probe("tok"))
    assert how == "mini-requête claude-haiku-5-5" and limits["seven_day"]["utilization"] == 0.61
    monkeypatch.setenv("COCKPIT_USAGE_SONDE", "0")
    with pytest.raises(usage.UsageError, match="désactivée"):
        asyncio.run(usage.probe("tok"))


def test_sonde_refusee(monkeypatch):
    mock(monkeypatch, lambda req: httpx.Response(401, json={"error": "auth"}))
    with pytest.raises(usage.UsageError, match="HTTP 401"):
        asyncio.run(usage.probe("tok"))
