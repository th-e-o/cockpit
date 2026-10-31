"""État git d'un dossier de travail : instantanés avant/après une tâche, résumé des modifications, worktrees.

L'instantané est un arbre git du dossier entier (fichiers suivis + nouveaux fichiers non ignorés), écrit avec un
index temporaire : le dépôt de l'utilisateur (index, branche, stash) n'est jamais modifié. Comparer l'arbre d'avant
et celui d'après donne exactement ce que l'agent a changé, même si le dossier n'était pas propre au départ.
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

WORKTREES = Path(os.environ.get("COCKPIT_WORKTREES") or Path.home() / "work" / ".cockpit-worktrees")
BIG_FILE = 5 * 1024 * 1024  # nouveaux fichiers plus gros : ignorés par l'instantané (données, modèles…)
MAX_FILES = 60
PATCH_FILE = 6000  # caractères de diff par fichier
PATCH_TOTAL = 40000


class GitError(Exception):
    pass


async def git(cwd: str | Path, *args: str, env: dict | None = None, timeout: float = 60,
              stdin: bytes | None = None) -> tuple[int, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", "-c", "core.quotepath=off", *args, cwd=str(cwd),
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0", **(env or {})})
    except (FileNotFoundError, NotADirectoryError):
        return 127, ""
    try:
        out, _ = await asyncio.wait_for(proc.communicate(stdin), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()  # pas de zombie
        return 124, "délai dépassé"
    return proc.returncode or 0, out.decode(errors="replace").strip()


async def toplevel(cwd: str | Path) -> str | None:
    rc, out = await git(cwd, "rev-parse", "--show-toplevel")
    return out if rc == 0 and out else None


async def head(cwd: str | Path) -> str | None:
    rc, out = await git(cwd, "rev-parse", "--verify", "-q", "HEAD")
    return out if rc == 0 and out else None


@dataclass
class Snapshot:
    top: str
    tree: str
    head: str | None


async def snapshot(cwd: str | Path) -> Snapshot | None:
    """Arbre git du dossier tel qu'il est (None hors dépôt git ou en cas d'échec)."""
    top = await toplevel(cwd)
    if not top:
        return None
    rc, index = await git(top, "rev-parse", "--git-path", "index")
    if rc:
        return None
    index_path = Path(top, index)
    with tempfile.TemporaryDirectory(prefix="cockpit-idx-") as tmp:
        tmp_index = Path(tmp, "index")
        if index_path.exists():
            shutil.copyfile(index_path, tmp_index)  # cache des stat : seuls les fichiers modifiés sont relus
        env = {"GIT_INDEX_FILE": str(tmp_index)}
        if (await git(top, "add", "-u", env=env))[0]:
            return None
        rc, others = await git(top, "ls-files", "-z", "--others", "--exclude-standard")
        small = [p for p in others.split("\0") if p and _size(Path(top, p)) <= BIG_FILE]
        if small:
            rc, _ = await git(top, "add", "--pathspec-from-file=-", "--pathspec-file-nul", env=env,
                              stdin="\0".join(small).encode())
            if rc:
                return None
        rc, tree = await git(top, "write-tree", env=env)
    return Snapshot(top, tree, await head(top)) if rc == 0 else None


def _size(p: Path) -> int:
    try:
        return p.lstat().st_size
    except OSError:
        return 0


async def changes(before: Snapshot, after: Snapshot) -> tuple[dict, dict[str, str]]:
    """Résumé des modifications entre deux instantanés, et diff par fichier (tronqué)."""
    top = after.top
    _, numstat = await git(top, "diff", "--numstat", "--no-renames", before.tree, after.tree)
    _, names = await git(top, "diff", "--name-status", "--no-renames", before.tree, after.tree)
    status = dict(reversed(line.split("\t", 1)) for line in names.splitlines() if "\t" in line)
    files, added, removed = [], 0, 0
    for line in numstat.splitlines():
        a, r, path = line.split("\t", 2)
        binary = a == "-"
        files.append({"path": path, "status": status.get(path, "M")[:1], "added": 0 if binary else int(a),
                      "removed": 0 if binary else int(r), **({"binary": True} if binary else {})})
        added += 0 if binary else int(a)
        removed += 0 if binary else int(r)

    patches: dict[str, str] = {}
    if files:
        _, patch = await git(top, "diff", "--no-color", "--no-renames", "-U2", before.tree, after.tree)
        budget = PATCH_TOTAL
        # Les blocs « diff --git » sont dans le même ordre que --numstat.
        for f, chunk in zip(files, re.split(r"^(?=diff --git )", patch, flags=re.M)[1:]):
            body = chunk.split("\n@@", 1)
            text = "@@" + body[1] if len(body) == 2 else ""
            if budget <= 0 or not text:
                continue
            text = text[: min(PATCH_FILE, budget)] + ("\n…" if len(text) > min(PATCH_FILE, budget) else "")
            patches[f["path"]] = text
            budget -= len(text)

    commits = []
    if after.head and after.head != before.head:
        rng = f"{before.head}..{after.head}" if before.head else after.head
        _, log = await git(top, "log", "--format=%h%x09%s", "-n", "20", rng)
        commits = [dict(zip(("sha", "subject"), c.split("\t", 1))) for c in log.splitlines() if "\t" in c]

    summary = {"files": files[:MAX_FILES], "file_count": len(files), "added": added, "removed": removed,
               "commits": commits, **await branch_info(top)}
    return summary, patches


async def branch_info(cwd: str | Path) -> dict:
    """Branche courante, branche amont et nombre de commits non poussés."""
    rc, branch = await git(cwd, "rev-parse", "--abbrev-ref", "HEAD")
    info: dict = {"branch": branch if rc == 0 else None}
    rc, up = await git(cwd, "rev-parse", "--abbrev-ref", "@{u}")
    if rc == 0 and up:
        info["upstream"] = up
        rc, counts = await git(cwd, "rev-list", "--left-right", "--count", "@{u}...HEAD")
        if rc == 0 and counts:
            behind, ahead = (int(x) for x in counts.split())
            info |= {"ahead": ahead, "behind": behind}
    return info


async def repo_status(cwd: str | Path) -> dict | None:
    """État d'un projet pour le tableau de bord (None hors dépôt git)."""
    top = await toplevel(cwd)
    if not top:
        return None
    _, porcelain = await git(top, "status", "--porcelain")
    info = {"dirty": len(porcelain.splitlines()), **await branch_info(top)}
    rc, last = await git(top, "log", "-1", "--format=%h%x09%ct%x09%s")
    if rc == 0 and last.count("\t") >= 2:
        sha, ts, subject = last.split("\t", 2)
        info["last_commit"] = {"sha": sha, "ts": int(ts), "subject": subject[:120]}
    return info


