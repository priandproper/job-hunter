#!/bin/bash
# Install / remove the launchd agent that runs the job-hunter worker on a cadence.
# Default: every 20 minutes. Logs to worker.log in this folder.
#
#   ./schedule.sh install     # run the worker every 20 min in the background
#   ./schedule.sh status      # is it loaded?
#   ./schedule.sh uninstall   # stop it
#
# This is a LOCAL alternative to the GitHub Actions cron. The worker rebuilds
# docs/jobs.json + data/private.local.json each run. View the dashboard with:
#   cd docs && python3 -m http.server 8777   (then open http://localhost:8777)

set -e
LABEL="com.priyanka.jobhunter"
DIR="$(cd "$(dirname "$0")" && pwd)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
INTERVAL=1200   # seconds (20 min)
PYTHON="$(command -v python3)"

case "$1" in
  install)
    mkdir -p "$HOME/Library/LaunchAgents"
    cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON</string>
    <string>$DIR/worker.py</string>
  </array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>StartInterval</key><integer>$INTERVAL</integer>
  <key>RunAtLoad</key><true/>
  <key>StandardOutPath</key><string>$DIR/worker.log</string>
  <key>StandardErrorPath</key><string>$DIR/worker.log</string>
</dict>
</plist>
EOF
    launchctl unload "$PLIST" 2>/dev/null || true
    launchctl load "$PLIST"
    echo "installed — worker runs every $((INTERVAL/60)) min. Log: $DIR/worker.log"
    echo "view the dashboard with:  cd docs && python3 -m http.server 8777"
    ;;
  uninstall)
    launchctl unload "$PLIST" 2>/dev/null || true
    rm -f "$PLIST"
    echo "uninstalled"
    ;;
  status)
    launchctl list | grep "$LABEL" || echo "not loaded"
    ;;
  *)
    echo "usage: $0 {install|uninstall|status}"; exit 1;;
esac
