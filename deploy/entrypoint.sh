#!/usr/bin/env bash
# Démarrage du runner cockpit dans un pod géré par Argo CD (équivalent de scripts/onyxia-init.sh, sans Onyxia).
# Variables : PROJECT_ID (obligatoire), GIT_REPOSITORY (dépôt du projet, optionnel), GIT_TOKEN (dépôt privé, optionnel).
# Secrets attendus dans l'environnement : CLAUDE_CODE_OAUTH_TOKEN, COCKPIT_WORKER_KEY.
set -euo pipefail

PROJECT_ID="${PROJECT_ID:?identifiant du projet attendu}"
[[ "$PROJECT_ID" =~ ^[a-z0-9-]{1,30}$ ]] || { echo "identifiant de projet invalide" >&2; exit 1; }
REPO="${GIT_REPOSITORY:-}"
WORK="$HOME/work"
COCKPIT="$WORK/.cockpit"
CONF="$WORK/.cockpit-config"

if [ -n "${GIT_TOKEN:-}" ]; then
  git config --global credential.helper '!f() { echo username=x-access-token; echo "password=$GIT_TOKEN"; }; f'
fi

if [ -n "$REPO" ]; then
  CWD="$WORK/$(basename "$REPO" .git)"
  if [ ! -d "$CWD" ]; then
    git clone -q "$REPO" "$CWD" 2>/dev/null || { mkdir -p "$CWD"; git -C "$CWD" init -q -b main; git -C "$CWD" remote add origin "$REPO"; }
  fi
else
  CWD="$WORK/$PROJECT_ID"
  mkdir -p "$CWD"
  [ -d "$CWD/.git" ] || git -C "$CWD" init -q
fi

if [ -d "$COCKPIT/.git" ]; then
  git -C "$COCKPIT" pull -q --ff-only || true
else
  git clone -q --depth 1 "${COCKPIT_REPO:-https://github.com/th-e-o/cockpit.git}" "$COCKPIT"
fi

# Config hors du dépôt : projects.yaml est propre à ce pod, le reste suit le dépôt (donc le git pull).
mkdir -p "$CONF"
for f in policy.yaml worker_key.sha256; do ln -sf "$COCKPIT/config/$f" "$CONF/$f"; done
cat > "$CONF/projects.yaml" <<YAML
projects:
  - id: $PROJECT_ID
    name: $PROJECT_ID
    cwd: $CWD
    description: ${REPO:+Dépôt $REPO}${REPO:-Projet $PROJECT_ID}
YAML

cd "$COCKPIT"
export COCKPIT_CONFIG="$CONF"
export COCKPIT_ROLE=projet
export COCKPIT_AUTO_UPDATE=1
export COCKPIT_WORKER_ID="${HOSTNAME:-$PROJECT_ID}"
export COCKPIT_WORKER_NAME="$PROJECT_ID"
export COCKPIT_VSCODE_PASSWORD=0
exec ./run.sh fg
