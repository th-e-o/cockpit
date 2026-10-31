"""Espace « 📊 Données » : questions en français sur des fichiers S3 (Parquet, CSV), sans agent ni pod par question.

Chaîne d'une question :
    question -> LLM (requête SQL DuckDB) -> exécution en lecture seule sur les jeux déclarés -> secret statistique appliqué
    par le code (cases de moins de `seuil` individus masquées) -> LLM (réponse rédigée à partir du résultat masqué)
    -> téléphone (réponse, tableau, graphique) ; « rapport » : document Quarto (HTML) des résultats de la conversation.

Le LLM ne voit jamais de ligne individuelle : seulement le schéma, les modalités des variables catégorielles et des
résultats agrégés, déjà masqués. Il peut être privé (API compatible OpenAI, par exemple sur le cloud du ministère) ;
sans configuration, c'est Claude, tel que configuré pour le cockpit (ANTHROPIC_BASE_URL d'une passerelle interne…).

    # config/donnees.yaml
    seuil: 11                       # effectif minimal d'une case publiée (secret statistique)
    llm:                            # facultatif : LLM privé, API compatible OpenAI
      url: https://llm.exemple.gouv.fr/v1
      modele: mistral-large
      cle_env: COCKPIT_DONNEES_LLM_KEY   # variable d'environnement qui contient la clé
    jeux:
      - id: salaires                # nom de la table dans les requêtes
        nom: Salaires des agents de l'État
        chemin: s3://mon-bucket/salaires/*.parquet
        description: Une ligne par agent et par année : corps, grade, sexe, salaire net annuel en euros courants.
"""

from __future__ import annotations

import asyncio
import csv
import html
import io
import json
import logging
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import yaml

log = logging.getLogger("cockpit.donnees")

PROJECT_ID = "donnees"
MAX_LIGNES = 500          # lignes d'un résultat, au plus (au-delà : la question doit être plus agrégée)
MODALITES_MAX = 40        # modalités montrées au LLM pour une variable texte (au-delà : variable trop fine, non listée)
REQUETE_TIMEOUT = 120     # secondes
ESSAIS_SQL = 3
SEUIL_DEFAUT = 11
# Tout ce qui sortirait des jeux déclarés ou modifierait quelque chose : jamais dans une requête du LLM
INTERDIT = re.compile(r"\b(read_\w+|copy|attach|detach|install|load|pragma|set|reset|export|import|call|checkpoint|glob|getenv|"
                      r"query_table|query|sniff_csv|parquet_\w+|iceberg_\w+|delta_scan)\b|s3://|https?://|file:|'/", re.I)

SQL_PROMPT = """Tu traduis une question en UNE requête SQL DuckDB sur les tables décrites ci-dessous, et rien d'autre.

Tables :
{catalogue}

Règles impératives :
- une seule requête SELECT (WITH autorisé), sur ces tables uniquement ;
- le résultat est AGRÉGÉ : jamais de ligne individuelle ; il contient une colonne entière nommée « effectif » = nombre
  d'individus de chaque case (COUNT(*), ou somme d'effectifs) ; les cases de moins de {seuil} individus seront masquées ;
- au plus {max_lignes} lignes ; trie le résultat dans un ordre lisible (souvent par année) ;
- montants : moyenne et médiane (median) plutôt que des valeurs extrêmes ; arrondis (ROUND) ;
- noms de colonnes courts et parlants en français, sans espace (ex. annee, corps, salaire_moyen).

Réponds UNIQUEMENT par un objet JSON :
{{"sql": "<requête>", "titre": "<titre court du tableau>", "dimensions": ["<colonnes de libellés : année, catégories>"],
  "graphique": {{"type": "ligne" | "barres" | "aucun", "x": "<colonne>", "y": "<colonne numérique>", "serie": "<colonne ou null>"}}}}
Si la question ne demande pas de données (ex. « quelles données as-tu ? ») : {{"reponse": "<réponse en français>"}}."""

REPONSE_PROMPT = """Tu réponds en français à une question sur des données, à partir du seul résultat agrégé fourni (déjà soumis
au secret statistique : « s » = case masquée car moins de {seuil} individus). 3 à 6 phrases : la réponse d'abord, chiffres à
l'appui (évolution en %, niveaux), puis une limite ou une précaution utile (champ, euros courants, cases masquées…).
N'invente rien qui ne soit dans le résultat. Pas de titre, pas de tableau (il est affiché à côté)."""


