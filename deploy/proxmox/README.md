# Deploykit — Werdens Media LLM op Proxmox

Alles hier draai je **als root op de Proxmox-host**, niet in een container.

| Script | Wat het doet |
|---|---|
| `create-werdens-media-llm-lxc.sh` | Maakt een unprivileged Debian-LXC aan en richt de hele stack daarin in. Dit is de makkelijkste weg. |
| `install.sh` | Haalt de kit op naar `/opt/werdens-media-llm-kit` en draait het maakscript. Dit is de one-liner. |
| `provision-werdens-media-llm.sh <CTID>` | Richt de stack in binnen een bestaande container (of opnieuw, om te herstellen). |
| `check-werdens-media-llm.sh <CTID>` | Controle in één keer: container, systeem, dienst, engine, modellen, poort, contextvenster en een echte testvraag. |
| `diagnose-werdens-media-llm.sh` | Als de container niet start: probeert het opnieuw met volledige uitleg, zoekt de bekende oorzaken op en zegt wat je moet doen. |
| `update-from-github.sh <CTID>` | Haalt de nieuwste versie van GitHub en draait de inrichting opnieuw. Modellen blijven staan. |
| `provision-media.sh <CTID>` | Alleen de medialaag: spraak naar tekst (whisper.cpp) en spraak uit (Piper met Nederlandse stemmen). Draait op CPU. |

## Snel

Plak dit in de shell van je Proxmox-host (als root):

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/Theberas04/werdens-media-llm/main/deploy/proxmox/install.sh)"
```

Dat haalt de kit op naar `/opt/werdens-media-llm-kit`, maakt een unprivileged
Debian-container aan en richt de hele stack daarin in. De kit blijft staan, zodat
je daarna `check-werdens-media-llm.sh` en `update-from-github.sh` bij de hand hebt.
De repo staat op openbaar, dus dit werkt zonder inlog.

**Eerst kijken wat er gebeurt**, zonder dat er iets wordt aangemaakt:

```bash
DROOG=1 bash -c "$(curl -fsSL https://raw.githubusercontent.com/Theberas04/werdens-media-llm/main/deploy/proxmox/install.sh)"
```

**Meer rekenkracht en een vast adres** meegeven:

```bash
CORES=6 MEMORY=16384 DISK=80 STATIC_IP=192.168.1.100/24 GATEWAY=192.168.1.1 \
  bash -c "$(curl -fsSL https://raw.githubusercontent.com/Theberas04/werdens-media-llm/main/deploy/proxmox/install.sh)"
