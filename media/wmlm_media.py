#!/usr/bin/env python3
"""
Werdens Media LLM — medialaag.

Spraak naar tekst (whisper.cpp) en spraak uit (Piper, Nederlandse stemmen),
achter OpenAI-vormige endpoints zodat elke bestaande client hetzelfde aanspreekt:

  POST /v1/audio/transcriptions   audiobestand → tekst   (Whisper)
  POST /v1/audio/speech           tekst → audiobestand   (Piper)
  POST /v1/images/generations     alleen als een ComfyUI-adres is ingesteld
  GET  /                          webinterface om beide richtingen te testen
  GET  /health                    status van de hulpmiddelen

Beide richtingen draaien op CPU, dus zonder GPU. Alles lokaal, geen abonnement.
Pure Python: alleen de standaardbibliotheek; de zware stukken zijn losse
programma's (whisper.cpp en Piper), net als bij de engine.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from email.parser import BytesParser
from email.policy import default as email_policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# --------------------------------------------------------------------------- #
# Configuratie
# --------------------------------------------------------------------------- #
BASIS = Path(__file__).resolve().parent
BIN = Path(os.environ.get("WMLM_MEDIA_BIN", BASIS.parent / "media" / "bin"))
STEMPEN = Path(os.environ.get("WMLM_VOICES", BASIS.parent / "media" / "voices"))
MODELLEN = Path(os.environ.get("WMLM_MEDIA_MODELS", BASIS.parent / "media" / "models"))
WERKMAP = Path(os.environ.get("WMLM_MEDIA_TMP", BASIS.parent / "data" / "media-tmp"))

WHISPER_BIN = Path(os.environ.get("WMLM_WHISPER_BIN", BIN / "whisper-cli"))
WHISPER_MODEL = Path(os.environ.get("WMLM_WHISPER_MODEL", MODELLEN / "ggml-small.bin"))
PIPER_BIN = Path(os.environ.get("WMLM_PIPER_BIN", BIN / "piper" / "piper"))
PIPER_STEM = Path(os.environ.get("WMLM_PIPER_STEM", STEMPEN / "nl_NL-mls-medium.onnx"))
FFMPEG = os.environ.get("WMLM_FFMPEG", "ffmpeg")
COMFY = os.environ.get("WMLM_COMFY_URL", "").rstrip("/")

TAAL = os.environ.get("WMLM_TAAL", "nl")
HOST = os.environ.get("WMLM_MEDIA_HOST", "0.0.0.0")
PORT = int(os.environ.get("WMLM_MEDIA_PORT", "8091"))
THREADS = int(os.environ.get("WMLM_MEDIA_THREADS", str(max(1, (os.cpu_count() or 4) // 2))))
MAX_AUDIO_BYTES = int(os.environ.get("WMLM_MAX_AUDIO_BYTES", str(64 * 1024 * 1024)))
VERSIE = "0.1.0"

WERKMAP.mkdir(parents=True, exist_ok=True)
# Whisper en Piper gebruiken allebei alle rekenkracht; één tegelijk voorkomt
# dat de machine omvalt. Zet WMLM_MEDIA_PARALLEL hoger op een sterkere machine.
PARALLEL = int(os.environ.get("WMLM_MEDIA_PARALLEL", "1"))
_media_slot = threading.Semaphore(PARALLEL)


def nu() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def sporen(pad: Path) -> bool:
    return pad.exists() and os.access(pad, os.X_OK)


def stemmen() -> list[str]:
    if not STEMPEN.exists():
        return []
    return sorted(p.stem for p in STEMPEN.glob("*.onnx"))


def stem_pad(naam: str | None) -> Path:
    if not naam or naam in ("standaard", "default", "alloy", "echo"):
        return PIPER_STEM
    kaal = naam[:-5] if naam.endswith(".onnx") else naam
    kandidaat = STEMPEN / f"{kaal}.onnx"
    if not kandidaat.exists():
        raise KeyError(f"stem '{naam}' bestaat niet; beschikbaar: {', '.join(stemmen()) or 'geen'}")
    return kandidaat


# --------------------------------------------------------------------------- #
# Audio omzetten naar wat Whisper wil: 16 kHz, mono, 16-bits PCM
# --------------------------------------------------------------------------- #
def naar_wav16(bron: Path, doel: Path) -> None:
    ffmpeg = shutil.which(FFMPEG)
    if not ffmpeg:
        raise RuntimeError(
            "ffmpeg niet gevonden; alleen WAV wordt dan geaccepteerd. Installeer ffmpeg."
        )
    uit = subprocess.run(
        [ffmpeg, "-y", "-hide_banner", "-loglevel", "error", "-i", str(bron),
         "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", str(doel)],
        capture_output=True, timeout=600,
    )
    if uit.returncode != 0:
        raise ValueError("kon het audiobestand niet lezen: " + uit.stderr.decode("utf-8", "replace")[:300])


def is_al_wav16(pad: Path) -> bool:
    """Snel voorwerk: alleen bij een 16 kHz mono WAV slaan we ffmpeg over."""
    try:
        with pad.open("rb") as f:
            kop = f.read(44)
        if kop[:4] != b"RIFF" or kop[8:12] != b"WAVE":
            return False
        kanalen = int.from_bytes(kop[22:24], "little")
        frequentie = int.from_bytes(kop[24:28], "little")
        bits = int.from_bytes(kop[34:36], "little")
        return kanalen == 1 and frequentie == 16000 and bits == 16
    except Exception:  # noqa: BLE001
        return False


# --------------------------------------------------------------------------- #
# Spraak naar tekst
# --------------------------------------------------------------------------- #
def transcriberen(audio: bytes, bestandsnaam: str = "audio", taal: str | None = None) -> dict:
    if not sporen(WHISPER_BIN):
        raise RuntimeError(f"whisper.cpp niet gevonden op {WHISPER_BIN}")
    if not WHISPER_MODEL.exists():
        raise RuntimeError(f"whisper-model niet gevonden op {WHISPER_MODEL}")
    if not audio:
        raise ValueError("geen audio ontvangen")
    suffix = Path(bestandsnaam).suffix or ".bin"
    with _media_slot:
        with tempfile.TemporaryDirectory(dir=WERKMAP) as map_:
            map_ = Path(map_)
            bron = map_ / f"in{suffix}"
            bron.write_bytes(audio)
            wav = map_ / "in16.wav"
            if is_al_wav16(bron):
                wav = bron
            else:
                naar_wav16(bron, wav)
            uit_prefix = map_ / "uit"
            begin = time.time()
            uit = subprocess.run(
                [str(WHISPER_BIN), "-m", str(WHISPER_MODEL), "-f", str(wav),
                 "-l", taal or TAAL, "-nt", "-otxt", "-of", str(uit_prefix),
                 "-t", str(THREADS)],
                capture_output=True, timeout=3600,
            )
            if uit.returncode != 0:
                raise RuntimeError(
                    "whisper faalde: " + uit.stderr.decode("utf-8", "replace")[-400:]
                )
            tekst_bestand = uit_prefix.with_suffix(".txt")
            ruw = tekst_bestand.read_text(encoding="utf-8", errors="replace") if tekst_bestand.exists() else ""
            if not ruw.strip():
                ruw = uit.stdout.decode("utf-8", "replace")
            duur = time.time() - begin
    tekst = re.sub(r"\[[\d:.\s→-]+\]", " ", ruw)
    tekst = re.sub(r"\s+", " ", tekst).strip()
    return {"tekst": tekst, "seconden": round(duur, 2), "model": WHISPER_MODEL.name, "taal": taal or TAAL}


# --------------------------------------------------------------------------- #
# Spraak uit
# --------------------------------------------------------------------------- #
def spreken(tekst: str, stem: str | None = None, tempo: float = 1.0) -> bytes:
    if not sporen(PIPER_BIN):
        raise RuntimeError(f"Piper niet gevonden op {PIPER_BIN}")
    tekst = (tekst or "").strip()
    if not tekst:
        raise ValueError("geen tekst om voor te lezen")
    model = stem_pad(stem)
    if not model.exists():
        raise RuntimeError(f"stemmodel niet gevonden op {model}")
    with _media_slot:
        with tempfile.TemporaryDirectory(dir=WERKMAP) as map_:
            uit_bestand = Path(map_) / "uit.wav"
            commando = [str(PIPER_BIN), "--model", str(model), "--output_file", str(uit_bestand)]
            if abs(tempo - 1.0) > 0.01:
                # lager is langzamer, hoger is sneller (Piper: length_scale)
                commando += ["--length_scale", f"{1.0 / max(0.25, min(4.0, tempo)):.3f}"]
            uit = subprocess.run(
                commando, input=tekst.encode("utf-8"), capture_output=True, timeout=1800,
                cwd=str(BIN / "piper") if (BIN / "piper").exists() else None,
            )
            if uit.returncode != 0 or not uit_bestand.exists():
                raise RuntimeError(
                    "Piper faalde: " + uit.stderr.decode("utf-8", "replace")[-400:]
                )
            return uit_bestand.read_bytes()


def wav_lengte(wav: bytes) -> float:
    """Duur in seconden uit de WAV-kop (16-bits PCM)."""
    try:
        import struct as _s

        byte_rate = _s.unpack("<I", wav[28:32])[0]
        return round(len(wav) / byte_rate, 2) if byte_rate else 0.0
    except Exception:  # noqa: BLE001
        return 0.0


# --------------------------------------------------------------------------- #
# Beeld (alleen als er een ComfyUI draait)
# --------------------------------------------------------------------------- #
def beeld(prompt: str, stappen: int = 20, breedte: int = 1024, hoogte: int = 1024) -> dict:
    if not COMFY:
        raise RuntimeError(
            "Beeldgeneratie is niet ingesteld. Zet WMLM_COMFY_URL naar je ComfyUI "
            "(bijvoorbeeld http://192.168.1.100:8188). Op CPU duurt een plaat minuten; "
            "met een NVIDIA-GPU met 12 GB of meer gaat het snel."
        )
    workflow = {
        "prompt": {
            "3": {"class_type": "KSampler",
                  "inputs": {"seed": int(time.time()) % 2**31, "steps": stappen, "cfg": 7.0,
                             "sampler_name": "euler", "scheduler": "normal", "denoise": 1.0,
                             "model": ["4", 0], "positive": ["6", 0], "negative": ["7", 0],
                             "latent_image": ["5", 0]}},
            "4": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": "model.safetensors"}},
            "5": {"class_type": "EmptyLatentImage", "inputs": {"width": breedte, "height": hoogte, "batch_size": 1}},
            "6": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["4", 1]}},
            "7": {"class_type": "CLIPTextEncode", "inputs": {"text": "lelijk, wazig, watermerk", "clip": ["4", 1]}},
            "8": {"class_type": "VAEDecode", "inputs": {"samples": ["3", 0], "vae": ["4", 2]}},
            "9": {"class_type": "SaveImage", "inputs": {"filename_prefix": "wmlm", "images": ["8", 0]}},
        }
    }
    req = urllib.request.Request(
        COMFY + "/prompt", data=json.dumps(workflow).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=60) as r:
        antwoord = json.load(r)
    return {"ingediend": True, "prompt_id": antwoord.get("prompt_id"), "comfyui": COMFY,
            "opmerking": "Het beeld wordt asynchroon gemaakt; haal het op bij ComfyUI (/history)."}


# --------------------------------------------------------------------------- #
# Webinterface om beide richtingen te testen
# --------------------------------------------------------------------------- #
PAGINA = """<!doctype html>
<html lang="nl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Werdens Media LLM — spraak</title><style>
 :root{--bg:#0b1020;--pane:#0f172e;--line:#243049;--txt:#e2e8f0;--dim:#94a3b8;--teal:#5eead4;--sky:#7dd3fc}
 *{box-sizing:border-box}
 body{margin:0;background:var(--bg);color:var(--txt);font:15px/1.55 ui-sans-serif,system-ui,"Segoe UI",sans-serif}
 header{padding:22px 28px 16px;border-bottom:1px solid var(--line);background:linear-gradient(180deg,#101a34,#0b1020)}
 h1{margin:0;font-size:26px}h1 span{background:linear-gradient(90deg,var(--teal),var(--sky));
   -webkit-background-clip:text;background-clip:text;color:transparent}
 .sub{color:var(--dim);font-size:13px;margin-top:4px}
 main{display:grid;grid-template-columns:1fr 1fr;gap:18px;padding:22px;max-width:1200px}
 .pane{background:var(--pane);border:1px solid var(--line);border-radius:14px;padding:18px}
 h2{margin:0 0 4px;font-size:17px}
 .hint{color:var(--dim);font-size:12px;margin-bottom:12px}
 textarea,input,select,button{font:inherit;color:var(--txt);background:#0b1220;
   border:1px solid var(--line);border-radius:9px;padding:10px 12px;width:100%}
 textarea{min-height:120px;resize:vertical}
 button{cursor:pointer;background:linear-gradient(90deg,#0e7490,#0f766e);border:0;font-weight:600;margin-top:10px}
 button.rec{background:linear-gradient(90deg,#b91c1c,#7f1d1d)}
 button:hover{filter:brightness(1.12)}
 .uit{margin-top:14px;padding:12px;border:1px dashed var(--line);border-radius:10px;
      min-height:52px;white-space:pre-wrap;background:#0b1220}
 .status{font-size:12px;color:var(--dim);margin-top:8px}
 .ok{color:var(--teal)}.fout{color:#fca5a5}
 @media(max-width:760px){main{grid-template-columns:1fr}}
</style></head><body>
<header><h1>Werdens <span>Media LLM</span> — spraak</h1>
<div class="sub">Spraak naar tekst met Whisper, spraak uit met Piper. Lokaal, op CPU, geen abonnement.
<span id="status" class="status"></span></div></header>
<main>
  <div class="pane">
    <h2>Spraak naar tekst</h2>
    <div class="hint">Neem op met de microfoon, of kies een audiobestand.</div>
    <button id="opnemen" class="rec">● Opnemen</button>
    <input type="file" id="bestand" accept="audio/*" style="margin-top:10px">
    <div class="uit" id="tekst">De getranscribeerde tekst verschijnt hier.</div>
    <div class="status" id="s1"></div>
  </div>
  <div class="pane">
    <h2>Spraak uit</h2>
    <div class="hint">Typ tekst en laat hem voorlezen met een Nederlandse stem.</div>
    <textarea id="voorleestekst">Welkom bij de Werdens Media LLM. Alles draait lokaal, zonder abonnement en zonder tokenkosten.</textarea>
    <select id="stem"><option>standaard</option></select>
    <button id="spreek">Voorlezen</button>
    <audio id="speler" controls style="width:100%;margin-top:12px"></audio>
    <div class="status" id="s2"></div>
  </div>
</main>
<script>
const $ = id => document.getElementById(id);
const stat = (id, t, fout) => { $(id).innerHTML = fout ? `<span class="fout">${t}</span>` : `<span class="ok">${t}</span>`; };
(async () => {
  try {
    const h = await (await fetch('/health')).json();
    $('status').innerHTML = `<span class="ok">whisper: ${h.whisper.klaar ? 'klaar' : 'ontbreekt'}</span>`
      + ` · <span class="ok">piper: ${h.piper.klaar ? 'klaar' : 'ontbreekt'}</span>`
      + ` · <span class="ok">ffmpeg: ${h.ffmpeg ? 'ja' : 'nee'}</span>`;
    const v = await (await fetch('/v1/voices')).json();
    $('stem').innerHTML = '<option>standaard</option>' + v.stemmen.map(s => `<option>${s}</option>`).join('');
  } catch(e) { $('status').innerHTML = '<span class="fout">status onbekend</span>'; }
})();
let recorder = null, brokken = [];
$('opnemen').onclick = async () => {
  if (recorder && recorder.state === 'recording') { recorder.stop(); return; }
  try {
    const stream = await navigator.mediaDevices.getUserMedia({audio: true});
    recorder = new MediaRecorder(stream); brokken = [];
    recorder.ondataavailable = e => brokken.push(e.data);
    recorder.onstop = async () => {
      stream.getTracks().forEach(t => t.stop());
      $('opnemen').textContent = '● Opnemen'; $('opnemen').className = 'rec';
      const blob = new Blob(brokken, {type: recorder.mimeType});
      stat('s1', 'bezig met transcriberen…');
      const fd = new FormData(); fd.append('file', blob, 'opname.webm');
      try {
        const r = await fetch('/v1/audio/transcriptions', {method:'POST', body: fd});
        const d = await r.json();
        if (!r.ok) throw new Error(d.error || r.status);
        $('tekst').textContent = d.text;
        stat('s1', `${d.duur_seconden}s audio · ${d.seconden}s rekenen`);
      } catch(e) { stat('s1', 'Fout: ' + e.message, true); }
    };
    recorder.start(); $('opnemen').textContent = '■ Stoppen';
  } catch(e) { stat('s1', 'Geen microfoon: ' + e.message, true); }
};
$('bestand').onchange = async () => {
  const f = $('bestand').files[0]; if (!f) return;
  stat('s1', 'bezig met transcriberen…');
  const fd = new FormData(); fd.append('file', f, f.name);
  try {
    const r = await fetch('/v1/audio/transcriptions', {method:'POST', body: fd});
    const d = await r.json();
    if (!r.ok) throw new Error(d.error || r.status);
    $('tekst').textContent = d.text; stat('s1', `${d.duur_seconden}s audio · ${d.seconden}s rekenen`);
  } catch(e) { stat('s1', 'Fout: ' + e.message, true); }
};
$('spreek').onclick = async () => {
  const stem = $('stem').value; stat('s2', 'bezig met inspreken…');
  try {
    const r = await fetch('/v1/audio/speech', {method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({input: $('voorleestekst').value, voice: stem})});
    if (!r.ok) { const d = await r.json(); throw new Error(d.error || r.status); }
    const blob = await r.blob();
    $('speler').src = URL.createObjectURL(blob); $('speler').play();
    stat('s2', `${(blob.size/1024).toFixed(0)} kB audio`);
  } catch(e) { stat('s2', 'Fout: ' + e.message, true); }
};
</script></body></html>
"""


# --------------------------------------------------------------------------- #
# HTTP-API
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"wmlm-media/{VERSIE}"

    def log_message(self, format, *args):  # noqa: A002
        sys.stderr.write("[wmlm-media] %s\n" % (format % args))

    # -- hulp -------------------------------------------------------------
    def _stuur(self, body: bytes, code: int = 200, ctype: str = "application/json; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code: int = 200):
        self._stuur(json.dumps(obj, ensure_ascii=False).encode(), code)

    def _fout(self, msg: str, code: int = 400):
        self._json({"error": msg}, code)

    def _ruw(self) -> bytes:
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_AUDIO_BYTES:
            raise ValueError(f"bestand te groot (max {MAX_AUDIO_BYTES // 1048576} MB)")
        return self.rfile.read(n) if n else b""

    def _multipart(self) -> dict:
        """Klein multipart-ontleden met de standaardbibliotheek (cgi bestaat niet meer
        in Python 3.13). Geeft velden als tekst en bestanden als bytes terug."""
        ctype = self.headers.get("Content-Type", "")
        if "multipart/form-data" not in ctype:
            return {}
        ruw = self._ruw()
        kop = f"Content-Type: {ctype}\r\nMIME-Version: 1.0\r\n\r\n".encode()
        bericht = BytesParser(policy=email_policy).parsebytes(kop + ruw)
        velden: dict = {}
        for deel in bericht.iter_parts():
            naam = deel.get_param("name", header="content-disposition")
            if not naam:
                continue
            inhoud = deel.get_payload(decode=True) or b""
            if isinstance(inhoud, str):
                inhoud = inhoud.encode()
            bestandsnaam = deel.get_filename()
            if bestandsnaam:
                velden[naam] = {"bytes": inhoud, "bestandsnaam": bestandsnaam}
            else:
                velden[naam] = inhoud.decode(deel.get_content_charset() or "utf-8", "replace")
        return velden

    # -- routes -----------------------------------------------------------
    def do_OPTIONS(self):
        self._stuur(b"", 204)

    def do_GET(self):
        pad = self.path.split("?")[0]
        if pad in ("/", "/index.html"):
            return self._stuur(PAGINA.encode(), 200, "text/html; charset=utf-8")
        if pad == "/health":
            return self._json(
                {
                    "status": "ok",
                    "versie": VERSIE,
                    "whisper": {"klaar": sporen(WHISPER_BIN), "programma": str(WHISPER_BIN),
                                "model": WHISPER_MODEL.name, "model_aanwezig": WHISPER_MODEL.exists()},
                    "piper": {"klaar": sporen(PIPER_BIN), "programma": str(PIPER_BIN),
                              "stemmen": stemmen()},
                    "ffmpeg": bool(shutil.which(FFMPEG)),
                    "beeld": {"comfyui": COMFY or None},
                    "taal": TAAL,
                }
            )
        if pad == "/v1/voices":
            return self._json({"stemmen": stemmen(), "standaard": PIPER_STEM.name})
        if pad == "/v1/models":
            return self._json(
                {
                    "object": "list",
                    "data": [
                        {"id": WHISPER_MODEL.stem, "object": "model", "owned_by": "wmlm",
                         "soort": "spraak naar tekst"},
                        {"id": PIPER_STEM.stem, "object": "model", "owned_by": "wmlm",
                         "soort": "spraak uit"},
                    ],
                }
            )
        return self._fout("niet gevonden", 404)

    def do_POST(self):
        pad = self.path.split("?")[0]
        try:
            if pad == "/v1/audio/transcriptions":
                return self._transcripties()
            if pad == "/v1/audio/speech":
                return self._spraak()
            if pad == "/v1/images/generations":
                b = json.loads(self._ruw() or b"{}")
                if not b.get("prompt"):
                    return self._fout("veld 'prompt' ontbreekt")
                return self._json(beeld(b["prompt"], int(b.get("steps") or 20),
                                        int(b.get("width") or b.get("size", "1024x1024").split("x")[0]),
                                        int(b.get("height") or b.get("size", "1024x1024").split("x")[1])))
            return self._fout("niet gevonden", 404)
        except KeyError as e:
            return self._fout(str(e), 404)
        except ValueError as e:
            return self._fout(str(e), 400)
        except RuntimeError as e:
            return self._fout(str(e), 503)
        except Exception as e:  # noqa: BLE001
            return self._fout(f"{type(e).__name__}: {e}", 500)

    def _transcripties(self):
        velden = self._multipart()
        if velden:
            onderdeel = velden.get("file")
            if not isinstance(onderdeel, dict):
                return self._fout("formulierveld 'file' met het audiobestand ontbreekt")
            audio, naam = onderdeel["bytes"], onderdeel["bestandsnaam"]
            taal = velden.get("language") or None
            formaat = velden.get("response_format") or "json"
        else:
            audio = self._ruw()
            naam = "opname" + (Path(self.path).suffix or ".wav")
            taal, formaat = None, "json"
        uit = transcriberen(audio, naam, taal)
        if formaat == "text":
            return self._stuur(uit["tekst"].encode(), 200, "text/plain; charset=utf-8")
        return self._json({"text": uit["tekst"], "seconden": uit["seconden"],
                           "duur_seconden": wav_lengte(audio) or None,
                           "model": uit["model"], "taal": uit["taal"]})

    def _spraak(self):
        b = json.loads(self._ruw() or b"{}")
        tekst = b.get("input") or b.get("text") or ""
        wav = spreken(tekst, b.get("voice") or b.get("stem"),
                      float(b.get("speed") or b.get("tempo") or 1.0))
        return self._stuur(wav, 200, "audio/wav")


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def cmd_serve(_a):
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Werdens Media LLM — medialaag {VERSIE}")
    print(f"  web      : http://{HOST}:{PORT}/")
    print(f"  whisper  : {'klaar' if sporen(WHISPER_BIN) else 'ONTBREEKT'} ({WHISPER_BIN})")
    print(f"  model    : {WHISPER_MODEL.name} ({'aanwezig' if WHISPER_MODEL.exists() else 'ONTBREEKT'})")
    print(f"  piper    : {'klaar' if sporen(PIPER_BIN) else 'ONTBREEKT'} ({PIPER_BIN})")
    print(f"  stemmen  : {', '.join(stemmen()) or 'geen'}")
    print(f"  ffmpeg   : {'ja' if shutil.which(FFMPEG) else 'nee (alleen WAV)'}")
    print(f"  taal     : {TAAL}")
    srv.serve_forever()


def cmd_transcribeer(a):
    audio = Path(a.bestand).read_bytes()
    uit = transcriberen(audio, Path(a.bestand).name, a.taal)
    print(uit["tekst"] or "(niets verstaan)")
    print(f"\n[{uit['seconden']}s rekenen met {uit['model']}]", file=sys.stderr)


def cmd_spreek(a):
    wav = spreken(" ".join(a.tekst), a.stem, a.tempo)
    Path(a.uit).write_bytes(wav)
    print(f"{len(wav) / 1024:.0f} kB geschreven naar {a.uit} ({wav_lengte(wav)}s audio)")


def main():
    p = argparse.ArgumentParser(prog="wmlm_media", description="Medialaag van de Werdens Media LLM")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("serve", help="server starten").set_defaults(fn=cmd_serve)
    t = sub.add_parser("transcribeer", help="audiobestand naar tekst")
    t.add_argument("bestand")
    t.add_argument("--taal", default=None)
    t.set_defaults(fn=cmd_transcribeer)
    s = sub.add_parser("spreek", help="tekst naar een WAV-bestand")
    s.add_argument("tekst", nargs="+")
    s.add_argument("--stem", default=None)
    s.add_argument("--tempo", type=float, default=1.0)
    s.add_argument("--uit", default="spraak.wav")
    s.set_defaults(fn=cmd_spreek)
    a = p.parse_args()
    if not getattr(a, "fn", None):
        p.print_help()
        return
    a.fn(a)


if __name__ == "__main__":
    main()
