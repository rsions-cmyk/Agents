#!/bin/bash
# Start mailagent. Gebruik: scripts/mailagent.sh run | inbox | overzicht | zelftest | test-bronnen | agendas | agenda-test | rondes-installeren | vergeet-bron <naam>
# Met backend 'claude-code' (standaard) is geen API-sleutel nodig.
# Met backend 'api' komt de sleutel uit de macOS-sleutelhanger, nooit uit een bestand.
set -euo pipefail
MAP="$(cd "$(dirname "$0")/.." && pwd)"
cd "$MAP"
# launchd start met een kale PATH; voeg de gebruikelijke plekken van 'claude' toe.
export PATH="$HOME/.local/bin:$HOME/.claude/local:/opt/homebrew/bin:/usr/local/bin:$PATH"
if grep -qE '^\s*backend:\s*api' config.yaml 2>/dev/null && [ -z "${ANTHROPIC_API_KEY:-}" ]; then
  ANTHROPIC_API_KEY="$(security find-generic-password -s mailagent-anthropic -w 2>/dev/null || true)"
  if [ -z "$ANTHROPIC_API_KEY" ]; then
    echo "backend: api is switched on but there is no key. Add it to the keychain with:" >&2
    echo "  security add-generic-password -s mailagent-anthropic -a \"\$USER\" -w" >&2
    exit 2
  fi
  export ANTHROPIC_API_KEY
fi
export PYTHONUNBUFFERED=1   # meldingen meteen in data/*.log, ook op de achtergrond
exec "$MAP/.venv/bin/python" -m mailagent -c "$MAP/config.yaml" "$@"
