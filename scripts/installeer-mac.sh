#!/bin/bash
# Eenmalige installatie op macOS: virtuele omgeving, config en twee launchd-taken.
#   nl.mailagent.run    haalt op werkdagen om 07:45, 12:45 en 16:15 mail op (aan te passen: rondes in config.yaml)
#   nl.mailagent.inbox  houdt de goedkeuringspagina op http://127.0.0.1:8765 open
set -euo pipefail
MAP="$(cd "$(dirname "$0")/.." && pwd)"
cd "$MAP"

python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -e ".[outlook,agenda]"
chmod +x scripts/mailagent.sh

[ -f config.yaml ] || { cp config.example.yaml config.yaml; echo "config.yaml created: fill it in first."; }
mkdir -p data "$HOME/Library/LaunchAgents"

schrijf_plist() {  # $1 label, $2 opdracht, $3 extra XML
cat > "$HOME/Library/LaunchAgents/$1.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$1</string>
  <key>ProgramArguments</key><array><string>$MAP/scripts/mailagent.sh</string><string>$2</string></array>
  <key>WorkingDirectory</key><string>$MAP</string>
  <key>StandardOutPath</key><string>$MAP/data/$2.log</string>
  <key>StandardErrorPath</key><string>$MAP/data/$2.log</string>
  $3
</dict></plist>
PLIST
}

TIJDEN=""
for dag in 1 2 3 4 5; do
  for tijd in "7 45" "12 45" "16 15"; do
    set -- $tijd
    TIJDEN="$TIJDEN<dict><key>Weekday</key><integer>$dag</integer><key>Hour</key><integer>$1</integer><key>Minute</key><integer>$2</integer></dict>"
  done
done
schrijf_plist nl.mailagent.run run "<key>StartCalendarInterval</key><array>$TIJDEN</array>"
schrijf_plist nl.mailagent.inbox inbox "<key>RunAtLoad</key><true/><key>KeepAlive</key><true/>"

cat <<TXT

Installation complete. Next steps:
  1. Fill in config.yaml (sources, rules) and the files in projecten/.
  2. Add your API key to the keychain:
       security add-generic-password -s mailagent-anthropic -a "\$USER" -w
  3. For Outlook: scripts/mailagent.sh login
  4. Test by hand: scripts/mailagent.sh run
  5. Switch on the background jobs:
       launchctl load ~/Library/LaunchAgents/nl.mailagent.run.plist
       launchctl load ~/Library/LaunchAgents/nl.mailagent.inbox.plist
  To switch off: launchctl unload <same path>
TXT