class ErreurDonnees(Exception):
    pass


@dataclass
class Jeu:
    id: str
    nom: str
    chemin: str
    description: str = ""


@dataclass
class Config:
    jeux: list[Jeu]
    seuil: int = SEUIL_DEFAUT
    llm: dict = field(default_factory=dict)

    @classmethod
    def charger(cls, path: Path) -> "Config | None":
        if not path.exists():
            return None
        raw = yaml.safe_load(path.read_text()) or {}
        jeux = [Jeu(**{k: str(j[k]) for k in ("id", "nom", "chemin", "description") if k in j}) for j in raw.get("jeux", [])]
        for j in jeux:
            if not re.fullmatch(r"[a-z_][a-z0-9_]{0,40}", j.id):
                raise ErreurDonnees(f"identifiant de jeu invalide : {j.id} (minuscules, chiffres, _)")
        return cls(jeux=jeux, seuil=int(raw.get("seuil") or SEUIL_DEFAUT), llm=raw.get("llm") or {})


# ---------------------------------------------------------------- DuckDB, en lecture seule sur les jeux déclarés

def connexion(cfg: Config):
    import duckdb

    con = duckdb.connect()
    if any(j.chemin.startswith("s3://") for j in cfg.jeux):
        con.execute("INSTALL httpfs; LOAD httpfs;")
        # Identifiants S3 injectés par Onyxia dans le pod (jetons temporaires)
        endpoint = re.sub(r"^https?://", "", os.environ.get("AWS_S3_ENDPOINT", ""))
        parts = [("KEY_ID", os.environ.get("AWS_ACCESS_KEY_ID")), ("SECRET", os.environ.get("AWS_SECRET_ACCESS_KEY")),
                 ("SESSION_TOKEN", os.environ.get("AWS_SESSION_TOKEN")), ("ENDPOINT", endpoint or None),
                 ("REGION", os.environ.get("AWS_DEFAULT_REGION", "us-east-1"))]
        opts = ", ".join(f"{k} '{v}'" for k, v in parts if v and "'" not in v)
        con.execute(f"CREATE SECRET s3_onyxia (TYPE s3, {opts}{', URL_STYLE ' + repr('path') if endpoint else ''})")
    for j in cfg.jeux:
        lecteur = "read_csv_auto" if re.search(r"\.csv(\.gz)?$", j.chemin, re.I) else "read_parquet"
        opts = ", hive_partitioning = true" if lecteur == "read_parquet" else ""
        con.execute(f"CREATE VIEW {j.id} AS SELECT * FROM {lecteur}('{j.chemin.replace(chr(39), '')}'{opts})")
    return con


def schema(con, cfg: Config) -> str:
    """Description des tables pour le LLM : colonnes, types, modalités des variables texte peu nombreuses (jamais de ligne)."""
    out = []
    for j in cfg.jeux:
        cols = con.execute(f"DESCRIBE {j.id}").fetchall()
        lignes = [f"Table {j.id} — {j.nom}" + (f" : {j.description}" if j.description else "")]
        for nom, typ, *_ in cols:
            info = f"  - {nom} ({typ})"
            if typ.upper().startswith("VARCHAR"):
                vals = [r[0] for r in con.execute(f'SELECT DISTINCT "{nom}" FROM {j.id} WHERE "{nom}" IS NOT NULL LIMIT {MODALITES_MAX + 1}').fetchall()]
                if len(vals) <= MODALITES_MAX:
                    info += " : " + ", ".join(str(v) for v in sorted(vals))
                else:
                    info += " : nombreuses modalités"
            elif nom.lower() in ("annee", "année", "an", "year", "millesime"):
                bornes = con.execute(f'SELECT MIN("{nom}"), MAX("{nom}") FROM {j.id}').fetchone()
                info += f" : de {bornes[0]} à {bornes[1]}"
            lignes.append(info)
        out.append("\n".join(lignes))
    return "\n\n".join(out)


