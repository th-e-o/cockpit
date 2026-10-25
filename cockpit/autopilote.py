"""Mode full auto : l'orchestrateur enchaîne les propositions de l'état de mémoire, parfois plusieurs jours de suite.

Le relais tient l'état (projets suivis, propositions, réglages armés depuis le téléphone) et impose les plafonds :
durée, tâches par jour, une seule tâche automatique à la fois, arrêt après plusieurs erreurs de suite. Ce module
décide seulement *quand* lancer (usage du forfait sous le seuil) et *quoi proposer* quand la file est vide.

Les tâches automatiques passent par la même politique que les autres : tout ce qui demande une validation attend
15 min puis est refusé (personne ne répond la nuit), jamais approuvé d'office.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time

from .relay import Relay
from .router import _ask_haiku
from .usage import Usage

log = logging.getLogger("cockpit.autopilote")

CHECK_EVERY = 60  # secondes entre deux lancements possibles
GENERATE_EVERY = 3 * 3600  # propositions regénérées au plus toutes les 3 h
WEEK_MARGIN = 0.10  # la limite hebdomadaire bloque 10 points au-dessus du seuil de session (95 % au plus)
PROPOSALS_PER_PROJECT = 3

GENERATE_PROMPT = """Tu aides un développeur dont les projets avancent même quand il n'est pas là. Pour chaque projet listé, propose
au plus {n} tâches concrètes, utiles et indépendantes que Claude Code peut mener seul dans le dépôt, à la suite de
l'historique donné : finir ce qui est resté à moitié, corriger les tests ou la CI en échec, ajouter les tests
manquants, documenter, simplifier, avancer la prochaine fonctionnalité évidente.

Règles :
- une tâche tient en une session de travail et se décrit en une instruction complète et autonome (en français) ;
- travail réversible dans le dépôt : pas de déploiement, pas de secrets, pas de suppression de données, pas de push sur main ;
- si rien d'utile ne s'impose pour un projet, n'en propose aucune.

Réponds UNIQUEMENT par un objet JSON : {{"<id du projet>": [{{"titre": "<5 à 8 mots>", "instruction": "<instruction complète>"}}]}}"""

SUFFIX = "\n\n(Tâche lancée par le mode full auto : travaille sur une branche dédiée, commite, et termine par le résumé habituel.)"


def usage_blocks(usage: Usage, config: dict) -> str | None:
    """Raison pour laquelle le forfait ne permet pas de lancer une tâche maintenant (None : c'est possible)."""
    threshold = float(config.get("threshold") or 0.85)
    for kind, limit in usage.limits.items():
        if limit.get("status") == "rejected":
            return f"limite atteinte ({kind})"
    session = usage.limits.get("five_hour", {}).get("utilization")
    if session is not None and session >= threshold:
        return f"session à {session:.0%} (seuil {threshold:.0%})"
    week = usage.limits.get("seven_day", {}).get("utilization")
    if week is not None and week >= min(0.95, threshold + WEEK_MARGIN):
        return f"semaine à {week:.0%}"
    return None


def parse_proposals(text: str, known: set[str]) -> dict[str, list[dict]]:
    """Réponse du modèle -> {projet: [{titre, instruction}]}, limitée aux projets connus."""
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError(f"réponse illisible : {text[:200]}")
    out: dict[str, list[dict]] = {}
    for project, items in json.loads(m.group(0)).items():
        if project not in known or not isinstance(items, list):
            continue
        clean = [{"titre": str(i["titre"]).strip(), "instruction": str(i["instruction"]).strip() + SUFFIX}
                 for i in items[:PROPOSALS_PER_PROJECT]
                 if isinstance(i, dict) and str(i.get("titre", "")).strip() and str(i.get("instruction", "")).strip()]
        if clean:
            out[project] = clean
    return out


class Autopilot:
    def __init__(self, relay: Relay, usage: Usage):
        self.relay = relay
        self.usage = usage
        self.config: dict = {}  # réglages armés depuis le téléphone, reçus à chaque poll
        self.waiting = ""  # dernière raison d'attente (journal, sans répétition)
        self._generated = 0.0

    async def run(self) -> None:
        while True:
            await asyncio.sleep(CHECK_EVERY)
            try:
                await self.tick()
            except Exception as e:
                log.warning("full auto : %s", e)

    async def tick(self) -> None:
        if not self.config.get("enabled"):
            self.waiting = ""
            return
        reason = usage_blocks(self.usage, self.config)
        if reason:
            self.note(f"en attente : {reason}")
            return
        # Plusieurs projets de front : tant que le relais accepte une tâche de plus (un projet à la fois, au plus max_parallel)
        for _ in range(max(1, int(self.config.get("max_parallel") or 1))):
            r = await self.relay.auto_tache()
            if r.get("task_id"):
                self.waiting = ""
                log.info("full auto : tâche %s lancée — %s", r["task_id"], r.get("titre"))
            elif r.get("skipped") == "aucune proposition":
                await self.generate()
                return
            else:
                self.note(f"en attente : {r.get('skipped')}")
                return

    def note(self, text: str) -> None:
        if text != self.waiting:
            self.waiting = text
            log.info("full auto %s", text)

    async def generate(self) -> None:
        """File vide : Claude propose de nouvelles tâches à partir de l'historique de chaque projet en ligne."""
        if time.time() - self._generated < GENERATE_EVERY:
            self.note("en attente : aucune proposition (nouvelle recherche dans quelques heures)")
            return
        self._generated = time.time()
        projects = [p for p in (await self.relay.projet_etat()).get("projects", []) if p.get("online")]
        sections = []
        for p in projects:
            state = await self.relay.projet_etat(p["id"])
            history = "\n".join(f"  - [{t['status']}] {t['instruction']}" + (f" → {t['result'][:200]}" if t.get("result") else "")
                                for t in state.get("tasks", [])[:6]) or "  (aucune tâche)"
            sections.append(f"Projet {p['id']} ({p.get('name', '')}) :\n{history}")
        if not sections:
            return
        text = await _ask_haiku(GENERATE_PROMPT.format(n=PROPOSALS_PER_PROJECT), "\n\n".join(sections)[:12000], model="sonnet")
        added = await self.relay.proposer(parse_proposals(text, {p["id"] for p in projects}))
        log.info("full auto : %d proposition(s) ajoutée(s)", added)
