"""Registre de projets et routage d'une instruction vers un projet."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from claude_agent_sdk import AssistantMessage, ClaudeAgentOptions, TextBlock, query

CONFIDENCE_THRESHOLD = 0.7


@dataclass
class Project:
    id: str
    name: str
    cwd: str
    description: str = ""
    keywords: list[str] = field(default_factory=list)
    test: str = ""  # commande de test lancée après chaque tâche (sinon : .cockpit.yml du dépôt)


@dataclass
class Route:
    project: str | None
    confidence: float
    reason: str


def load_projects(path: str | Path) -> dict[str, Project]:
    raw = yaml.safe_load(Path(path).read_text()).get("projects", [])
    return {p["id"]: Project(**p) for p in raw}


def keyword_route(instruction: str, projects: dict[str, Project]) -> Route:
    text = instruction.lower()
    scores = {
        pid: sum(1 for k in p.keywords if re.search(rf"\b{re.escape(k.lower())}\b", text))
        for pid, p in projects.items()
    }
    ranked = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
    if not ranked or ranked[0][1] == 0:
        return Route(None, 0.0, "aucun mot-clé reconnu")
    if len(ranked) > 1 and ranked[1][1] == ranked[0][1]:
        return Route(None, 0.4, "plusieurs projets possibles")
    return Route(ranked[0][0], 0.75, "mots-clés")


ROUTER_PROMPT = """Tu es un routeur. Choisis le projet le plus adapté à l'instruction.
Projets disponibles :
{catalog}

Réponds UNIQUEMENT par un objet JSON sur une ligne :
{{"project": "<id ou null>", "confidence": <0..1>, "reason": "<courte justification>"}}
Une instruction qui concerne un dépôt ou du code GitHub de l'utilisateur absent de cette liste (« reprends mon code
sur … », « mon dépôt qui … ») va au projet qui gère les services et les nouveaux projets, s'il existe : il sait
chercher dans les dépôts de l'utilisateur et ouvrir un pod dessus.
Mets project=null si l'instruction est ambiguë ou ne correspond à aucun projet."""


async def llm_route(instruction: str, projects: dict[str, Project]) -> Route:
    catalog = "\n".join(
        f"- {p.id}: {p.name} — {p.description.strip()} (mots-clés: {', '.join(p.keywords)})"
        for p in projects.values()
    )
    options = ClaudeAgentOptions(
        model="haiku",
        system_prompt=ROUTER_PROMPT.format(catalog=catalog),
        tools=[],
        max_turns=1,
        setting_sources=[],
    )
    text = ""
    async for msg in query(prompt=f"Instruction : {instruction}", options=options):
        if isinstance(msg, AssistantMessage):
            text += "".join(b.text for b in msg.content if isinstance(b, TextBlock))
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError(f"réponse du routeur illisible : {text[:200]}")
    data = json.loads(m.group(0))
    pid = data.get("project")
    if pid not in projects:
        pid = None
    return Route(pid, float(data.get("confidence", 0)), data.get("reason", ""))


async def route(instruction: str, projects: dict[str, Project]) -> Route:
    if len(projects) == 1:
        return Route(next(iter(projects)), 1.0, "projet unique")
    try:
        r = await llm_route(instruction, projects)
    except Exception as e:  # le routeur LLM ne doit jamais bloquer le cockpit
        r = keyword_route(instruction, projects)
        r.reason = f"{r.reason} (repli : {e})"
    if r.confidence < CONFIDENCE_THRESHOLD:
        return Route(None, r.confidence, r.reason)
    return r