def verifier_sql(con, sql: str) -> str:
    sql = sql.strip().rstrip(";").strip()
    if not sql:
        raise ErreurDonnees("requête vide")
    if ";" in sql:
        raise ErreurDonnees("une seule requête à la fois")
    if m := INTERDIT.search(sql):
        raise ErreurDonnees(f"« {m.group(0)} » interdit : interroge seulement les tables décrites, par leur nom")
    ast = json.loads(con.execute("SELECT json_serialize_sql(?)", [sql]).fetchone()[0])
    if ast.get("error"):
        raise ErreurDonnees(f"requête refusée : {ast.get('error_message', 'seules les requêtes SELECT sont permises')}")
    if len(ast.get("statements", [])) != 1:
        raise ErreurDonnees("une seule requête à la fois")
    if not re.search(r'"function_name":\s*"(count|count_star|sum)"', json.dumps(ast)):
        raise ErreurDonnees("le résultat doit être agrégé, avec une colonne effectif (COUNT(*))")
    return sql


def executer(con, sql: str) -> tuple[list[str], list[list]]:
    cur = con.execute(f"SELECT * FROM ({sql}) LIMIT {MAX_LIGNES + 1}")
    colonnes = [d[0] for d in cur.description]
    lignes = [list(r) for r in cur.fetchall()]
    if len(lignes) > MAX_LIGNES:
        raise ErreurDonnees(f"plus de {MAX_LIGNES} lignes : agrège davantage (moins de catégories, regroupements)")
    if "effectif" not in colonnes:
        raise ErreurDonnees("colonne « effectif » absente du résultat (nombre d'individus de chaque case)")
    return colonnes, lignes


def masquer(colonnes: list[str], lignes: list[list], dimensions: list[str], seuil: int) -> tuple[list[list], int]:
    """Secret statistique : dans une case de moins de `seuil` individus, toutes les valeurs sauf les libellés deviennent « s ».

    Appliqué par le code, avant que le résultat ne reparte vers le LLM ou le téléphone : une requête qui listerait des
    individus (effectif 1) est ainsi entièrement masquée. Le secret secondaire (déduction par différence) n'est pas traité."""
    i_eff = colonnes.index("effectif")
    garde = {i for i, c in enumerate(colonnes) if c in dimensions and c != "effectif"}
    if not garde:  # libellés non déclarés : colonnes non numériques
        garde = {i for i, c in enumerate(colonnes) if c != "effectif" and lignes and all(not isinstance(r[i], (int, float)) for r in lignes)}
    out, n = [], 0
    for r in lignes:
        eff = r[i_eff]
        if eff is None or not isinstance(eff, (int, float)) or eff < seuil:
            out.append([v if i in garde else "s" for i, v in enumerate(r)])
            n += 1
        else:
            out.append(r)
    return out, n


def _cellule(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float):
        return f"{v:,.2f}".replace(",", " ").replace(".", ",").rstrip("0").rstrip(",")
    if isinstance(v, int) and not (1900 <= v <= 2100):
        return f"{v:,}".replace(",", " ")
    return str(v)


