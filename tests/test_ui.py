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
ROOT_DIR = Path(__file__).resolve().parent.parent


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
            wait_js(page, "document.querySelectorAll('.pill.st-done').length === 2")
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
       task(t1, status="done", project="ui-cockpit", cost_usd=0.84, changes=changes, duration_s=640, session_delta=0.07,
            tokens={"input": 9000, "output": 21000, "cache_read": 300000, "cache_write": 20000, "total": 350000},
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

    r4 = call(p, "tache", {"instruction": "Corrige le calcul des altitudes", "project": "ui-drones"})
    assert r4.status_code == 200, r4.text
    t4 = r4.json()["id"]
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


def wait_js(page, expr, timeout=10):
    """Comme page.wait_for_function, sans eval (interdit par la CSP) : interroge la page depuis Python."""
    end = time.time() + timeout
    while not page.evaluate(expr):
        assert time.time() < end, f"condition jamais vraie : {expr}"
        page.wait_for_timeout(100)


def csp():
    """Politique de sécurité du contenu de production (web/.htaccess), appliquée aussi pendant les tests."""
    import re

    return re.search(r'Content-Security-Policy "([^"]+)"', (ROOT_DIR / "web" / ".htaccess").read_text()).group(1)


def with_csp(ctx):
    def handler(route):
        if route.request.resource_type != "document":
            return route.continue_()
        resp = route.fetch()
        route.fulfill(response=resp, headers={**resp.headers, "content-security-policy": csp()})
    ctx.route("**/*", handler)
    return ctx


def browser_page(p, browser, relay, **ctx_args):
    ctx = with_csp(browser.new_context(**ctx_args))
    host = relay["origin"].removeprefix("http://").split(":")[0]
    ctx.add_cookies([{"name": c.name, "value": c.value, "domain": host, "path": "/"} for c in p.cookies.jar])
    page = ctx.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: m.type == "error" and errors.append(m.text))
    page.on("console", lambda m: "Content Security Policy" in m.text and errors.append(m.text))
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
        page.wait_for_selector(".pill.st-done", state="attached")  # dans un groupe de projet peut-être replié
        assert int(page.inner_text("#tabCount")) >= 2  # d’autres tests du module peuvent laisser des tâches actives
        page.click("#tabDash")
        page.wait_for_selector(".podcard >> text=drones")
        # L'essentiel d'abord ; chiffres du jour, bilan et mémoire sont repliés sous « Plus de détails »
        assert page.is_hidden(".kpis")
        page.click("details[data-more] > summary")
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
        wait_js(page, "document.querySelectorAll('.bcard:first-child .activity i.z').length === 29")
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
        wait_js(page, f"!document.querySelector('#t-{t1} .nexts')")
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
        actions = []
        for _ in range(30):  # le clic rend la main avant la fin de la requête : on attend sa livraison
            actions += wpoll(relay, "orch-ui", True)["worktree_actions"]
            if actions:
                break
            page.wait_for_timeout(100)
        assert actions[0]["action"] == "merge"
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
                     {id: 't2', status: 'error', result: 'boom', next_steps: [{titre: 'x', instruction: 'y'}]},
                     {id: 't3', status: 'done', result: 'Quel format ?', question: {text: 'Quel **format** ?', options: ['Anki', 'CSV', 'JSON']}}]})""")
        browser.close()
    v, pod, done, err, ask = out
    assert ask["title"] == "❓ Claude attend ta réponse" and ask["body"] == "Quel format ?" and ask["urgent"]
    assert [a["action"] for a in ask["actions"]] == ["answer-0", "answer-1"] and ask["data"]["options"] == ["Anki", "CSV"]
    assert v["body"] == "opération sensible : rm\nrm -rf build" and [a["action"] for a in v["actions"]] == ["approve", "deny"]
    assert v["data"] == {"approval": "a1"} and v["urgent"]
    assert pod["title"] == "🟢 Pod « drones » connecté"
    assert done["body"].startswith("2 fichiers · +5 −1\nFait — ok") and "💡 Lancer les tests · Publier · Trois" in done["body"]
    assert [a["title"] for a in done["actions"]] == ["▶ Lancer les tests", "▶ Publier"] and done["data"]["task"] == "t1"
    assert err["actions"] == [] and err["title"] == "❌ Tâche en erreur"


def test_appairage_ordinateur_par_cle(relay):
    """Ordinateur sans appareil photo : clé d'appairage saisie, puis passkey (authentificateur de plateforme virtuel)."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        ctx = browser.new_context(viewport={"width": 1280, "height": 800})
        page = ctx.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        cdp = ctx.new_cdp_session(page)
        cdp.send("WebAuthn.enable")
        cdp.send("WebAuthn.addVirtualAuthenticator", {"options": {
            "protocol": "ctap2", "transport": "internal", "hasResidentKey": True,
            "hasUserVerification": True, "isUserVerified": True, "automaticPresenceSimulation": True}})
        page.goto(relay["app"])
        page.wait_for_selector("#pairForm:not(.hidden)")
        page.fill("#pairCode", "zzzzz-zzzzz")
        page.click("#pairBtn")
        page.wait_for_selector("#authHint >> text=incorrecte")
        code = worker_call(relay, "w_appairage", {}).json()["code"]
        page.fill("#pairCode", code.lower())
        assert page.input_value("#pairCode") == code  # affichée en majuscules
        page.screenshot(path=SHOTS / "10-cle-appairage.png")
        page.press("#pairCode", "Enter")
        page.wait_for_selector("text=Ordinateur connecté ✓")
        assert page.is_hidden("#pairForm")
        page.click("#authBtn")  # Créer ma passkey
        page.wait_for_selector("#auth.hidden", state="attached")
        page.wait_for_selector("#composer")
        assert not errors, errors
        browser.close()



