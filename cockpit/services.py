"""Outils de gestion des services Onyxia, exposés aux agents de l'orchestrateur (serveur MCP en processus).

Seul le pod orchestrateur (rôle Kubernetes admin) peut installer des releases helm. Les agents n'ont pas de
`helm` brut : ils passent par ces trois outils, qui valident les paramètres, ne touchent qu'aux releases
préfixées `cockpit-` et dont le lancement/l'arrêt est toujours soumis à validation (voir config/policy.yaml).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path

import yaml
from claude_agent_sdk import create_sdk_mcp_server, tool

PREFIX = "cockpit-"
CHART = "vscode-python"
CHART_VERSION = "2.6.6"
CHART_REPO = "https://inseefrlab.github.io/helm-charts-interactive-services"
INIT_SCRIPT = "https://raw.githubusercontent.com/th-e-o/cockpit/main/scripts/onyxia-init.sh"
VAULT_SECRET = "Claude"
SERVER_NAME = "services"
TOOL_PREFIX = f"mcp__{SERVER_NAME}__"
LAUNCH_TOOL, LIST_TOOL, STOP_TOOL = (f"{TOOL_PREFIX}{n}" for n in ("lancer_service", "lister_services", "arreter_service"))

_NAME = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,28}[a-z0-9])?$")
_REPO = re.compile(r"^https://[A-Za-z0-9._~/-]+$")  # ni espace, ni virgule, ni identifiants dans l'URL
_BRANCH = re.compile(r"^[A-Za-z0-9._/-]{1,100}$")
_CPU = re.compile(r"^(\d{1,2}|\d{1,5}m)$")
_QTY = re.compile(r"^\d{1,4}(Mi|Gi|Ti)$")


class ServiceError(Exception):
    pass


@dataclass
class Launch:
    nom: str
    depot_git: str = ""  # vide : nouveau projet, dossier vide initialisé avec git
    branche: str = ""
    cpu: str = "2"
    memoire: str = "8Gi"
    disque: str = "10Gi"
    gpu: bool = False

    @property
    def release(self) -> str:
        return PREFIX + self.nom

    def validate(self) -> "Launch":
        if not _NAME.match(self.nom):
            raise ServiceError("nom invalide : 1 à 30 caractères, minuscules, chiffres et tirets")
        if self.depot_git and (not _REPO.match(self.depot_git) or ".." in self.depot_git):
            raise ServiceError("dépôt invalide : URL https:// sans identifiants")
        if self.branche and not self.depot_git:
            raise ServiceError("une branche n'a de sens qu'avec un dépôt")
        if self.branche and not _BRANCH.match(self.branche):
            raise ServiceError("nom de branche invalide")
        if not _CPU.match(self.cpu):
            raise ServiceError("cpu invalide (ex. 2 ou 500m)")
        for label, v in (("mémoire", self.memoire), ("disque", self.disque)):
            if not _QTY.match(v):
                raise ServiceError(f"{label} invalide (ex. 8Gi)")
        return self


def helm_values(p: Launch, vault: dict[str, str]) -> dict:
    """Valeurs du chart `vscode-python` pour un pod de projet : rôle `view`, clone du dépôt, init du worker."""
    limits = {"cpu": p.cpu, "memory": p.memoire}
    if p.gpu:
        limits["nvidia.com/gpu"] = "1"
    values: dict = {
        "kubernetes": {"enabled": True, "role": "view"},  # jamais admin pour un projet
        "git": {"enabled": True, "repository": p.depot_git, "branch": p.branche},
        "init": {"personalInit": INIT_SCRIPT, "personalInitArgs": p.nom},
        "resources": {"requests": {"cpu": p.cpu, "memory": p.memoire}, "limits": limits},
        "persistence": {"enabled": True, "size": p.disque},
    }
    if vault.get("token"):
        # Onyxia injecte ces valeurs lui-même ; en lançant helm directement, on reprend celles de l'orchestrateur.
        values["vault"] = {"enabled": True, "secret": VAULT_SECRET, **vault}
    return values


def helm_args(p: Launch, namespace: str, values_file: str) -> list[str]:
    return ["helm", "upgrade", "--install", p.release, CHART, "--repo", CHART_REPO, "--version", CHART_VERSION,
            "-n", namespace, "-f", values_file]


def namespace() -> str:
    if ns := os.environ.get("COCKPIT_NAMESPACE") or os.environ.get("KUBERNETES_NAMESPACE"):
        return ns
    f = Path("/var/run/secrets/kubernetes.io/serviceaccount/namespace")
    if f.exists():
        return f.read_text().strip()
    raise ServiceError("namespace Kubernetes introuvable (définir COCKPIT_NAMESPACE)")


def vault_values() -> dict[str, str]:
    env = os.environ
    keys = {"url": "VAULT_ADDR", "token": "VAULT_TOKEN", "mount": "VAULT_MOUNT", "directory": "VAULT_TOP_DIR"}
    return {k: env[v] for k, v in keys.items() if env.get(v)}


async def _run(*cmd: str, timeout: float = 300) -> str:
    proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise ServiceError(f"{cmd[0]} : délai dépassé")
    text = out.decode(errors="replace").strip()
    if proc.returncode:
        raise ServiceError(f"{cmd[0]} a échoué : {text[-600:]}")
    return text


async def quota(ns: str) -> dict:
    """Quota du namespace : {ressource: {"used": ..., "hard": ...}} (vide si illisible)."""
    try:
        items = json.loads(await _run("kubectl", "get", "resourcequota", "-n", ns, "-o", "json", timeout=30))["items"]
    except Exception:
        return {}
    out: dict = {}
    for it in items:
        status = it.get("status", {})
        for k, hard in status.get("hard", {}).items():
            out[k] = {"used": status.get("used", {}).get(k), "hard": hard}
    return out


def _quota_summary(q: dict) -> str:
    keys = ("count/pods", "pods", "requests.cpu", "requests.memory", "requests.storage", "requests.nvidia.com/gpu")
    parts = [f"{k.removeprefix('requests.').removeprefix('count/')} {q[k]['used']}/{q[k]['hard']}" for k in keys if k in q]
    return ", ".join(parts) or "quota illisible"


async def lister() -> list[dict]:
    ns = namespace()
    releases = json.loads(await _run("helm", "list", "-n", ns, "-o", "json", timeout=60) or "[]")
    mine = [r for r in releases if r["name"].startswith(PREFIX)]
    pods: dict[str, list[str]] = {}
    try:
        for pod in json.loads(await _run("kubectl", "get", "pods", "-n", ns, "-o", "json", timeout=30))["items"]:
            rel = pod["metadata"].get("labels", {}).get("app.kubernetes.io/instance", "")
            ready = all(c.get("ready") for c in pod["status"].get("containerStatuses", [])) and bool(
                pod["status"].get("containerStatuses"))
            pods.setdefault(rel, []).append("prêt" if ready else pod["status"].get("phase", "?"))
    except ServiceError:
        pass
    return [{"nom": r["name"].removeprefix(PREFIX), "release": r["name"], "etat_helm": r["status"],
             "pods": pods.get(r["name"], []), "maj": r.get("updated", "")[:19]} for r in mine]


async def lancer(p: Launch) -> str:
    p.validate()
    ns = namespace()
    fd, path = tempfile.mkstemp(suffix=".yaml")  # 0600 : contient le jeton Vault
    try:
        with os.fdopen(fd, "w") as f:
            yaml.safe_dump(helm_values(p, vault_values()), f)
        await _run(*helm_args(p, ns, path))
    finally:
        os.unlink(path)
    return (f"Service {p.release} lancé dans {ns}. Le pod démarre : le projet « {p.nom} » apparaîtra dans le "
            f"cockpit dès que son worker sera connecté (1 à 3 minutes).")


async def arreter(nom: str) -> str:
    if not _NAME.match(nom):
        raise ServiceError("nom invalide")
    ns = namespace()
    release = PREFIX + nom
    if release not in {r["release"] for r in await lister()}:
        raise ServiceError(f"aucune release {release} : seuls les services lancés par le cockpit peuvent être arrêtés")
    await _run("helm", "uninstall", release, "-n", ns, timeout=120)
    return f"Service {release} arrêté."


async def describe(tool_name: str, tool_input: dict) -> str | None:
    """Texte de la carte de validation (dépôt, ressources, quota restant) ; None si ce n'est pas un outil de services."""
    if tool_name == STOP_TOOL:
        return f"Arrêter le service {PREFIX}{tool_input.get('nom', '?')} (suppression de la release et de son stockage)"
    if tool_name != LAUNCH_TOOL:
        return None
    try:
        p = _launch_from(tool_input)
        q = _quota_summary(await quota(namespace()))
    except Exception as e:
        return f"Lancer un service ({e})"
    gpu = " + 1 GPU" if p.gpu else ""
    source = f"depuis {p.depot_git}{' @' + p.branche if p.branche else ''}" if p.depot_git else "sans dépôt (dossier vide)"
    return f"Lancer le service {p.release} {source} : {p.cpu} CPU, {p.memoire}, {p.disque}{gpu}. Quota : {q}"


