#!/usr/bin/env bash
# Script de démarrage d'un service de projet (chart Onyxia, init.personalInit ; $1 = identifiant du projet).
# Clone le cockpit dans ~/work/.cockpit, déclare le dépôt du service comme unique projet et lance le worker.
# Prérequis : le secret Onyxia « Claude » contient CLAUDE_CODE_OAUTH_TOKEN et COCKPIT_WORKER_KEY (lus par run.sh).
set -euo pipefail

PROJECT_ID="${1:?identifiant du projet attendu}"
[[ "$PROJECT_ID" =~ ^[a-z0-9-]{1,30}$ ]] || { echo "identifiant de projet invalide" >&2; exit 1; }
REPO="${GIT_REPOSITORY:-}"
WORK="${HOME:-/home/onyxia}/work"
COCKPIT="$WORK/.cockpit"
if [ -n "$REPO" ]; then
  CWD="$WORK/$(basename "$REPO" .git)"   # cloné par le chart Onyxia
else
  CWD="$WORK/$PROJECT_ID"                 # nouveau projet : dossier vide sous git
  mkdir -p "$CWD"
  [ -d "$CWD/.git" ] || git -C "$CWD" init -q
fi

# Installation de Claude (CLI, extension VSCode, thème) : script commun https://github.com/th-e-o/script_init.
# Non bloquant : le worker n'a besoin que de CLAUDE_CODE_OAUTH_TOKEN, pas de l'interface VSCode.
curl -fsSL "${COCKPIT_CLAUDE_INIT_URL:-https://raw.githubusercontent.com/th-e-o/script_init/main/script_init}" | bash \
  || echo "script_init a échoué : poursuite sans l'installation de Claude" >&2
export PATH="$HOME/.local/bin:$PATH"

if [ -d "$COCKPIT/.git" ]; then
  git -C "$COCKPIT" pull -q --ff-only || true
else
  git clone -q --depth 1 https://github.com/th-e-o/cockpit.git "$COCKPIT"
fi
cd "$COCKPIT"

cat > config/projects.yaml <<YAML
projects:
  - id: $PROJECT_ID
    name: $PROJECT_ID
    cwd: $CWD
    description: ${REPO:+Dépôt $REPO}${REPO:-Nouveau projet $PROJECT_ID}
YAML

[ -x .venv/bin/python ] || { uv venv -q .venv && uv pip install -q --python .venv/bin/python -r requirements.txt; }
# Dépendances système du navigateur des captures d'écran (non bloquant : sans sudo, les captures seront indisponibles)
sudo -n "$COCKPIT/.venv/bin/python" -m playwright install-deps chromium >/dev/null 2>&1 \
  || sudo -n apt-get install -y -qq libnss3 libatk1.0-0 libatk-bridge2.0-0 libgbm1 libxkbcommon0 libasound2 >/dev/null 2>&1 || true

# Un worker de projet n'orchestre rien ; son identité est celle du pod.
export COCKPIT_ROLE=projet
export COCKPIT_AUTO_UPDATE=1   # le cockpit de ce pod se met à jour seul (git pull), entre deux tâches
export COCKPIT_WORKER_ID="${HOSTNAME:-$PROJECT_ID}"
export COCKPIT_WORKER_NAME="$PROJECT_ID"
exec ./run.sh