def seed_vibe(relay, p):
    """Un pod avec aperçu et mémoire ; une tâche finie (diff, capture, tests et CI en échec), une en cours, deux variantes."""
    import base64

    from test_relay import call, wpoll

    gi = 1024 ** 3
    wpoll(relay, "pod-vibe", False, [{"id": "vibe", "name": "Vibe"}], hello=True, state={
        "resources": {"cpu": 0.2, "cpu_cores": 2, "mem_used": 2 * gi, "mem_limit": 8 * gi, "disk_used": 1 * gi, "disk_total": 10 * gi},
        "projects": {"vibe": {"branch": "main", "dirty": 0}}, "running": 1, "max_agents": 3, "started": time.time() - 600,
        "preview": {"url": "https://user-x-cockpit-vibe-user.example/", "port": 5000}, "worktrees": [],
        "vscode": {"url": "https://user-x-cockpit-vibe.example/", "password": "mot-de-passe-vscode"},
        "usage": {"ts": int(time.time()), "source": "compte", "limits": [
            {"type": "five_hour", "utilization": 0.87, "resets_at": int(time.time()) + 3600},
            {"type": "seven_day", "utilization": 0.41, "resets_at": int(time.time()) + 3 * 86400}]}})
    worker_call(relay, "w_memoire", {"project": "vibe", "content": "# Vibe\n- tests : pytest", "hash": "m1", "worker_id": "pod-vibe"})
    ev = lambda *events: worker_call(relay, "w_evenements", {"worker_id": "pod-vibe", "events": list(events)})
    msg = lambda tid, **kw: {"kind": "message", "task_id": tid, "ts": time.time(), **kw}
    jpeg = (ROOT_DIR / "web" / "static" / "icon-192.png").read_bytes()  # remplacée ci-dessous par un vrai JPEG
    from playwright.sync_api import sync_playwright as spw
    with spw() as pw:
        b = pw.chromium.launch()
        pg = b.new_page(viewport={"width": 390, "height": 600})
        pg.set_content("<body style='margin:0;font:24px sans-serif;background:#fff'><h1 style='background:#d97757;color:#fff;padding:20px'>Mon app</h1><p>Accueil</p></body>")
        jpeg = pg.screenshot(type="jpeg", quality=60)
        b.close()

    a = call(p, "tache", {"instruction": "Ajoute une page d'accueil", "project": "vibe", "model": "standard"}).json()["id"]
    changes = {"files": [{"path": "app/main.py", "status": "M", "added": 2, "removed": 1}], "file_count": 1, "added": 2, "removed": 1,
               "branch": "feat/accueil", "upstream": "origin/feat/accueil", "ahead": 0, "commits": [{"sha": "abc1234", "subject": "Accueil"}]}
    ev(msg(a, type="changes", changes=changes, patches={"app/main.py": "@@ -10,3 +10,4 @@\n def accueil():\n-    return 'ok'\n+    titre = 'Mon app'\n+    return titre"}),
       {"kind": "task", "task_id": a, "task": {"status": "done", "project": "vibe", "changes": changes, "result": "**Fait** — page d'accueil",
                                               "tests": {"ok": False, "command": "pytest -q", "duration_s": 4}}},
       msg(a, type="tests", ok=False, command="pytest -q", duration_s=4, output="FAILED tests/test_app.py::test_titre - AssertionError"),
       msg(a, type="ci", state="failure", sha="abc1234", url="https://github.com/a/b/actions/runs/1",
           checks=[{"name": "tests", "status": "completed", "conclusion": "failure"}]))
    worker_call(relay, "w_image", {"task_id": a, "data": base64.b64encode(jpeg).decode(), "caption": "Accueil (téléphone)", "worker_id": "pod-vibe"})

    v = call(p, "tache", {"instruction": "Propose un logo", "project": "vibe", "variants": 2}).json()["variants"]
    for i, vid in enumerate(v):
        ev({"kind": "task", "task_id": vid, "task": {"status": "done", "project": "vibe",
            "worktree": {"branch": f"cockpit/{vid}", "path": "/w", "state": "active"},
            "changes": {"files": [{"path": f"logo{i}.svg", "status": "A", "added": 20 + i, "removed": 0}], "file_count": 1, "added": 20 + i, "removed": 0, "commits": []}}})
        worker_call(relay, "w_image", {"task_id": vid, "data": base64.b64encode(jpeg).decode(), "caption": f"Logo {i + 1}", "worker_id": "pod-vibe"})

    r = call(p, "tache", {"instruction": "Refais le menu de navigation", "project": "vibe"}).json()["id"]
    wpoll(relay, "pod-vibe", False)  # livrée au worker, qui la démarre
    ev({"kind": "task", "task_id": r, "task": {"status": "running", "project": "vibe", "model_used": "claude-opus-5-5",
                                               "model_auto": {"choice": "approfondi", "reason": "écriture de code"},
                                               "models": ["claude-opus-5-5", "claude-haiku-5-5"]}},
       msg(r, type="tool_use", tool="Edit", text="{}", label="✏️ Modifie menu.py"))
    return a, v, r


