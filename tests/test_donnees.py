"""Espace « 📊 Données » : SQL en lecture seule sur les jeux déclarés, secret statistique appliqué par le code, réponse."""

import asyncio
import json

import pytest

duckdb = pytest.importorskip("duckdb")

from cockpit import donnees  # noqa: E402


@pytest.fixture
def espace(tmp_path, monkeypatch):
    con = duckdb.connect()
    con.execute("""CREATE TABLE t AS SELECT annee, corps, 30000 + (annee - 2015) * 500 + i AS salaire
                   FROM range(2015, 2019) a(annee), (VALUES ('gendarme'), ('policier'), ('rare')) c(corps), range(0, 20) r(i)
                   WHERE corps <> 'rare' OR i < 3""")
    con.execute(f"COPY t TO '{tmp_path}/salaires.parquet' (FORMAT parquet)")
    cfg = tmp_path / "donnees.yaml"
    cfg.write_text(f"seuil: 11\njeux:\n  - id: salaires\n    nom: Salaires\n    chemin: {tmp_path}/salaires.parquet\n    description: test\n")
    return donnees.Espace.charger(cfg, tmp_path / "espace")


def llm(*reponses):
    """LLM simulé : renvoie les réponses données, dans l'ordre ; garde les invites reçues."""
    vues = []

    async def demander(cfg, system, prompt):
        vues.append((system, prompt))
        return reponses[len(vues) - 1]
    return demander, vues


SQL = ("SELECT annee, corps, COUNT(*) AS effectif, ROUND(AVG(salaire)) AS salaire_moyen FROM salaires "
       "GROUP BY annee, corps ORDER BY annee, corps")


def test_question_agregee_et_secret(espace, monkeypatch):
    demander, vues = llm(json.dumps({"sql": SQL, "titre": "Salaire moyen", "dimensions": ["annee", "corps"],
                                     "graphique": {"type": "ligne", "x": "annee", "y": "salaire_moyen", "serie": "corps"}}),
                         "Le salaire moyen des gendarmes passe de 30 010 à 31 510 €.")
    monkeypatch.setattr(donnees, "demander", demander)
    r = asyncio.run(espace.repondre("Évolution du salaire des gendarmes ?", "c1"))
    assert r["texte"].startswith("Le salaire moyen") and r["colonnes"] == ["annee", "corps", "effectif", "salaire_moyen"]
    rares = [l for l in r["lignes"] if l[1] == "rare"]
    assert len(rares) == 4 and all(l[2:] == ["s", "s"] for l in rares) and r["masquees"] == 4  # 3 individus : masqué
    assert [2015, "gendarme", 20, 30010.0] in r["lignes"]
    # Le LLM voit le schéma et les modalités, jamais une ligne ; la réponse est rédigée sur le tableau masqué
    assert "gendarme, policier, rare" in vues[0][0] and "de 2015 à 2018" in vues[0][0]
    assert ";s;s" in vues[1][1] and "30010" not in vues[0][0]
    assert r["svg"].startswith("<svg") and "<script" not in r["svg"] and "polyline" in r["svg"]
    # Suite de conversation : la requête précédente est fournie au LLM
    demander2, vues2 = llm(json.dumps({"reponse": "Une table : salaires."}))
    monkeypatch.setattr(donnees, "demander", demander2)
    assert asyncio.run(espace.repondre("Et quelles données as-tu ?", "c1"))["texte"] == "Une table : salaires."
    assert "SQL : SELECT annee" in vues2[0][1]


@pytest.mark.parametrize("sql", [
    "SELECT * FROM read_parquet('/etc/passwd')",
    "COPY salaires TO '/tmp/x.csv'",
    "SELECT 1; DROP TABLE salaires",
    "INSERT INTO salaires VALUES (1)",
    "SELECT * FROM 's3://autre-bucket/x.parquet'",
    "SELECT salaire FROM salaires",            # pas d'agrégat : refusé
])
def test_requetes_refusees(espace, sql):
    with pytest.raises(Exception):
        donnees.verifier_sql(espace._connexion(), sql)


