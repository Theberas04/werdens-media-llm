# Media-laag (in aanbouw)

Hier komt de media-uitbreiding, achter dezelfde OpenAI-vormige endpoints zodat
elke client hetzelfde aanspreekt:

| Functie | Endpoint | Techniek | Op CPU? |
|---|---|---|---|
| Spraak naar tekst | `/v1/audio/transcriptions` | whisper.cpp | ja, sneller dan realtime |
| Spraak uit | `/v1/audio/speech` | Piper, Nederlandse stemmen | ja |
| Beeld | `/v1/images/generations` | ComfyUI met SDXL of FLUX | ja, maar minuten per plaat |
| Video | `/v1/videos/generations` | Wan of LTX-Video | nee — vereist een NVIDIA-GPU met 12 tot 24 GB |

Doel: een spraakbericht in je app wordt getranscribeerd, de engine antwoordt, en
Piper leest het antwoord voor — zonder dat er iets je netwerk verlaat.
