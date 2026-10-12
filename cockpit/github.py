"""Dépôts GitHub de l'utilisateur (token git injecté par Onyxia : GIT_PERSONAL_ACCESS_TOKEN)."""

from __future__ import annotations

import os
import re

import httpx

API = "https://api.github.com"
_NAME = re.compile(r"^[A-Za-z0-9._-]{1,100}$")


class GitHubError(Exception):
    pass


def _client() -> httpx.AsyncClient:
    token = os.environ.get("GIT_PERSONAL_ACCESS_TOKEN")
    if not token:
        raise GitHubError("aucun token git (GIT_PERSONAL_ACCESS_TOKEN) dans ce pod")
    return httpx.AsyncClient(base_url=API, timeout=20, headers={
        "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"})


async def depots(limit: int = 300) -> list[dict]:
    """Dépôts de l'utilisateur, du plus récemment poussé au plus ancien ([] sans token)."""
    if not os.environ.get("GIT_PERSONAL_ACCESS_TOKEN"):
        return []
    out: list[dict] = []
    async with _client() as http:
        for page in range(1, limit // 100 + 1):
            r = await http.get("/user/repos", params={
                "per_page": 100, "page": page, "sort": "pushed", "affiliation": "owner,collaborator,organization_member"})
            r.raise_for_status()
            batch = r.json()
            out += [{"name": x["full_name"], "url": x["html_url"], "private": x["private"], "branch": x["default_branch"],
                     "description": (x.get("description") or "")[:140], "pushed": x.get("pushed_at") or "",
                     "archived": x.get("archived", False)} for x in batch]
            if len(batch) < 100:
                break
    return out


def valider_nom(nom: str) -> str:
    nom = (nom or "").strip()
    if not _NAME.match(nom) or nom in {".", ".."} or nom.endswith(".git"):
        raise GitHubError("nom de dépôt invalide : lettres, chiffres, « . », « _ » et « - » (100 au plus)")
    return nom


async def creer_depot(nom: str, prive: bool = True, description: str = "") -> dict:
    """Crée un dépôt vide sur le compte de l'utilisateur ; renvoie {"name", "url", "private", "branch"}."""
    nom = valider_nom(nom)
    async with _client() as http:
        r = await http.post("/user/repos", json={"name": nom, "private": bool(prive),
                                                 "description": (description or "")[:350], "auto_init": False})
    if r.status_code == 422:
        raise GitHubError(f"le dépôt « {nom} » existe déjà sur ton compte (ou nom refusé par GitHub)")
    if r.status_code in (401, 403, 404):
        raise GitHubError("création refusée par GitHub : le token git d'Onyxia doit avoir le droit « Administration : "
                          "écriture » (token à grain fin) ou la portée « repo » (token classique)")
    if r.status_code >= 400:
        raise GitHubError(f"GitHub : HTTP {r.status_code} {r.text[:200]}")
    x = r.json()
    return {"name": x["full_name"], "url": x["html_url"], "private": x["private"], "branch": x.get("default_branch") or "main"}
