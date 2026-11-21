#!/bin/bash
# Start mailagent met de API-sleutel uit de macOS-sleutelhanger (nooit in een bestand).
# Gebruik: scripts/mailagent.sh run | inbox | overzicht | login
set -euo pipefail
MAP="$(cd "$(dirname "$0")/.." && pwd)"
cd "$MAP"
if [ -z "${ANTHROPIC_API_KEY:-}" ]; then
  ANTHROPIC_API_KEY="$(security find-generic-password -s mailagent-anthropic -w 2>/dev/null || true)"
  export ANTHROPIC_API_KEY
fi
if [ -z "$ANTHROPIC_API_KEY" ] && [ "${1:-}" != "overzicht" ] && [ "${1:-}" != "login" ]; then
  echo "Geen API-sleutel. Zet hem in de sleutelhanger met:" >&2
  echo "  security add-generic-password -s mailagent-anthropic -a \"\$USER\" -w" >&2
  exit 2
fi
exec "$MAP/.venv/bin/python" -m mailagent -c "$MAP/config.yaml" "$@"
