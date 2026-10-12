"""Test de bout en bout de la PWA dans Chromium (vue iPhone), avec un authentificateur WebAuthn virtuel.

Nécessite PHP (voir test_relay.py) et Playwright avec Chromium (`playwright install chromium`).
"""

import asyncio
import os
import threading
import time
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


def seed_dashboard(relay, p):
    """Deux pods et quatre tâches (en cours, en validation, terminées avec modifications, branche parallèle)."""
    from test_relay import call, wpoll

    gi = 1024 ** 3
    wpoll(relay, "orch-ui", True, [{"id": "ui-onyxia", "name": "Environnement Onyxia"}, {"id": "ui-cockpit", "name": "Cockpit"}],
          hello=True, state={"resources": {"cpu": 0.37, "cpu_cores": 4, "mem_used": 5.6 * gi, "mem_limit": 16 * gi,
                                           "disk_used": 8.9 * gi, "disk_total": 9.8 * gi},
                             "projects": {"ui-onyxia": None, "ui-cockpit": {"branch": "main", "dirty": 3, "ahead": 1,
                                          "last_commit": {"sha": "317a3bd", "ts": time.time() - 5400, "subject": "Politique : push automatique"}}},
                             "running": 2, "max_agents": 3, "queued": 0, "started": time.time() - 7300,
                             "worktrees": [{"project": "ui-cockpit", "conversation": "PLACEHOLDER", "branch": "cockpit/PLACEHOLDER"}]})
    wpoll(relay, "pod-drones", False, [{"id": "ui-drones", "name": "drones"}], hello=True,
          state={"resources": {"cpu": 0.05, "cpu_cores": 2, "mem_used": 1.1 * gi, "mem_limit": 8 * gi, "disk_used": 0.4 * gi, "disk_total": 9.8 * gi},
                 "projects": {"ui-drones": {"branch": "feat/carte", "dirty": 0, "last_commit": {"sha": "a1b2c3d", "ts": time.time() - 200000, "subject": "Carte des vols"}}},
                 "running": 0, "max_agents": 3, "started": time.time() - 90000, "worktrees": []})
    ev = lambda wid, *events: worker_call(relay, "w_evenements", {"worker_id": wid, "events": list(events)})
    task = lambda tid, **kw: {"kind": "task", "task_id": tid, "task": kw}
    msg = lambda tid, **kw: {"kind": "message", "task_id": tid, "ts": time.time(), **kw}

    t1 = call(p, "tache", {"instruction": "Ajoute un tableau de bord des pods dans la PWA, lisible sur téléphone", "project": "ui-cockpit"}).json()["id"]
    changes = {"files": [{"path": "web/static/app.js", "status": "M", "added": 214, "removed": 12},
                         {"path": "web/static/style.css", "status": "M", "added": 96, "removed": 0},
                         {"path": "cockpit/podstate.py", "status": "A", "added": 88, "removed": 0},
                         {"path": "web/static/icon-512.png", "status": "M", "added": 0, "removed": 0, "binary": True}],
               "file_count": 4, "added": 398, "removed": 12, "branch": "cockpit/" + t1, "upstream": None,
               "commits": [{"sha": "9f1e2d3", "subject": "Tableau de bord des pods"}]}
    ev("orch-ui", msg(t1, type="tool_use", tool="Edit", text="{}", label="✏️ Modifie app.js"),
       msg(t1, type="changes", changes=changes, patches={"web/static/app.js": "@@ -10,3 +10,4 @@\n const a = 1;\n-const b = 2;\n+const b = 3;\n+const c = 4;"}),
       task(t1, status="done", project="ui-cockpit", cost_usd=0.84, changes=changes, duration_s=640,
            next_steps=[{"titre": "Fusionner la branche", "instruction": "Fusionne la branche dans main"}],
            worktree={"branch": "cockpit/" + t1, "path": "/w", "state": "active"},
            result="**Fait**\n- tableau de bord avec jauges CPU, mémoire, disque\n- résumé des modifications\n**Choix** — onglets sur téléphone, vue côte à côte sur ordinateur\n**À faire** — fusionner la branche"))

    t2 = call(p, "tache", {"instruction": "Refactorise le routeur et ajoute des tests", "project": "ui-cockpit"}).json()["id"]
    ev("orch-ui", task(t2, status="running", project="ui-cockpit"),
       msg(t2, type="tool_use", tool="TodoWrite", text="{}", label="📋 Met à jour son plan", todos=[
           {"content": "Lire router.py", "status": "completed", "activeForm": "Lit router.py"},
           {"content": "Extraire le score", "status": "completed", "activeForm": "Extrait le score"},
           {"content": "Écrire les tests", "status": "in_progress", "activeForm": "Écrit les tests"},
           {"content": "Lancer pytest", "status": "pending", "activeForm": "Lance pytest"}]),
       msg(t2, type="tool_use", tool="Bash", text="{}", label="⚙️ Lance les tests du routeur"))

    t3 = call(p, "tache", {"instruction": "Mets à jour la release mlflow", "project": "ui-onyxia"}).json()["id"]
    ev("orch-ui", task(t3, status="waiting_approval", project="ui-onyxia"),
       {"kind": "approval", "task_id": t3, "approval": {"id": "ap-ui", "task_id": t3, "project": "ui-onyxia", "tool": "Bash",
        "tool_input": {"command": "helm upgrade mlflow inseefrlab/mlflow"}, "reason": "opération sensible : helm upgrade",
        "expires": time.time() + 800}})

    t4 = call(p, "tache", {"instruction": "Corrige le calcul des altitudes", "project": "ui-drones"}).json()["id"]
    c4 = {"files": [{"path": "src/altitude.py", "status": "M", "added": 6, "removed": 4}], "file_count": 1, "added": 6,
          "removed": 4, "branch": "feat/carte", "upstream": "origin/feat/carte", "ahead": 0, "commits": [{"sha": "c0ffee1", "subject": "Altitude : corrige l'unité"}]}
    ev("pod-drones", msg(t4, type="changes", changes=c4, patches={}), task(t4, status="done", project="ui-drones", cost_usd=0.21, changes=c4, duration_s=180, next_steps=[
           {"titre": "Ajouter un test de l'altitude", "instruction": "Ajoute un test unitaire pour la conversion pieds → mètres"},
           {"titre": "Publier sur GitHub", "instruction": "Pousse la branche feat/carte et ouvre une pull request"}],
                                                                           result="**Fait** — unité corrigée (pieds → mètres)."))
    # Le worktree listé par l'orchestrateur correspond à la conversation de t1
    st = {"resources": {"cpu": 0.37, "cpu_cores": 4, "mem_used": 5.6 * gi, "mem_limit": 16 * gi, "disk_used": 8.9 * gi, "disk_total": 9.8 * gi},
          "projects": {"ui-onyxia": None, "ui-cockpit": {"branch": "main", "dirty": 3, "ahead": 1,
                       "last_commit": {"sha": "317a3bd", "ts": time.time() - 5400, "subject": "Politique : push automatique"}}},
          "running": 2, "max_agents": 3, "started": time.time() - 7300,
          "worktrees": [{"project": "ui-cockpit", "conversation": t1, "branch": "cockpit/" + t1}]}
    wpoll(relay, "orch-ui", True, state=st)
    worker_call(relay, "w_depots", {"depots": [
        {"name": "th-e-o/drones-vols", "url": "https://github.com/th-e-o/drones-vols", "private": True, "branch": "main",
         "description": "Analyse des vols de drones", "pushed": "2026-10-11T10:00:00Z", "archived": False},
        {"name": "th-e-o/PLFMarket", "url": "https://github.com/th-e-o/PLFMarket", "private": False, "branch": "main",
         "description": "Marché de prédiction", "pushed": "2026-10-10T22:49:29Z", "archived": False}]})
    return t1


