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
        self.trust_default = bool(trust.get("default", False))  # validations de routine automatiques partout
        self.trust_outside = [re.compile(p) for p in trust.get("extended", {}).get("outside_paths", [])]
        self.trust_scratch = [re.compile(p) for p in trust.get("scratch_paths", [])]  # hors projet, mais sans risque
        self.user_rules: list[tuple[str, re.Pattern | None]] = []  # règles « Toujours autoriser » du téléphone
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

    def trusted(self, tool: str, tool_input: dict, cwd: str, extended: bool = False) -> bool:
        """Validation de routine : une demande (ask) peut-elle passer sans l'utilisateur ?

        Jamais pour : un refus (deny) ; une écriture dans trust.paths_never (.git, workflows de CI) ou hors du
        projet (sauf extended : mode confiance 🛡, dans trust.extended.outside_paths) ; un outil hors de
        trust.tools ; un dépôt GitHub public ; une commande qui touche à trust.never (cluster, push sur main…).
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
            if resolved == root or resolved.startswith(root + "/"):
                return True
            if any(rx.search(resolved) for rx in self.trust_scratch):
                return True
            return extended and any(rx.search(resolved) for rx in self.trust_outside)
        if tool != "Bash":
            if tool.endswith("creer_depot") and tool_input.get("prive") is False:
                return False  # un dépôt public reste toujours validé
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

    def set_user_rules(self, rules: list[dict]) -> None:
        """Règles « Toujours autoriser » créées depuis le téléphone : [{tool, pattern}] (pattern : regex ou vide)."""
        out = []
        for r in rules or []:
            try:
                out.append((str(r["tool"]), re.compile(r["pattern"]) if r.get("pattern") else None))
            except (KeyError, re.error):
                continue
        self.user_rules = out

    def user_allowed(self, tool: str, tool_input: dict, cwd: str) -> str | None:
        """Règle « Toujours autoriser » qui couvre cette demande (son motif), ou None.

        Jamais pour un refus (deny), une commande opaque ($(…), eval…) ni une écriture dans paths_never.
        """
        if not self.user_rules or self.evaluate(tool, tool_input, cwd).action == DENY:
            return None
        if tool == "Bash":
            cmd = tool_input.get("command", "").strip()
            if _OPAQUE.search(cmd):
                return None
            rules = [rx for t, rx in self.user_rules if t == "Bash" and rx]
            segs = [s.strip() for s in _SPLIT.split(cmd) if s.strip() and self._segment(s.strip()).action != ALLOW]
            hits = [next((rx for rx in rules if rx.search(re.sub(r"^(\w+=\S*\s+)+", "", s))), None) for s in segs]
            return hits[0].pattern if segs and all(hits) else None
        if tool in FILE_TOOLS:
            path = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
            resolved = str(Path(path).expanduser().resolve()) if path else ""
            if not resolved or any(rx.search(resolved) for rx in self.trust_paths_never):
                return None
            return next((rx.pattern for t, rx in self.user_rules if t == "files" and rx and rx.search(resolved)), None)
        return next((tool for t, rx in self.user_rules if t == tool and rx is None), None)

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
