"""Client du relais OVH : le worker n'est jamais exposé, il interroge le relais."""

from __future__ import annotations

import asyncio
import logging
import secrets
from pathlib import Path

import httpx

from .store import Store

log = logging.getLogger("cockpit.relay")


class Relay:
    def __init__(self, url: str, key: str, worker_id: str = "principal", name: str | None = None,
                 orchestrator: bool = True):
        self.http = httpx.AsyncClient(
            base_url=url.rstrip("/"),
            headers={"Authorization": f"Bearer {key}"},
            timeout=httpx.Timeout(10, read=40),
        )
        self.worker_id = worker_id  # stable (un par pod)
        self.name = name or worker_id
        self.orchestrator = orchestrator
        self.instance = secrets.token_hex(8)  # identifie ce processus auprès du relais
        self.events: list[dict] = []
        self.audit: list[dict] = []
        self._wake = asyncio.Event()

    async def call(self, route: str, body: dict) -> dict:
        r = await self.http.post("/api.php", params={"r": route}, json=body)
        if r.status_code >= 400:
            raise RuntimeError(f"relais {route} : HTTP {r.status_code} {r.text[:200]}")
        return r.json()

    async def poll(self, projects: list[dict], hello: bool = False, wait: float | None = None,
                   state: dict | None = None) -> dict:
        body: dict = {"projects": projects, "hello": hello, "instance": self.instance, "worker_id": self.worker_id,
                      "name": self.name, "orchestrateur": self.orchestrator}
        if state is not None:
            body["state"] = state
        if wait is not None:
            body["wait"] = wait
        return await self.call("w_poll", body)

    async def affecter(self, task_id: str, project: str) -> str:
        """Confie une tâche au worker qui héberge le projet ; renvoie l'id de ce worker."""
        return (await self.call("w_affecter", {"task_id": task_id, "project": project}))["worker"]

    async def creer_taches(self, parent: str, tasks: list[dict], defer: bool = False) -> list[str]:
        """Crée des tâches filles (une conversation chacune) ; le relais les livre au worker de leur projet.

        defer : le projet peut ne pas exister encore (pod en cours de démarrage) ; la tâche attend son worker.
        """
        return (await self.call("w_taches", {"parent": parent, "tasks": tasks, "worker_id": self.worker_id,
                                             "defer": defer}))["ids"]

    async def image(self, task_id: str, jpeg_b64: str, caption: str = "") -> str:
        """Envoie une capture d'écran rattachée à une tâche ; renvoie son identifiant."""
        return (await self.call("w_image", {"task_id": task_id, "data": jpeg_b64, "caption": caption,
                                             "worker_id": self.worker_id}))["id"]

    async def memoire(self, project: str, content: str, digest: str) -> None:
        """Publie la mémoire du projet (CLAUDE.md) pour l'afficher et la modifier depuis le téléphone."""
        await self.call("w_memoire", {"project": project, "content": content, "hash": digest, "worker_id": self.worker_id})

    async def projet_etat(self, project: str = "", limit: int = 8) -> dict:
        """État d'un projet du cockpit (ou liste des projets si vide), vu du relais ; limit : dernières tâches (40 au plus)."""
        return await self.call("w_projet_etat", {"project": project, "limit": limit})

    async def pr_etat(self, task_id: str, url: str, state: str) -> None:
        """Publie la fusion ou la fermeture d'une pull request sur une tâche d'un autre pod (éventuellement hors ligne)."""
        await self.call("w_pr_etat", {"task_id": task_id, "url": url, "state": state, "worker_id": self.worker_id})

    async def alerte(self, kind: str) -> None:
        """Signale une réinitialisation de limite d'usage (notification sur le téléphone)."""
        await self.call("w_alerte", {"kind": kind, "worker_id": self.worker_id})

    async def reveil(self) -> list[dict]:
        """Full auto : projets du périmètre dont le pod est hors ligne alors qu'il y a du travail pour eux ({id, waiting, proposals})."""
        return (await self.call("w_reveil", {"worker_id": self.worker_id}))["projects"]

    async def auto_tache(self) -> dict:
        """Full auto : le relais lance la prochaine proposition ouverte ({task_id, titre}) ou répond {skipped: raison}."""
        return await self.call("w_auto_tache", {"worker_id": self.worker_id})

    async def proposer(self, proposals: dict[str, list[dict]]) -> int:
        """Publie des propositions {projet: [{titre, instruction}]} dans l'état de mémoire ; renvoie le nombre ajouté."""
        return (await self.call("w_memoire_projets", {"proposals": proposals, "worker_id": self.worker_id}))["added"]

    async def depots(self, depots: list[dict]) -> None:
        """Publie la liste des dépôts git de l'utilisateur (formulaire « Nouveau pod » du téléphone)."""
        await self.call("w_depots", {"depots": depots})

    async def pairing(self) -> dict:
        """Lien d'appairage (QR code) et clé équivalente à saisir sur un ordinateur : {"url", "code"}."""
        return await self.call("w_appairage", {})

    def send(self, evt: dict) -> None:
        self.events.append(evt)
        self._wake.set()

    def send_audit(self, line: dict) -> None:
        self.audit.append(line)
        self._wake.set()

    async def flush(self) -> None:
        if not (self.events or self.audit):
            return
        events, audit = self.events[:200], self.audit[:200]
        await self.call("w_evenements", {"events": events, "audit": audit, "worker_id": self.worker_id})
        del self.events[: len(events)]
        del self.audit[: len(audit)]

    async def flush_forever(self) -> None:
        """Envoie les événements par lots ; en cas d'échec, les garde et réessaie."""
        delay = 0.3
        while True:
            await self._wake.wait()
            self._wake.clear()
            await asyncio.sleep(0.25)  # regroupe les rafales
            try:
                await self.flush()
                delay = 0.3
            except Exception as e:
                log.warning("envoi au relais impossible : %s", e)
                delay = min(delay * 2, 30)
                await asyncio.sleep(delay)
            if self.events or self.audit:
                self._wake.set()


class RelayStore(Store):
    def __init__(self, data_dir: Path, relay: Relay):
        super().__init__(data_dir)
        self.relay = relay

    def forward(self, evt: dict) -> None:
        self.relay.send(evt)

    def audit(self, event: str, **fields) -> dict:
        line = super().audit(event, **fields)
        self.relay.send_audit(line)
        return line
