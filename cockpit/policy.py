"""Moteur de politique : décide allow / ask / deny pour chaque appel d'outil.

Garde-fou contre les erreurs de l'agent, pas une frontière de sécurité :
un agent autorisé à exécuter du Python peut contourner des regex. La vraie
frontière reste le RBAC Kubernetes du namespace et l'isolation des pods.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

ALLOW, ASK, DENY = "allow", "ask", "deny"
_RANK = {ALLOW: 0, ASK: 1, DENY: 2}

# Séparateurs de commandes shell : chaque segment est évalué séparément.
_SPLIT = re.compile(r"&&|\|\||;|\||\n")
# Constructions qui empêchent d'analyser la commande de façon fiable.
_OPAQUE = re.compile(r"`|\$\(|<\(|>\(|\beval\b|\bsource\b|^\s*\.\s|\bbash\s+-c|\bsh\s+-c")
# Redirection vers un fichier (hors /dev/null et 2>&1).
_REDIRECT = re.compile(r"(?<![0-9&])>{1,2}\s*(?!&|/dev/null)\S")

FILE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}


@dataclass
class Decision:
    action: str
    reason: str


class Policy:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.default = cfg.get("default", ASK)
        self.timeout = float(cfg.get("approval_timeout_s", 900))
        self.allow_tools = set(cfg.get("allow_tools", []))
        self.deny_tools = set(cfg.get("deny_tools", []))
        self.ask_tools = set(cfg.get("ask_tools", []))
        files = cfg.get("files", {})
        self.ask_paths = [re.compile(p, re.I) for p in files.get("ask_paths", [])]
        self.deny_paths = [re.compile(p, re.I) for p in files.get("deny_paths", [])]
        bash = cfg.get("bash", {})
        self.bash_deny = [re.compile(p, re.I) for p in bash.get("deny", [])]
        self.bash_ask = [re.compile(p, re.I) for p in bash.get("ask", [])]
        self.bash_allow = [re.compile(p, re.I) for p in bash.get("allow", [])]
        trust = cfg.get("trust", {})
        self.trust_tools = set(trust.get("tools", []))
        self.trust_bash = [re.compile(p, re.I) for p in trust.get("bash", [])]
        self.trust_never = [re.compile(p, re.I) for p in trust.get("never", [])]
        self.trust_paths_never = [re.compile(p, re.I) for p in trust.get("paths_never", [])]
        self.trust_unknown = bool(trust.get("unknown_commands", False))
        self.trust_opaque = bool(trust.get("opaque", False))

    @classmethod
    def load(cls, path: str | Path) -> "Policy":
        return cls(yaml.safe_load(Path(path).read_text()))

    def evaluate(self, tool: str, tool_input: dict, cwd: str) -> Decision:
        if tool in self.deny_tools:
            return Decision(DENY, f"outil {tool} interdit")
        if tool == "Bash":
            return self._bash(tool_input.get("command", ""))
        if tool in FILE_TOOLS:
            path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
            return self._file(path, cwd)
        if tool in self.ask_tools:
            return Decision(ASK, f"outil {tool} soumis à validation")
        if tool in self.allow_tools:
            return Decision(ALLOW, f"outil {tool} autorisé")
        if tool.startswith("mcp__"):
            return Decision(ASK, f"outil MCP {tool}")
        return Decision(self.default, f"outil {tool} : règle par défaut")

    def _file(self, path: str, cwd: str) -> Decision:
        resolved = str(Path(path).expanduser().resolve()) if path else ""
        for rx in self.deny_paths:
            if rx.search(resolved):
                return Decision(DENY, f"écriture interdite : {resolved}")
        root = str(Path(cwd).resolve())
        if not (resolved == root or resolved.startswith(root + "/")):
            return Decision(ASK, f"écriture hors du projet : {resolved}")
        for rx in self.ask_paths:
            if rx.search(resolved):
                return Decision(ASK, f"fichier sensible : {resolved}")
        return Decision(ALLOW, "écriture dans le projet")

    def _bash(self, command: str) -> Decision:
        cmd = command.strip()
        if not cmd:
            return Decision(ASK, "commande vide")
        for rx in self.bash_deny:
            if rx.search(cmd):
                return Decision(DENY, f"commande interdite ({rx.pattern})")

        worst = Decision(ALLOW, "commande en lecture / sans risque")
        if _OPAQUE.search(cmd):
            worst = Decision(ASK, "commande non analysable (substitution, eval, sh -c…)")
        elif _REDIRECT.search(cmd):
            worst = Decision(ASK, "redirection vers un fichier")

        for seg in (s.strip() for s in _SPLIT.split(cmd)):
            if not seg:
                continue
            d = self._segment(seg)
            if _RANK[d.action] > _RANK[worst.action]:
                worst = d
        return worst

    def trusted(self, tool: str, tool_input: dict, cwd: str) -> bool:
        """Mode confiance : une demande de validation (ask) peut-elle passer sans l'utilisateur ?

        Jamais pour : un refus (deny) ; une écriture hors du projet ou dans trust.paths_never
        (.git, workflows de CI) ; un outil hors de trust.tools (services, dépôts, agents en
        parallèle…) ; une commande qui touche à trust.never (cluster, push sur main…).
        """
        d = self.evaluate(tool, tool_input, cwd)
        if d.action != ASK:
            return d.action == ALLOW
        if tool in FILE_TOOLS:
            path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
            root = str(Path(cwd).resolve())
            resolved = str(Path(path).expanduser().resolve()) if path else ""
            if any(rx.search(resolved) for rx in self.trust_paths_never):
                return False
            return resolved == root or resolved.startswith(root + "/")
        if tool != "Bash":
            return tool in self.trust_tools
        cmd = tool_input.get("command", "").strip()
        if any(rx.search(cmd) for rx in self.trust_never):
            return False
        if (_OPAQUE.search(cmd) or _REDIRECT.search(cmd)) and not self.trust_opaque:
            return False
        for seg in (s.strip() for s in _SPLIT.split(cmd)):
            if not seg or self._segment(seg).action == ALLOW:
                continue
            if any(rx.search(seg) for rx in self.bash_ask):
                if not any(rx.search(seg) for rx in self.trust_bash):
                    return False
            elif not self.trust_unknown:
                return False
        return True

    def _segment(self, seg: str) -> Decision:
        for rx in self.bash_deny:
            if rx.search(seg):
                return Decision(DENY, f"commande interdite : {seg}")
        for rx in self.bash_ask:
            if rx.search(seg):
                return Decision(ASK, f"opération sensible : {seg}")
        # Ignore les affectations de variables en tête (FOO=bar cmd)
        bare = re.sub(r"^(\w+=\S*\s+)+", "", seg)
        for rx in self.bash_allow:
            if rx.search(bare):
                return Decision(ALLOW, "commande autorisée")
        return Decision(self.default, f"commande non répertoriée : {seg}")
