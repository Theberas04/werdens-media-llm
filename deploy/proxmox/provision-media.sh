#!/usr/bin/env bash
# Richt de medialaag van de Werdens Media LLM in binnen een bestaande container:
# spraak naar tekst (whisper.cpp) en spraak uit (Piper, Nederlandse stemmen).
# Draai als root op de Proxmox-host:
#   bash provision-media.sh <CTID>
#
# Draait volledig op CPU, dus zonder GPU. De kant-en-klare binaries worden
# gedownload, er is geen compiler nodig.
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
[ -f "$APP_DIR/media/wmlm_media.py" ] || { printf '\033[1;31m[fout]\033[0m %s ontbreekt.\n' "$APP_DIR/media/wmlm_media.py" >&2; exit 1; }

TARGET="${TARGET:-/opt/werdens-media-llm}"
DIENST="${DIENST:-werdens-media-llm-media}"
GEBRUIKER="${GEBRUIKER:-wmlm}"
WHISPER_TAG="${WHISPER_TAG:-b5454}"
WHISPER_MODEL_FILE="${WMLM_WHISPER_MODEL_FILE:-ggml-small.bin}"
PIPER_TAG="${PIPER_TAG:-2023.11.14-2}"
STEM="${WMLM_STEM:-nl_NL-mls-medium}"
STEM_PAD="${WMLM_STEM_PAD:-nl/nl_NL/mls/medium}"
POORT="${WMLM_MEDIA_PORT:-8091}"
THREADS="${WMLM_MEDIA_THREADS:-$(nproc 2>/dev/null || echo 4)}"
TAAL="${WMLM_TAAL:-nl}"

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

log "1/5 Hulpmiddelen installeren (waaronder ffmpeg voor webm, ogg en mp3)"
binnen_bash 'export DEBIAN_FRONTEND=noninteractive; apt-get update -qq && apt-get install -y -qq python3 curl ca-certificates tar ffmpeg >/dev/null 2>&1' \
  || die "Pakketten installeren mislukte. Controleer of de container internet heeft."

log "2/5 Mappen aanmaken in $TARGET/media"
binnen mkdir -p "$TARGET/media/bin" "$TARGET/media/models" "$TARGET/media/voices" \
               "$TARGET/media/tmp" "$TARGET/logs"

log "3/5 whisper.cpp ophalen (tag $WHISPER_TAG) en het model $WHISPER_MODEL_FILE"
if ! binnen test -x "$TARGET/media/bin/whisper/whisper-cli"; then
  binnen_bash "cd $TARGET/media/bin && curl -fsSL --retry 3 -o whisper.tar.gz \
    https://github.com/ggml-org/whisper.cpp/releases/download/$WHISPER_TAG/whisper-bin-ubuntu-x64.tar.gz \
    && mkdir -p whisper && tar xzf whisper.tar.gz -C whisper && rm -f whisper.tar.gz" \
    || die "whisper.cpp downloaden mislukte. Bestaat tag $WHISPER_TAG nog? Zet WHISPER_TAG=... naar een bestaande build."
fi
if ! binnen test -s "$TARGET/media/models/$WHISPER_MODEL_FILE"; then
  log "    spraakmodel: $WHISPER_MODEL_FILE (enkele honderden MB)"
  binnen_bash "cd $TARGET/media/models && curl -fL --retry 3 -o '$WHISPER_MODEL_FILE.part' \
    'https://huggingface.co/ggerganov/whisper.cpp/resolve/main/$WHISPER_MODEL_FILE' \
    && mv '$WHISPER_MODEL_FILE.part' '$WHISPER_MODEL_FILE'" \
    || die "Spraakmodel downloaden mislukte."
fi

log "4/5 Piper en de Nederlandse stem $STEM ophalen"
if ! binnen test -x "$TARGET/media/bin/piper/piper"; then
  binnen_bash "cd $TARGET/media/bin && curl -fsSL --retry 3 -o piper.tar.gz \
    https://github.com/rhasspy/piper/releases/download/$PIPER_TAG/piper_linux_x86_64.tar.gz \
    && tar xzf piper.tar.gz && rm -f piper.tar.gz" \
    || die "Piper downloaden mislukte."
fi
for bestand in "$STEM.onnx" "$STEM.onnx.json"; do
  if ! binnen test -s "$TARGET/media/voices/$bestand"; then
    binnen_bash "cd $TARGET/media/voices && curl -fL --retry 3 -o '$bestand.part' \
      'https://huggingface.co/rhasspy/piper-voices/resolve/main/$STEM_PAD/$bestand' \
      && mv '$bestand.part' '$bestand'" \
      || die "Stem $bestand downloaden mislukte."
  fi
