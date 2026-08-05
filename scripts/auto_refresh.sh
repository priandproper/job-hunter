#!/bin/bash
# Daily auto-refresh: fetch fresh jobs from all company boards and publish to the live
# dashboard — no manual steps. Invoked by the launchd agent
# ~/Library/LaunchAgents/com.jobhunter.refresh.plist (see scripts/install_auto_refresh.sh).
# Output is appended to worker.log in the repo.

cd "$(dirname "$0")/.." || exit 1

# launchd runs with a bare PATH — add the usual tool locations + python + git + claude.
export PATH="/Library/Frameworks/Python.framework/Versions/3.13/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"

LOG="worker.log"
{
  echo ""
  echo "======== auto_refresh $(date) ========"
  python3 worker.py            # worker.py publishes by default (commits + pushes)
  echo "======== done $(date) ========"
} >> "$LOG" 2>&1
