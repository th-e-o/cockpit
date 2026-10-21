"""Choix des questions de Claude : survivent aux réécritures du fil, du tableau de bord et de la mini-fenêtre."""
import pytest

sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright
from test_ui import browser_page
from test_relay import paired_phone, wpoll, call, worker_call, relay  # noqa: F401


def test_choix_conserves_apres_rendu(relay):
    p = paired_phone(relay)
    wpoll(relay, "pod-qu", False, [{"id": "qu", "name": "Russe"}], hello=True)
    q3 = call(p, "tache", {"instruction": "Nouveau projet de bot", "project": "qu"}).json()
    worker_call(relay, "w_evenements", {"worker_id": "pod-qu", "events": [
        {"kind": "task", "task_id": q3["id"], "task": {"status": "done", "project": "qu", "result": "Questions posées.",
         "question": {"text": "Avant de commencer :", "options": [], "items": [
             {"question": "Où héberger le bot ?", "options": ["Pod Onyxia", "Serveur OVH"]},
             {"question": "Format des cartes ?", "options": ["Anki .apkg", "CSV"]}]}}}]})
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page, errors = browser_page(p, browser, relay, **pw.devices["iPhone 13"])
        page.wait_for_selector(f"#t-{q3['id']} form.question")
        form = page.locator(f"#t-{q3['id']} form.question")
        form.locator("[data-pick='Serveur OVH']").click()
        form.locator("[data-pick='CSV']").click()
        form.locator("textarea").fill("bot en français")
        page.evaluate("render(state.order)")                      # fil réécrit (événement / poll)
        assert form.locator("[aria-pressed='true']").count() == 2
        assert form.locator("textarea").input_value() == "bot en français"
        page.click("#tabDash")                                    # tableau de bord : même question
        dash = page.locator("#dash form.question")
        dash.wait_for()
        assert dash.locator("[aria-pressed='true']").count() == 2
        page.evaluate("state.dashHtml = ''; renderDash()")        # tableau de bord reconstruit
        assert page.locator("#dash form.question [aria-pressed='true']").count() == 2
        dash.locator("[data-pick='Pod Onyxia']").click()          # changement d'avis dans le tableau de bord
        page.evaluate("state.dashHtml = ''; renderDash(); render(state.order)")
        assert form.locator("[data-pick='Pod Onyxia']").get_attribute("aria-pressed") == "true"
        assert form.locator("[data-pick='Serveur OVH']").get_attribute("aria-pressed") == "false"
        assert not errors, errors
        browser.close()
