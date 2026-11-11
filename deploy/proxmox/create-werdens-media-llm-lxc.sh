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
# Architectuur en opslag worden hieronder bepaald (zie 'Opslag kiezen').
ARCH="${ARCH:-$(dpkg --print-architecture 2>/dev/null || echo amd64)}"
TEMPLATE_STORAGE="${TEMPLATE_STORAGE:-}"
ROOTFS_STORAGE="${ROOTFS_STORAGE:-}"
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
TARGET="${TARGET:-/opt/werdens-media-llm}"
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

case "$ARCH" in
  amd64) ARCH_NAAM="x86-64" ;;
  arm64) ARCH_NAAM="arm64"  ;;
  *) die "Onbekende architectuur '$ARCH'. Deze kit is er voor amd64 (Intel/AMD) en arm64." ;;
esac
log "Host: $ARCH ($ARCH_NAAM)"

# --- Opslag kiezen -----------------------------------------------------------
# Een NAS als opslag klinkt logisch, maar root mag daar vaak niet schrijven
# (root_squash): dan mislukt het downloaden van de template en het maken van de
# schijf met 'Permission denied'. Daarom eerst lokale opslag, en we proeven of
# root er echt mag schrijven voordat we er iets op zetten.
opslag_kandidaten(){
  # Lokale opslag eerst (1 = blokopslag, 2 = map), netwerkopslag achteraan;
  # binnen een soort de opslag met de meeste ruimte vooraan.
  pvesm status --content "$1" 2>/dev/null | awk 'NR>1 && $3=="active"{
      t=$2
      p = (t=="lvmthin" || t=="zfspool" || t=="btrfs") ? 1 : (t=="lvm" || t=="dir") ? 2 : 3
      printf "%d %019d %s\n", p, 999999999999999999-$6, $1
    }' | sort | awk '{print $3}'
}
opslag_pad(){
  local s="$1" t pad
  t="$(pvesm status 2>/dev/null | awk -v s="$s" 'NR>1 && $1==s{print $2}')"
  pad="$(awk -v s="$s" '$1 ~ /:$/ {naam=$2} naam==s && $1=="path" {print $2; exit}' \
    /etc/pve/storage.cfg 2>/dev/null)"
  if [ -z "$pad" ]; then
    case "$t" in nfs|cifs|glusterfs) pad="/mnt/pve/$s" ;; esac
  fi
  printf '%s' "$pad"
}
opslag_schrijfbaar(){
  local pad; pad="$(opslag_pad "$1")"
  [ -n "$pad" ] || return 0                  # blokopslag heeft geen pad
  [ -d "$pad" ] || return 1
  local proef="$pad/.wmlm-schrijfproef.$$"
  touch "$proef" 2>/dev/null || return 1
  rm -f "$proef"; return 0
}
kies_opslag(){
  local content="$1" k
  for k in $(opslag_kandidaten "$content"); do
    if opslag_schrijfbaar "$k"; then printf '%s' "$k"; return 0; fi
    warn "Opslag '$k' is niet beschrijfbaar door root (bijvoorbeeld een NAS met root_squash); ik sla die over."
  done
  return 1
}

if [ -z "$ROOTFS_STORAGE" ]; then
  ROOTFS_STORAGE="$(kies_opslag rootdir)" || die "Geen beschrijfbare opslag gevonden voor de schijf van de container.
Kijk zelf met: pvesm status --content rootdir
Wijs er een aan met: ROOTFS_STORAGE=<naam> ...
Staat je opslag op een NAS, zet die export dan op 'no_root_squash' of gebruik lokale opslag."
fi
if [ -z "$TEMPLATE_STORAGE" ]; then
  TEMPLATE_STORAGE="$(kies_opslag vztmpl)" || TEMPLATE_STORAGE="local"
fi
log "Schijf op '$ROOTFS_STORAGE', template op '$TEMPLATE_STORAGE'"

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
# Let op: de lijst bevat templates voor meerdere architecturen (amd64 én arm64).
# Zonder filter pakt 'sort -V | tail -1' de arm64-versie, en dan start de
# container niet: het is een rootfs voor een andere processor.
TEMPLATE=$(pveam available --section system 2>/dev/null | awk '{print $NF}' \
  | grep -E "^debian-${DEBIAN_MAJOR}-standard_.*_${ARCH}\.tar" | sort -V | tail -1)
if [ -z "$TEMPLATE" ]; then
  TEMPLATE=$(pveam available --section system 2>/dev/null | awk '{print $NF}' \
    | grep -E "^debian-${DEBIAN_MAJOR}-standard_.*\.tar" | sort -V | tail -1)
  [ -n "$TEMPLATE" ] || die "Geen Debian $DEBIAN_MAJOR-template gevonden. Controleer: pveam available --section system"
  warn "Geen Debian $DEBIAN_MAJOR-template voor $ARCH in de lijst; ik gebruik $TEMPLATE."
fi
case "$TEMPLATE" in
  *_${ARCH}.tar*) : ;;
  *) die "Template $TEMPLATE hoort niet bij $ARCH. Zo'n container start niet.
Kijk zelf met: pveam available --section system | grep debian-${DEBIAN_MAJOR}" ;;
esac
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

log "Container $CTID aanmaken (Debian $DEBIAN_MAJOR, ${CORES} cores, ${MEMORY} MB, ${DISK} GB op $ROOTFS_STORAGE, ${STATIC_IP:-DHCP})…"
pct create "${ARGS[@]}"
if ! pct start "$CTID"; then
  warn "Container $CTID startte niet. De oorzaak staat in de regels hierboven;"
  warn "dit is de volledige uitleg van de laatste poging:"
  pct start "$CTID" --debug 2>&1 | tail -25 | sed 's/^/    /'
  printf '\n' >&2
  die "Diagnose met uitleg in gewone taal:
  CTID=$CTID bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Theberas04/werdens-media-llm/main/deploy/proxmox/diagnose-werdens-media-llm.sh)\"
De container is aangemaakt maar draait niet. Weghalen kan met: pct destroy $CTID"
fi
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