def test_vibe_coding_ui(relay):
    from test_relay import paired_phone, wpoll

    p = paired_phone(relay)
    a, v, r = seed_vibe(relay, p)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page, errors = browser_page(p, browser, relay, viewport={"width": 1440, "height": 1000},
                                    permissions=["clipboard-read", "clipboard-write"])
        page.on("dialog", lambda d: d.accept())
        page.wait_for_selector(f"#t-{r}")
        # Accès au pod : VSCode et mot de passe (copié, jamais affiché dans la page)
        vs = page.locator(".podcard", has=page.locator("[data-copy-pw='pod-vibe']"))
        assert vs.locator("a", has_text="Ouvrir VSCode").get_attribute("href") == "https://user-x-cockpit-vibe.example/"
        assert "mot-de-passe-vscode" not in page.content()
        vs.locator("[data-copy-pw]").click()
        page.wait_for_selector("[data-copy-pw] >> text=✓ Copié")
        assert page.evaluate("navigator.clipboard.readText()") == "mot-de-passe-vscode"
        # Barres du tableau de bord dimensionnées malgré la CSP (pas de style en ligne)
        page.wait_for_selector(".podcard >> text=Vibe")
        assert page.evaluate("document.querySelector('.meter .bar i').getBoundingClientRect().width") > 0
        assert page.locator("a.preview", has_text="Aperçu").get_attribute("href").startswith("https://user-x-cockpit-vibe-user")
        # Barre d'usage de Claude : sous l'en-tête (session de 5 h) et détaillée dans le tableau de bord
        page.wait_for_selector("#usageBar:not(.hidden).warn >> text=Session (5 h) 87 %")
        assert page.evaluate("document.querySelector('#usageBar i').getBoundingClientRect().width") > 1000
        card = page.locator(".usagecard")
        assert "87 %" in card.inner_text() and "41 %" in card.inner_text() and "réinitialisée" in card.inner_text()
        page.locator(".sect", has=card).screenshot(path=SHOTS / "15-usage.png")

        # Modèle qui écrit : sur la carte (avec les sous-agents) et dans le tableau de bord
        assert page.locator(f"#t-{r} .pill.model").inner_text() == "◆ Opus 5.5 auto + Haiku 5.5"
        assert "choisi automatiquement (◆ Approfondi) : écriture de code" in page.get_attribute(f"#t-{r} .pill.model", "title")
        assert "Opus 5.5" in page.locator(".live", has_text="Refais le menu").inner_text()
        names = page.evaluate("""['claude-opus-5-5', 'claude-sonnet-4-5-20250929', 'claude-3-5-sonnet-20241022', 'claude-haiku-4-5', 'claude-fable-5-1', 'gpt']
          .map(modelName)""")
        assert names == ["Opus 5.5", "Sonnet 4.5", "Sonnet 3.5", "Haiku 4.5", "Fable 5.1", "gpt"]

        # Recadrer la tâche en cours (conversation reprise automatiquement)
        assert "Recadrer" in page.get_attribute("#input", "placeholder")
        page.fill("#input", "Garde les icônes actuelles")
        page.click("#send")
        page.wait_for_selector(f"#t-{r} .steer >> text=Garde les icônes actuelles")
        assert wpoll(relay, "pod-vibe", False)["steers"] == [{"task_id": r, "text": "Garde les icônes actuelles"}]

        # Carte terminée : capture, tests et CI en échec, PR à ouvrir, revue ligne à ligne
        card = page.locator(f"#t-{a}")
        card.locator(".shot").first.click()
        page.wait_for_selector("#imgDlg[open] #imgFull")
        assert page.evaluate("document.querySelector('#imgFull').naturalWidth") > 0
        page.click("#imgDlg [data-close]")
        assert "Tests ❌ en échec" in card.locator("details.tests summary").inner_text()
        assert "CI en échec" in card.locator(".ci").inner_text()
        card.locator("details.file").click()
        card.locator(".diffpre span[data-ln='11']").click()  # « +    titre = 'Mon app' »
        card.locator(".rvform textarea").fill("utilise une constante")
        card.locator(".rvform button[type=submit]").click()
        page.wait_for_selector(f"#t-{a} .review >> text=1 commentaire")
        card.locator(".diffpre").first.scroll_into_view_if_needed()
        card.screenshot(path=SHOTS / "11-carte-vibe.png")
        card.locator("[data-rv-send]").click()
        page.wait_for_selector("section.suite .user >> text=Revue de ton travail")
        assert "`app/main.py` ligne 11" in page.locator("section.suite .user").last.inner_text()

        # Ouvrir une PR depuis une carte : demandée à Claude dans la conversation
        assert page.locator(f"#t-{a} [data-open-pr]").count() == 0  # la conversation a avancé (revue envoyée)

        # Validations passées automatiquement : signalées sur la carte
        worker_call(relay, "w_evenements", {"worker_id": "pod-vibe", "events": [{"kind": "message", "task_id": r,
            "type": "trusted", "tool": "Bash", "text": "opération sensible : pip install rich", "ts": time.time()}]})
        page.wait_for_selector(f"#t-{r} details.trusted >> text=1 validation passée automatiquement")

        # Modèle choisi pour une nouvelle conversation, commandes rapides
        page.click("#newConv")
        page.select_option("#modelSel", "rapide")
        page.click(".chip.qc >> text=🧪 Tests")
        assert page.input_value("#input").startswith("Lance les tests")
        page.click("#send")
        page.wait_for_selector("section .pill >> text=⚡ Rapide")
        page.click("#editQuick")
        page.click("#addCmd")
        page.locator("#cmdRows .cmdrow").last.locator(".ct").fill("📦 Dépendances")
        page.locator("#cmdRows .cmdrow").last.locator(".ci").fill("Mets à jour les dépendances")
        page.click("#cmdForm button[type=submit]")
        page.wait_for_selector(".chip.qc >> text=📦 Dépendances")

        # Mémoire du projet
        page.locator(".podcard [data-memory='vibe']").click()
        wait_js(page, "document.querySelector('#memText').value.includes('tests : pytest')")
        page.fill("#memText", "# Vibe\n- tests : pytest\n- style : black")
        page.click("#memSave")
        page.wait_for_selector("#memDlg", state="hidden")
        assert wpoll(relay, "pod-vibe", False)["memory_edits"][0]["content"].endswith("style : black")

        # Variantes : comparées côte à côte, on en garde une
        group = page.locator(".vgroup", has_text="Propose un logo")
        assert group.locator(".vcol").count() == 2 and group.locator(".vcol img").count() == 2
        group.screenshot(path=SHOTS / "12-variantes.png")
        group.locator("[data-keep]").nth(1).click()
        page.wait_for_timeout(300)
        actions = {x["task_id"]: x["action"] for x in wpoll(relay, "pod-vibe", False)["worktree_actions"]}
        assert actions == {v[1]: "merge", v[0]: "discard"}
        page.screenshot(path=SHOTS / "13-vibe-ordinateur.png")
        assert not errors, errors
        browser.close()



