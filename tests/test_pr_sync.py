"""Pull requests fusionnées ou fermées sur GitHub : le worker le publie pour qu'elles quittent « à valider »."""
import asyncio

from cockpit import github
from cockpit import worker as worker_mod
from cockpit.relay import Relay
from cockpit.store import Task
from test_relay import relay  # noqa: F401  (fixture réutilisée)

OUVERTE = "https://github.com/th-e-o/projet/pull/1"
FUSIONNEE = "https://github.com/th-e-o/projet/pull/2"
FERMEE = "https://github.com/th-e-o/projet/pull/3"
RELAIS = "https://github.com/th-e-o/projet/pull/4"   # connue seulement du relais (worker redémarré)
AILLEURS = "https://github.com/autre/depot/pull/9"   # hors de portée du token


def test_sync_prs(tmp_path, monkeypatch):
    monkeypatch.setattr(worker_mod, "DATA", tmp_path / "data")
    w = worker_mod.Worker(Relay("http://relais.invalid", "k"))
    w.projects = {"p": object()}
    t = Task(id="t1", instruction="x", project="p", result=f"PR ouverte : {OUVERTE}")
    t.events = [{"type": "pr", "url": FUSIONNEE}, {"type": "pr", "url": FERMEE}, {"type": "pr", "url": AILLEURS}]
    w.store.tasks["t1"] = t

    async def projet_etat(project="", limit=8):
        return {"tasks": [{"id": "t0", "result": "", "pull_requests": [{"url": RELAIS}]}]}

    async def pr_etat(task_id, url, state):  # tâche d'un autre pod : publiée par le relais, pas par ce worker
        sent.append({"kind": "message", "type": "pr_merged" if state == "merged" else "pr_closed", "url": url, "task_id": task_id})

    async def etat_pr(url):
        if url == AILLEURS:
            raise github.GitHubError("pull request introuvable (HTTP 404)")
        return {OUVERTE: "open", FUSIONNEE: "merged", FERMEE: "closed", RELAIS: "merged"}[url]

    sent = []
    w.relay.projet_etat = projet_etat
    w.relay.pr_etat = pr_etat
    w.store.forward = sent.append
    monkeypatch.setattr(github, "etat_pr", etat_pr)

    asyncio.run(w.sync_prs_once())
    got = {(e["type"], e["url"], e["task_id"]) for e in sent if e["kind"] == "message"}
    assert got == {("pr_merged", FUSIONNEE, "t1"), ("pr_closed", FERMEE, "t1"), ("pr_merged", RELAIS, "t0")}

    # Rien n'est republié au cycle suivant ; la PR ouverte reste suivie
    sent.clear()
    asyncio.run(w.sync_prs_once())
    assert not sent


def test_pr_fermee_quitte_a_valider(relay):
    """Côté téléphone : une PR fusionnée ou fermée sur GitHub disparaît de « Pull requests à valider »."""
    import time

    import pytest
    sync_playwright = pytest.importorskip("playwright.sync_api").sync_playwright
    from test_relay import call, paired_phone, worker_call, wpoll
    from test_ui import browser_page

    p = paired_phone(relay)
    wpoll(relay, "pod-pr2", False, [{"id": "prj2", "name": "Projet"}], hello=True)
    t = call(p, "tache", {"instruction": "ouvre deux PR", "project": "prj2"}).json()
    wpoll(relay, "pod-pr2", False)
    u1, u2 = "https://github.com/th-e-o/projet/pull/10", "https://github.com/th-e-o/projet/pull/11"
    worker_call(relay, "w_evenements", {"worker_id": "pod-pr2", "events": [
        {"kind": "message", "task_id": t["id"], "type": "pr", "url": u, "number": u.split("/")[-1], "title": "PR " + u[-2:],
         "head": "f", "base": "main", "ts": time.time()} for u in (u1, u2)] + [
        {"kind": "task", "task_id": t["id"], "task": {"status": "done", "project": "prj2", "result": "ok"}}]})
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page, errors = browser_page(p, browser, relay, viewport={"width": 1440, "height": 900})
        page.wait_for_selector(".sect >> text=Pull requests à valider · 2")
        worker_call(relay, "w_evenements", {"worker_id": "pod-pr2", "events": [
            {"kind": "message", "task_id": t["id"], "type": "pr_merged", "url": u1, "ts": time.time()},
            {"kind": "message", "task_id": t["id"], "type": "pr_closed", "url": u2, "ts": time.time()}]})
        page.wait_for_selector(f"#t-{t['id']} .prbox.merged >> text=✅ fusionnée")
        page.wait_for_selector(f"#t-{t['id']} .prbox.closed >> text=fermée")
        assert page.locator(".sect >> text=Pull requests à valider").count() == 0
        assert page.locator("[data-merge-url]").count() == 0  # plus de bouton « Fusionner » sur une PR fermée
        assert not errors, errors
        browser.close()


def test_superseded_pardonne_une_fois(tmp_path, monkeypatch):
    """Le premier « superseded » n'arrête pas le worker (après une panne du relais, tous l'ont reçu en même temps) ; le second oui."""
    monkeypatch.setattr(worker_mod, "DATA", tmp_path / "data")
    w = worker_mod.Worker(Relay("http://relais.invalid", "k"))
    pause = w.superseded_pause(now=1000.0)
    assert 30 <= pause <= 60
    assert w.superseded_pause(now=1100.0) is None  # deuxième en moins de 5 min : un vrai doublon, on s'arrête
    w2 = worker_mod.Worker(Relay("http://relais.invalid", "k"))
    assert w2.superseded_pause(now=1000.0) is not None
    assert w2.superseded_pause(now=1000.0 + worker_mod.SUPERSEDED_WINDOW + 1) is not None  # espacés : pardonnés à nouveau
