"""Suivi de conversation côté worker : session reprise par conversation, contexte rappelé, routage."""

from cockpit.agent import Runner
from cockpit.policy import Policy
from cockpit.router import Project
from cockpit.store import Store, Task
from cockpit.worker import Worker

ROOT = __import__("pathlib").Path(__file__).resolve().parent.parent
CTX = [{"instruction": "lance un service pour les drones", "result": "Quel dépôt git ?", "project": "onyxia"}]


def runner(tmp_path):
    return Runner(Store(tmp_path), Policy.load(ROOT / "config" / "policy.yaml"))


def test_session_par_conversation(tmp_path):
    r = runner(tmp_path)
    proj = Project(id="onyxia", name="Onyxia", cwd=str(tmp_path))
    r.store.sessions["onyxia"] = "session-d-une-autre-conversation"
    assert r._session(Task(id="a", instruction="x", conversation="a"), proj) is None  # nouvelle conversation
    t = Task(id="b", instruction="x", conversation="a", resume_session="du-relais")
    assert r._session(t, proj) == "du-relais"  # après un redémarrage du worker
    r.store.conversations["a:onyxia"] = "locale"
    assert r._session(t, proj) == "locale"  # la plus récente, connue du worker


def test_contexte_sans_session(tmp_path):
    r = runner(tmp_path)
    t = Task(id="b", instruction="aucun, dossier vide", conversation="a", context=CTX)
    assert r._prompt(t, "sess") == "aucun, dossier vide"  # la session contient déjà l'historique
    prompt = r._prompt(t, None)
    assert "Quel dépôt git ?" in prompt and prompt.endswith("Nouvelle instruction : aucun, dossier vide")
    assert "context" not in Store.task_public(t) and t.conversation == Store.task_public(t)["conversation"]


def test_routage_avec_contexte():
    t = Task(id="b", instruction="oui, fais-le", context=CTX)
    text = Worker.routing_text(t)
    assert text.startswith("oui, fais-le") and "lance un service pour les drones (projet onyxia)" in text
    assert Worker.routing_text(Task(id="c", instruction="seul")) == "seul"


def test_toolsearch_autorise():
    assert Policy.load(ROOT / "config" / "policy.yaml").evaluate("ToolSearch", {"query": "x"}, "/").action == "allow"


def test_etapes_lisibles():
    from cockpit.agent import tool_label

    assert tool_label("Read", {"file_path": "/a/b/README.md"}) == "📖 Lit README.md"
    assert tool_label("Bash", {"command": "pytest -q", "description": "Lance les tests"}) == "⚙️ Lance les tests"
    assert tool_label("Bash", {"command": "ls"}) == "⚙️ ls"
    assert tool_label("mcp__services__lancer_service", {"nom": "drones"}) == "🚀 Lance le service drones"
    assert tool_label("ToolSearch", {}) is None
    assert tool_label("Inconnu", {}) == "🔧 Inconnu"