def test_nouveau_pod_depuis_un_projet(relay):
    """Un « nouveau pod » demandé dans la conversation d'un projet part, après confirmation, à l'orchestrateur."""
    from test_relay import call, paired_phone, wpoll

    p = paired_phone(relay)
    wpoll(relay, "orch-np2", True, [{"id": "np2-onyxia", "name": "Environnement Onyxia"}], hello=True)
    wpoll(relay, "pod-np2", False, [{"id": "np2-etp", "name": "np2-etp"}], hello=True)
    t = call(p, "tache", {"instruction": "analyse les ETP", "project": "np2-etp"}).json()
    worker_call(relay, "w_evenements", {"worker_id": "pod-np2", "events": [
        {"kind": "task", "task_id": t["id"], "task": {"status": "done", "project": "np2-etp", "result": "fait"}}]})
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page, errors = browser_page(p, browser, relay, viewport={"width": 1280, "height": 900})
        page.wait_for_selector(f"#t-{t['id']} .pill.st-done")
        page.evaluate(f"setReplyTo('{t['id']}')")  # la conversation du projet est suivie
        assert "np2-etp" in page.inner_text("#threadText")
        dialogs = []
        page.on("dialog", lambda d: (dialogs.append(d.message), d.accept()))
        page.fill("#input", "relance le bot telegram dans un nouveau pod")
        page.click("#send")
        page.wait_for_selector("section .user >> text=relance le bot telegram dans un nouveau pod")
        assert dialogs and "orchestrateur" in dialogs[0]
        mine = [e["task"] for e in call(p, "etat").json()["events"]
                if e["kind"] == "task" and e["task"]["instruction"] == "relance le bot telegram dans un nouveau pod"][-1]
        assert mine["project"] is None and mine["reply_to"] is None  # nouvelle conversation, routée (Auto)
        # Un message ordinaire reste une suite du projet, sans question
        page.evaluate(f"setReplyTo('{t['id']}')")
        page.fill("#input", "ajoute un graphique")
        page.click("#send")
        page.wait_for_selector("section .user >> text=ajoute un graphique")
        assert len(dialogs) == 1
        assert not errors, errors
        browser.close()



