#!/usr/bin/env bash
# Werdens Media LLM — waarom start container <CTID> niet?
#
# Draai dit als root op de Proxmox-host:
#
#   CTID=105 bash -c "$(curl -fsSL https://raw.githubusercontent.com/Theberas04/werdens-media-llm/main/deploy/proxmox/diagnose-werdens-media-llm.sh)"
#
# Het script start de container één keer opnieuw met volledige uitleg, zoekt in die
# uitvoer naar de bekende oorzaken en zegt in gewone taal wat je moet doen. Zonder
# CTID pakt het de eerste container met de naam werdens-media-llm.
set -uo pipefail

CTID="${CTID:-${1:-}}"
log(){ printf '\033[1;36m==>\033[0m %s\n' "$*"; }
kop(){ printf '\n\033[1;37m%s\033[0m\n' "$*"; }
die(){ printf '\033[1;31m[fout]\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "Draai dit als root op de Proxmox-host."
command -v pct >/dev/null || die "pct niet gevonden: dit is geen Proxmox VE-host."

if [ -z "$CTID" ]; then
  CTID="$(pct list 2>/dev/null | awk '$3=="werdens-media-llm"{print $1; exit}')"
fi
[ -n "$CTID" ] || die "Geen container gevonden. Geef het nummer mee: CTID=105 bash -c \"\$(curl -fsSL ...)\""
pct config "$CTID" >/dev/null 2>&1 || die "Container $CTID bestaat niet."

LOG="$(mktemp)"
trap 'rm -f "$LOG"' EXIT

kop "1. De container opnieuw proberen te starten (met volledige uitleg)"
pct start "$CTID" --debug >"$LOG" 2>&1
UITKOMST=$?
if [ "$UITKOMST" -eq 0 ]; then
  log "Container $CTID startte nu wél. Er was niets mis met de configuratie."
  exit 0
fi
log "Mislukte opnieuw. De laatste regels van de reden:"
tail -25 "$LOG" | sed 's/^/    /'

kop "2. Wat er op deze host speelt"
printf '  Proxmox   : %s\n' "$(pveversion 2>/dev/null | head -1)"
printf '  kernel    : %s\n' "$(uname -r)"
printf '  LXC       : %s\n' "$(lxc-start --version 2>/dev/null || echo onbekend)"
printf '  cgroup    : %s\n' "$(stat -fc %T /sys/fs/cgroup 2>/dev/null)"
if command -v aa-status >/dev/null; then
  printf '  AppArmor  : %s\n' "$(aa-status --enabled 2>/dev/null && echo actief || echo uit)"
fi
printf '  containers: %s\n' "$(pct list 2>/dev/null | awk 'NR>1' | wc -l) in totaal"
printf '  draaiend  : %s\n' "$(pct list 2>/dev/null | awk 'NR>1 && $2=="running"' | wc -l)"

kop "3. De opslag van deze container"
ROOTFS="$(pct config "$CTID" | awk -F': ' '/^rootfs/{print $2}')"
printf '  rootfs    : %s\n' "${ROOTFS:-onbekend}"
OPSLAG="${ROOTFS%%:*}"
if [ -n "$OPSLAG" ]; then
  pvesm status --content rootdir 2>/dev/null | sed 's/^/    /'
  printf '  vrij op %s: %s\n' "$OPSLAG" "$(pvesm status --content rootdir 2>/dev/null | awk -v s="$OPSLAG" '$1==s{print $6" van "$4}')"
fi
printf '  schijf op de host:\n'
df -h /var/lib/lxc /var/lib/vz 2>/dev/null | sed 's/^/    /'

kop "4. Wat ik hiervan vind"

# De template is niet uitgepakt: /etc ontbreekt in de container.
if grep -qE 'Failed to exec "/sbin/init"|/etc not present in CT|unexpected ostype \(unmanaged' "$LOG"; then
  printf '\033[1;33m>> De schijf van de container is leeg: de template is niet uitgepakt.\033[0m\n'
  printf '   De container is aangemaakt, maar er zit geen Debian in. Dat komt bijna\n'
  printf '   altijd door de opslag die je bij --rootfs hebt gekozen.\n\n'
  printf '   Doe dit:\n'
  printf '     pct destroy %s\n' "$CTID"
  printf '     # kijk welke opslag je hebt en welke ruimte heeft:\n'
  printf '     pvesm status --content rootdir\n'
  printf '     # en installeer opnieuw. Het maakscript kiest nu zelf de opslag met de\n'
  printf '     # meeste ruimte; wil je er een aanwijzen, zet die dan ervoor:\n'
  printf '     ROOTFS_STORAGE=<naam-uit-die-lijst> \\\n'
  printf '       bash -c "$(curl -fsSL https://raw.githubusercontent.com/Theberas04/werdens-media-llm/main/deploy/proxmox/install.sh)"\n'
  exit 0
fi

# De opslag geeft een symlink-pad (bekend met iSCSI/LVM in PVE 9).
if grep -q 'symlink encountered' "$LOG"; then
  printf '\033[1;33m>> De opslag geeft een symlink-pad; Proxmox weigert dat bij het starten.\033[0m\n'
  printf '   Bekende fout met iSCSI-/LVM-opslag in Proxmox 9. Kies een andere opslag\n'
  printf '   voor de schijf van de container:\n\n'
  printf '     pct destroy %s\n' "$CTID"
  printf '     pvesm status --content rootdir\n'
  printf '     ROOTFS_STORAGE=<andere-naam> \\\n'
  printf '       bash -c "$(curl -fsSL https://raw.githubusercontent.com/Theberas04/werdens-media-llm/main/deploy/proxmox/install.sh)"\n'
  exit 0
fi

# De host zelf houdt het tegen: dan faalt elke container.
if grep -qE 'Failed to clone a new set of namespaces|Failed to mount "proc"|Failed to load bpf program|Failed to setup first automatic mounts' "$LOG"; then
  printf '\033[1;33m>> De host-kernel houdt het tegen, niet deze container.\033[0m\n'
  printf '   Dit raakt alle containers op deze machine. Meestal na een kernel- of\n'
  printf '   lxc-update. Doe dit:\n\n'
  printf '     apt-get update && apt-get install -y --reinstall lxc-pve apparmor\n'
  printf '     systemctl restart apparmor\n'
  printf '     pct start %s\n' "$CTID"
  printf '   Helpt dat niet, herstart dan de host: na een nieuwe kernel werkt de oude\n'
  printf '   gebruikersruimte soms niet meer.\n'
  exit 0
fi

# id-map: /etc/subuid en /etc/subgid op de host.
if grep -qE 'Failed to set up id mapping|newuidmap failed' "$LOG"; then
  printf '\033[1;33m>> De gebruikersafbeelding (id-map) klopt niet op de host.\033[0m\n'
  printf '   Controleer of root een bereik heeft:\n\n'
  printf '     grep -E "^root:" /etc/subuid /etc/subgid\n'
  printf '   Staat daar niets, voeg dan toe: root:100000:65536 in beide bestanden,\n'
  printf '   en probeer daarna: pct start %s\n' "$CTID"
  exit 0
fi

# AppArmor-profiel of hook die niet loopt.
if grep -qE 'autodev hooks|Script exited with status|apparmor|AppArmor' "$LOG"; then
  printf '\033[1;33m>> AppArmor of een opstarthook zit in de weg.\033[0m\n'
  printf '   Probeer het zonder de extra rechten:\n\n'
  printf '     pct set %s --features ""\n' "$CTID"
  printf '     pct start %s\n' "$CTID"
  printf '   En controleer: aa-status | head -5\n'
  exit 0
fi

if grep -qE 'Failed to mount .* on ' "$LOG"; then
  printf '\033[1;33m>> Er wordt een map van de host in de container gehangen die niet bestaat.\033[0m\n'
  printf '   Kijk in pct config %s naar regels die met mp of lxc.mount beginnen.\n' "$CTID"
  exit 0
fi

printf '\033[1;33m>> Geen bekende oorzaak gevonden. Stuur de uitvoer hierboven door.\033[0m\n'
printf '   De volledige opstartlog staat hier:\n'
cp "$LOG" /tmp/wmlm-start-"$CTID".log
printf '     /tmp/wmlm-start-%s.log\n' "$CTID"
