#!/usr/bin/env bash
# Démarrage du runner cockpit dans un pod géré par Argo CD (équivalent de scripts/onyxia-init.sh, sans Onyxia).
# Le code du cockpit est dans l'image (/opt/cockpit) : ni clone, ni git pull, ni mise à jour automatique.
# Variables : PROJECT_ID (obligatoire), GIT_REPOSITORY (dépôt du projet, optionnel), GIT_TOKEN (dépôt privé, optionnel).
# Secrets attendus dans l'environnement : CLAUDE_CODE_OAUTH_TOKEN, COCKPIT_WORKER_KEY.
set -euo pipefail

PROJECT_ID="${PROJECT_ID:?identifiant du projet attendu}"
[[ "$PROJECT_ID" =~ ^[a-z0-9-]{1,30}$ ]] || { echo "identifiant de projet invalide" >&2; exit 1; }
REPO="${GIT_REPOSITORY:-}"
WORK="$HOME/work"
COCKPIT=/opt/cockpit
CONF="$WORK/.cockpit-config"
DATA="$WORK/.cockpit-data"   # sur le volume : survit aux changements d'image

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

# Reprise des données d'une version qui clonait le cockpit dans le volume ($WORK/.cockpit/data)
if [ ! -d "$DATA" ] && [ -d "$WORK/.cockpit/data" ]; then
  cp -a "$WORK/.cockpit/data" "$DATA"
fi
mkdir -p "$DATA"

# Config hors de l'image : projects.yaml est propre à ce pod, le reste suit la version de l'image.
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
export COCKPIT_DATA="$DATA"
export COCKPIT_ROLE=projet
export COCKPIT_AUTO_UPDATE=0
export COCKPIT_WORKER_ID="${HOSTNAME:-$PROJECT_ID}"
export COCKPIT_WORKER_NAME="$PROJECT_ID"
export COCKPIT_VSCODE_PASSWORD=0
echo "cockpit ${COCKPIT_VERSION:-dev}"
exec ./run.sh fg
