#!/usr/bin/env bash
# Nightly local backup: SQLite online backup + rsync of the raw archive into
# a sibling directory. Run from project root or via cron.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

BACKUP_ROOT="${WXM_BACKUP_DIR:-$REPO_ROOT/../wxm-backup}"
TODAY="$(date -u +%Y-%m-%d)"
DEST="$BACKUP_ROOT/$TODAY"
mkdir -p "$DEST"

if [[ -f data/wxm.db ]]; then
  sqlite3 data/wxm.db ".backup $DEST/wxm.db"
  echo "DB backup -> $DEST/wxm.db"
fi

rsync -a --delete data/raw/ "$DEST/raw/" || true
echo "raw archive synced -> $DEST/raw/"
