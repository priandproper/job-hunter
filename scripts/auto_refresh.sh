#!/bin/bash
# Daily auto-refresh: fetch fresh jobs from all company boards and publish to the live
# dashboard — no manual steps. Invoked by the launchd agent
# ~/Library/LaunchAgents/com.jobhunter.refresh.plist (see scripts/install_auto_refresh.sh).
# Output is appended to worker.log in the repo.

cd "$(dirname "$0")/.." || exit 1

# launchd runs with a bare PATH — add the usual tool locations + python + git + claude.
# (~/.local/bin is where the `claude` CLI lives; without it coach_rank can't call Claude.)
export PATH="$HOME/.local/bin:/Library/Frameworks/Python.framework/Versions/3.13/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

LOG="worker.log"
{
  echo ""
  echo "======== auto_refresh $(date) ========"
  python3 worker.py            # fetch fresh jobs + publish (commits + pushes)
  python3 scripts/coach_rank.py --publish   # Claude re-ranks/curates the pool + publishes coach.json
  echo "======== done $(date) ========"
} >> "$LOG" 2>&1
