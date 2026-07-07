#!/usr/bin/env bash
# Auto-restart wrapper for the EU recompute. Resumes via checkpoint after each crash.
# Stop with: tmux kill-session -t recompute-eu  (or touch /tmp/stop_recompute_eu)
set -u
REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
source .venv/bin/activate

LOG_DIR="logs"
mkdir -p "$LOG_DIR"
STOP_FILE="/tmp/stop_recompute_eu"
ATTEMPT=0

while true; do
  if [[ -f "$STOP_FILE" ]]; then echo "[wrapper] stop file present, exiting"; break; fi
  ATTEMPT=$((ATTEMPT + 1))
  TS=$(date +%Y%m%d_%H%M%S)
  LOG="$LOG_DIR/recompute_eu_attempt${ATTEMPT}_${TS}.log"
  echo "[wrapper] attempt $ATTEMPT — log $LOG" | tee -a "$LOG_DIR/recompute_eu.wrapper.log"

  PYTHONUNBUFFERED=1 PYTHONPATH=src stdbuf -oL python scripts/recompute_all_profiles.py \
    --sources healthyfoods,myplate,foodhero,recipe1m,irish_safefood \
    --write \
    --checkpoint-every 25 \
    2>&1 | grep -vE 'Loading weights|Materializing|UNEXPECTED|LOAD REPORT' | tee "$LOG"

  RC=${PIPESTATUS[0]}
  echo "[wrapper] attempt $ATTEMPT exited with rc=$RC" | tee -a "$LOG_DIR/recompute_eu.wrapper.log"
  if [[ $RC -eq 0 ]]; then echo "[wrapper] script returned 0 — done"; break; fi
  echo "[wrapper] sleeping 30s before retry"
  sleep 30
done
