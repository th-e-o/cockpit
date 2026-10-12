"""Aperçu visuel : captures d'écran de l'application en développement, et URL d'aperçu publique du pod.

Les captures ne visent que le pod lui-même (localhost) : l'outil ne sert pas à naviguer sur Internet.
"""

from __future__ import annotations

import asyncio
import json
import os
from urllib.parse import urlparse

LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]"}
PREVIEW_PORT = int(os.environ.get("COCKPIT_APERCU_PORT", "5000"))
MAX_BYTES = 1_500_000


class ApercuError(Exception):
    pass


def check_url(url: str) -> str:
    u = urlparse(url.strip())
    if u.scheme not in ("http", "https") or (u.hostname or "") not in LOCAL_HOSTS:
        raise ApercuError("seules les adresses du pod sont acceptées (http://localhost:<port>/…)")
    return url.strip()


async def capture(url: str, mobile: bool = True, full_page: bool = False, wait_ms: int = 800) -> bytes:
    """Capture JPEG d'une page servie dans le pod (vue téléphone par défaut)."""
    url = check_url(url)
    try:
        from playwright.async_api import async_playwright
    except ImportError as e:
        raise ApercuError("Playwright n'est pas installé dans ce pod") from e
    viewport = {"width": 390, "height": 844} if mobile else {"width": 1280, "height": 800}
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch()
            try:
                page = await browser.new_page(viewport=viewport, device_scale_factor=2 if mobile else 1,
                                              is_mobile=mobile, has_touch=mobile)
                await page.goto(url, wait_until="networkidle", timeout=20000)
                await page.wait_for_timeout(wait_ms)
                for quality in (70, 50, 35):
                    data = await page.screenshot(type="jpeg", quality=quality, full_page=full_page)
                    if len(data) <= MAX_BYTES:
                        return data
                raise ApercuError("capture trop volumineuse : essaie sans page_entiere")
            finally:
                await browser.close()
    except ApercuError:
        raise
    except Exception as e:
        hint = " (navigateur absent : .venv/bin/playwright install chromium)" if "Executable doesn't exist" in str(e) else ""
        raise ApercuError(f"capture impossible : {str(e).splitlines()[0][:200]}{hint}") from e


async def _kubectl(*args: str) -> dict | None:
    try:
        proc = await asyncio.create_subprocess_exec("kubectl", *args, "-o", "json", stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), 20)
        return json.loads(out) if proc.returncode == 0 else None
    except (OSError, asyncio.TimeoutError, json.JSONDecodeError):
        return None


async def preview_url() -> dict | None:
    """URL publique du port utilisateur de ce pod (ingress « -user » de sa release Onyxia), ou None."""
    pod = await _kubectl("get", "pod", os.environ.get("HOSTNAME", ""))
    release = ((pod or {}).get("metadata", {}).get("labels") or {}).get("app.kubernetes.io/instance")
    if not release:
        return None
    ingresses = await _kubectl("get", "ingress", "-l", f"app.kubernetes.io/instance={release}") or {"items": []}
    for ing in ingresses.get("items", []):
        for rule in ing.get("spec", {}).get("rules", []):
            for path in rule.get("http", {}).get("paths", []):
                port = path.get("backend", {}).get("service", {}).get("port", {}).get("number")
                if port and port != 8080 and "-user" in rule.get("host", ""):
                    return {"url": f"https://{rule['host']}{path.get('path', '/')}", "port": port}
    return None
