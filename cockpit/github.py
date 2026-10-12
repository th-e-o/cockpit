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


_REMOTE = re.compile(r"github\.com[/:]([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?/?$")


def owner_repo(remote_url: str) -> tuple[str, str] | None:
    """(propriétaire, dépôt) d'une URL GitHub (https ou ssh), sans identifiants ; None sinon."""
    m = _REMOTE.search(re.sub(r"//[^@/]*@", "//", remote_url.strip()))
    return (m.group(1), m.group(2)) if m else None


async def creer_pr(owner: str, repo: str, head: str, titre: str, description: str = "", base: str = "",
                   brouillon: bool = False) -> dict:
    """Ouvre une pull request (base : branche par défaut du dépôt) ; renvoie {"number", "url", "title"}."""
    async with _client() as http:
        if not base:
            r = await http.get(f"/repos/{owner}/{repo}")
            if r.status_code >= 400:
                raise GitHubError(f"dépôt {owner}/{repo} inaccessible (HTTP {r.status_code})")
            base = r.json().get("default_branch") or "main"
        if head == base:
            raise GitHubError(f"la branche {head} est la branche de base : crée d'abord une branche de travail")
        r = await http.post(f"/repos/{owner}/{repo}/pulls", json={
            "title": titre[:250], "body": description[:60000], "head": head, "base": base, "draft": bool(brouillon)})
    if r.status_code == 422:
        detail = "; ".join(e.get("message", "") for e in r.json().get("errors", [])) or r.text[:200]
        raise GitHubError(f"pull request refusée : {detail}")
    if r.status_code >= 400:
        raise GitHubError(f"GitHub : HTTP {r.status_code} {r.text[:200]}")
    x = r.json()
    return {"number": x["number"], "url": x["html_url"], "title": x["title"], "base": base, "head": head}


async def etat_ci(owner: str, repo: str, sha: str) -> dict:
    """État de la CI d'un commit : {"state": none|pending|success|failure, "checks": [...], "url"}."""
    async with _client() as http:
        r = await http.get(f"/repos/{owner}/{repo}/commits/{sha}/check-runs", params={"per_page": 50})
        runs = r.json().get("check_runs", []) if r.status_code == 200 else []
        r2 = await http.get(f"/repos/{owner}/{repo}/commits/{sha}/status")
        statuses = r2.json().get("statuses", []) if r2.status_code == 200 else []
    checks = [{"name": c["name"], "status": c["status"], "conclusion": c.get("conclusion"), "url": c.get("html_url")}
              for c in runs]
    checks += [{"name": s["context"], "status": "completed" if s["state"] != "pending" else "in_progress",
                "conclusion": {"success": "success", "pending": None}.get(s["state"], "failure"), "url": s.get("target_url")}
               for s in statuses]
    if not checks:
        state = "none"
    elif any(c["status"] != "completed" for c in checks):
        state = "pending"
    elif all(c["conclusion"] in ("success", "neutral", "skipped") for c in checks):
        state = "success"
    else:
        state = "failure"
    failed = [c for c in checks if c["conclusion"] not in (None, "success", "neutral", "skipped")]
    return {"state": state, "sha": sha[:7], "checks": checks[:20],
            "url": (failed or checks or [{}])[0].get("url") or f"https://github.com/{owner}/{repo}/commit/{sha}"}


async def chercher(requete: str, limite: int = 10) -> list[dict]:
    """Dépôts de l'utilisateur liés à une requête : nom et description, README, puis code (recherche GitHub).

    Chaque résultat indique pourquoi il remonte (« nom », « README », « code : chemin/fichier.py »).
    """
    requete = " ".join(requete.split())[:200]
    if not requete:
        raise GitHubError("requête vide")
    mots = [m.lower() for m in re.findall(r"\w{3,}", requete)]
    found: dict[str, dict] = {}

    def add(x: dict, why: str) -> None:
        if x["name"] not in found:
            found[x["name"]] = {**x, "why": why}

    # 1. Liste des dépôts (y compris organisations et collaborations) filtrée sur le nom et la description
    mine = await depots()
    for x in mine:
        texte = f"{x['name']} {x['description']}".lower()
        if mots and any(m in texte for m in mots):
            add(x, "nom ou description")
    by_name = {x["name"]: x for x in mine}
    async with _client() as http:
        r = await http.get("/user")
        login = r.json().get("login") if r.status_code == 200 else None
        if login:
            # 2. README et description (recherche de dépôts), 3. code (recherche de code)
            r = await http.get("/search/repositories", params={"q": f"{requete} user:{login} fork:true in:name,description,readme",
                                                               "per_page": limite})
            for item in r.json().get("items", []) if r.status_code == 200 else []:
                add(by_name.get(item["full_name"]) or {"name": item["full_name"], "url": item["html_url"],
                    "private": item["private"], "branch": item.get("default_branch") or "main",
                    "description": (item.get("description") or "")[:140], "pushed": item.get("pushed_at") or ""}, "README")
            r = await http.get("/search/code", params={"q": f"{requete} user:{login}", "per_page": limite})
            for item in r.json().get("items", []) if r.status_code == 200 else []:
                repo = item.get("repository", {})
                add(by_name.get(repo.get("full_name")) or {"name": repo.get("full_name"), "url": repo.get("html_url"),
                    "private": repo.get("private"), "branch": "", "description": (repo.get("description") or "")[:140],
                    "pushed": ""}, f"code : {item.get('path')}")
    return sorted(found.values(), key=lambda x: x.get("pushed") or "", reverse=True)[:limite]
