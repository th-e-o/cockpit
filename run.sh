#!/usr/bin/env bash
# Worker du cockpit (à lancer dans un service VSCode Onyxia).
#   ./run.sh          démarre le worker en arrière-plan (journal : data/worker.log)
#   ./run.sh pair     affiche un QR code pour connecter un téléphone
#   ./run.sh init     génère la clé du worker (une seule fois)
#   ./run.sh logs     suit le journal ; ./run.sh stop arrête le worker
set -euo pipefail
cd "$(dirname "$0")"
[ -x .venv/bin/python ] || { uv venv -q .venv && uv pip install -q --python .venv/bin/python -r requirements.txt; }
mkdir -p data
PY=.venv/bin/python

# Secrets : variable d'env, sinon Vault (secret Onyxia « Claude », champs CLAUDE_CODE_OAUTH_TOKEN et COCKPIT_WORKER_KEY)
vault_field() {
  command -v vault >/dev/null || return 0
  vault kv get -field="$1" "${COCKPIT_VAULT_PATH:-${VAULT_MOUNT:-onyxia-kv}/${VAULT_TOP_DIR:-}/Claude}" 2>/dev/null || true
}
if [ -z "${CLAUDE_CODE_OAUTH_TOKEN:-}${ANTHROPIC_API_KEY:-}" ]; then
  CLAUDE_CODE_OAUTH_TOKEN="$(vault_field CLAUDE_CODE_OAUTH_TOKEN)"
  [ -n "$CLAUDE_CODE_OAUTH_TOKEN" ] && export CLAUDE_CODE_OAUTH_TOKEN || unset CLAUDE_CODE_OAUTH_TOKEN
fi
if [ -z "${COCKPIT_WORKER_KEY:-}" ] && [ ! -f data/worker_key ]; then
  COCKPIT_WORKER_KEY="$(vault_field COCKPIT_WORKER_KEY)"
  [ -n "$COCKPIT_WORKER_KEY" ] && export COCKPIT_WORKER_KEY || unset COCKPIT_WORKER_KEY
fi

case "${1:-start}" in
  init) exec $PY -m cockpit.worker init ;;
  pair) exec $PY -m cockpit.worker pair ;;
  logs) exec tail -f data/worker.log ;;
  stop) pkill -f "cockpit.worker run" && echo "Worker arrêté." || echo "Aucun worker actif." ;;
  fg)   exec $PY -m cockpit.worker run ;;
  start)
    [ -n "${CLAUDE_CODE_OAUTH_TOKEN:-}${ANTHROPIC_API_KEY:-}" ] || echo "⚠️  Aucun identifiant Claude : lance 'claude setup-token' (voir README)." >&2
    if pgrep -f "cockpit.worker run" >/dev/null; then echo "Worker déjà actif (./run.sh logs)."; exit 0; fi
    nohup $PY -m cockpit.worker run >> data/worker.log 2>&1 &
    sleep 2
    tail -n 3 data/worker.log
    echo "Worker démarré (pid $!). Journal : ./run.sh logs" ;;
  *) sed -n '2,7p' "$0"; exit 1 ;;
esac
