#!/usr/bin/env bash
# Push this repo to the VPS and (re)start the stack.
#
#   ./deploy/deploy.sh root@YOUR-SERVER-IP
#
# Idempotent. Safe to run repeatedly. Never overwrites the remote .env, and
# never touches data/ or reports/ on the server, so the audit trail survives
# every redeploy.
set -euo pipefail

TARGET="${1:-}"
REMOTE_DIR="${REMOTE_DIR:-/opt/portfolio-manager}"

if [[ -z "$TARGET" ]]; then
  echo "usage: $0 user@host" >&2
  exit 1
fi

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

echo "==> syncing $HERE -> $TARGET:$REMOTE_DIR"
ssh "$TARGET" "mkdir -p $REMOTE_DIR"

# Exclusions are anchored with ./ on purpose. A bare 'data' pattern also
# matches src/data, which ships a tree with no news or fundamentals
# providers and fails at import time on the server.
tar -C "$HERE" -czf - \
    --exclude='./.venv' --exclude='./.git' --exclude='__pycache__' \
    --exclude='./.pytest_cache' --exclude='./data' --exclude='./reports' \
    --exclude='./.env' --exclude='*.png' --exclude='./.playwright-mcp' \
    . | ssh "$TARGET" "tar -C $REMOTE_DIR -xzf -"

echo "==> checking remote .env"
ssh "$TARGET" "test -f $REMOTE_DIR/.env" || {
  echo "!! $REMOTE_DIR/.env does not exist on the server." >&2
  echo "   Copy .env.example to it and fill it in, then re-run." >&2
  exit 1
}

echo "==> building and starting"
ssh "$TARGET" "cd $REMOTE_DIR/deploy && \
  set -a && . ../.env && set +a && \
  docker compose up -d --build"

echo "==> status"
ssh "$TARGET" "cd $REMOTE_DIR/deploy && docker compose ps"

HOSTNAME_VALUE="$(ssh "$TARGET" "grep -E '^PM_HOSTNAME=' $REMOTE_DIR/.env | cut -d= -f2-")"
echo
echo "Dashboard: https://${HOSTNAME_VALUE}"
echo "Logs:      ssh $TARGET 'cd $REMOTE_DIR/deploy && docker compose logs -f'"
