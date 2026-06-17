#!/usr/bin/env bash
# Phase 0 local soak orchestration. Starts a tmux session with four windows
# running the long-lived recorders + periodic loops. Stop with `touch data/KILL`
# from inside the project directory, then close the tmux session.
#
# Prereqs: tmux installed, .venv populated (uv pip install -e ".[dev]"), and the
# user has run `wxm init-db` at least once.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

VENV_PY="$REPO_ROOT/.venv/bin/python"
WXM="$REPO_ROOT/.venv/bin/wxm"
SESSION="wxm"

if [[ ! -x "$WXM" ]]; then
  echo "wxm CLI not found at $WXM — run: uv pip install -e \".[dev]\"" >&2
  exit 1
fi

if ! command -v tmux >/dev/null; then
  echo "tmux not installed; aborting" >&2
  exit 1
fi

mkdir -p data/bridge data/raw data/logs reports
rm -f data/KILL

tmux has-session -t "$SESSION" 2>/dev/null && {
  echo "tmux session '$SESSION' already exists; attach with: tmux attach -t $SESSION"
  exit 0
}

# Books — long-lived WS recorder
tmux new-session -d -s "$SESSION" -n books \
  "exec '$WXM' record books |& tee -a data/logs/books.log"

# Markets — hourly discovery
tmux new-window -t "$SESSION:" -n markets "
  while [[ ! -f data/KILL ]]; do
    '$WXM' fetch markets |& tee -a data/logs/markets.log || true
    sleep 3600
  done
"

# Ensembles — every 30 minutes (cheap; Open-Meteo dedup makes this idempotent)
tmux new-window -t "$SESSION:" -n ensembles "
  while [[ ! -f data/KILL ]]; do
    '$WXM' fetch ensembles |& tee -a data/logs/ensembles.log || true
    sleep 1800
  done
"

# Calibrate — every 15 minutes (recompute probs.json from latest forecasts)
tmux new-window -t "$SESSION:" -n calibrate "
  while [[ ! -f data/KILL ]]; do
    '$WXM' calibrate |& tee -a data/logs/calibrate.log || true
    sleep 900
  done
"

# Paper — long-lived signal loop
tmux new-window -t "$SESSION:" -n paper \
  "exec '$WXM' paper run |& tee -a data/logs/paper.log"

# Nightly orchestration — fires when local hour rolls into a quiet window
tmux new-window -t "$SESSION:" -n nightly "
  while [[ ! -f data/KILL ]]; do
    HOUR=\$(date -u +%H)
    if [[ \$HOUR == \"09\" ]]; then
      '$WXM' cycle nightly |& tee -a data/logs/nightly.log || true
      sleep 3600
    else
      sleep 600
    fi
  done
"

echo "wxm tmux session started. Attach with: tmux attach -t $SESSION"
echo "Halt with: touch $REPO_ROOT/data/KILL"
