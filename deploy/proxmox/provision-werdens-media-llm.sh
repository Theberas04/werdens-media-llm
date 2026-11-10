#!/usr/bin/env bash
# Richt de Werdens Media LLM in binnen een bestaande Debian-container.
# Draai als root op de Proxmox-host:
#   bash provision-werdens-media-llm.sh <CTID>
#
# Wat het doet: Python en hulpmiddelen installeren, de llama.cpp-engine ophalen
# (voorgebouwde binary, dus geen compiler nodig), de standaardmodellen downloaden,
# de server en een systemd-dienst plaatsen, en daarna testen of de API antwoordt.
set -euo pipefail

CTID="${1:-${CTID:-}}"
if [ -z "$CTID" ]; then
  printf '\033[1;31m[fout]\033[0m Geef het containernummer mee: bash %s <CTID>\n' "$(basename "$0")" >&2
  exit 1
fi
[ "$(id -u)" -eq 0 ] || { printf '\033[1;31m[fout]\033[0m Draai dit als root op de Proxmox-host.\n' >&2; exit 1; }
command -v pct >/dev/null || { printf '\033[1;31m[fout]\033[0m pct niet gevonden: dit is geen Proxmox VE-host.\n' >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"
[ -f "$APP_DIR/engine/wllm.py" ] || { printf '\033[1;31m[fout]\033[0m %s ontbreekt.\n' "$APP_DIR/engine/wllm.py" >&2; exit 1; }

TARGET="${TARGET:-/opt/werdens-media-llm}"
DIENST="${DIENST:-werdens-media-llm}"
GEBRUIKER="${GEBRUIKER:-wmlm}"
LLAMA_TAG="${LLAMA_TAG:-b12262}"
MODEL_REPO="${WMLM_MODEL_REPO:-unsloth/Qwen3-4B-Instruct-2507-GGUF}"
MODEL_FILE="${WMLM_MODEL_FILE:-Qwen3-4B-Instruct-2507-Q4_K_M.gguf}"
EMBED_REPO="${WMLM_EMBED_REPO:-nomic-ai/nomic-embed-text-v1.5-GGUF}"
EMBED_FILE="${WMLM_EMBED_FILE:-nomic-embed-text-v1.5.Q8_0.gguf}"
SKIP_MODELS="${SKIP_MODELS:-0}"
WLLM_CTX="${WLLM_CTX:-65536}"
WLLM_THREADS="${WLLM_THREADS:-$(nproc 2>/dev/null || echo 4)}"
POORT="${WLLM_PORT:-11434}"

log(){ printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn(){ printf '\033[1;33m[!]\033[0m %s\n' "$*" >&2; }
die(){ printf '\033[1;31m[fout]\033[0m %s\n' "$*" >&2; exit 1; }
binnen(){ pct exec "$CTID" -- "$@"; }
binnen_bash(){ pct exec "$CTID" -- bash -c "$1"; }

pct status "$CTID" >/dev/null 2>&1 || die "Container $CTID bestaat niet."
if [ "$(pct status "$CTID" | awk '{print $2}')" != "running" ]; then
  log "Container $CTID starten…"
  pct start "$CTID"
fi
ready=0
for _ in $(seq 1 30); do binnen true >/dev/null 2>&1 && { ready=1; break; }; sleep 2; done
[ "$ready" = 1 ] || die "Container $CTID is niet bereikbaar via pct exec."

log "1/6 Basispakketten installeren"
binnen_bash 'export DEBIAN_FRONTEND=noninteractive; apt-get update -qq && apt-get install -y -qq python3 curl ca-certificates tar jq procps >/dev/null 2>&1' \
  || die "Pakketten installeren mislukte. Controleer of de container internet heeft."

log "2/6 Gebruiker en mappen aanmaken in $TARGET"
binnen_bash "id -u $GEBRUIKER >/dev/null 2>&1 || useradd --system --home-dir $TARGET --shell /usr/sbin/nologin $GEBRUIKER"
binnen mkdir -p "$TARGET/engine" "$TARGET/models" "$TARGET/data" "$TARGET/logs"

log "3/6 llama.cpp-engine ophalen (tag $LLAMA_TAG, voorgebouwde binary)"
if ! binnen test -x "$TARGET/engine/llama-$LLAMA_TAG/llama-server"; then
  binnen_bash "cd $TARGET/engine && curl -fsSL --retry 3 -o llama.tar.gz https://github.com/ggml-org/llama.cpp/releases/download/$LLAMA_TAG/llama-$LLAMA_TAG-bin-ubuntu-x64.tar.gz && tar xzf llama.tar.gz && rm -f llama.tar.gz" \
    || die "Engine downloaden mislukte. Bestaat tag $LLAMA_TAG nog? Zet LLAMA_TAG=... naar een bestaande build."
fi
binnen test -x "$TARGET/engine/llama-$LLAMA_TAG/llama-server" || die "llama-server niet uitvoerbaar na uitpakken."
log "    engine-versie: $(binnen "$TARGET/engine/llama-$LLAMA_TAG/llama-server" --version 2>&1 | head -1)"

log "4/6 Modellen downloaden"
if [ "$SKIP_MODELS" = "1" ]; then
  warn "SKIP_MODELS=1 — modellen overgeslagen. Haal ze later met: pct exec $CTID -- python3 $TARGET/wllm.py pull hf:<repo>/<bestand>.gguf"
else
  if ! binnen test -s "$TARGET/models/$MODEL_FILE"; then
    log "    chatmodel: $MODEL_FILE (enkele GB, dit duurt even)"
    binnen_bash "cd $TARGET/models && curl -fL --retry 3 -o '$MODEL_FILE.part' 'https://huggingface.co/$MODEL_REPO/resolve/main/$MODEL_FILE' && mv '$MODEL_FILE.part' '$MODEL_FILE'" \
      || die "Chatmodel downloaden mislukte."
  fi
  if ! binnen test -s "$TARGET/models/$EMBED_FILE"; then
    log "    embeddingmodel: $EMBED_FILE (klein)"
    binnen_bash "cd $TARGET/models && curl -fL --retry 3 -o '$EMBED_FILE.part' 'https://huggingface.co/$EMBED_REPO/resolve/main/$EMBED_FILE' && mv '$EMBED_FILE.part' '$EMBED_FILE'" \
      || die "Embeddingmodel downloaden mislukte."
  fi
fi

log "5/6 Server, configuratie en dienst plaatsen"
pct push "$CTID" "$APP_DIR/engine/wllm.py" "$TARGET/wllm.py" --perms 0755

cat > /tmp/wmlm-config.env <<EOF
WLLM_HOME=$TARGET
WLLM_MODELS=$TARGET/models
WLLM_ENGINE=$TARGET/engine/llama-$LLAMA_TAG/llama-server
WLLM_HOST=0.0.0.0
WLLM_PORT=$POORT
WLLM_CTX=$WLLM_CTX
WLLM_KV_TYPE=q8_0
WLLM_THREADS=$WLLM_THREADS
WLLM_KEEP_ALIVE=300
WLLM_ENGINE_PORT=18500
EOF
pct push "$CTID" /tmp/wmlm-config.env "$TARGET/config.env" --perms 0640
rm -f /tmp/wmlm-config.env

cat > /tmp/wmlm.service <<EOF
[Unit]
Description=Werdens Media LLM — eigen LLM-server (Ollama- en OpenAI-API)
Documentation=https://github.com/Theberas04/werdens-media-llm
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$GEBRUIKER
Group=$GEBRUIKER
WorkingDirectory=$TARGET
EnvironmentFile=$TARGET/config.env
ExecStart=/usr/bin/python3 $TARGET/wllm.py serve
Restart=on-failure
RestartSec=5
# de server start en stopt de engine-processen zelf mee
KillMode=control-group
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
EOF
pct push "$CTID" /tmp/wmlm.service "/etc/systemd/system/$DIENST.service" --perms 0644
rm -f /tmp/wmlm.service

binnen chown -R "$GEBRUIKER:$GEBRUIKER" "$TARGET"
binnen_bash "systemctl daemon-reload && systemctl enable $DIENST >/dev/null 2>&1 && systemctl restart $DIENST"

log "6/6 Testen of de API antwoordt"
OK=0
for _ in $(seq 1 20); do
  if binnen_bash "curl -fsS --max-time 3 http://127.0.0.1:$POORT/api/version >/dev/null 2>&1"; then OK=1; break; fi
  sleep 2
done
if [ "$OK" != 1 ]; then
  warn "De API antwoordt nog niet. Bekijk het journaal:"
  warn "  pct exec $CTID -- journalctl -u $DIENST -n 40 --no-pager"
  exit 1
fi
VERSIE="$(binnen_bash "curl -fsS http://127.0.0.1:$POORT/api/version" 2>/dev/null || echo '?')"
MODELCOUNT="$(binnen_bash "curl -fsS http://127.0.0.1:$POORT/api/tags | jq -r '.models | length'" 2>/dev/null || echo '?')"
IP="$(binnen ip -4 -o addr show dev eth0 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -1 || true)"

cat <<EOF

Werdens Media LLM is ingericht in container $CTID.

  API      : http://${IP:-<ip-van-container>}:$POORT
  Ollama   : http://${IP:-<ip>}:$POORT/api/...      (version: $VERSIE)
  OpenAI   : http://${IP:-<ip>}:$POORT/v1/...
  modellen : $MODELCOUNT gevonden
  dienst   : pct exec $CTID -- systemctl status $DIENST
  journaal : pct exec $CTID -- journalctl -u $DIENST -f
  testen   : bash $(basename "$SCRIPT_DIR")/check-werdens-media-llm.sh $CTID

Eerste keer een model laden duurt even; daarna blijft het warm (WLLM_KEEP_ALIVE).
EOF