def en_csv(colonnes: list[str], lignes: list[list]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(colonnes)
    w.writerows([[_cellule(v) for v in r] for r in lignes])
    return buf.getvalue()


# ---------------------------------------------------------------- graphique (SVG, sans bibliothèque ni script)

def graphique_svg(colonnes: list[str], lignes: list[list], spec: dict, autonome: bool = False) -> str:
    """Courbes ou barres en SVG. Couleurs par classes CSS dans l'application ; explicites (autonome) dans un rapport."""
    typ = (spec or {}).get("type")
    x, y, serie = (spec or {}).get("x"), (spec or {}).get("y"), (spec or {}).get("serie")
    if typ not in ("ligne", "barres") or x not in colonnes or y not in colonnes:
        return ""
    ix, iy = colonnes.index(x), colonnes.index(y)
    iserie = colonnes.index(serie) if serie in colonnes else None
    points = [(r[ix], r[iy], r[iserie] if iserie is not None else "") for r in lignes if isinstance(r[iy], (int, float))]
    if not points:
        return ""
    xs = list(dict.fromkeys(p[0] for p in points))
    series = list(dict.fromkeys(p[2] for p in points))[:8]
    vmax, vmin = max(p[1] for p in points), min(p[1] for p in points)
    if typ == "ligne":  # une évolution se lit sur son étendue (partir de 0 aplatirait la courbe) ; des barres partent de 0
        marge = (vmax - vmin) * 0.08 or abs(vmax) * 0.05 or 1
        vmin, vmax = vmin - marge, vmax + marge
    else:
        vmin, vmax = min(0, vmin), max(0, vmax) or 1
    W, H, G, B, T = 420, 230, 54, 34, 12  # étroit : lisible sur un téléphone sans zoomer
    sx = lambda i: G + (i + 0.5) * (W - G - 10) / len(xs)
    sy = lambda v: T + (H - T - B) * (1 - (v - vmin) / ((vmax - vmin) or 1))
    palette = ["#4f46e5", "#e8590c", "#0ca678", "#d6336c", "#1971c2", "#f59f00", "#7048e8", "#495057"]
    # Couleur d'une série : attributs explicites (rapport) ou classe CSS (application, thèmes clair et sombre)
    col = (lambda k: f'fill="{palette[k]}"') if autonome else (lambda k: f'class="s{k}"')
    trait = (lambda k: f'fill="none" stroke="{palette[k]}"') if autonome else (lambda k: f'class="s{k}"')
    e = lambda t: html.escape(_cellule(t), quote=True)
    out = [f'<svg viewBox="0 0 {W} {H + 18 * ((len(series) + 2) // 3 if series != [""] else 0)}" xmlns="http://www.w3.org/2000/svg" '
           f'role="img" aria-label="{e(y)} selon {e(x)}" font-size="13" font-family="sans-serif"{" fill=\"#333\"" if autonome else " class=\"chart\""}>']
    for k in range(5):  # graduations
        v = vmin + (vmax - vmin) * k / 4
        out.append(f'<line x1="{G}" x2="{W - 10}" y1="{sy(v):.1f}" y2="{sy(v):.1f}" stroke="#8884" stroke-width="1"/>'
                   f'<text x="{G - 6}" y="{sy(v) + 4:.1f}" text-anchor="end">{e(round(v) if abs(vmax) >= 100 else round(v, 2))}</text>')
    pas = max(1, -(-len(xs) // 6))  # 6 libellés d'abscisse au plus
    for i, xv in enumerate(xs):
        if i % pas == 0:
            out.append(f'<text x="{sx(i):.1f}" y="{H - B + 16}" text-anchor="middle">{e(xv)}</text>')
    largeur = (W - G - 10) / len(xs) * 0.8 / max(1, len(series))
    for k, s in enumerate(series):
        pts = [(xs.index(p[0]), p[1]) for p in points if p[2] == s]
        if typ == "ligne":
            chemin = " ".join(f"{sx(i):.1f},{sy(v):.1f}" for i, v in pts)
            out.append(f'<polyline points="{chemin}" stroke-width="2.5" {trait(k)}/>')
            out += [f'<circle cx="{sx(i):.1f}" cy="{sy(v):.1f}" r="3" {col(k)}><title>{e(s)} {e(xs[i])} : {e(v)}</title></circle>' for i, v in pts]
        else:
            for i, v in pts:
                x0 = sx(i) - largeur * len(series) / 2 + k * largeur
                out.append(f'<rect x="{x0:.1f}" y="{min(sy(v), sy(0)):.1f}" width="{largeur:.1f}" height="{abs(sy(0) - sy(v)):.1f}" {col(k)}>'
                           f'<title>{e(s)} {e(xs[i])} : {e(v)}</title></rect>')
    if series != [""]:
        for k, s in enumerate(series):
            lx, ly = G + (k % 3) * 125, H + 6 + 18 * (k // 3)
            out.append(f'<rect x="{lx}" y="{ly}" width="10" height="10" {col(k)}/><text x="{lx + 14}" y="{ly + 9}">{e(s)}</text>')
    out.append("</svg>")
    return "".join(out)


# ---------------------------------------------------------------- LLM (privé, ou Claude tel que configuré pour le cockpit)

async def demander(cfg: Config, system: str, prompt: str) -> str:
    llm = cfg.llm
    if llm.get("url"):
        cle = os.environ.get(llm.get("cle_env") or "COCKPIT_DONNEES_LLM_KEY", "")
        async with httpx.AsyncClient(timeout=120) as http:
            r = await http.post(llm["url"].rstrip("/") + "/chat/completions",
                                headers={"Authorization": f"Bearer {cle}"} if cle else {},
                                json={"model": llm.get("modele", ""), "temperature": 0,
                                      "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}]})
            if r.status_code >= 400:
                raise ErreurDonnees(f"LLM privé indisponible (HTTP {r.status_code}) : {r.text[:200]}")
            return r.json()["choices"][0]["message"]["content"]
    from .router import _ask_haiku
    return await _ask_haiku(system, prompt, model=llm.get("modele") or "sonnet")


def _json(text: str) -> dict:
    m = re.search(r"\{.*\}", text or "", re.S)
    if not m:
        raise ErreurDonnees(f"réponse du LLM illisible : {(text or '')[:200]}")
    return json.loads(m.group(0))


# ---------------------------------------------------------------- l'espace

RAPPORT = re.compile(r"\b(rapport|quarto|document|synth[eè]se)\b", re.I)


class Espace:
    def __init__(self, cfg: Config, dossier: Path):
        self.cfg = cfg
        self.dossier = dossier
        self.dossier.mkdir(parents=True, exist_ok=True)
        self._con = None
        self._schema: str | None = None
        self._lock = asyncio.Lock()  # une connexion DuckDB, une requête à la fois
        self.ecrire_memoire()

    @classmethod
    def charger(cls, path: Path, dossier: Path) -> "Espace | None":
        cfg = Config.charger(path)
        return cls(cfg, dossier) if cfg and cfg.jeux else None

    def ecrire_memoire(self) -> None:
        """Mémoire du projet (CLAUDE.md) : le catalogue, lisible depuis le téléphone ; la mémoire n'y manque jamais."""
        lignes = ["# 📊 Données", "", "Questions en français sur les jeux ci-dessous, en lecture seule ; secret statistique : "
                  f"cases de moins de {self.cfg.seuil} individus masquées. « Fais-en un rapport » : document Quarto.", ""]
        lignes += [f"- **{j.nom}** (`{j.id}`) : {j.description or j.chemin}" for j in self.cfg.jeux]
        Path(self.dossier, "CLAUDE.md").write_text("\n".join(lignes) + "\n")

    def _connexion(self):
        if self._con is None:
            self._con = connexion(self.cfg)
        return self._con

    async def catalogue(self) -> str:
        if self._schema is None:
            self._schema = await asyncio.to_thread(schema, self._connexion(), self.cfg)
        return self._schema

    async def _requete(self, sql: str) -> tuple[list[str], list[list]]:
        con = self._connexion()
        sql = verifier_sql(con, sql)
        job = asyncio.to_thread(executer, con, sql)
        try:
            return await asyncio.wait_for(job, REQUETE_TIMEOUT)
        except asyncio.TimeoutError:
            con.interrupt()
            raise ErreurDonnees(f"requête trop longue (plus de {REQUETE_TIMEOUT} s) : agrège ou filtre davantage")

    def historique(self, conversation: str) -> list[dict]:
        try:
            return json.loads(Path(self.dossier, f"conv-{conversation}.json").read_text())
        except (OSError, ValueError):
            return []

    def _garder(self, conversation: str, item: dict) -> None:
        h = self.historique(conversation)[-19:] + [item]
        Path(self.dossier, f"conv-{conversation}.json").write_text(json.dumps(h, ensure_ascii=False, default=str))

    async def repondre(self, question: str, conversation: str, etape=lambda texte: None) -> dict:
        """Réponse à une question : {texte, titre, colonnes, lignes, masquees, sql, svg} (ou {texte} sans données)."""
        async with self._lock:
            etape("📚 Lit le catalogue des données")
            cat = await self.catalogue()
            avant = self.historique(conversation)[-4:]
            contexte = "\n".join(f"- Question : {h['question']}\n  SQL : {h.get('sql', '')}" for h in avant)
            prompt = (f"Échanges précédents (pour comprendre « et pour… ? ») :\n{contexte}\n\n" if contexte else "") + f"Question : {question}"
            system = SQL_PROMPT.format(catalogue=cat, seuil=self.cfg.seuil, max_lignes=MAX_LIGNES)
            erreur, choix = "", {}
            for essai in range(ESSAIS_SQL):
                etape("🔎 Écrit la requête" if not essai else f"🔁 Corrige la requête ({erreur[:80]})")
                choix = _json(await demander(self.cfg, system, prompt + (f"\n\nTa requête précédente a échoué : {erreur}\nCorrige-la." if erreur else "")))
                if "reponse" in choix and "sql" not in choix:
                    return {"texte": str(choix["reponse"])}
                try:
                    etape("🧮 Interroge les données")
                    colonnes, lignes = await self._requete(str(choix.get("sql", "")))
                    break
                except Exception as e:  # erreur DuckDB ou garde-fou : renvoyée au LLM, qui corrige
                    erreur = f"{type(e).__name__}: {e}"[:400]
            else:
                raise ErreurDonnees(f"aucune requête valable après {ESSAIS_SQL} essais : {erreur}")
            lignes, masquees = masquer(colonnes, lignes, list(choix.get("dimensions") or []), self.cfg.seuil)
            etape("✍️ Rédige la réponse")
            table = en_csv(colonnes, lignes)
            texte = (await demander(self.cfg, REPONSE_PROMPT.format(seuil=self.cfg.seuil),
                                    f"Question : {question}\nTableau « {choix.get('titre', '')} » :\n{table[:12000]}")).strip()
            out = {"texte": texte, "titre": str(choix.get("titre") or question)[:120], "colonnes": colonnes,
                   "lignes": [[_cellule(v) if not isinstance(v, (int, float)) or isinstance(v, bool) else v for v in r] for r in lignes],
                   "masquees": masquees, "sql": choix["sql"], "seuil": self.cfg.seuil,
                   "svg": graphique_svg(colonnes, lignes, choix.get("graphique") or {}), "graphique": choix.get("graphique") or {}}
            self._garder(conversation, {"question": question, **{k: out[k] for k in ("texte", "titre", "colonnes", "lignes", "masquees", "sql", "graphique")},
                                        "ts": int(time.time())})
            return out

    def rapport(self, conversation: str, titre: str = "") -> tuple[str, bytes]:
        """Rapport Quarto (HTML autonome) des résultats de la conversation : (nom du fichier, contenu).

        Le .qmd ne contient aucun code exécutable (tableaux et graphiques déjà calculés, requêtes en annexe) : il suffit du
        binaire quarto, sans noyau Python ni R, et le rapport est reproductible en relançant les requêtes de l'annexe."""
        h = self.historique(conversation)
        if not h:
            raise ErreurDonnees("rien à mettre dans le rapport : pose d'abord une question sur les données")
        quarto = shutil.which("quarto")
        if not quarto:
            raise ErreurDonnees("quarto absent de ce pod (installe-le, ou utilise une image Onyxia qui l'inclut)")
        titre = titre or h[0]["titre"]
        doc = ["---", f'title: "{titre.replace(chr(34), "")}"', f"date: {time.strftime('%d/%m/%Y')}", "lang: fr",
               "format:", "  html:", "    embed-resources: true", "    toc: true", "---", "",
               f"*Résultats agrégés ; secret statistique : cases de moins de {self.cfg.seuil} individus masquées (« s »).*", ""]
        for k, item in enumerate(h, 1):
            doc += [f"## {item['titre']}", "", f"> {item['question']}", "", item["texte"], ""]
            if svg := graphique_svg(item["colonnes"], item["lignes"], item.get("graphique") or {}, autonome=True):
                doc += [f"```{{=html}}\n{svg}\n```", ""]
            doc += ["| " + " | ".join(item["colonnes"]) + " |", "|" + "---|" * len(item["colonnes"])]
            doc += ["| " + " | ".join(_cellule(v).replace("|", "/") for v in r) + " |" for r in item["lignes"]]
            doc += [""]
        doc += ["## Annexe : requêtes", ""] + [f"**{k}. {i['titre']}**\n\n```sql\n{i['sql']}\n```\n" for k, i in enumerate(h, 1)]
        with tempfile.TemporaryDirectory() as d:
            src = Path(d, "rapport.qmd")
            src.write_text("\n".join(doc))
            import subprocess
            r = subprocess.run([quarto, "render", str(src), "--to", "html"], cwd=d, capture_output=True, text=True, timeout=300)
            out = Path(d, "rapport.html")
            if r.returncode or not out.exists():
                raise ErreurDonnees(f"quarto render a échoué : {(r.stderr or r.stdout)[-400:]}")
            nom = re.sub(r"[^\w-]+", "-", titre.lower()).strip("-")[:50] or "rapport"
            shutil.copy(src, Path(self.dossier, f"{nom}.qmd"))  # source gardée dans le pod
            return f"{nom}.html", out.read_bytes()

    def est_rapport(self, instruction: str, conversation: str) -> bool:
        return bool(RAPPORT.search(instruction)) and bool(self.historique(conversation)) and len(instruction) < 200
