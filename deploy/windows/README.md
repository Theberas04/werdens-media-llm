# Werdens Media LLM op Windows

Alles wat op Debian draait, draait ook op Windows: de drie lagen zijn gewone
Python-programma's (alleen de standaardbibliotheek) en de rekenkracht komt van de
Windows-versie van llama.cpp. Geen compiler, geen Docker, geen WSL nodig.

## Wat je nodig hebt

- Windows 10 of 11, 64-bits
- **Python 3** van [python.org](https://www.python.org/downloads/windows/) — zet
  tijdens de installatie het vinkje aan bij *Add python.exe to PATH*
- ongeveer 3 GB vrije schijfruimte (llama.cpp 25 MB + de modellen circa 2,5 GB)

## Stappen

1. Download deze repo: groene **Code**-knop → *Download ZIP*, en pak het bestand uit
   (bijvoorbeeld naar `C:\Werdens-Media-LLM`).
2. Dubbelklik op **`deploy\windows\start-werdens-media-llm.bat`**.

De eerste keer worden llama.cpp en de modellen opgehaald; dat duurt een paar
minuten. Daarna starten de drie lagen meteen en opent de chatpagina in je browser.

| laag | poort | wat |
|---|---|---|
| engine | 11434 | chatten met de AI (`/chat`) |
| werkruimte | 8090 | documenten toevoegen en bevragen met bronvermelding |
| media | 8091 | spraak in tekst en tekst in spraak |

Stoppen doet **`stop-werdens-media-llm.bat`** — dat stopt de drie lagen én de
rekenkern, zodat er geen geheugen bezet blijft.

## Rekenkracht kiezen

Standaard wordt de cpu-versie opgehaald: die werkt overal. Heb je een
videokaart, dan is dat veel sneller:

```powershell
python deploy\windows\setup.py --variant vulkan     # de meeste moderne kaarten
python deploy\windows\setup.py --variant cuda12     # NVIDIA (CUDA 12)
python deploy\windows\setup.py --variant cuda13     # NVIDIA (CUDA 13)
```

Opnieuw draaien is veilig: wat er al staat wordt overgeslagen. Met `--opnieuw`
wordt llama.cpp toch opnieuw opgehaald, met `--zonder-modellen` alleen de
rekenkracht.

## Poorten en firewall

De lagen staan standaard open voor je eigen netwerk, zodat je telefoon of laptop
er ook bij kan. Windows vraagt daar eenmalig om toestemming — sta het toe voor
*particuliere netwerken*. Wil je het alleen op deze pc, zet dan in
`start-werdens-media-llm.ps1` de drie `*_HOST`-regels op `127.0.0.1`.

## Spraak (optioneel)

Spraak werkt pas als je de twee losse programma's erbij zet. De medialaag meldt
op http://127.0.0.1:8091 wat er nog ontbreekt.

```powershell
# 1) whisper.cpp (spraak naar tekst)
curl.exe -sL -o whisper.zip https://github.com/ggml-org/whisper.cpp/releases/download/b5454/whisper-bin-x64.zip
Expand-Archive whisper.zip -DestinationPath media\bin\whisper -Force
curl.exe -sL -o media\modellen\ggml-small.bin https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.bin

# 2) Piper (tekst naar spraak, Nederlandse stem)
curl.exe -sL -o piper.zip https://github.com/rhasspy/piper/releases/download/2023.11.14-2/piper_windows_amd64.zip
Expand-Archive piper.zip -DestinationPath media\bin -Force
curl.exe -sL -o media\stemmen\nl_NL-mls-medium.onnx https://huggingface.co/rhasspy/piper-voices/resolve/main/nl/nl_NL/mls/medium/nl_NL-mls-medium.onnx
curl.exe -sL -o media\stemmen\nl_NL-mls-medium.onnx.json https://huggingface.co/rhasspy/piper-voices/resolve/main/nl/nl_NL/mls/medium/nl_NL-mls-medium.onnx.json
```

De medialaag zoekt de programma's zelf (ook `whisper-cli.exe` en `main.exe`).
Wijken de paden af, wijs ze dan aan met `WMLM_WHISPER_BIN`, `WMLM_WHISPER_MODEL`,
`WMLM_PIPER_BIN`, `WMLM_PIPER_STEM` en `WMLM_FFMPEG`.

## Als terugvalmodel in Hermes

Deze machine praat dezelfde OpenAI-API als de Linux-versie:

```
base_url: http://<ip-van-deze-pc>:11434/v1
api_key : maakt niet uit (de engine controleert hem niet)
model   : Llama-3.2-3B-Instruct-Q4_K_M
```

## Logbestanden

Elke laag schrijft naar `logs\`: `engine.log`, `werkruimte.log`, `media.log` en
per laag een `-fout.log`. Loopt er iets niet, kijk daar eerst.

## Wat er getest is

- Het ophaalprogramma (`setup.py`) is gedraaid en haalt llama.cpp correct op,
  pakt het uit en vindt `llama-server.exe` terug.
- De engine kiest op een Windows-indeling inderdaad `llama-server.exe`.
- De start- en stopbestanden zijn nog niet op een echte Windows-machine gedraaid.
  Gaat er iets mis, stuur dan de regel uit `logs\*-fout.log` mee.