```

**Liever zelf de bestanden in handen** (dan houd je de kit in je eigen map):

```bash
git clone https://github.com/Theberas04/werdens-media-llm.git
cd werdens-media-llm
bash deploy/proxmox/create-werdens-media-llm-lxc.sh
```

Of download op de GitHub-pagina *Code → Download ZIP*, zet het bestand op de
Proxmox-host en pak het daar uit met `unzip werdens-media-llm-main.zip`.

Let op: `create-werdens-media-llm-lxc.sh` alleen via `bash <(curl ...)` ophalen
werkt **niet** -- dat script heeft de rest van de kit nodig (engine, medialaag).
`install.sh` haalt daarom de hele map op; daarom is dat de one-liner.

Wordt de repo weer privé, zet dan `GH_TOKEN=<token>` voor het commando.

## De container start niet

`pct start` faalt wel eens met alleen `__lxc_start: … Failed to spawn container`.
Die regel zegt níet waaróm; de oorzaak staat in de regels erboven, en die zie je
alleen met `--debug`. Draai daarom dit, als root op de host:

```bash
CTID=105 bash -c "$(curl -fsSL https://raw.githubusercontent.com/Theberas04/werdens-media-llm/main/deploy/proxmox/diagnose-werdens-media-llm.sh)"
```

Dat probeert het nog één keer met volledige uitleg, zoekt de bekende oorzaken op en
print in gewone taal wat je moet doen. Zonder `CTID=` zoekt het zelf de container
met de hostnaam `werdens-media-llm`.

De vier die het vaakst voorkomen:

1. **De template is voor een andere processor.** Proxmox 9 zet in de templatelijst
   zowel `amd64` als `arm64`-bestanden. Zonder filter pakt een keuze op naam de
   arm64-versie, en dan wordt de container wél aangemaakt maar start hij niet: de
   rootfs is voor een andere processor (`Failed to spawn container`, in de
   volledige log `Failed to exec "/sbin/init"`). Het maakscript filtert nu op de
   architectuur van de host en weigert een template die er niet bij hoort.
2. **De schijf van de container is leeg** (`Failed to exec "/sbin/init"`, `/etc not
   present in CT`). De container is aangemaakt, maar de template is er niet in
   gekomen. Meestal door de gekozen opslag. Het maakscript kiest die nu zelf (de
   actieve opslag met de meeste ruimte) in plaats van blind `local-lvm`; met
   `ROOTFS_STORAGE=` wijs je er zelf een aan. Daarna: `pct destroy <CTID>` en
   opnieuw installeren.
3. **De host houdt het tegen** (`Failed to clone a new set of namespaces`, `Failed
   to mount "proc"`, `Failed to load bpf program`). Dan faalt élke container op die
   machine, meestal na een kernel- of lxc-update: `apt-get install -y --reinstall
   lxc-pve apparmor && systemctl restart apparmor`, of de host herstarten.
4. **De opslag geeft een symlink-pad** (`symlink encountered`). Bekende fout met
   iSCSI-/LVM-opslag in Proxmox 9. Kies een andere opslag voor de schijf.

## Opslag en architectuur

Het maakscript kiest de opslag zelf en zet **lokale opslag vóór netwerkopslag**. Dat
is niet voor de snelheid: op een NAS mag root vaak niet schrijven (`root_squash`),
en dan mislukt het ophalen van de template en het maken van de schijf met
`Permission denied`. Voor het kiezen proeft het script of root er echt mag
schrijven; is geen enkele opslag beschrijfbaar, dan stopt het met uitleg in plaats
van halverwege. Wil je de NAS toch gebruiken, zet die export dan op
`no_root_squash`, of wijs hem aan met `ROOTFS_STORAGE=` en `TEMPLATE_STORAGE=`.

De kit werkt op **amd64 (Intel/AMD) en arm64**. De kant-en-klare builds van
llama.cpp, whisper.cpp en Piper bestaan voor allebei; het script kiest de juiste op
basis van `dpkg --print-architecture`. Op arm64 is rekenen trager — reken op de
helft of minder van wat een vergelijkbare x86-machine doet.

## Instellingen (env-variabelen)

| Variabele | Default | Betekenis |
|---|---|---|
| `CTID` | eerste vrije nummer | containernummer |
| `CT_HOSTNAME` | `werdens-media-llm` | hostnaam van de container |
| `CORES` / `MEMORY` / `SWAP` | `4` / `12288` / `2048` | rekenkracht en geheugen (MB) |
| `DISK` | `60` | schijf in GB — modellen zijn groot |
| `ROOTFS_STORAGE` | actieve opslag met de meeste ruimte | waar de schijf van de container komt |
| `TEMPLATE_STORAGE` | idem, voor templates | waar de Debian-template komt te staan |
| `DEBIAN_MAJOR` | `13` | Debian-versie van de container |
| `STATIC_IP` / `GATEWAY` | leeg (DHCP) | vast adres, bijvoorbeeld `192.168.1.100/24` |
| `BRIDGE` | `vmbr0` | netwerkbrug |
| `WMLM_MODEL_REPO` / `WMLM_MODEL_FILE` | Qwen3-4B-Instruct-2507 Q4_K_M | standaard chatmodel |
| `WMLM_EMBED_REPO` / `WMLM_EMBED_FILE` | nomic-embed-text-v1.5 Q8_0 | model voor de vectordatabase |
| `SKIP_MODELS` | `0` | op `1` zetten om geen modellen te downloaden |
| `LLAMA_TAG` | `b12262` | versie van de llama.cpp-build |
| `WLLM_CTX` | `65536` | contextvenster (Hermes eist minimaal 64000) |
| `WLLM_THREADS` | `0` (automatisch) | rekenkracht-threads; `0` laat de engine zelf kijken wat de machine aankan |
| `WLLM_UPSTREAM_TIMEOUT` | `1800` | seconden die de engine op de rekenkern wacht; op een cpu mag één lange vraag rustig een kwartier duren |

Voorbeeld met een vast adres en meer rekenkracht:

```bash
CORES=6 MEMORY=16384 DISK=80 STATIC_IP=192.168.1.100/24 GATEWAY=192.168.1.1 \
  bash deploy/proxmox/create-werdens-media-llm-lxc.sh
```

## Rekenkracht: lees dit voordat je klaagt over snelheid

Op een CPU zonder GPU werkt het, maar het is traag. Gemeten op 4 vCPU met 64k
context: ongeveer 30 tokens per seconde promptverwerking, dus een lange prompt
duurt minuten. Met een NVIDIA-GPU (12 GB of meer) gaat het tientallen keren
sneller. Zet de container op een machine met een GPU als je hem als dagelijkse
assistent wil gebruiken.

## Wat er in de container komt te staan

```
/opt/werdens-media-llm/
├── wllm.py                      de engine (Ollama- en OpenAI-API, poort 11434)
├── config.env                   instellingen (systemd leest dit)
├── engine/llama-<tag>/          de llama.cpp-engine
├── models/*.gguf                de modellen
├── logs/                        journaal per model
├── workspace/wmlm_workspace.py  de werkruimtelaag (poort 8090)
├── data/werkruimte.db           documenten, brokken en vectoren
└── media/                       de medialaag (poort 8091)
    ├── wmlm_media.py
    ├── bin/whisper/             whisper-cli
    ├── bin/piper/               piper
    ├── models/ggml-small.bin    spraakmodel
    └── voices/nl_NL-*.onnx      Nederlandse stem
```

Drie diensten (systemd):

| Dienst | Poort | Wat |
|---|---|---|
| `werdens-media-llm` | 11434 | de engine — dezelfde poort als Ollama, zodat bestaande clients meteen werken |
| `werdens-media-llm-werkruimte` | 8090 | documenten met bronvermelding, webinterface, embed-widget |
| `werdens-media-llm-media` | 8091 | spraak naar tekst en spraak uit |