def test_questions_de_claude(relay):
    """Une question de Claude : en évidence (carte, pastille, tableau de bord), réponse par un choix ou en texte libre."""
    from test_relay import call, paired_phone, wpoll

    p = paired_phone(relay)
    wpoll(relay, "pod-qu", False, [{"id": "qu", "name": "Russe"}], hello=True)
    q = call(p, "tache", {"instruction": "Crée le bot de cartes de russe", "project": "qu"}).json()
    q2 = call(p, "tache", {"instruction": "Ajoute l'export", "project": "qu"}).json()
    q3 = call(p, "tache", {"instruction": "Nouveau projet de bot", "project": "qu"}).json()
    worker_call(relay, "w_evenements", {"worker_id": "pod-qu", "events": [
        {"kind": "task", "task_id": q["id"], "task": {"status": "done", "project": "qu", "result": "Plan prêt.",
         "question": {"text": "Quel format pour les cartes ?", "options": ["Anki .apkg", "CSV"]}}},
        {"kind": "task", "task_id": q2["id"], "task": {"status": "done", "project": "qu", "result": "Quel format d'export ?",
         "question": {"text": "Quel format d'export ?", "options": [], "detected": True}}},
        {"kind": "task", "task_id": q3["id"], "task": {"status": "done", "project": "qu", "result": "Questions posées.",
         "question": {"text": "Avant de commencer :", "options": [], "items": [
             {"question": "Où héberger le bot ?", "options": ["Pod Onyxia", "Serveur OVH"]},
             {"question": "Format des cartes ?", "options": ["Anki .apkg", "CSV"]}]}}}]})
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page, errors = browser_page(p, browser, relay, **pw.devices["iPhone 13"])
        page.wait_for_selector(f"#t-{q['id']} form.question >> text=Quel format pour les cartes ?")
        assert "❓" in page.inner_text("#pendingBtn") and "3" in page.inner_text("#pendingBtn")
        page.locator(f"#t-{q['id']} form.question").scroll_into_view_if_needed()
        page.screenshot(path=SHOTS / "16-question.png")
        # Mini-fenêtre toujours visible : ce qui attend une réponse d'abord (contenu calculé par pipHtml)
        mini = page.evaluate("pipHtml()")
        assert mini["attention"] >= 3 and mini["html"].count("form class=\"question") == 3
        assert mini["html"].index("Quel format pour les cartes") < mini["html"].index("<div class=\"pipcard done")
        page.set_content(f"<link rel=stylesheet href='{relay['app']}static/style.css'><body class='pip'><div class='pipwrap'>{mini['html']}</div></body>")
        page.set_viewport_size({"width": 380, "height": 520})
        page.screenshot(path=SHOTS / "18-mini-fenetre.png")
        page.goto(relay["app"])
        page.wait_for_selector(f"#t-{q['id']} form.question")
        # Questionnaire : un choix par question (modifiable), précisions, une seule réponse
        form = page.locator(f"#t-{q3['id']} form.question")
        form.locator("[data-pick='Serveur OVH']").click()
        form.locator("[data-pick='Pod Onyxia']").click()  # change d'avis : un seul choix par question
        form.locator("[data-pick='CSV']").click()
        assert form.locator("[aria-pressed='true']").count() == 2
        # Un nouveau rendu du fil (événement, poll) ne doit pas effacer les choix déjà faits
        page.evaluate("render(state.order)")
        assert form.locator("[aria-pressed='true']").count() == 2
        assert form.locator("[data-pick='CSV']").get_attribute("aria-pressed") == "true"
        form.locator("textarea").fill("bot en français")
        form.scroll_into_view_if_needed()
        page.screenshot(path=SHOTS / "17-questionnaire.png")
        form.locator("[type=submit]").click()
        page.wait_for_selector("section.suite .user >> text=Où héberger le bot ? → Pod Onyxia")
        sent = [e["task"]["instruction"] for e in call(p, "etat").json()["events"] if e["kind"] == "task" and e["task"].get("reply_to") == q3["id"]][-1]
        assert sent == "- Où héberger le bot ? → Pod Onyxia\n- Format des cartes ? → CSV\n\nPrécisions : bot en français"
        # Réponse libre : le texte tapé survit à un nouveau rendu du fil
        box = page.locator(f"#t-{q2['id']} form.question textarea")
        box.fill("CSV avec point-virgule")
        page.evaluate("render(state.order)")
        assert box.input_value() == "CSV avec point-virgule"
        page.locator(f"#t-{q2['id']} form.question [type=submit]").click()
        page.wait_for_selector("section.suite .user >> text=CSV avec point-virgule")
        # Réponse par un choix, depuis le tableau de bord
        page.click("#tabDash")
        page.wait_for_selector(".follow.ask >> text=Quel format pour les cartes ?")
        page.click(".follow.ask [data-answer] >> text=Anki .apkg")
        page.wait_for_selector("section.suite .user >> text=Anki .apkg")
        assert page.locator("#feed form.question").count() == 0 and "❓" not in page.inner_text("#pendingBtn")
        suites = {e["task"]["instruction"]: e["task"]["reply_to"] for e in call(p, "etat").json()["events"] if e["kind"] == "task"}
        assert suites["Anki .apkg"] == q["id"] and suites["CSV avec point-virgule"] == q2["id"]
        assert not errors, errors
        browser.close()



