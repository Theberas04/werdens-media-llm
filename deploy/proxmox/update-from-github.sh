#!/usr/bin/env bash
# Werkt de Werdens Media LLM in een container bij vanaf GitHub. Draai als root op
# de Proxmox-host:
#   bash <(curl -fsSL https://raw.githubusercontent.com/Theberas04/werdens-media-llm/main/deploy/proxmox/update-from-github.sh) <CTID>
#
# Haalt de nieuwste versie van de hoofdvertakking op en draait het inrichtscript
# opnieuw. Modellen blijven staan (het script slaat bestaande bestanden over).
set -euo pipefail

CTID="${1:-${CTID:-}}"
if [ -z "$CTID" ]; then
  printf '\033[1;31m[fout]\033[0m Geef het containernummer mee: bash update-from-github.sh <CTID>\n' >&2
  exit 1
fi
[ "$(id -u)" -eq 0 ] || { printf '\033[1;31m[fout]\033[0m Draai dit als root op de Proxmox-host.\n' >&2; exit 1; }

REPO="${REPO:-Theberas04/werdens-media-llm}"
BRANCH="${BRANCH:-main}"
WERKMAP="$(mktemp -d)"
trap 'rm -rf "$WERKMAP"' EXIT

log(){ printf '\033[1;36m==>\033[0m %s\n' "$*"; }
die(){ printf '\033[1;31m[fout]\033[0m %s\n' "$*" >&2; exit 1; }

log "Nieuwste versie ophalen van $REPO ($BRANCH)"
if command -v git >/dev/null 2>&1; then
  git clone --depth 1 --branch "$BRANCH" "https://github.com/$REPO.git" "$WERKMAP/repo" >/dev/null 2>&1 \
    || die "Klonen mislukte. Bestaat de repo en de vertakking '$BRANCH'?"
else
  curl -fsSL "https://github.com/$REPO/archive/refs/heads/$BRANCH.tar.gz" -o "$WERKMAP/repo.tar.gz" \
    || die "Downloaden mislukte."
  mkdir -p "$WERKMAP/repo" && tar xzf "$WERKMAP/repo.tar.gz" -C "$WERKMAP/repo" --strip-components=1
fi

[ -f "$WERKMAP/repo/deploy/proxmox/provision-werdens-media-llm.sh" ] || die "Inrichtscript niet gevonden in de download."

log "Inrichting opnieuw draaien voor container $CTID"
bash "$WERKMAP/repo/deploy/proxmox/provision-werdens-media-llm.sh" "$CTID"

log "Klaar. Controleer met: bash check-werdens-media-llm.sh $CTID"
