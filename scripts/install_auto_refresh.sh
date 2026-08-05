#!/bin/bash
# Install (or update) the daily auto-refresh launchd agent, so job-hunter fetches fresh
# jobs and publishes to the live dashboard on its own — no terminal, no manual publish.
#
#   ./scripts/install_auto_refresh.sh [HOUR]     # HOUR 0-23, default 8 (8am)
#   launchctl unload ~/Library/LaunchAgents/com.jobhunter.refresh.plist   # stop it
set -e
HOUR="${1:-8}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
LABEL="com.jobhunter.refresh"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

chmod +x "$REPO/scripts/auto_refresh.sh"
mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<PLISTEOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/bin/bash</string>
    <string>$REPO/scripts/auto_refresh.sh</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>$HOUR</integer><key>Minute</key><integer>0</integer></dict>
  <key>StandardOutPath</key><string>$REPO/worker.log</string>
  <key>StandardErrorPath</key><string>$REPO/worker.log</string>
  <key>RunAtLoad</key><false/>
</dict>
</plist>
PLISTEOF

launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "✅ Installed '$LABEL' — runs every day at ${HOUR}:00 and publishes automatically."
echo "   Log:     $REPO/worker.log"
echo "   Change:  ./scripts/install_auto_refresh.sh <hour>"
echo "   Stop:    launchctl unload $PLIST"