def test_lignes_individuelles_masquees_et_correction(espace, monkeypatch):
    """Une requête qui listerait des individus (effectif 1) est entièrement masquée ; une requête invalide est corrigée."""
    individuelle = "SELECT corps, salaire, COUNT(*) AS effectif FROM salaires GROUP BY corps, salaire"
    demander, vues = llm(json.dumps({"sql": "SELECT * FROM read_csv('/etc/passwd')"}),
                         json.dumps({"sql": individuelle, "titre": "x", "dimensions": ["corps"]}), "ok")
    monkeypatch.setattr(donnees, "demander", demander)
    r = asyncio.run(espace.repondre("liste les salaires", "c2"))
    assert "a échoué" in vues[1][1]  # l'erreur du garde-fou est renvoyée au LLM
    assert all(l[1] == "s" and l[2] == "s" for l in r["lignes"])  # aucun salaire individuel ne sort


def test_rapport_et_memoire(espace, monkeypatch):
    assert "Salaires" in (espace.dossier / "CLAUDE.md").read_text()
    assert espace.est_rapport("Fais-en un rapport Quarto", "inconnue") is False  # rien à mettre dedans
    monkeypatch.setattr(donnees.shutil, "which", lambda name: None)
    espace._garder("c3", {"question": "q", "texte": "t", "titre": "T", "colonnes": ["annee", "effectif"], "lignes": [[2015, 20]],
                          "masquees": 0, "sql": "SELECT 1", "graphique": {}})
    assert espace.est_rapport("Fais-en un rapport Quarto", "c3")
    with pytest.raises(donnees.ErreurDonnees, match="quarto absent"):
        espace.rapport("c3")


def test_rapport_quarto(espace, monkeypatch, tmp_path):
    """Le .qmd contient la réponse, le graphique (SVG autonome), le tableau et les requêtes en annexe ; quarto le rend en HTML."""
    faux = tmp_path / "quarto"
    faux.write_text('#!/bin/sh\ncp "$2" "${2%.qmd}.html"\n')  # « quarto render x.qmd --to html » simulé
    faux.chmod(0o755)
    monkeypatch.setattr(donnees.shutil, "which", lambda name: str(faux))
    espace._garder("c4", {"question": "Évolution ?", "texte": "Hausse de 5 %.", "titre": "Salaire moyen",
                          "colonnes": ["annee", "effectif", "salaire_moyen"], "lignes": [[2015, 20, 30010.0], [2016, 20, 30510.0]],
                          "masquees": 0, "sql": SQL, "graphique": {"type": "barres", "x": "annee", "y": "salaire_moyen"}})
    nom, contenu = espace.rapport("c4")
    html = contenu.decode()
    assert nom == "salaire-moyen.html" and "Hausse de 5 %." in html and "<svg" in html and 'fill="#4f46e5"' in html
    assert "| 2015 | 20 | 30 010 |" in html and "## Annexe : requêtes" in html and "embed-resources: true" in html
    assert (espace.dossier / "salaire-moyen.qmd").exists()


def test_question_traitee_par_le_worker(espace, tmp_path, monkeypatch):
    """Tâche du projet « donnees » : pas d'agent Claude Code, réponse et tableau publiés sur la carte."""
    from cockpit import worker as worker_mod
    from cockpit.relay import Relay
    from cockpit.store import Task

    monkeypatch.setattr(worker_mod, "DATA", tmp_path / "data")
    w = worker_mod.Worker(Relay("http://relais.invalid", "k"))
    w.donnees = espace
    demander, _ = llm(json.dumps({"sql": SQL, "titre": "Salaire moyen", "dimensions": ["annee", "corps"]}), "Réponse.")
    monkeypatch.setattr(donnees, "demander", demander)
    t = Task(id="q1", instruction="Salaire des gendarmes ?", project="donnees", conversation="q1")
    w.store.tasks["q1"] = t
    asyncio.run(w.answer_data(t))
    assert t.status == "done" and t.result == "Réponse."
    evt = next(e for e in t.events if e.get("type") == "donnees")
    assert evt["titre"] == "Salaire moyen" and evt["masquees"] == 4 and evt["seuil"] == 11
    labels = [e["label"] for e in t.events if e.get("type") == "tool_use"]
    assert labels[:2] == ["📚 Lit le catalogue des données", "🔎 Écrit la requête"]
