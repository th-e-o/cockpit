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
    model: str | None = None  # niveau de modèle choisi dans le même appel (with_model), sinon None
    model_reason: str = ""


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


async def llm_route(instruction: str, projects: dict[str, Project], with_model: bool = False) -> Route:
    catalog = "\n".join(
        f"- {p.id}: {p.name} — {p.description.strip()} (mots-clés: {', '.join(p.keywords)})"
        for p in projects.values()
    )
    system = ROUTER_PROMPT.format(catalog=catalog)
    if with_model:  # un seul appel pour le projet et le modèle (« Modèle auto ») : une latence et un coût en moins
        system += ROUTER_MODEL_PROMPT
    text = await _ask_haiku(system, f"Instruction : {instruction}")
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError(f"réponse du routeur illisible : {text[:200]}")
    data = json.loads(m.group(0))
    pid = data.get("project")
    if pid not in projects:
        pid = None
    model = data.get("model") if with_model and data.get("model") in MODEL_CHOICES else None
    return Route(pid, float(data.get("confidence", 0)), data.get("reason", ""),
                 model, str(data.get("model_reason", ""))[:200] if model else "")


async def route(instruction: str, projects: dict[str, Project], with_model: bool = False) -> Route:
    """Projet de l'instruction ; avec with_model, aussi le niveau de modèle quand le routeur LLM répond (sinon None)."""
    if len(projects) == 1:
        return Route(next(iter(projects)), 1.0, "projet unique")
    try:
        r = await llm_route(instruction, projects, with_model)
    except Exception as e:  # le routeur LLM ne doit jamais bloquer le cockpit
        r = keyword_route(instruction, projects)
        r.reason = f"{r.reason} (repli : {e})"
    if r.confidence < CONFIDENCE_THRESHOLD:
        return Route(None, r.confidence, r.reason)
    return r


# ---------------------------------------------------------------- choix du modèle (sélecteur « Modèle auto »)

MODEL_RULES = """L'utilisateur écrit son code avec Opus : c'est la règle, les autres modèles sont des exceptions.

- "approfondi" (Opus) : TOUT ce qui écrit, modifie, corrige ou refactorise du code, même une petite retouche ;
  conception, architecture, nouveau projet ou nouvelle fonctionnalité ; débogage ; analyse de données ou de code
  non triviale ; tâche en plusieurs étapes ; revue de code avec corrections. EN CAS DE DOUTE : "approfondi".
- "standard" (Sonnet) : tâche courante SANS écriture de code applicatif : lancer les tests et rapporter le résultat,
  mettre à jour une documentation ou un README, petite configuration, commit, push ou pull request d'un travail déjà
  fait, expliquer un morceau de code précis.
- "rapide" (Haiku) : simple question ou lecture : état ou avancement d'un projet, lister des fichiers, des services
  ou des pods, résumer, retrouver une information, lancer ou arrêter un service, transmettre à un autre projet une
  consigne déjà claire (le travail sera fait là-bas), répondre « oui » à une question de confirmation sans autre
  travail.

Une réponse courte (« oui », « vas-y », « continue ») reprend la nature du travail en cours dans la conversation."""

MODEL_PROMPT = f"""Tu choisis le modèle Claude qui traitera une demande de développement envoyée depuis un téléphone.
{MODEL_RULES}
Réponds UNIQUEMENT par un objet JSON sur une ligne :
{{"model": "rapide" | "standard" | "approfondi", "reason": "<justification courte, en français>"}}"""

ROUTER_MODEL_PROMPT = f"""

Choisis AUSSI le modèle Claude qui traitera l'instruction :
{MODEL_RULES}
Ajoute au même objet JSON les champs "model" ("rapide" | "standard" | "approfondi") et "model_reason" (justification
courte, en français), même si project vaut null."""

MODEL_CHOICES = ("rapide", "standard", "approfondi")


async def _ask_haiku(system: str, prompt: str, model: str = "haiku") -> str:
    options = ClaudeAgentOptions(model=model, system_prompt=system, tools=[], max_turns=1, setting_sources=[])
    text = ""
    async for msg in query(prompt=prompt, options=options):
        if isinstance(msg, AssistantMessage):
            text += "".join(b.text for b in msg.content if isinstance(b, TextBlock))
    return text


async def choose_model(instruction: str, context: str = "", project: str = "") -> tuple[str, str]:
    """Niveau de modèle pour une demande (rapide, standard, approfondi) et sa raison ; Opus en cas d'échec."""
    prompt = f"Projet : {project or 'inconnu'}\n"
    if context:
        prompt += f"Échanges précédents de la conversation :\n{context}\n\n"
    prompt += f"Nouvelle demande : {instruction}"
    try:
        text = await _ask_haiku(MODEL_PROMPT, prompt[:6000])
        m = re.search(r"\{.*\}", text, re.S)
        data = json.loads(m.group(0)) if m else {}
        choice = data.get("model")
        if choice in MODEL_CHOICES:
            return choice, str(data.get("reason", ""))[:200]
        return "approfondi", "réponse du sélecteur illisible : Opus par défaut"
    except Exception as e:  # le choix du modèle ne doit jamais bloquer une tâche
        return "approfondi", f"sélecteur indisponible ({type(e).__name__}) : Opus par défaut"
