#!/usr/bin/env bash
# Script de démarrage d'un service de projet (chart Onyxia, init.personalInit ; $1 = identifiant du projet).
# Clone le cockpit dans ~/work/.cockpit, déclare le dépôt du service comme unique projet et lance le worker.
# Prérequis : le secret Onyxia « Claude » contient CLAUDE_CODE_OAUTH_TOKEN et COCKPIT_WORKER_KEY (lus par run.sh).
set -euo pipefail

PROJECT_ID="${1:?identifiant du projet attendu}"
[[ "$PROJECT_ID" =~ ^[a-z0-9-]{1,30}$ ]] || { echo "identifiant de projet invalide" >&2; exit 1; }
REPO="${GIT_REPOSITORY:?GIT_REPOSITORY absent (service sans dépôt git)}"
WORK="${HOME:-/home/onyxia}/work"
COCKPIT="$WORK/.cockpit"
CWD="$WORK/$(basename "$REPO" .git)"

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
    description: Dépôt $REPO
YAML

# Un worker de projet n'orchestre rien ; son identité est celle du pod.
export COCKPIT_ROLE=projet
export COCKPIT_WORKER_ID="${HOSTNAME:-$PROJECT_ID}"
export COCKPIT_WORKER_NAME="$PROJECT_ID"
exec ./run.sh