def test_inciter_a_economiser(relay):
    """Consommation visible (carte, tableau de bord) et conseils sous la saisie pour économiser les tokens."""
    from test_relay import call, paired_phone, wpoll

    p = paired_phone(relay)
    t1 = seed_dashboard(relay, p)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page, errors = browser_page(p, browser, relay, viewport={"width": 1440, "height": 900})
        page.wait_for_selector(f"#t-{t1} .pill.cost.heavy")
        assert page.inner_text(f"#t-{t1} .pill.cost") == "≈ 350 k tokens · +7 % session"
        assert "86 % relus depuis le cache" in page.get_attribute(f"#t-{t1} .pill.cost", "title")
        import re
        if page.is_hidden(".kpis"):
            page.click("details[data-more] > summary")
        assert re.search(r"\d+ k\nTokens aujourd'hui", page.inner_text(".kpis"))  # données créées par plusieurs tests
        page.wait_for_selector(".sect >> text=Tâches les plus gourmandes aujourd'hui")
        # Conseils sous la saisie
        page.click("#newConv")
        page.fill("#input", "où en est le projet drones ?")
        page.wait_for_selector("#costTip:not(.hidden) >> text=visible gratuitement dans le tableau de bord")
        page.fill("#input", "ajoute un export CSV au projet drones")
        page.wait_for_selector("#costTip >> text=choisis sa puce")
        page.click("#costTip [data-tip]")
        assert page.get_attribute(".chip[data-project='ui-drones']", "aria-checked") == "true"
        page.fill("#input", "")
        page.select_option("#modelSel", "approfondi")
        page.fill("#input", "liste les fichiers")
        page.wait_for_selector("#costTip >> text=Modèle auto")
        page.click("#costTip [data-tip]")
        assert page.input_value("#modelSel") == ""
        # Conversation longue : proposer d'en ouvrir une nouvelle
        page.evaluate(f"""(() => {{ const conv = state.tasks.get('{t1}').task.conversation || '{t1}';
          for (let i = 0; i < 8; i++) onEvent({{kind: 'task', task_id: 'long' + i, task: {{id: 'long' + i, status: 'done', instruction: 'x',
            conversation: conv, created: Date.now() / 1000, updated: Date.now() / 1000}}}});
          setReplyTo('long7'); }})()""")
        page.fill("#input", "et maintenant ?")
        page.wait_for_selector("#costTip >> text=Conversation longue")
        page.locator("#costTip").screenshot(path=SHOTS / "19-conseil.png")
        page.click("#costTip [data-tip]")
        page.wait_for_selector("#thread.hidden", state="attached")
        assert not errors, errors
        browser.close()



