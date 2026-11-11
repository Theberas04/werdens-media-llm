#!/usr/bin/env bash
# Werkt de Werdens Media LLM in een container bij vanaf GitHub. Draai als root op
# de Proxmox-host:
#   bash update-from-github.sh <CTID>
#
# De repo staat op openbaar, dus een anonieme clone werkt. Wordt hij weer privé,
# geef dan een token mee met leesrechten op de repo:
#   GH_TOKEN=ghp_xxx bash update-from-github.sh <CTID>
# Het token wordt nergens geprint; haal het daarna weer uit je geschiedenis met
#   history -d $((HISTCMD-1))   (of zet er een spatie voor)
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
TOKEN="${GH_TOKEN:-${GITHUB_TOKEN:-${TOKEN:-}}}"
if [ -n "$TOKEN" ]; then
  BASIS="https://$TOKEN@github.com/$REPO"
  CODELOAD="https://$TOKEN@codeload.github.com/$REPO"
  printf '\033[1;33m[!]\033[0m Let op: dit is een privérepo; er wordt met een token gekloond.\n\n'
else
  BASIS="https://github.com/$REPO"
  CODELOAD="https://codeload.github.com/$REPO"
fi
WERKMAP="$(mktemp -d)"
trap 'rm -rf "$WERKMAP"' EXIT

log(){ printf '\033[1;36m==>\033[0m %s\n' "$*"; }
die(){ printf '\033[1;31m[fout]\033[0m %s\n' "$*" >&2; exit 1; }

log "Nieuwste versie ophalen van $REPO ($BRANCH)"
if [ -z "$TOKEN" ]; then
  log "Geen token meegegeven. Lukt dit niet, dan is de repo privé: gebruik"
  log "  GH_TOKEN=<token> bash $(basename "$0") $CTID"
fi
if command -v git >/dev/null 2>&1; then
  git clone --depth 1 --branch "$BRANCH" "$BASIS.git" "$WERKMAP/repo" >/dev/null 2>&1 \
    || die "Klonen mislukte. Bestaat de repo en de vertakking '$BRANCH'? Is de repo privé, geef dan een token mee: GH_TOKEN=<token> bash $(basename "$0") $CTID"
else
  # Let op: github.com/<repo>/archive/... geeft 404; de tarball komt van codeload.
  curl -fsSL "$CODELOAD/tar.gz/refs/heads/$BRANCH" -o "$WERKMAP/repo.tar.gz" \
    || die "Downloaden mislukte. Is de repo privé, geef dan een token mee: GH_TOKEN=<token> bash $(basename "$0") $CTID"
  mkdir -p "$WERKMAP/repo" && tar xzf "$WERKMAP/repo.tar.gz" -C "$WERKMAP/repo" --strip-components=1
fi

[ -f "$WERKMAP/repo/deploy/proxmox/provision-werdens-media-llm.sh" ] || die "Inrichtscript niet gevonden in de download."

log "Inrichting opnieuw draaien voor container $CTID"
bash "$WERKMAP/repo/deploy/proxmox/provision-werdens-media-llm.sh" "$CTID"

log "Klaar. Controleer met: bash check-werdens-media-llm.sh $CTID"
