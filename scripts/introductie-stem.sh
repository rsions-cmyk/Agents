#!/bin/bash
# Legt de gesproken tekst onder de video van de introductie, met een stem van macOS.
#
#   scripts/introductie-stem.sh ~/Downloads/attache-introductie.mp4             # kiest zelf de beste Britse stem
#   scripts/introductie-stem.sh ~/Downloads/attache-introductie.mp4 "Daniel"    # of noem een stem (say -v '?' toont ze)
#   STEM_TEMPO=165 scripts/introductie-stem.sh ~/Downloads/attache-introductie.mp4   # woorden per minuut, standaard 170
#
# Nodig: macOS (say) en ffmpeg (brew install ffmpeg).
# Een betere stem: Systeeminstellingen > Toegankelijkheid > Gesproken materiaal > Systeemstem > Beheer stemmen,
# dan Engels (Verenigd Koninkrijk) en een Premium- of Enhanced-stem downloaden. De Siri-stemmen werken niet met say.
#
# De tijden in introductie-stem.tsv horen bij de video die erbij is geleverd: elke zin begint bij zijn stap.
# Past een zin niet in de ruimte tot de volgende stap, dan spreekt de stem die zin iets sneller uit.
set -eo pipefail

VIDEO="$1"
STEM="$2"
TEMPO="${STEM_TEMPO:-170}"
MAX_TEMPO=210
TSV="${STEM_TSV:-$(cd "$(dirname "$0")" && pwd)/introductie-stem.tsv}"

if [ -z "$VIDEO" ] || [ ! -f "$VIDEO" ]; then
  echo "Usage: $0 <video.mp4> [voice]" >&2; exit 1
fi
command -v say >/dev/null || { echo "This script needs macOS (the say command)." >&2; exit 1; }
command -v ffmpeg >/dev/null && command -v ffprobe >/dev/null || { echo "ffmpeg is missing. Install it with: brew install ffmpeg" >&2; exit 1; }
[ -f "$TSV" ] || { echo "Cannot find the timings file: $TSV" >&2; exit 1; }

# de beste Britse stem: Premium, dan Enhanced, dan een bekende naam, dan elke Britse stem
if [ -z "$STEM" ]; then
  LIJST="$(say -v '?' | sed -E 's/^(.*[^ ]) +([a-z]{2,3}_[A-Za-z0-9]{2,4}) +#.*$/\1	\2/' | awk -F'\t' '$2 == "en_GB" {print $1}')"
  STEM="$(printf '%s\n' "$LIJST" | grep -i '(premium)' | head -1 || true)"
  [ -n "$STEM" ] || STEM="$(printf '%s\n' "$LIJST" | grep -i '(enhanced)' | head -1 || true)"
  if [ -z "$STEM" ]; then
    for naam in Daniel Serena Jamie Kate Arthur Stephanie Oliver; do
      STEM="$(printf '%s\n' "$LIJST" | grep -E "^$naam( |$)" | head -1 || true)"; [ -n "$STEM" ] && break
    done
  fi
  [ -n "$STEM" ] || STEM="$(printf '%s\n' "$LIJST" | head -1)"
  [ -n "$STEM" ] || STEM="Daniel"
fi
echo "Voice: $STEM, $TEMPO words per minute"
case "$STEM" in *Premium*|*premium*|*Enhanced*|*enhanced*) ;; *)
  echo "Tip: a Premium or Enhanced voice sounds much better. See the comment at the top of this script." ;;
esac

WERK="$(mktemp -d)"; trap 'rm -rf "$WERK"' EXIT
duur(){ ffprobe -v error -show_entries format=duration -of csv=p=0 "$1" < /dev/null; }

INVOER=(-i "$VIDEO"); FILTER=""; LABELS=""; N=0; TE_LANG=0
printf '%-5s %-8s %-8s %-8s %s\n' "step" "length" "room" "speed" ""
while IFS=$'\t' read -r stap begin ruimte tekst; do
  case "$stap" in ''|\#*) continue ;; esac
  N=$((N + 1)); bestand="$WERK/stap-$stap.aiff"; tempo="$TEMPO"; noot=""
  say -v "$STEM" -r "$tempo" -o "$bestand" "$tekst" < /dev/null
  lengte="$(duur "$bestand")"
  if awk -v l="$lengte" -v r="$ruimte" 'BEGIN{exit !(l > r)}'; then
    tempo="$(awk -v t="$TEMPO" -v l="$lengte" -v r="$ruimte" -v m="$MAX_TEMPO" 'BEGIN{x = int(t * l / r * 1.04) + 1; print (x > m ? m : x)}')"
    say -v "$STEM" -r "$tempo" -o "$bestand" "$tekst" < /dev/null
    lengte="$(duur "$bestand")"; noot="spoken faster to fit"
    if awk -v l="$lengte" -v r="$ruimte" 'BEGIN{exit !(l > r)}'; then
      noot="still too long, it runs into the next step"; TE_LANG=$((TE_LANG + 1))
    fi
  fi
  printf '%-5s %-8.1f %-8.1f %-8s %s\n' "$stap" "$lengte" "$ruimte" "$tempo" "$noot"
  ms="$(awk -v b="$begin" 'BEGIN{printf "%d", b * 1000}')"
  INVOER+=(-i "$bestand")
  FILTER="$FILTER[$N:a]aresample=48000,aformat=channel_layouts=mono,adelay=$ms:all=1[s$N];"
  LABELS="$LABELS[s$N]"
done < "$TSV"

[ "$N" -gt 0 ] || { echo "The timings file has no steps." >&2; exit 1; }
LENGTE_VIDEO="$(duur "$VIDEO")"
UIT="${VIDEO%.*}-voice.mp4"
FILTER="$FILTER${LABELS}amix=inputs=$N:normalize=0:dropout_transition=0,loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000,apad,atrim=0:$LENGTE_VIDEO[stem]"
ffmpeg -y -loglevel error "${INVOER[@]}" -filter_complex "$FILTER" -map 0:v -map "[stem]" \
  -c:v copy -c:a aac -b:a 160k -movflags +faststart "$UIT"
echo
echo "Done: $UIT"
[ "$TE_LANG" -eq 0 ] || echo "Note: $TE_LANG sentence(s) run into the next step. Try a faster speed, for example STEM_TEMPO=185."
