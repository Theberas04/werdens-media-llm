#!/usr/bin/env bash
# Kijkt in één keer na hoe de Werdens Media LLM ervoor staat: de container, het
# systeem, de dienst, de engine, de modellen, de poort en een echte testvraag.
# Draai als root op de Proxmox-host:
#   bash check-werdens-media-llm.sh <CTID>
set -uo pipefail

CTID="${1:-${CTID:-}}"
TARGET="${TARGET:-/opt/werdens-media-llm}"
DIENST="${DIENST:-werdens-media-llm}"
POORT="${WLLM_PORT:-11434}"

kop(){ printf '\n\033[1;36m== %s\033[0m\n' "$*"; }
binnen(){ pct exec "$CTID" -- "$@" 2>&1; }
binnen_bash(){ pct exec "$CTID" -- bash -c "$1" 2>&1; }

if [ -z "$CTID" ]; then printf '\033[1;31m[fout]\033[0m Geef het containernummer mee: bash %s <CTID>\n' "$(basename "$0")" >&2; exit 1; fi
command -v pct >/dev/null || { printf '\033[1;31m[fout]\033[0m pct niet gevonden: dit is geen Proxmox VE-host.\n' >&2; exit 1; }

kop "1. De container"
pct status "$CTID" 2>&1
printf 'geheugen-limiet: '; pct config "$CTID" 2>/dev/null | grep -E '^memory' || echo 'onbekend'
printf 'cores:           '; pct config "$CTID" 2>/dev/null | grep -E '^cores' || echo 'onbekend'
printf 'schijf-limiet:   '; pct config "$CTID" 2>/dev/null | grep -E '^rootfs' || echo 'onbekend'
if [ "$(pct status "$CTID" 2>/dev/null)" != "status: running" ]; then
  printf '\033[1;31mDe container draait niet.\033[0m Start hem met: pct start %s\n' "$CTID"
  exit 0
fi

kop "2. Het systeem"
printf 'Debian-versie: '; binnen cat /etc/debian_version
binnen_bash '. /etc/os-release 2>/dev/null; echo "codenaam: ${VERSION_CODENAME:-onbekend}"'
printf 'hostnaam:      '; binnen hostname
printf 'uptime:        '; binnen uptime | tr -s ' '
printf 'geheugen:      '; binnen_bash 'free -h | awk "/^Mem:/{print \$2\" totaal, \"\$7\" beschikbaar\"}"'
printf 'python:        '; binnen_bash 'python3 -V'
printf 'adressen:\n'; binnen ip -4 -o addr show | sed 's/^/    /'

kop "3. De dienst"
printf 'actief:            '; binnen systemctl is-active "$DIENST"
printf 'start bij boot:    '; binnen systemctl is-enabled "$DIENST"
printf 'laatste regels:\n'; binnen journalctl -u "$DIENST" -n 8 --no-pager 2>/dev/null | sed 's/^/    /' || echo '    geen journaal'

kop "4. De server en de poort"
printf 'antwoordt de API? '; binnen_bash "curl -fsS --max-time 5 http://127.0.0.1:$POORT/api/version" || echo 'nee'
printf 'luistert op:      \n'; binnen_bash "ss -ltnpH 2>/dev/null | grep ':$POORT' | sed 's/^/    /'" || echo '    niets op die poort'
printf 'processen:        \n'; binnen_bash 'ps -eo pid,rss,comm --sort=-rss | grep -E "python3|llama-server" | grep -v grep | sed "s/^/    /"' || true

kop "5. Modellen op schijf"
binnen_bash "ls -lh $TARGET/models/*.gguf 2>/dev/null | awk '{print \"    \"\$9\"  \"\$5}'" || echo '    geen modellen gevonden'
printf 'wat de API meldt:\n'; binnen_bash "curl -fsS --max-time 5 http://127.0.0.1:$POORT/api/tags | jq -r '.models[] | \"    \\(.name)  \\(.details.parameter_size)  \\(.details.quantization_level)\"' 2>/dev/null" || echo '    niets'
printf 'contextvenster:   '; binnen_bash "curl -fsS --max-time 5 http://127.0.0.1:$POORT/v1/models | jq -r '.data[0].context_length'" 2>/dev/null || echo 'onbekend'