def _launch_from(args: dict) -> Launch:
    return Launch(nom=args.get("nom", ""), depot_git=args.get("depot_git", ""), branche=args.get("branche", ""),
                  cpu=str(args.get("cpu", "2")), memoire=args.get("memoire", "8Gi"), disque=args.get("disque", "10Gi"),
                  gpu=bool(args.get("gpu", False)))


def _reply(text: str, error: bool = False) -> dict:
    return {"content": [{"type": "text", "text": text}], **({"is_error": True} if error else {})}


@tool("lancer_service",
      "Lance un service Onyxia (VSCode + worker cockpit) qui devient un projet pilotable. depot_git (https) est "
      "facultatif : sans dépôt, le projet démarre dans un dossier vide initialisé avec git. Seul nom est obligatoire "
      "(minuscules, chiffres, tirets). Nécessite une validation sur le téléphone. Ressources par défaut : 2 CPU, 8Gi, 10Gi.",
      {"nom": str, "depot_git": str, "branche": str, "cpu": str, "memoire": str, "disque": str, "gpu": bool})
async def lancer_service(args: dict) -> dict:
    try:
        return _reply(await lancer(_launch_from(args)))
    except ServiceError as e:
        return _reply(str(e), error=True)


@tool("lister_services", "Liste les services lancés par le cockpit (état des pods) et le quota du namespace.", {})
async def lister_services(args: dict) -> dict:
    try:
        return _reply(json.dumps({"services": await lister(), "quota": _quota_summary(await quota(namespace()))},
                                 ensure_ascii=False))
    except ServiceError as e:
        return _reply(str(e), error=True)


@tool("arreter_service", "Arrête un service lancé par le cockpit (helm uninstall). Nécessite une validation.", {"nom": str})
async def arreter_service(args: dict) -> dict:
    try:
        return _reply(await arreter(args.get("nom", "")))
    except ServiceError as e:
        return _reply(str(e), error=True)


def server():
    return create_sdk_mcp_server(SERVER_NAME, tools=[lancer_service, lister_services, arreter_service])
