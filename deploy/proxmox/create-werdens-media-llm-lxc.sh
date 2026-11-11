#!/usr/bin/env bash
# Maakt een unprivileged Debian LXC aan en richt de Werdens Media LLM daarin in.
# Draai als root op de Proxmox-host:
#   bash create-werdens-media-llm-lxc.sh
#
# Alles is via env-variabelen te sturen, bijvoorbeeld:
#   CORES=6 MEMORY=16384 DISK=80 STATIC_IP=192.168.1.100/24 GATEWAY=192.168.1.1 \
#     bash create-werdens-media-llm-lxc.sh
set -euo pipefail

CTID="${CTID:-}"
CT_HOSTNAME="${CT_HOSTNAME:-werdens-media-llm}"
TEMPLATE_STORAGE="${TEMPLATE_STORAGE:-local}"
ROOTFS_STORAGE="${ROOTFS_STORAGE:-local-lvm}"
BRIDGE="${BRIDGE:-vmbr0}"
STATIC_IP="${STATIC_IP:-}"
GATEWAY="${GATEWAY:-}"
DNS_SERVER="${DNS_SERVER:-1.1.1.1}"
# Geheugen bepaalt hoe goed het model mag zijn. Bij 64k context kost een
# Qwen3-8B ongeveer 9,4 GB (gemeten), dus 12 GB laat een echt goed model toe;
# met 8 GB past alleen een klein model.
CORES="${CORES:-4}"
MEMORY="${MEMORY:-12288}"
SWAP="${SWAP:-2048}"
DISK="${DISK:-60}"
DEBIAN_MAJOR="${DEBIAN_MAJOR:-13}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

log(){ printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn(){ printf '\033[1;33m[!]\033[0m %s\n' "$*" >&2; }
die(){ printf '\033[1;31m[fout]\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Draai dit als root op de Proxmox-host."
command -v pct >/dev/null || die "pct niet gevonden: dit is geen Proxmox VE-host."
command -v pveam >/dev/null || die "pveam niet gevonden."
for f in "$APP_DIR/engine/wllm.py" "$APP_DIR/assets/werdens-media-logo.png" \
         "$SCRIPT_DIR/provision-werdens-media-llm.sh"; do
  [ -f "$f" ] || die "$f ontbreekt."
done
ip link show "$BRIDGE" >/dev/null 2>&1 || die "Bridge '$BRIDGE' bestaat niet."

if [ -n "$STATIC_IP" ]; then
  [ -n "$GATEWAY" ] || die "Geef GATEWAY op bij STATIC_IP."
  NET0="name=eth0,bridge=$BRIDGE,ip=$STATIC_IP,gw=$GATEWAY,type=veth"
else
  NET0="name=eth0,bridge=$BRIDGE,ip=dhcp,type=veth"
fi

if [ -z "$CTID" ]; then CTID="$(pvesh get /cluster/nextid 2>/dev/null || true)"; fi
[ -n "$CTID" ] || die "Kon geen vrij CTID bepalen; geef CTID=... mee."
pct status "$CTID" >/dev/null 2>&1 && die "Container $CTID bestaat al."

log "Debian $DEBIAN_MAJOR-template zoeken…"
pveam update >/dev/null 2>&1 || warn "pveam update mislukte; lokale template-lijst wordt gebruikt."
TEMPLATE=$(pveam available --section system 2>/dev/null | awk '{print $NF}' | grep -E "^debian-${DEBIAN_MAJOR}-standard_.*\.tar" | sort -V | tail -1)
[ -n "$TEMPLATE" ] || die "Geen Debian $DEBIAN_MAJOR-template gevonden. Controleer: pveam available --section system"
if ! pvesm list "$TEMPLATE_STORAGE" --content vztmpl 2>/dev/null | grep -q "$TEMPLATE"; then
  pveam download "$TEMPLATE_STORAGE" "$TEMPLATE" || die "Template downloaden mislukte."
fi

ARGS=("$CTID" "${TEMPLATE_STORAGE}:vztmpl/${TEMPLATE}"
  --hostname "$CT_HOSTNAME"
  --cores "$CORES" --memory "$MEMORY" --swap "$SWAP"
  --rootfs "${ROOTFS_STORAGE}:${DISK}"
  --net0 "$NET0" --nameserver "$DNS_SERVER"
  --unprivileged 1 --onboot 1 --ostype debian
  --features nesting=1
  --description "Werdens Media LLM — eigen LLM-server (Ollama- en OpenAI-API)")

log "Container $CTID aanmaken (Debian $DEBIAN_MAJOR, ${CORES} cores, ${MEMORY} MB, ${DISK} GB, ${STATIC_IP:-DHCP})…"
pct create "${ARGS[@]}"
pct start "$CTID" || die "Container startte niet; inspecteer: pct config $CTID"
ready=0
for _ in $(seq 1 60); do pct exec "$CTID" -- true >/dev/null 2>&1 && { ready=1; break; }; sleep 2; done
[ "$ready" = 1 ] || die "Container werd niet bereikbaar via pct exec."

"$SCRIPT_DIR/provision-werdens-media-llm.sh" "$CTID" \
  || die "Inrichten mislukte. Herstel met: bash $SCRIPT_DIR/provision-werdens-media-llm.sh $CTID"

IP=$(pct exec "$CTID" -- ip -4 -o addr show dev eth0 2>/dev/null | awk '{print $4}' | cut -d/ -f1 | head -1 || true)
[ -n "$IP" ] || IP="${STATIC_IP%%/*}"

cat <<EOF

Werdens Media LLM staat in LXC $CTID.

  API      : http://${IP:-<ip-van-container>}:11434
  Ollama   : http://${IP:-<ip>}:11434/api/...
  OpenAI   : http://${IP:-<ip>}:11434/v1/...
  controle : bash $SCRIPT_DIR/check-werdens-media-llm.sh $CTID
  bijwerken: bash <(curl -fsSL https://raw.githubusercontent.com/Theberas04/werdens-media-llm/main/deploy/proxmox/update-from-github.sh) $CTID

Koppelen aan Hermes (per sessie, zonder je configuratie te veranderen):
  (naam van het gekozen model: pct exec $CTID -- python3 $TARGET/wllm.py list)
  hermes chat -q "vraag" -m Llama-3.2-3B-Instruct-Q4_K_M \\
    --provider custom --base-url http://${IP:-<ip>}:11434/v1 --api-key wmlm-local

Spraak proberen (medialaag, poort 8091):
  curl -s http://${IP:-<ip>}:8091/v1/audio/speech \\
    -d '{"input":"Hallo, dit is de Werdens Media LLM."}' -o hallo.wav
EOF