kop "6. De engine"
printf 'llama-server:     '; binnen_bash "ls -d $TARGET/engine/llama-*/llama-server 2>/dev/null | head -1" || echo 'ontbreekt'
printf 'versie:           '; binnen_bash "$TARGET/engine/llama-*/llama-server --version 2>&1 | head -1" || echo 'onbekend'

kop "7. Echte testvraag (laadt het model, dus dit duurt even)"
MODEL="$(binnen_bash "curl -fsS --max-time 5 http://127.0.0.1:$POORT/api/tags | jq -r '.models[0].name'" 2>/dev/null)"
if [ -n "$MODEL" ] && [ "$MODEL" != "null" ]; then
  printf 'model: %s\n' "$MODEL"
  START=$(date +%s)
  ANTWOORD="$(binnen_bash "curl -fsS --max-time 300 http://127.0.0.1:$POORT/api/chat -d '{\"model\":\"$MODEL\",\"messages\":[{\"role\":\"user\",\"content\":\"Zeg alleen: hallo\"}],\"stream\":false,\"options\":{\"num_predict\":8}}' | jq -r '.message.content'" 2>/dev/null)"
  EIND=$(date +%s)
  printf 'antwoord: %s  (%s seconden)\n' "${ANTWOORD:-geen antwoord}" "$((EIND - START))"
else
  echo 'geen model gevonden; haal er een met: pct exec '"$CTID"' -- python3 '"$TARGET"'/wllm.py pull hf:<repo>/<bestand>.gguf'
fi

kop "8. Werkruimtelaag (documenten met bronvermelding)"
WK_DIENST="$DIENST-werkruimte"
WK_POORT="${WMLM_WERKRUIMTE_PORT:-8090}"
printf 'dienst:            '; binnen systemctl is-active "$WK_DIENST"
printf 'antwoordt?         '; binnen_bash "curl -fsS --max-time 5 http://127.0.0.1:$WK_POORT/health" || echo 'nee'
printf 'werkruimtes:\n'; binnen_bash "curl -fsS --max-time 5 http://127.0.0.1:$WK_POORT/api/workspaces | jq -r '.workspaces[] | \"    \\(.slug) — \\(.documenten) documenten, \\(.brokken) brokken\"' 2>/dev/null" || echo '    geen'
printf 'database:          '; binnen_bash "du -h $TARGET/data/werkruimte.db 2>/dev/null || echo 'nog geen database'"

kop "9. Medialaag (spraak naar tekst en spraak uit)"
MD_DIENST="$DIENST-media"
MD_POORT="${WMLM_MEDIA_PORT:-8091}"
printf 'dienst:            '; binnen systemctl is-active "$MD_DIENST"
printf 'antwoordt?         '; binnen_bash "curl -fsS --max-time 5 http://127.0.0.1:$MD_POORT/health | jq -c '{whisper:.whisper.klaar,piper:.piper.klaar,stemmen:.piper.stemmen,ffmpeg}'" || echo 'nee'
printf 'spraak uit proberen:\n'
binnen_bash "curl -fsS --max-time 120 http://127.0.0.1:$MD_POORT/v1/audio/speech -d '{\"input\":\"Test van de medialaag.\"}' -o /tmp/proef.wav && ls -lh /tmp/proef.wav | awk '{print \"    \"\$5\" audio geschreven\"}' && curl -fsS --max-time 300 http://127.0.0.1:$MD_POORT/v1/audio/transcriptions -F 'file=@/tmp/proef.wav' | jq -r '\"    teruggehoord: \" + .text'" 2>/dev/null || echo '    proef mislukte'

kop "Samenvatting"
printf 'engine      : http://<ip-van-container>:%s   (Ollama en OpenAI)\n' "$POORT"
printf 'werkruimte  : http://<ip-van-container>:%s   (documenten met bronvermelding)\n' "$WK_POORT"
printf 'media       : http://<ip-van-container>:%s   (spraak naar tekst en spraak uit)\n' "$MD_POORT"
printf 'Hermes eist minimaal 64000 tokens context; controleer dat hierboven bij "contextvenster".\n'
