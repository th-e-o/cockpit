"""Usage du forfait Claude (session de 5 h, semaine) pour la barre d'usage du cockpit.

Trois sources, la plus récente l'emporte :
- l'interface d'usage du compte (celle de « /usage » dans Claude Code) : refusée au jeton de « claude setup-token » ;
- les en-têtes d'usage (anthropic-ratelimit-unified-5h/7d-*) que l'API renvoie à chaque requête, lus par une sonde :
  comptage de tokens (gratuit) ou, à défaut, mini-requête Haiku d'un token (COCKPIT_USAGE_SONDE=0 pour la couper) ;
- les événements RateLimitEvent émis par Claude Code pendant les tâches (seuils d'alerte, limite atteinte).

    python -m cockpit.usage     # diagnostic : valeurs lues et méthode utilisée (jamais le jeton)
"""

from __future__ import annotations

import os
import time
from datetime import datetime

import httpx

API = "https://api.anthropic.com"
USAGE_URL = f"{API}/api/oauth/usage"
PROBE_MODELS = [m for m in (os.environ.get("COCKPIT_USAGE_MODEL"), "claude-haiku-4-5", "claude-haiku-5-5") if m]
HEADER_KINDS = {"5h": "five_hour", "7d": "seven_day"}
KINDS = ("five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet")


class UsageError(Exception):
    pass


def _ts(value) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value)
    try:  # en-têtes de l'API : horodatage Unix en texte
        return int(float(value))
    except ValueError:
        pass
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


def from_headers(headers) -> dict[str, dict]:
    """En-têtes d'usage d'une réponse de l'API -> {type: limite}. Utilisation en fraction (pourcentage toléré)."""
    out = {}
    for key, kind in HEADER_KINDS.items():
        u = headers.get(f"anthropic-ratelimit-unified-{key}-utilization")
        if u is None:
            continue
        try:
            value = float(u)
        except ValueError:
            continue
        out[kind] = limit(kind, value / 100 if value > 1.5 else value, headers.get(f"anthropic-ratelimit-unified-{key}-reset"),
                          headers.get("anthropic-ratelimit-unified-status"))
    return out


async def probe(token: str | None = None) -> tuple[dict[str, dict], str]:
    """Lit l'usage dans les en-têtes de l'API : comptage de tokens (gratuit), sinon mini-requête d'un token."""
    token = token or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if not token:
        raise UsageError("pas de jeton OAuth Claude (CLAUDE_CODE_OAUTH_TOKEN)")
    headers = {"Authorization": f"Bearer {token}", "anthropic-version": "2023-06-01", "anthropic-beta": "oauth-2025-04-20"}
    messages = [{"role": "user", "content": "ok"}]
    system = "You are Claude Code, Anthropic's official CLI for Claude."
    last = None
    async with httpx.AsyncClient(base_url=API, timeout=20) as http:
        r = await http.post("/v1/messages/count_tokens", headers=headers,
                            json={"model": PROBE_MODELS[0], "system": system, "messages": messages})
        if limits := from_headers(r.headers):
            return limits, "comptage de tokens"
        last = r.status_code
        if os.environ.get("COCKPIT_USAGE_SONDE", "1") == "0":
            raise UsageError(f"pas d'en-têtes d'usage sur le comptage (HTTP {last}) et sonde désactivée")
        for model in PROBE_MODELS:
            r = await http.post("/v1/messages", headers=headers,
                                json={"model": model, "max_tokens": 1, "system": system, "messages": messages})
            if limits := from_headers(r.headers):
                return limits, f"mini-requête {model}"
            last = r.status_code
            if r.status_code not in (400, 404):  # modèle inconnu : essai suivant ; autre erreur : abandon
                break
    raise UsageError(f"aucun en-tête d'usage dans les réponses de l'API (HTTP {last})")


def from_event(info) -> dict | None:
    """RateLimitInfo du SDK (utilisation déjà en fraction) -> limite, ou None si le type est inconnu."""
    kind = getattr(info, "rate_limit_type", None)
    if kind not in KINDS:
        return None
    return limit(kind, getattr(info, "utilization", None), getattr(info, "resets_at", None), getattr(info, "status", None))


# Une réinitialisation n'est signalée que si la limite était presque épuisée : sinon la fenêtre de 5 h
# ferait une notification toutes les quelques heures.
RESET_NOTIFY_MIN = float(os.environ.get("COCKPIT_RESET_NOTIFY_MIN", "0.8"))


def reset_detected(old: dict, new: dict, now: float) -> bool:
    """La fenêtre de la limite s'est-elle renouvelée entre deux lectures ? (old : lecture précédente, new : brute)"""
    old_at, old_util = old.get("resets_at"), old.get("utilization")
    if not old_at or now < old_at - 120:  # fenêtre inconnue ou pas encore écoulée
        return False
    if (old_util or 0) < RESET_NOTIFY_MIN and old.get("status") != "rejected":
        return False
    new_at, new_util = new.get("resets_at"), new.get("utilization")
    return bool((new_at and new_at > old_at + 60) or (new_util is not None and old_util is not None and new_util <= old_util - 0.3))


class Usage:
    """Dernier état connu des limites, fusionné depuis les deux sources."""

    def __init__(self) -> None:
        self.limits: dict[str, dict] = {}
        self.ts = 0.0
        self.source = ""

    def update(self, limits: dict[str, dict], source: str) -> list[str]:
        """Intègre de nouvelles limites ; renvoie les types de limite qui viennent d'être réinitialisés."""
        resets, now = [], time.time()
        for kind, new in limits.items():
            old = self.limits.get(kind, {})
            if reset_detected(old, new, now):
                resets.append(kind)
            if new.get("utilization") is None:  # un événement sans pourcentage garde le dernier connu
                new = {**new, "utilization": old.get("utilization")}
            self.limits[kind] = new
        self.ts, self.source = time.time(), source
        return resets

    def next_reset(self) -> float | None:
        """Prochaine heure de réinitialisation connue (pour relire l'usage juste après)."""
        now = time.time()
        return min((v["resets_at"] for v in self.limits.values() if v.get("resets_at") and v["resets_at"] > now), default=None)

    def public(self) -> dict | None:
        if not self.limits:
            return None
        return {"limits": [self.limits[k] for k in KINDS if k in self.limits], "ts": int(self.ts), "source": self.source}


async def _diagnostic() -> None:
    try:
        print("Interface d'usage du compte :", {k: round(v["utilization"] * 100) for k, v in (await fetch()).items()})
    except Exception as e:
        print("Interface d'usage du compte : indisponible —", e)
    try:
        limits, how = await probe()
        for v in limits.values():
            reset = datetime.fromtimestamp(v["resets_at"]).strftime("%a %H:%M") if v.get("resets_at") else "?"
            print(f"En-têtes de l'API ({how}) : {v['type']} = {v['utilization'] * 100:.0f} % (réinitialisation {reset})")
    except Exception as e:
        print("En-têtes de l'API : indisponibles —", e)


if __name__ == "__main__":
    import asyncio

    asyncio.run(_diagnostic())
