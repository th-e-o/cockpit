"""Ressources du pod pour le tableau de bord : CPU et mémoire (cgroup v2, comme kubectl top), disque de travail."""

from __future__ import annotations

import os
import shutil
import time
from pathlib import Path

CGROUP = Path(os.environ.get("COCKPIT_CGROUP", "/sys/fs/cgroup"))
WORK = Path(os.environ.get("COCKPIT_WORK_DIR") or Path.home() / "work")


def _read(name: str) -> str | None:
    try:
        return (CGROUP / name).read_text().strip()
    except OSError:
        return None


def _stat(name: str) -> dict[str, int]:
    out = {}
    for line in (_read(name) or "").splitlines():
        k, _, v = line.partition(" ")
        if v.isdigit():
            out[k] = int(v)
    return out


def cpu_limit() -> float:
    """Nombre de cœurs alloués (limite du conteneur, sinon cœurs visibles)."""
    quota, _, period = (_read("cpu.max") or "max").partition(" ")
    if quota != "max" and period:
        return int(quota) / int(period)
    try:
        return float(len(os.sched_getaffinity(0)))
    except AttributeError:
        return float(os.cpu_count() or 1)


def memory() -> tuple[int | None, int | None]:
    """(utilisée, limite) en octets ; utilisée = ensemble de travail (hors cache de fichiers inactif)."""
    current = _read("memory.current")
    if current is None:
        return None, None
    used = int(current) - _stat("memory.stat").get("inactive_file", 0)
    limit = _read("memory.max")
    if not limit or limit == "max":
        try:
            kb = next(int(l.split()[1]) for l in Path("/proc/meminfo").read_text().splitlines() if l.startswith("MemTotal"))
            limit = str(kb * 1024)
        except (OSError, StopIteration):
            return max(used, 0), None
    return max(used, 0), int(limit)


class PodState:
    def __init__(self) -> None:
        self._cpu_prev: tuple[float, int] | None = None

    def cpu(self) -> float | None:
        """Part de la limite CPU utilisée depuis le dernier appel (0-1) ; None au premier appel."""
        usage = _stat("cpu.stat").get("usage_usec")
        if usage is None:
            return None
        now, prev = time.monotonic(), self._cpu_prev
        self._cpu_prev = (now, usage)
        if not prev or now - prev[0] < 1:
            return None
        return max(0.0, (usage - prev[1]) / 1e6 / (now - prev[0]) / cpu_limit())

    def resources(self) -> dict:
        used, limit = memory()
        out: dict = {"cpu": self.cpu(), "cpu_cores": round(cpu_limit(), 2), "mem_used": used, "mem_limit": limit}
        try:
            d = shutil.disk_usage(WORK if WORK.exists() else Path.home())
            out |= {"disk_used": d.used, "disk_total": d.total}
        except OSError:
            pass
        return out