def test_toujours_autoriser(relay):
    """« Toujours autoriser » : approuve et crée une règle ciblée ; règles visibles et supprimables dans le menu."""
    from test_relay import call, paired_phone, wpoll

    p = paired_phone(relay)
    wpoll(relay, "pod-ta", False, [{"id": "ta", "name": "TA"}], hello=True)
    t = call(p, "tache", {"instruction": "construis l'image", "project": "ta"}).json()
    wpoll(relay, "pod-ta", False)
    worker_call(relay, "w_evenements", {"worker_id": "pod-ta", "events": [
        {"kind": "task", "task_id": t["id"], "task": {"status": "waiting_approval", "project": "ta"}},
        {"kind": "approval", "task_id": t["id"], "approval": {"id": "ap-ta", "task_id": t["id"], "tool": "Bash",
         "tool_input": {"command": "docker build -t app ."}, "reason": "opération sensible : docker build -t app .",
         "expires": time.time() + 600}}]})
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page, errors = browser_page(p, browser, relay, viewport={"width": 1280, "height": 900})
        page.on("dialog", lambda d: d.accept())
        btn = page.locator("[data-always='ap-ta']")
        btn.wait_for()
        assert btn.inner_text() == "Toujours autoriser « docker build … »"
        btn.click()
        msg = {}
        for _ in range(30):  # approbation transmise au worker, avec la nouvelle règle
            msg = wpoll(relay, "pod-ta", False)
            if msg["decisions"]:
                break
            page.wait_for_timeout(100)
        assert msg["decisions"] == [{"id": "ap-ta", "decision": "approve"}]
        assert {"tool": "Bash", "pattern": r"^docker\s+build\b"} in msg["rules"]
        page.click("#menuBtn")
        page.click("#mRules")
        page.wait_for_selector("#listDlg >> text=les commandes « docker build … »")
        page.click("#listDlg [data-del-rule]")
        page.wait_for_selector("#listDlg >> text=Aucune règle")
        assert not errors, errors
        browser.close()


