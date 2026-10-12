"""Usage du forfait Claude (session de 5 h, semaine) pour la barre d'usage du cockpit.

Deux sources, la plus récente l'emporte :
- l'interface d'usage du compte (celle de « /usage » dans Claude Code), interrogée avec le jeton OAuth du worker ;
  non documentée publiquement : en cas de refus, le worker n'utilise plus que la source suivante ;
- les événements RateLimitEvent émis par Claude Code pendant les tâches (seuils d'alerte, limite atteinte).
"""

from __future__ import annotations

import os
import time
from datetime import datetime

import httpx

USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
KINDS = ("five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet")


class UsageError(Exception):
    pass


def _ts(value) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value)
    try:
        return int(datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def limit(kind: str, utilization: float | None, resets_at=None, status: str | None = None) -> dict:
    return {"type": kind, "utilization": None if utilization is None else max(0.0, min(1.0, float(utilization))),
            "resets_at": _ts(resets_at), **({"status": status} if status else {})}


def parse(body: dict) -> dict[str, dict]:
    """Réponse de l'interface d'usage (utilisation en pourcentage) -> {type: limite (utilisation 0-1)}."""
    out = {}
    for kind in KINDS:
        v = body.get(kind)
        if isinstance(v, dict) and v.get("utilization") is not None:
            out[kind] = limit(kind, float(v["utilization"]) / 100, v.get("resets_at"))
    return out


async def fetch(token: str | None = None) -> dict[str, dict]:
    token = token or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if not token:
        raise UsageError("pas de jeton OAuth Claude (CLAUDE_CODE_OAUTH_TOKEN)")
    async with httpx.AsyncClient(timeout=15) as http:
        r = await http.get(USAGE_URL, headers={"Authorization": f"Bearer {token}", "anthropic-beta": "oauth-2025-04-20"})
    if r.status_code != 200:
        raise UsageError(f"HTTP {r.status_code}")
    limits = parse(r.json())
    if not limits:
        raise UsageError("réponse sans données d'usage")
    return limits


def from_event(info) -> dict | None:
    """RateLimitInfo du SDK (utilisation déjà en fraction) -> limite, ou None si le type est inconnu."""
    kind = getattr(info, "rate_limit_type", None)
    if kind not in KINDS:
        return None
    return limit(kind, getattr(info, "utilization", None), getattr(info, "resets_at", None), getattr(info, "status", None))


class Usage:
    """Dernier état connu des limites, fusionné depuis les deux sources."""

    def __init__(self) -> None:
        self.limits: dict[str, dict] = {}
        self.ts = 0.0
        self.source = ""

    def update(self, limits: dict[str, dict], source: str) -> None:
        for kind, new in limits.items():
            old = self.limits.get(kind, {})
            if new.get("utilization") is None:  # un événement sans pourcentage garde le dernier connu
                new = {**new, "utilization": old.get("utilization")}
            self.limits[kind] = new
        self.ts, self.source = time.time(), source

    def public(self) -> dict | None:
        if not self.limits:
            return None
        return {"limits": [self.limits[k] for k in KINDS if k in self.limits], "ts": int(self.ts), "source": self.source}
