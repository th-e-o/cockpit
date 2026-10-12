"""Configuration d'un projet (.cockpit.yml), tests lancés après chaque tâche et suivi de la CI GitHub.

    # .cockpit.yml, à la racine du dépôt (l'agent peut le créer)
    test: pytest -q          # commande de test, lancée après chaque tâche qui modifie des fichiers
    apercu_port: 5000        # port du serveur de développement (aperçu)
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import yaml

from .policy import ALLOW, Policy

CONFIG_FILE = ".cockpit.yml"
TEST_TIMEOUT = int(os.environ.get("COCKPIT_TEST_TIMEOUT", "600"))
OUTPUT_TAIL = 4000


def config(cwd: str, override: dict | None = None) -> dict:
    """.cockpit.yml du dossier de travail, complété par la déclaration du projet (projects.yaml) qui l'emporte."""
    out: dict = {}
    try:
        raw = yaml.safe_load(Path(cwd, CONFIG_FILE).read_text()) or {}
        if isinstance(raw, dict):
            out = {k: raw[k] for k in ("test", "apercu_port") if k in raw}
    except (OSError, yaml.YAMLError):
        pass
    return out | {k: v for k, v in (override or {}).items() if v}


def test_command(cwd: str, policy: Policy, declared: str = "") -> tuple[str | None, str]:
    """Commande de test à lancer automatiquement, ou (None, raison).

    .cockpit.yml fait partie du dépôt (l'agent peut l'écrire) : la commande n'est lancée sans validation que si la
    politique l'autorise déjà telle quelle (pytest, npm test, python…).
    """
    cmd = str(config(cwd, {"test": declared}).get("test") or "").strip()
    if not cmd:
        return None, "aucune commande de test déclarée"
    d = policy.evaluate("Bash", {"command": cmd}, cwd)
    if d.action != ALLOW:
        return None, f"commande de test non lancée automatiquement ({d.reason}) : {cmd}"
    return cmd, ""


async def run_tests(cwd: str, cmd: str, timeout: float = TEST_TIMEOUT) -> dict:
    """Lance la commande de test ; renvoie {ok, command, duration_s, output} (fin de sortie seulement)."""
    start = time.monotonic()
    proc = await asyncio.create_subprocess_exec(
        "bash", "-c", cmd, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        stdin=asyncio.subprocess.DEVNULL, env={**os.environ, "CI": "1"}, start_new_session=True)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
        ok, text = proc.returncode == 0, out.decode(errors="replace")
    except asyncio.TimeoutError:
        proc.kill()
        ok, text = False, f"délai dépassé ({int(timeout)} s)"
    return {"ok": ok, "command": cmd, "duration_s": round(time.monotonic() - start),
            "output": text[-OUTPUT_TAIL:].strip()}


def fix_tests_step(result: dict) -> dict:
    """Prochaine étape proposée quand les tests échouent."""
    tail = result["output"][-1500:]
    return {"titre": "Corriger les tests",
            "instruction": f"Les tests échouent (`{result['command']}`). Trouve la cause et corrige-la, puis relance-les."
                           f"\n\nFin de la sortie :\n```\n{tail}\n```"}