def browser_page(p, browser, relay, **ctx_args):
    ctx = browser.new_context(**ctx_args)
    host = relay["origin"].removeprefix("http://").split(":")[0]
    ctx.add_cookies([{"name": c.name, "value": c.value, "domain": host, "path": "/"} for c in p.cookies.jar])
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: m.type == "error" and errors.append(m.text))
    page.goto(relay["app"])
    return page, errors


def test_tableau_de_bord(relay):
    from test_relay import paired_phone

    p = paired_phone(relay)
    t1 = seed_dashboard(relay, p)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        # Téléphone : onglet « Pods »
        page, errors = browser_page(p, browser, relay, **pw.devices["iPhone 13"])
        page.wait_for_selector(".pill.st-done")
        assert int(page.inner_text("#tabCount")) >= 2  # d’autres tests du module peuvent laisser des tâches actives
        page.click("#tabDash")
        page.wait_for_selector(".podcard >> text=drones")
        assert "⚠ 1" in page.inner_text(".kpis")
        assert page.locator(".live", has_text="Refactorise le routeur").count() == 1 and page.locator(".live.alert", has_text="mlflow").count() == 1
        assert "Plan 2/4" in page.inner_text(".lives") and "Lance les tests du routeur" in page.inner_text(".lives")
        assert "⚠ 8,9 Go / 9,8 Go" in page.inner_text(".podcard")  # disque presque plein : alerte avec icône
        assert page.locator(f".wtrow >> text=cockpit/{t1}").count() == 1
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")  # pas de défilement horizontal
        page.screenshot(path=SHOTS / "7-pods-mobile.png", full_page=True)
        # Bilan par projet : période modifiable
        page.wait_for_selector(".bcard >> text=Cockpit")
        bilan = page.locator(".bcard", has_text="Cockpit").first
        assert "10 min" in bilan.inner_text() and "398" in bilan.inner_text() and "app.js" in bilan.inner_text()
        assert bilan.locator(".activity i.b").count() == 1
        page.click("button[data-bilan='30']")
        page.wait_for_function("document.querySelectorAll('.bcard:first-child .activity i.z').length === 29")
        page.locator(".bcard").first.screenshot(path=SHOTS / "7b-bilan-mobile.png")
        # Nouveau pod sur un de mes dépôts
        page.click("#newPod")
        page.wait_for_selector(".repo >> text=th-e-o/drones-vols")
        page.click("[data-pod-mode='new']")  # nouveau dépôt : nom synchronisé avec celui du pod
        assert page.is_hidden("#repoList") and page.is_visible("#newRepoName")
        page.fill("#podName", "meteo")
        assert page.input_value("#newRepoName") == "meteo"
        page.screenshot(path=SHOTS / "7e-nouveau-depot.png")
        page.click("[data-pod-mode='existing']")
        page.fill("#repoSearch", "drone")
        assert page.locator(".repo").count() == 1
        page.click(".repo")
        assert page.input_value("#podName") == "drones-vols" and "main" in page.get_attribute("#podBranch", "placeholder")
        page.select_option("#podMem", "16Gi")
        page.screenshot(path=SHOTS / "7c-nouveau-pod.png")
        page.click("#podSubmit")
        page.wait_for_selector("#podDlg", state="hidden")
        page.wait_for_selector(".live >> text=Nouveau pod « drones-vols »")
        # Retour au fil depuis une tâche du tableau de bord : résumé des modifications, diff à la demande
        # Prochaines étapes : suivi dans le tableau de bord
        follow = page.locator(".follow", has_text="Corrige le calcul des altitudes")
        assert follow.locator(".nmain").count() == 2
        follow.screenshot(path=SHOTS / "7d-suivi.png")
        follow.locator(".nmain", has_text="Ajouter un test").click()  # préparée dans la saisie, modifiable
        page.wait_for_selector("#feed", state="visible")
        assert page.input_value("#input").startswith("Ajoute un test unitaire")
        assert "Corrige le calcul des altitudes" in page.inner_text("#threadText")
        page.fill("#input", "")
        card4 = page.locator("section", has=page.locator(".user", has_text="Corrige le calcul des altitudes")).first
        card4.locator(".nexts").scroll_into_view_if_needed()
        page.screenshot(path=SHOTS / "8b-prochaines-etapes.png")
        card4.locator(".nrun").nth(1).click()  # ▶ : lancée tout de suite, dans la conversation
        page.wait_for_selector("section.suite .user >> text=Pousse la branche feat/carte")
        assert card4.locator(".nexts").count() == 0  # la conversation a avancé : plus proposées
        page.click("#tabDash")
        page.click(f".recents .recent[data-jump='{t1}']")
        page.wait_for_selector("#feed", state="visible")
        card = page.locator(f"#t-{t1}")
        assert "4 fichiers" in card.inner_text() and "+398" in card.inner_text() and "1 commit" in card.inner_text()
        page.click(f"#t-{t1} button[data-dismiss]")  # Ignorer
        page.wait_for_function(f"!document.querySelector('#t-{t1} .nexts')")
        card.locator("details.file >> text=app.js").click()
        card.locator(".diffpre .a >> text=const c = 4;").wait_for()
        assert card.locator("button[data-wt='merge']").count() == 1
        card.scroll_into_view_if_needed()
        page.screenshot(path=SHOTS / "8-modifications-mobile.png")
        assert not errors, errors

        # Ordinateur : fil et tableau de bord côte à côte
        page, errors = browser_page(p, browser, relay, viewport={"width": 1440, "height": 900})
        page.wait_for_selector(".podcard >> text=drones")
        assert page.is_visible("#feed") and page.is_visible("#dash") and not page.is_visible(".tabs")
        page.screenshot(path=SHOTS / "9-pods-ordinateur.png")
        page.keyboard.press("Control+k")
        assert page.evaluate("document.activeElement.id") == "input"
        # Raccourcis de l'icône de l'application installée (manifest)
        page.goto(relay["app"] + "?vue=pods&action=pod")
        page.wait_for_selector("#podDlg[open] .repo >> text=th-e-o/PLFMarket")
        assert page.evaluate("location.search") == "" and "view-dash" in page.get_attribute("body", "class")
        page.keyboard.press("Escape")
        dark, dark_errors = browser_page(p, browser, relay, viewport={"width": 1440, "height": 900}, color_scheme="dark")
        dark.wait_for_selector(".podcard >> text=drones")
        dark.screenshot(path=SHOTS / "9b-pods-ordinateur-sombre.png")
        assert not dark_errors, dark_errors
        page.click(f"#t-{t1} button[data-wt='merge']")
        from test_relay import wpoll
        assert wpoll(relay, "orch-ui", True)["worktree_actions"][0]["action"] == "merge"
        assert not errors, errors
        browser.close()


