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

    async def poll(self, projects: list[dict], hello: bool = False, wait: float | None = None) -> dict:
        body: dict = {"projects": projects, "hello": hello, "instance": self.instance, "worker_id": self.worker_id,
                      "name": self.name, "orchestrateur": self.orchestrator}
        if wait is not None:
            body["wait"] = wait
        return await self.call("w_poll", body)

    async def affecter(self, task_id: str, project: str) -> str:
        """Confie une tâche au worker qui héberge le projet ; renvoie l'id de ce worker."""
        return (await self.call("w_affecter", {"task_id": task_id, "project": project}))["worker"]

    async def pairing_url(self) -> str:
        return (await self.call("w_appairage", {}))["url"]

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