done

log "5/5 Server, configuratie en dienst plaatsen"
binnen mkdir -p "$TARGET/assets"
# Het merklogo dat de webpagina's linksboven in de kop tonen.
pct push "$CTID" "$APP_DIR/assets/werdens-media-logo.png" \
  "$TARGET/assets/werdens-media-logo.png" --perms 0644
pct push "$CTID" "$APP_DIR/media/wmlm_media.py" "$TARGET/media/wmlm_media.py" --perms 0755

cat > /tmp/wmlm-media.env <<EOF
WMLM_MEDIA_BIN=$TARGET/media/bin
WMLM_MEDIA_MODELS=$TARGET/media/models
WMLM_VOICES=$TARGET/media/voices
WMLM_MEDIA_TMP=$TARGET/media/tmp
WMLM_WHISPER_BIN=$TARGET/media/bin/whisper/whisper-cli
WMLM_WHISPER_MODEL=$TARGET/media/models/$WHISPER_MODEL_FILE
WMLM_PIPER_BIN=$TARGET/media/bin/piper/piper
WMLM_PIPER_STEM=$TARGET/media/voices/$STEM.onnx
WMLM_MEDIA_HOST=0.0.0.0
WMLM_MEDIA_PORT=$POORT
WMLM_MEDIA_THREADS=$THREADS
WMLM_TAAL=$TAAL
# Whisper en Piper gebruiken allebei alle rekenkracht; één tegelijk voorkomt
# dat de machine omvalt aan een lange opname.
WMLM_MEDIA_PARALLEL=1
EOF
pct push "$CTID" /tmp/wmlm-media.env "$TARGET/media/config.env" --perms 0640
rm -f /tmp/wmlm-media.env

cat > /tmp/wmlm-media.service <<EOF
[Unit]
Description=Werdens Media LLM — medialaag (spraak naar tekst en spraak uit)
Documentation=https://github.com/Theberas04/werdens-media-llm
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$GEBRUIKER
Group=$GEBRUIKER
WorkingDirectory=$TARGET/media
EnvironmentFile=$TARGET/media/config.env
ExecStart=/usr/bin/python3 $TARGET/media/wmlm_media.py serve
Restart=on-failure
RestartSec=5
TimeoutStopSec=30

[Install]
WantedBy=multi-user.target
EOF
pct push "$CTID" /tmp/wmlm-media.service "/etc/systemd/system/$DIENST.service" --perms 0644
rm -f /tmp/wmlm-media.service

binnen chown -R "$GEBRUIKER:$GEBRUIKER" "$TARGET/media"
binnen_bash "systemctl daemon-reload && systemctl enable $DIENST >/dev/null 2>&1 && systemctl restart $DIENST"

log "Testen of de medialaag antwoordt"
OK=0
for _ in $(seq 1 20); do
  if binnen_bash "curl -fsS --max-time 3 http://127.0.0.1:$POORT/health >/dev/null 2>&1"; then OK=1; break; fi
  sleep 2
done
if [ "$OK" != 1 ]; then
  warn "De medialaag antwoordt nog niet. Bekijk het journaal:"
  warn "  pct exec $CTID -- journalctl -u $DIENST -n 40 --no-pager"
  exit 1
fi
GEZOND="$(binnen_bash "curl -fsS http://127.0.0.1:$POORT/health" 2>/dev/null || echo '{}')"
IP="$(binnen ip -4 -o addr show dev eth0 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -1 || true)"

cat <<EOF

De medialaag van de Werdens Media LLM staat in container $CTID.

  spraak naar tekst : POST http://${IP:-<ip>}:$POORT/v1/audio/transcriptions
  spraak uit        : POST http://${IP:-<ip>}:$POORT/v1/audio/speech
  stemmen           : http://${IP:-<ip>}:$POORT/v1/voices
  webpagina         : http://${IP:-<ip>}:$POORT/   (opnemen met de microfoon)
  status            : $GEZOND
  dienst            : pct exec $CTID -- systemctl status $DIENST

Even proberen vanaf de host:
  curl -s http://${IP:-<ip>}:$POORT/v1/audio/speech -d '{"input":"Hallo, dit is de Werdens Media LLM."}' -o hallo.wav
EOF