def test_manifeste_application():
    """Manifeste de l'application installable (Android, PC) : icônes présentes, raccourcis dans le périmètre."""
    import json
    from pathlib import Path

    static = Path(__file__).resolve().parent.parent / "web" / "static"
    m = json.loads((static / "manifest.json").read_text())
    assert m["display"] == "standalone" and m["scope"] == m["start_url"] == "../"
    assert {i["purpose"] for i in m["icons"]} == {"any", "maskable"}
    for icon in m["icons"] + [i for sc in m["shortcuts"] for i in sc["icons"]]:
        assert (static / icon["src"]).exists(), icon
    assert all(sc["url"].startswith("../?") for sc in m["shortcuts"])


def test_notifications_du_service_worker(relay):
    """Logique des notifications (sw.js) évaluée dans Chromium : boutons d'action, étapes, pods."""
    from pathlib import Path

    src = (Path(__file__).resolve().parent.parent / "web" / "sw.js").read_text()
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.goto(relay["app"])
        page.add_script_tag(content=src)
        out = page.evaluate("""notifications({
          approvals: [{id: 'a1', tool: 'Bash', reason: 'opération sensible : rm', preview: 'rm -rf build', task_id: 't'}],
          new_pods: [{id: 'pod-x', name: 'drones', since: 1}],
          finished: [{id: 't1', status: 'done', result: '**Fait** — ok', changes: {file_count: 2, added: 5, removed: 1, commits: []},
                      next_steps: [{titre: 'Lancer les tests', instruction: 'pytest'}, {titre: 'Publier', instruction: 'push'}, {titre: 'Trois', instruction: '3'}]},
                     {id: 't2', status: 'error', result: 'boom', next_steps: [{titre: 'x', instruction: 'y'}]}]})""")
        browser.close()
    v, pod, done, err = out
    assert v["body"] == "opération sensible : rm\nrm -rf build" and [a["action"] for a in v["actions"]] == ["approve", "deny"]
    assert v["data"] == {"approval": "a1"} and v["urgent"]
    assert pod["title"] == "🟢 Pod « drones » connecté"
    assert done["body"].startswith("2 fichiers · +5 −1\nFait — ok") and "💡 Lancer les tests · Publier · Trois" in done["body"]
    assert [a["title"] for a in done["actions"]] == ["▶ Lancer les tests", "▶ Publier"] and done["data"]["task"] == "t1"
    assert err["actions"] == [] and err["title"] == "❌ Tâche en erreur"