# ---------------------------------------------------------------- worktrees (agents en parallèle)

def branch_name(conversation: str) -> str:
    return f"cockpit/{conversation}"


def worktree_path(project: str, conversation: str) -> Path:
    return WORKTREES / f"{project}-{conversation}"


async def add_worktree(cwd: str, project: str, conversation: str) -> str:
    """Worktree dédié à une conversation (créé s'il n'existe pas) ; renvoie le dossier de travail de l'agent."""
    top = await toplevel(cwd)
    if not top:
        raise GitError(f"{cwd} n'est pas un dépôt git")
    path, branch = worktree_path(project, conversation), branch_name(conversation)
    if not path.exists():
        if not await head(top):
            raise GitError("dépôt sans commit : impossible de créer un worktree")
        path.parent.mkdir(parents=True, exist_ok=True)
        exists = (await git(top, "rev-parse", "--verify", "-q", f"refs/heads/{branch}"))[0] == 0
        args = [str(path), branch] if exists else ["-b", branch, str(path), "HEAD"]
        rc, out = await git(top, "worktree", "add", "-q", *args)
        if rc:
            raise GitError(f"git worktree add : {out[-300:]}")
    return str(path / os.path.relpath(Path(cwd).resolve(), Path(top).resolve())).removesuffix("/.")


async def merge_worktree(cwd: str, project: str, conversation: str) -> str:
    """Fusionne la branche du worktree dans la branche courante du dossier principal, puis supprime le worktree."""
    top = await toplevel(cwd)
    path, branch = worktree_path(project, conversation), branch_name(conversation)
    if not top or not path.exists():
        raise GitError("worktree introuvable")
    _, dirty = await git(path, "status", "--porcelain")
    if dirty:
        rc, out = await git(path, "add", "-A")
        rc, out = (rc, out) if rc else await git(path, "commit", "-q", "-m", f"Cockpit : travail de la conversation {conversation}")
        if rc:
            raise GitError(f"commit du travail en cours impossible : {out[-300:]}")
    _, target = await git(top, "rev-parse", "--abbrev-ref", "HEAD")
    rc, out = await git(top, "merge", "--no-ff", "--no-edit", branch)
    if rc:
        await git(top, "merge", "--abort")
        raise GitError(f"fusion impossible dans {target} (conflit ou modifications locales) : {out[-400:]}")
    await remove_worktree(cwd, project, conversation, force=False)
    return f"Branche {branch} fusionnée dans {target}."


async def remove_worktree(cwd: str, project: str, conversation: str, force: bool = True) -> str:
    top = await toplevel(cwd)
    path, branch = worktree_path(project, conversation), branch_name(conversation)
    if not top:
        raise GitError("dépôt introuvable")
    if path.exists():
        rc, out = await git(top, "worktree", "remove", *(["--force"] if force else []), str(path))
        if rc:
            raise GitError(f"git worktree remove : {out[-300:]}")
    await git(top, "branch", "-D" if force else "-d", branch)
    return f"Worktree et branche {branch} supprimés."


# ---------------------------------------------------------------- sessions Claude

def session_usable(cwd: str, session_id: str) -> bool:
    """Une session Claude n'est reprise que depuis le dossier où elle a été créée (sinon : historique dans le prompt).

    Claude Code range ses sessions dans ~/.claude/projects/<dossier, caractères non alphanumériques remplacés par ->/.
    """
    root = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects"
    if not root.is_dir():
        return True  # emplacement inconnu : on laisse Claude décider
    found = list(root.glob(f"*/{session_id}.jsonl"))
    want = re.sub(r"[^A-Za-z0-9]", "-", str(Path(cwd).resolve()))
    return any(f.parent.name == want for f in found)
