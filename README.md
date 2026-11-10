<p align="center">
  <img src="docs/banner.svg" alt="Werdens Media LLM" width="820">
</p>

<p align="center">
  <b>Werdens Media LLM</b> — je eigen LLM-stack op je eigen server.<br>
  Draait volledig lokaal, praat Ollama én OpenAI, en koppelt aan Hermes en je eigen apps.
</p>

<p align="center">
  <img src="https://img.shields.io/badge/licentie-alle%20rechten%20voorbehouden-lightgrey" alt="Licentie">
  <img src="https://img.shields.io/badge/platform-Proxmox%20%7C%20Debian%20%7C%20Linux-informational" alt="Platform">
  <img src="https://img.shields.io/badge/python-3.11%2B-blue" alt="Python">
  <img src="https://img.shields.io/badge/engine-llama.cpp-orange" alt="Engine">
  <img src="https://img.shields.io/badge/API-Ollama%20%2B%20OpenAI-success" alt="API">
</p>

---

## Wat het is

Eén stack, drie lagen, alles op je eigen hardware:

| Laag | Wat het doet | Status |
|---|---|---|
| **1. Engine** | Eigen LLM-server met dezelfde API als Ollama plus de OpenAI-API. Beheert modellen, start en stopt de rekenkracht-engine, houdt modellen warm. | **werkt en is getest** |
| **2. Werkruimte** | Werkruimtes per project, documenten erin met bronvermelding, vectordatabase, model-routing, embed-widget voor je eigen sites. | in aanbouw |
| **3. Media** | Spraak naar tekst (Whisper), spraak uit (Piper, Nederlandse stemmen), beeld (ComfyUI/SDXL) en video. | in aanbouw |

Waarom eigen code in plaats van een bestaande app forken: één techniek in plaats
van twee, geen licentie die meereist, en alles wat erin zit is van jou.

## Snel starten (op een Debian-machine)

```bash
# 1) engine ophalen (llama.cpp, voorgebouwde binary — geen compiler nodig)
mkdir -p engine && cd engine
curl -sLO https://github.com/ggml-org/llama.cpp/releases/download/b12262/llama-b12262-bin-ubuntu-x64.tar.gz
tar xzf llama-b12262-bin-ubuntu-x64.tar.gz

# 2) server starten
cd .. && python3 engine/wllm.py serve      # luistert op :11434
```

## Installeren op Proxmox

Op de **Proxmox-host**, als root:

```bash
# de hele stack in één unprivileged LXC (Debian 13, 4 cores, 8 GB, 40 GB schijf)
bash deploy/proxmox/create-werdens-media-llm-lxc.sh
```

Of stap voor stap, in een bestaande container:

```bash
bash deploy/proxmox/provision-werdens-media-llm.sh <CTID>
bash deploy/proxmox/check-werdens-media-llm.sh <CTID>
```

Instellingen gaan via env-variabelen: `CORES`, `MEMORY`, `DISK`, `STATIC_IP`,
`GATEWAY`, `CT_HOSTNAME`, `WMLM_MODEL`, `WMLM_EMBED_MODEL`.

## API

De server praat twee talen op dezelfde poort, zodat bestaande software zonder
aanpassing werkt.

**Ollama-vorm** — `/api/version`, `/api/tags`, `/api/ps`, `/api/show`,
`/api/pull`, `/api/chat`, `/api/generate`, `/api/embed`

**OpenAI-vorm** — `/v1/models`, `/v1/chat/completions`, `/v1/completions`,
`/v1/embeddings`

```bash
# chat, Ollama-vorm met streaming
curl http://192.168.1.100:11434/api/chat -d '{
  "model": "qwen3-4b-instruct-2507-q4_k_m",
  "messages": [{"role": "user", "content": "Noem drie Nederlandse provincies."}],
  "stream": true
}'

# chat, OpenAI-vorm
curl http://192.168.1.100:11434/v1/chat/completions -d '{
  "model": "qwen3-4b-instruct-2507-q4_k_m",
  "messages": [{"role": "user", "content": "Hallo"}]
}'
```

## CLI

```bash
python3 engine/wllm.py serve        # server starten
python3 engine/wllm.py list         # modellen met grootte, quantisatie en digest
python3 engine/wllm.py ps           # wat er nu geladen is
python3 engine/wllm.py pull hf:unsloth/Qwen3-4B-Instruct-2507-GGUF/Qwen3-4B-Instruct-2507-Q4_K_M.gguf
python3 engine/wllm.py run qwen3-4b-instruct-2507-q4_k_m "Wat is een LLM?"
```

## Koppelen

**Hermes Agent** (per sessie, zonder je configuratie te veranderen):

```bash
hermes chat -q "vraag" -m qwen3-4b-instruct-2507-q4_k_m \
  --provider custom --base-url http://192.168.1.100:11434/v1 --api-key wmlm-local
```

Twee dingen om te weten voordat je dit doet:
- Hermes eist een contextvenster van **minimaal 64.000 tokens**. Zit je daaronder,
  dan weigert hij met een duidelijke melding — dat is geen storing.
- Op een CPU zonder GPU is dit **traag**: een prompt van 14.000 tokens duurt al
  snel tien minuten. Bruikbaar voor losse vragen en documentvragen, niet als
  agent-brein. Met een GPU (12 GB of meer) verandert dat volledig.

**Je eigen apps**: alles wat OpenAI of Ollama praat, kan tegen deze server. Geen
API-sleutel nodig en er verlaat niets je eigen netwerk.

## Configuratie

| Variabele | Default | Betekenis |
|---|---|---|
| `WLLM_HOME` | projectmap | basis voor modellen, logs en manifests |
| `WLLM_MODELS` | `$WLLM_HOME/models` | waar de `.gguf`-bestanden staan |
| `WLLM_ENGINE` | `$WLLM_HOME/engine/llama-b12262/llama-server` | pad naar de engine |
| `WLLM_HOST` / `WLLM_PORT` | `0.0.0.0` / `11434` | luisteradres |
| `WLLM_CTX` | `65536` | contextvenster (Hermes eist minimaal 64.000) |
| `WLLM_KV_TYPE` | `q8_0` | KV-cache-type; 8-bits halveert het geheugen |
| `WLLM_KEEP_ALIVE` | `300` | seconden voordat een ongebruikt model wordt ontladen |
| `WLLM_THREADS` | aantal CPU's | rekenkracht-threads |

## Architectuur

```
Hermes Agent  ·  je eigen apps  ·  embed-widget
        │  Ollama-API (:11434/api/*)   of   OpenAI-API (:11434/v1/*)
        ▼
  engine/wllm.py  (pure Python, alleen stdlib)
        ├── modellen     : gguf-bestanden, manifests, digest, pull en delete
        ├── enginebeheer : één llama-server per model en context, warm houden, ontladen
        └── vertaling    : Ollama- en OpenAI-verzoeken naar de engine
        ▼
  llama-server (llama.cpp)
```

## Status en roadmap

Zie [docs/ROADMAP.md](docs/ROADMAP.md). Kort: de engine werkt en is getest op een
CPU-only machine; de werkruimte- en medialaag volgen.

## Licentie

Alle rechten voorbehouden. Dit is eigen code; gebruik, kopiëren of verspreiden
alleen met toestemming van de maker. Gebruikte onderdelen van derden houden hun
eigen licentie — zie [docs/ROADMAP.md](docs/ROADMAP.md) voor de details.
