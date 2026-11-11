#!/usr/bin/env bash
# Werdens Media LLM — installatie op een Proxmox VE-host.
#
# Draai dit als root in de shell van de Proxmox-host, in één regel:
#
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/Theberas04/werdens-media-llm/main/deploy/proxmox/install.sh)"
#
# Het script haalt de kit op naar /opt/werdens-media-llm-kit, pakt hem daar uit en
# draait create-werdens-media-llm-lxc.sh. De kit blijft staan, zodat je daarna
# check-werdens-media-llm.sh en update-from-github.sh bij de hand hebt.
#
# Alles wat het maakscript kent mag je ervoor zetten:
#
#   CORES=6 MEMORY=16384 DISK=80 STATIC_IP=192.168.1.100/24 GATEWAY=192.168.1.1 \
#     bash -c "$(curl -fsSL https://raw.githubusercontent.com/Theberas04/werdens-media-llm/main/deploy/proxmox/install.sh)"
#
# Eerst alleen kijken wat er gebeurt, zonder iets aan te maken:
#   DROOG=1 bash -c "$(curl -fsSL .../install.sh)"
set -euo pipefail

REPO="${WMLM_REPO:-Theberas04/werdens-media-llm}"
TAK="${WMLM_TAK:-main}"
KIT="${WMLM_KIT_DIR:-/opt/werdens-media-llm-kit}"
DROOG="${DROOG:-0}"

log(){ printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn(){ printf '\033[1;33m[!]\033[0m %s\n' "$*" >&2; }
die(){ printf '\033[1;31m[fout]\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Draai dit als root op de Proxmox-host."
command -v pct >/dev/null || die "pct niet gevonden: dit is geen Proxmox VE-host."
command -v pveam >/dev/null || die "pveam niet gevonden: dit is geen Proxmox VE-host."
command -v curl >/dev/null || die "curl ontbreekt. Installeer het met: apt-get install -y curl"
command -v tar >/dev/null || die "tar ontbreekt."

# Een token is alleen nodig als de repo (weer) privé staat; de waarde wordt nooit
# geprint. Zet hem voor het commando: GH_TOKEN=... bash -c "$(curl ...)".
TOKEN="${GH_TOKEN:-${GITHUB_TOKEN:-${TOKEN:-}}}"
URL="https://codeload.github.com/$REPO/tar.gz/refs/heads/$TAK"

# Oude kit opruimen, maar alleen als het echt onze kit is.
if [ -e "$KIT" ]; then
  if [ -f "$KIT/deploy/proxmox/create-werdens-media-llm-lxc.sh" ]; then
    log "Oude kit in $KIT opruimen…"
    rm -rf "$KIT"
  else
    die "$KIT bestaat al en is niet de kit van deze app. Verplaats die map of geef een andere plek: WMLM_KIT_DIR=/pad/naar/kit"
  fi
fi
mkdir -p "$KIT"

log "Kit ophalen van $REPO ($TAK)…"
TARBALL="$(mktemp -t wmlm-kit.XXXXXXXX.tar.gz)"
trap 'rm -f "$TARBALL"' EXIT
if [ -n "$TOKEN" ]; then
  curl -fsSL --retry 3 -H "Authorization: Bearer $TOKEN" "$URL" -o "$TARBALL" \
    || die "Kon de kit niet ophalen. Klopt het token nog?"
else
  curl -fsSL --retry 3 "$URL" -o "$TARBALL" \
    || die "Kon de kit niet ophalen van $URL.
Staat de repo op privé? Zet er dan een token voor:
  GH_TOKEN=<token> bash -c \"\$(curl -fsSL .../install.sh)\""
fi
tar xzf "$TARBALL" -C "$KIT" --strip-components=1 || die "Uitpakken mislukte."
rm -f "$TARBALL"

for f in engine/wllm.py assets/werdens-media-logo.png deploy/proxmox/provision-werdens-media-llm.sh; do
  [ -f "$KIT/$f" ] || die "De kit is onvolledig: $f ontbreekt."
done
log "Kit staat in $KIT"

if [ "$DROOG" = 1 ]; then
  log "DROOG=1: niets aangemaakt. Zonder DROOG was dit gestart:"
  printf '  bash %s/deploy/proxmox/create-werdens-media-llm-lxc.sh\n' "$KIT"
  exit 0
fi

printf '\n  Dit duurt een kwartier tot een half uur: Debian ophalen, llama.cpp bouwen,\n'
printf '  de modellen downloaden (enkele gigabytes) en de drie diensten opzetten.\n\n'
exec bash "$KIT/deploy/proxmox/create-werdens-media-llm-lxc.sh"
