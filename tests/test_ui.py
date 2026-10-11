"""Test de bout en bout de la PWA dans Chromium (vue iPhone), avec un authentificateur WebAuthn virtuel.

Nécessite PHP (voir test_relay.py) et Playwright avec Chromium (`playwright install chromium`).
"""

import asyncio
import os
import threading
from pathlib import Path

import pytest

from test_relay import KEY, PHP, relay, worker_call  # noqa: F401  (fixture réutilisée)

sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright
pytestmark = pytest.mark.skipif(not PHP, reason="PHP introuvable (définir COCKPIT_PHP)")
SHOTS = Path(os.environ.get("COCKPIT_SHOTS", "/tmp"))


def start_worker(relay, tmp_path, monkeypatch):
    """Lance le vrai worker dans un thread ; l'agent Claude est simulé (une validation, puis un résultat)."""
    from cockpit import worker as worker_mod
    from cockpit.relay import Relay

    monkeypatch.setattr(worker_mod, "DATA", tmp_path)
    loop = asyncio.new_event_loop()
    holder = {}

    async def boot():
        w = worker_mod.Worker(Relay(relay["base"], KEY))
        w.relay.http.headers["Host"] = relay["origin"].removeprefix("http://")

        async def fake_run(task, project, resume=True):
            w.store.task_update(task, status="running", project=project.id)
            w.store.publish("message", {"task_id": task.id, "type": "text", "text": "Je vérifie la release **mlflow**…"})
            w.store.publish("message", {"task_id": task.id, "type": "tool_use", "tool": "TodoWrite", "text": "{}",
                                        "label": "📋 Met à jour son plan", "todos": [
                {"content": "Lire la release", "status": "completed", "activeForm": "Lit la release"},
                {"content": "Mettre à jour mlflow", "status": "in_progress", "activeForm": "Met à jour mlflow"}]})
            w.store.publish("message", {"task_id": task.id, "type": "tool_use", "tool": "ToolSearch", "text": "{}", "label": None})
            w.store.publish("message", {"task_id": task.id, "type": "tool_use", "tool": "Bash", "text": "{}",
                                        "label": "⚙️ Met à jour la release mlflow"})
            ok = await w.runner.request_approval(task, project, "Bash",
                                                 {"command": "helm upgrade mlflow-563237 inseefrlab/mlflow"},
                                                 "opération sensible : helm upgrade")
            w.store.task_update(task, status="done", cost_usd=0.042,
                                result="Release **mlflow** mise à jour.\n```\nREVISION: 4\n```" if ok else "Refusé.")

        w.runner.run = fake_run
        holder["worker"] = w
        await w.run()

    threading.Thread(target=lambda: loop.run_until_complete(boot()), daemon=True).start()
    return loop


def test_parcours_mobile(relay, tmp_path, monkeypatch):
    loop = start_worker(relay, tmp_path, monkeypatch)
    errors = []
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            ctx = browser.new_context(**p.devices["iPhone 13"])
            page = ctx.new_page()
            page.on("pageerror", lambda e: errors.append(str(e)))
            page.on("console", lambda m: m.type == "error" and errors.append(m.text))
            cdp = ctx.new_cdp_session(page)
            cdp.send("WebAuthn.enable")
            cdp.send("WebAuthn.addVirtualAuthenticator", {"options": {
                "protocol": "ctap2", "transport": "internal", "hasResidentKey": True,
                "hasUserVerification": True, "isUserVerified": True, "automaticPresenceSimulation": True}})

            # 1. Sans appairage : instructions
            page.goto(relay["app"])
            page.wait_for_selector("text=./run.sh pair")
            page.screenshot(path=SHOTS / "1-bienvenue.png")

            # 2. Scan du QR code -> passkey
            url = worker_call(relay, "w_appairage", {}).json()["url"]
            assert url.startswith(relay["app"] + "#pair=")
            page.goto(url)
            page.wait_for_selector("text=Téléphone connecté")
            page.screenshot(path=SHOTS / "2-appaire.png")
            page.click("#authBtn")
            page.wait_for_selector("#auth.hidden", state="attached")
            page.wait_for_selector(".chip >> text=Environnement Onyxia")
            assert page.locator("#conn.on").count() == 1

            # 3. Instruction -> validation -> résultat
            page.click(".chip >> text=Environnement Onyxia")
            page.fill("#input", "Mets à jour la release mlflow avec la dernière version du chart")
            page.click("#send")
            page.wait_for_selector(".approval:not(.resolved)")
            page.wait_for_selector("#pendingBtn:not(.hidden)")
            page.wait_for_selector(".todos >> text=Plan · 1/2")
            page.wait_for_selector(".todo.in_progress >> text=Met à jour mlflow")
            assert "Met à jour la release mlflow" in page.inner_text(".now")
            assert "ToolSearch" not in page.inner_text(".log")
            page.screenshot(path=SHOTS / "3-validation.png")
            page.click("text=Approuver")
            page.wait_for_selector(".pill.st-done")
            assert "REVISION: 4" in page.inner_text(".result")
            assert "$" not in page.inner_text(".meta")
            page.wait_for_selector(".steps summary >> text=2 étape(s)")
            assert page.locator("#pendingBtn.hidden").count() == 1
            page.screenshot(path=SHOTS / "4-termine.png")

            # 3 bis. Le message suivant continue la conversation ; « Nouvelle conversation » la referme
            page.wait_for_selector("#thread:not(.hidden) >> text=Suite de")
            page.fill("#input", "Et vérifie que le pod redémarre bien")
            page.click("#send")
            page.wait_for_selector("section.suite.current")
            page.click(".approval:not(.resolved) >> text=Approuver")
            page.wait_for_function("document.querySelectorAll('.pill.st-done').length === 2")
            assert page.locator("section.current").count() == 2
            page.screenshot(path=SHOTS / "4b-conversation.png")
            page.click("#newConv")
            page.wait_for_selector("#thread.hidden", state="attached")
            assert page.locator("section.current").count() == 0

            # 4. Cookie perdu (PWA installée, nouveau navigateur) -> Face ID seul
            ctx.clear_cookies()
            page.reload()
            page.wait_for_selector("#authBtn >> text=Face ID")
            page.screenshot(path=SHOTS / "5-connexion.png")
            page.click("#authBtn")
            page.wait_for_selector(".pill.st-done")  # historique rechargé

            # 5. Menu et journal d'audit
            page.click("#menuBtn")
            page.click("#mAudit")
            page.wait_for_selector(".auditList >> text=approve")
            page.screenshot(path=SHOTS / "6-audit.png")
            browser.close()
    finally:
        loop.call_soon_threadsafe(lambda: [t.cancel() for t in asyncio.all_tasks(loop)])
    assert not [e for e in errors if "401" not in e], errors
