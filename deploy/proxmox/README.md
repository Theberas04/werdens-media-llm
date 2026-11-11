# Deploykit — Werdens Media LLM op Proxmox

Alles hier draai je **als root op de Proxmox-host**, niet in een container.

| Script | Wat het doet |
|---|---|
| `create-werdens-media-llm-lxc.sh` | Maakt een unprivileged Debian-LXC aan en richt de hele stack daarin in. Dit is de makkelijkste weg. |
| `provision-werdens-media-llm.sh <CTID>` | Richt de stack in binnen een bestaande container (of opnieuw, om te herstellen). |
| `check-werdens-media-llm.sh <CTID>` | Controle in één keer: container, systeem, dienst, engine, modellen, poort, contextvenster en een echte testvraag. |
| `update-from-github.sh <CTID>` | Haalt de nieuwste versie van GitHub en draait de inrichting opnieuw. Modellen blijven staan. |
| `provision-media.sh <CTID>` | Alleen de medialaag: spraak naar tekst (whisper.cpp) en spraak uit (Piper met Nederlandse stemmen). Draait op CPU. |

## Snel

```bash
git clone https://github.com/Theberas04/werdens-media-llm.git
cd werdens-media-llm
bash deploy/proxmox/create-werdens-media-llm-lxc.sh
```

## Instellingen (env-variabelen)

| Variabele | Default | Betekenis |
|---|---|---|
| `CTID` | eerste vrije nummer | containernummer |
| `CT_HOSTNAME` | `werdens-media-llm` | hostnaam van de container |
| `CORES` / `MEMORY` / `SWAP` | `4` / `8192` / `2048` | rekenkracht en geheugen (MB) |
| `DISK` | `40` | schijf in GB — modellen zijn groot |
| `STATIC_IP` / `GATEWAY` | leeg (DHCP) | vast adres, bijvoorbeeld `192.168.1.100/24` |
| `BRIDGE` | `vmbr0` | netwerkbrug |
| `WMLM_MODEL_REPO` / `WMLM_MODEL_FILE` | Qwen3-4B-Instruct-2507 Q4_K_M | standaard chatmodel |
| `WMLM_EMBED_REPO` / `WMLM_EMBED_FILE` | nomic-embed-text-v1.5 Q8_0 | model voor de vectordatabase |
| `SKIP_MODELS` | `0` | op `1` zetten om geen modellen te downloaden |
| `LLAMA_TAG` | `b12262` | versie van de llama.cpp-build |
| `WLLM_CTX` | `65536` | contextvenster (Hermes eist minimaal 64000) |
| `WLLM_THREADS` | aantal CPU's | rekenkracht-threads |

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
