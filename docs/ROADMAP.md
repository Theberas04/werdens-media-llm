# Roadmap — Werdens Media LLM

## Laag 1 — Engine: klaar

`engine/wllm.py` is een eigen LLM-server in pure Python (alleen de standaard­bibliotheek,
geen pip-pakketten) met dezelfde API als Ollama én de OpenAI-API op dezelfde poort.

Wat er in zit:

- modellen beheren: `.gguf`-bestanden, manifests, digest, ophalen (`pull`) en verwijderen
- enginebeheer: één `llama-server` per model en context, health-check vóór doorsturen,
  warm houden en ontladen na inactiviteit
- contextvenster per verzoek (`num_ctx`), zoals Ollama
- het contextvenster wordt ook gemeld waar Ollama-clients het zoeken
  (`/api/show` → `model_info["<arch>.context_length"]` en `parameters: num_ctx …`)
- streaming in beide vormen: NDJSON (Ollama) en SSE (OpenAI)
- embeddings via de engine, met automatische `--embeddings`-vlag voor embedding-modellen

Getest op: Debian 13, 4 vCPU (i5-7500T), 7,6 GB RAM, geen GPU.

### Gemeten op die machine

| Model | Situatie | Uitkomst |
|---|---|---|
| Llama-3.2-1B-Instruct Q4_K_M | 4k context | ~16 tok/s genereren, ~105 tok/s promptverwerking |
| Llama-3.2-1B-Instruct Q4_K_M | 64k context, q8_0 KV | ~30 tok/s promptverwerking → een Hermes-turn duurt ruim tien minuten |

Conclusie: op CPU is dit bruikbaar voor losse vragen en documentvragen, niet als
agent-brein. Voor agentwerk is een GPU met 12 GB of meer nodig (bijvoorbeeld een
tweedehands RTX 3060 12 GB of 3090 24 GB).

### Valkuilen die al verholpen zijn

1. `llama-server` verdeelt de KV-cache over parallelle slots. Zonder `--parallel 1`
   krijgt één verzoek maar een fractie van de context en volgt
   `Context size has been exceeded`. Opgelost.
2. Zonder contextmelding raadt de client het venster en faalt hij pas halverwege.
   Opgelost (zie hierboven).
3. Een ongebruikt model werd ontladen terwijl er nog een verzoek liep. Opgelost met
   een bezig-teller die het ontladen tegenhoudt.

## Laag 2 — Werkruimte: in aanbouw

Eigen implementatie, geen overgenomen code. Wat er komt:

- **Werkruimtes** per project: elk zijn eigen documenten, instellingen en gesprekken
- **Documenten erin** met bronvermelding in het antwoord: tekst, markdown, PDF, Word,
  webpagina's, GitHub-repo's
- **Vectordatabase** op SQLite met cosine-zoeken; embeddings via de eigen engine
- **Model-routing**: regels die per vraag het beste model kiezen (lokaal voor simpel,
  zwaarder model voor moeilijk)
- **Embed-widget**: een chatvenster dat je op je eigen websites zet
- **REST-API** met sleutels per werkruimte, voor je eigen apps
- **Gesprekken en threads** met geschiedenis per werkruimte

## Laag 3 — Media: spraak werkt

`media/wmlm_media.py` is gebouwd en getest op CPU (geen GPU nodig):

| Functie | Endpoint | Techniek | Gemeten op 4 vCPU |
|---|---|---|---|
| Spraak naar tekst | `/v1/audio/transcriptions` | whisper.cpp, model `small` | 6,8 s audio in 9,8 s (dus iets langzamer dan realtime) |
| Spraak uit | `/v1/audio/speech` | Piper, stem `nl_NL-mls-medium` | 8,7 s audio in 0,54 s (16× sneller dan realtime) |
| Beeld | `/v1/images/generations` | ComfyUI, zodra `WMLM_COMFY_URL` is ingesteld | nog niet aangesloten |

De heen-en-weer-test is gedaan: Piper sprak een Nederlandse zin in, Whisper
schreef die vrijwel foutloos terug ("Alles draait lokaal, zonder abonnement en
zonder tokenkosten"). Alleen de merknaam werd verhaspeld — Piper spreekt
"Werdens Media LLM" niet goed uit, en dan hoort Whisper iets anders. Voor
spraakberichten van mensen is dat geen probleem.

Werkende endpoints: `/v1/audio/transcriptions` (multipart en rauw bestand),
`/v1/audio/speech`, `/v1/voices`, `/health` en een webpagina op `/` waarin je
met de microfoon kunt opnemen en tekst kunt laten voorlezen. Audioformaten
worden met ffmpeg omgezet naar 16 kHz mono, dus webm, ogg en mp3 werken ook.

Nog te doen: **beeld** (ComfyUI aansluiten) en **video** (Wan of LTX-Video —
vereist een NVIDIA-GPU met 12 tot 24 GB).

## Onderdelen van derden en hun licenties

Deze staan los van de eigen code en houden hun eigen licentie:

| Onderdeel | Licentie |
|---|---|
| llama.cpp en de engine-binaries | MIT (ggml-org) |
| Modellen (Qwen3, Nomic embed, Whisper, Piper-stemmen) | per model; Qwen3 is Apache-2.0, Nomic embed is Apache-2.0 |
| Llama-modellen | Llama Community License — bij afgeleide modellen moet de naam met "Llama" beginnen en moet "Built with Llama" vermeld worden |

De eigen code (`engine/`, `workspace/`, `media/`, de deploykit) valt onder
"alle rechten voorbehouden".