def test_pull_requests(relay):
    """PR : encadré avec lien et fusion sur la carte, section du tableau de bord, fusion confiée au pod du projet."""
    from test_relay import call, paired_phone, wpoll

    p = paired_phone(relay)
    wpoll(relay, "pod-pr", False, [{"id": "prj", "name": "Russe"}], hello=True)
    t = call(p, "tache", {"instruction": "ouvre la PR du déploiement", "project": "prj"}).json()
    wpoll(relay, "pod-pr", False)
    url = "https://github.com/th-e-o/russe-telegram-cartes/pull/3"
    worker_call(relay, "w_evenements", {"worker_id": "pod-pr", "events": [
        {"kind": "message", "task_id": t["id"], "type": "pr", "url": url, "number": 3, "title": "Déploiement OVH",
         "head": "feat/deploy", "base": "main", "ts": time.time()},
        {"kind": "task", "task_id": t["id"], "task": {"status": "done", "project": "prj", "result": f"PR ouverte : {url}"}}]})
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page, errors = browser_page(p, browser, relay, viewport={"width": 1440, "height": 900})
        page.on("dialog", lambda d: d.accept())
        box = page.locator(f"#t-{t['id']} .prbox")
        box.wait_for()
        assert box.count() == 1  # le lien cité dans la réponse n'est pas dupliqué
        assert "Pull request #3 · Déploiement OVH" in box.inner_text() and "feat/deploy → main" in box.inner_text()
        assert box.locator("a", has_text="Voir sur GitHub").get_attribute("href") == url
        page.wait_for_selector(".sect >> text=Pull requests à valider")
        box.screenshot(path=SHOTS / "20-pull-request.png")
        box.locator("[data-merge-url]").click()
        page.wait_for_selector("section .user >> text=Fusionner la pull request #3 (th-e-o/russe-telegram-cartes)")
        act = [x for x in wpoll(relay, "pod-pr", False)["tasks"] if x.get("action")]
        assert act and act[0]["action"] == {"type": "fusionner_pr", "url": url}  # au pod du projet
        worker_call(relay, "w_evenements", {"worker_id": "pod-pr", "events": [
            {"kind": "message", "task_id": act[0]["id"], "type": "pr_merged", "url": url, "ts": time.time()},
            {"kind": "task", "task_id": act[0]["id"], "task": {"status": "done", "result": "✅ Pull request fusionnée"}}]})
        page.wait_for_selector(f"#t-{t['id']} .prbox.merged >> text=✅ fusionnée")
        assert page.locator(".sect >> text=Pull requests à valider").count() == 0
        assert not errors, errors
        browser.close()


def test_theme_et_repartition(relay):
    """Thème clair / sombre choisi dans le menu (mémorisé) ; largeur du fil réglable ; tableau de bord masquable."""
    from test_relay import paired_phone

    p = paired_phone(relay)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page, errors = browser_page(p, browser, relay, viewport={"width": 1440, "height": 900}, color_scheme="light")
        page.wait_for_selector("#splitter")
        bg = lambda: page.evaluate("getComputedStyle(document.body).backgroundColor")  # noqa: E731
        assert bg() == "rgb(246, 247, 249)"  # auto : suit le système (clair)
        page.click("#menuBtn")
        page.click("[data-theme-set='dark']")
        assert bg() == "rgb(11, 12, 16)" and page.get_attribute("html", "data-theme") == "dark"
        assert page.get_attribute("[data-theme-set='dark']", "aria-pressed") == "true"
        page.click("#menuDlg [data-close]")
        # Largeur du fil : glisser la séparation
        box = page.locator("#splitter").bounding_box()
        page.mouse.move(box["x"] + 3, box["y"] + 200)
        page.mouse.down()
        page.mouse.move(900, box["y"] + 200, steps=5)
        page.mouse.up()
        assert abs(page.locator("#chat").bounding_box()["width"] - 900) < 12
        page.screenshot(path=SHOTS / "21-sombre-large.png")
        page.reload()
        page.wait_for_selector("#splitter")
        assert bg() == "rgb(11, 12, 16)"  # thème mémorisé, appliqué avant l'affichage
        assert abs(page.locator("#chat").bounding_box()["width"] - 900) < 12  # largeur mémorisée
        page.dblclick("#splitter")
        assert page.locator("#chat").bounding_box()["width"] < 700  # largeur par défaut
        # Tableau de bord masqué : tout l'espace pour le fil
        page.click("#dashToggle")
        assert page.is_hidden("#dash") and page.locator("#chat").bounding_box()["width"] > 1400
        page.keyboard.press("Alt+d")
        assert page.is_visible("#dash")
        page.click("#menuBtn")
        page.click("[data-theme-set='light']")
        assert bg() == "rgb(246, 247, 249)"
        assert not errors, errors
        browser.close()
