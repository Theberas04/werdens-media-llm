#!/usr/bin/env python3
"""
wllm — een eigen, vanaf-nul geschreven LLM-server met dezelfde API als Ollama.

Geen Go, geen build-tools, geen externe Python-pakketten: alleen de stdlib.
De rekenkracht komt van llama.cpp (llama-server) als engine; deze server doet
wat Ollama's Go-server doet: modellen beheren, een blob/manifest-store, de
engine starten/stoppen (keep-alive + idle unload) en de Ollama-API vertalen
naar de engine, inclusief OpenAI-compatible /v1 endpoints.

Endpoints (Ollama-compatible):
  GET  /api/version          GET  /api/tags         GET  /api/ps
  POST /api/show             POST /api/pull         POST /api/delete
  POST /api/chat             POST /api/generate     POST /api/embed
  GET  /api/chat             POST /api/embeddings    (legacy)
  GET  /v1/models            POST /v1/chat/completions
  POST /v1/completions       POST /v1/embeddings

CLI:
  wllm.py serve                       start de server (default :11434)
  wllm.py list                        toon modellen
  wllm.py pull hf:repo/file.gguf      download een GGUF naar de model-store
  wllm.py run <model> "prompt"        eenmalige prompt via de eigen API
  wllm.py ps                          toon geladen modellen
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import select
import signal
import shutil
import socket
import subprocess
import struct
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# --------------------------------------------------------------------------- #
# Merk: het logo van Werdens Media staat in assets/ naast de drie lagen.
# --------------------------------------------------------------------------- #
def _logo_pad() -> Path:
    """Zoek het merklogo. In de repo staat dit script in engine/, in de container
    staat het los in de projectmap; probeer daarom beide niveau's."""
    hier = Path(__file__).resolve().parent
    for basis in (hier.parent, hier):
        kandidaat = basis / "assets" / "werdens-media-logo.png"
        if kandidaat.exists():
            return kandidaat
    return hier.parent / "assets" / "werdens-media-logo.png"


LOGO = _logo_pad()
# Lege PNG van 1x1. Ontbreekt het logobestand (bijvoorbeeld wanneer alleen dit
# script is gekopieerd), dan krijgt de pagina deze door en toont hij nooit een
# kapot beeld.
LEGE_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def logo_info() -> tuple[bytes, bool]:
    """Het merklogo plus of dit de terugval is.

    Ontbreekt het bestand, dan komt er een lege PNG door. Die mag NOOIT gecacht
    worden: een browser bewaart hem anders een dag en dan staat er een rood blok
    in de kop in plaats van het logo -- precies wat er bij de klant gebeurde."""
    try:
        return LOGO.read_bytes(), False
    except OSError:
        return LEGE_PNG, True


def logo_bytes() -> bytes:
    return logo_info()[0]


# Huisstijl van Werdens Monitor, letterlijk dezelfde tokens: licht, kaarten op wit,
# blauw #2ea3f2 als accent en de merk-gradient (cyaan -> blauw) voor de hoofdknoppen.
HUISSTIJL = """
:root{
  --bg:#f4f7fb; --pane:#ffffff; --pane2:#f7fafd; --pane3:#eef3fa;
  --line:#e3eaf4; --line2:#cfdaeb;
  --txt:#0f172a; --muted:#55657f; --muted2:#8296ae;
  --accent:#2ea3f2; --accent-d:#1e73be; --accent-soft:#e8f4fd;
  --ok:#059669; --ok-soft:#e6f6ef;
  --amber:#b45309; --amber-soft:#fdf1e2;
  --red:#dc2626; --red-soft:#fdeaea;
  --blue:#1e73be; --blue-soft:#e8f4fd;
  --merk:linear-gradient(118deg,#5ff0e8 0%,#2ea3f2 55%,#1e73be 100%);
  --r:16px; --r-klein:10px; --hoogte:40px;
  --schaduw:0 1px 2px rgba(15,23,42,.05),0 12px 28px -20px rgba(15,23,42,.3);
  --font:"Inter",-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
  --mono:"JetBrains Mono",ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;color:var(--txt);font:15px/1.5 var(--font);
  -webkit-font-smoothing:antialiased;
  background:radial-gradient(1100px 520px at 90% -10%,rgba(95,240,232,.12),transparent 62%),
             radial-gradient(900px 480px at -8% -4%,rgba(74,76,248,.08),transparent 58%),var(--bg)}
a{color:var(--accent);text-decoration:none}a:hover{text-decoration:underline}
header.kop{position:sticky;top:0;z-index:30;background:rgba(255,255,255,.88);
  -webkit-backdrop-filter:blur(8px);backdrop-filter:blur(8px);border-bottom:1px solid var(--line);
  padding:12px 26px;display:flex;align-items:center;gap:11px}
.merk{display:flex;align-items:center;gap:11px;min-width:0}
.merk .logo{width:44px;height:44px;flex:none}
.merk .logo img{width:100%;height:100%;object-fit:contain;display:block}
.merk .naam{font-weight:700;font-size:17px;letter-spacing:-.025em;white-space:nowrap}
.merk .sub{font-size:11.5px;color:var(--muted2);letter-spacing:.08em;text-transform:uppercase;
  white-space:nowrap}
.kop-rechts{margin-left:auto;display:flex;align-items:center;gap:8px;font-size:12px;
  color:var(--muted2);flex:none}
.kaart{background:var(--pane);border:1px solid var(--line);border-radius:var(--r);box-shadow:var(--schaduw)}
.kaart-kop{background:var(--pane2);border-bottom:1px solid var(--line);
  border-radius:var(--r) var(--r) 0 0;padding:14px 18px 12px}
.kaart-kop h2{margin:0;font-size:12px;letter-spacing:.12em;text-transform:uppercase;
  color:var(--muted);font-weight:700}
.kaart-body{padding:16px 18px}
.knop{display:inline-flex;align-items:center;justify-content:center;gap:7px;height:var(--hoogte);
  padding:0 14px;border-radius:var(--r-klein);background:var(--pane);border:1px solid var(--line2);
  color:var(--txt);font:inherit;font-weight:600;cursor:pointer}
.knop:hover{background:var(--pane2);border-color:#b6c7dd}
.knop.primair{background:var(--merk);border-color:transparent;color:#fff;font-weight:700}
input,select,textarea{font:inherit;color:var(--txt);background:var(--pane);border:1px solid var(--line2);
  border-radius:var(--r-klein);padding:0 12px;height:var(--hoogte);width:100%}
input:focus,select:focus,textarea:focus{outline:2px solid var(--accent-soft);border-color:var(--accent)}
textarea{height:auto;padding:10px 12px;resize:vertical}
code{font-family:var(--mono);font-size:12.5px;background:var(--pane3);border:1px solid var(--line);
  border-radius:6px;padding:1px 6px}
.tabel{width:100%;border-collapse:collapse;font-size:14px}
.tabel th{text-align:left;font-size:11.5px;letter-spacing:.1em;text-transform:uppercase;
  color:var(--muted2);font-weight:700;padding:0 10px 8px 0}
.tabel td{padding:9px 10px 9px 0;border-top:1px solid var(--line)}
.ok{color:var(--ok)}
"""


# --------------------------------------------------------------------------- #
# Chatpagina: praten met de LLM in de browser
# --------------------------------------------------------------------------- #
# Let op (twee keer eerder misgegaan): dit is GEEN f-string, want de JS staat vol
# accolades -- de huisstijl komt er via __HUISSTIJL__ in. En de JS is volledig
# backslash-vrij geschreven: nieuwe regels via String.fromCharCode(10), regexes
# met tekenklassen ([*], [^`], [(]) in plaats van \*, \` en \(.
CHAT_PAGINA = """<!doctype html><html lang="nl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Werdens Media LLM — chat</title>
<style>
__HUISSTIJL__
/* Geen vaste hoogte met een berekend getal: op een telefoon is de kop twee keer zo
   hoog, waardoor de invoerbalk en de voettekst buiten beeld vielen. De pagina is
   nu een kolom over het hele scherm en alleen het gesprek scrolt. */
html,body{height:100%}
body{display:flex;flex-direction:column;overflow:hidden}
header.kop{flex:none}
main.chat{flex:1;min-height:0;padding:18px 26px 20px;display:flex;flex-direction:column;gap:12px;
  max-width:920px}
.gesprek{flex:1;min-height:0;overflow-y:auto;display:flex;flex-direction:column;gap:12px}
.leeg{color:var(--muted2);font-size:14px;padding:6px 2px}
.bubbel{max-width:min(720px,90%);border:1px solid var(--line);border-radius:var(--r);
  padding:11px 14px;background:var(--pane);box-shadow:var(--schaduw)}
.bubbel.van-mij{align-self:flex-end;background:var(--accent-soft);border-color:#cbe6fb}
.wie{font-size:11px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted2);
  font-weight:700;margin-bottom:5px}
.bubbel .tekst{white-space:pre-wrap;overflow-wrap:break-word}
.bubbel .tekst h3,.bubbel .tekst h4,.bubbel .tekst h5{margin:10px 0 4px;font-size:15px}
.bubbel .tekst ul{margin:6px 0;padding-left:20px}
.bubbel pre{background:var(--pane3);border:1px solid var(--line);border-radius:10px;
  padding:10px;overflow-x:auto;margin:8px 0}
.bubbel pre code{background:none;border:0;padding:0;font-size:12.5px}
.wacht{color:var(--muted2)}
.onder{display:flex;gap:10px;align-items:flex-end;border-top:1px solid var(--line);padding-top:12px}
.onder textarea{min-height:44px;max-height:180px}
.onder .knop{height:44px;flex:none}
.voet{font-size:12px;color:var(--muted2);min-height:16px}
/* Het hidden-attribuut van de browser verliest van de .knop-regel in de huisstijl
   (die zet display:inline-flex), waardoor de stopknop zichtbaar bleef. */
[hidden]{display:none !important}
.kop-rechts select{width:auto;height:34px;font-size:13px;max-width:240px}
@media(max-width:820px){
  main.chat{padding:12px 14px 14px}
  header.kop{flex-wrap:wrap;gap:8px}
  .kop-rechts{margin-left:0;width:100%;flex-wrap:wrap}
  .kop-rechts select{max-width:100%;flex:1}
  .merk .sub{display:none}
  .bubbel{max-width:100%}}
</style></head><body>
<header class="kop"><div class="merk">
<span class="logo"><img src="/merk.png" alt="Werdens Media" onerror="this.style.display='none'"></span>
<div><div class="naam">Werdens Media LLM</div>
<div class="sub">Werdens Media &middot; chat</div></div></div>
<div class="kop-rechts">
<select id="model" title="Model"></select>
<button class="knop" id="nieuw">Nieuw gesprek</button>
<a class="knop" href="/">Status</a></div>
</header>
<main class="chat">
<div class="gesprek" id="gesprek"></div>
<div class="onder">
<textarea id="invoer" rows="1" placeholder="Typ je vraag…" title="Enter stuurt, Shift+Enter geeft een nieuwe regel"></textarea>
<button class="knop primair" id="stuur">Stuur</button>
<button class="knop" id="stop" hidden>Stop</button>
</div>
<div class="voet" id="voet"></div>
</main>
<script>
var GESPREK = [];
var BEZIG = false;
var STOP = null;

function el(id){ return document.getElementById(id); }
function nl(){ return String.fromCharCode(10); }
function veilig(t){
  return String(t).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function koppelingen(t){
  var uit = "";
  var i = 0;
  while (i < t.length){
    if (t.charAt(i) === "["){
      var eind = t.indexOf("]", i);
      var open = eind >= 0 ? t.indexOf("(", eind) : -1;
      var sluit = open === eind + 1 ? t.indexOf(")", open) : -1;
      if (eind > i && sluit > open){
        var naam = t.slice(i + 1, eind);
        var url = t.slice(open + 1, sluit);
        if (url.indexOf("http") === 0){
          uit += '<a href="' + url + '" target="_blank" rel="noopener">' + naam + "</a>";
          i = sluit + 1;
          continue;
        }
      }
    }
    uit += t.charAt(i);
    i++;
  }
  return uit;
}

function opmaakKlein(t){
  var s = t;
  s = s.replace(new RegExp("[*][*]([^*]+)[*][*]", "g"), "<b>$1</b>");
  s = s.replace(new RegExp("`([^`]+)`", "g"), "<code>$1</code>");
  s = s.replace(new RegExp("[*]([^*]+)[*]", "g"), "<i>$1</i>");
  var regels = s.split(nl());
  var uit = "";
  var inLijst = false;
  for (var i = 0; i < regels.length; i++){
    var r = regels[i];
    var punt = r.match(new RegExp("^[-*] (.*)$"));
    var kop = r.match(new RegExp("^(#{1,4}) (.*)$"));
    if (punt){
      if (!inLijst){ uit += "<ul>"; inLijst = true; }
      uit += "<li>" + punt[1] + "</li>";
      continue;
    }
    if (inLijst){ uit += "</ul>"; inLijst = false; }
    if (kop){
      var n = Math.min(kop[1].length + 2, 5);
      uit += "<h" + n + ">" + kop[2] + "</h" + n + ">";
    } else {
      uit += r + nl();
    }
  }
  if (inLijst){ uit += "</ul>"; }
  return uit;
}

function opmaak(tekst){
  var t = veilig(tekst);
  var delen = t.split("```");
  var uit = "";
  for (var i = 0; i < delen.length; i++){
    if (i % 2 === 1){
      var stuk = delen[i].replace(new RegExp("^[a-zA-Z0-9+#-]*" + nl()), "");
      uit += "<pre><code>" + stuk + "</code></pre>";
    } else {
      uit += opmaakKlein(koppelingen(delen[i]));
    }
  }
  return uit;
}

function bewaar(){
  try { localStorage.setItem("wmlm-gesprek", JSON.stringify(GESPREK)); } catch (e) {}
}

function herstel(){
  try {
    var d = JSON.parse(localStorage.getItem("wmlm-gesprek") || "[]");
    if (Object.prototype.toString.call(d) === "[object Array]"){ GESPREK = d; }
  } catch (e) { GESPREK = []; }
}

function tekenen(){
  var bak = el("gesprek");
  bak.innerHTML = "";
  if (!GESPREK.length){
    var leeg = document.createElement("div");
    leeg.className = "leeg";
    leeg.textContent = "Stel een vraag en de Werdens Media LLM antwoordt. Alles blijft op deze machine.";
    bak.appendChild(leeg);
  }
  for (var i = 0; i < GESPREK.length; i++){
    var m = GESPREK[i];
    var d = document.createElement("div");
    d.className = "bubbel " + (m.rol === "gebruiker" ? "van-mij" : "van-ai");
    var wie = document.createElement("div");
    wie.className = "wie";
    wie.textContent = m.rol === "gebruiker" ? "jij" : "Werdens Media LLM";
    var lijf = document.createElement("div");
    lijf.className = "tekst";
    if (m.rol === "gebruiker"){ lijf.textContent = m.tekst; }
    else if (m.tekst){ lijf.innerHTML = opmaak(m.tekst); }
    else { lijf.innerHTML = '<span class="wacht">aan het nadenken…</span>'; }
    d.appendChild(wie);
    d.appendChild(lijf);
    bak.appendChild(d);
  }
  bak.scrollTop = bak.scrollHeight;
}

function groei(){
  var v = el("invoer");
  v.style.height = "auto";
  v.style.height = Math.min(v.scrollHeight, 180) + "px";
}

function laadModellen(){
  fetch("/api/tags").then(function(r){ return r.json(); }).then(function(d){
    var sel = el("model");
    var namen = [];
    var lijst = d.models || [];
    for (var i = 0; i < lijst.length; i++){
      var n = lijst[i].name || lijst[i].model;
      if (n && namen.indexOf(n) < 0){ namen.push(n); }
    }
    sel.innerHTML = "";
    for (var j = 0; j < namen.length; j++){
      var o = document.createElement("option");
      o.value = namen[j];
      o.textContent = namen[j];
      sel.appendChild(o);
    }
    var bewaard = null;
    try { bewaard = localStorage.getItem("wmlm-model"); } catch (e) {}
    if (bewaard && namen.indexOf(bewaard) >= 0){ sel.value = bewaard; }
    el("voet").textContent = namen.length ? "klaar om te chatten" : "nog geen model in huis";
  }).catch(function(){
    el("voet").textContent = "kon de modellen niet ophalen";
  });
}

async function stuur(){
  var vak = el("invoer");
  var tekst = vak.value.trim();
  if (!tekst || BEZIG){ return; }
  vak.value = "";
  groei();
  GESPREK.push({ rol: "gebruiker", tekst: tekst });
  var ai = { rol: "ai", tekst: "" };
  GESPREK.push(ai);
  tekenen();
  bewaar();
  BEZIG = true;
  el("stuur").disabled = true;
  el("stop").hidden = false;
  var t0 = Date.now();
  var tekens = 0;
  STOP = new AbortController();
  try {
    var berichten = [];
    for (var i = 0; i < GESPREK.length; i++){
      var m = GESPREK[i];
      if (m === ai){ break; }
      if (m.tekst){
        berichten.push({ role: m.rol === "gebruiker" ? "user" : "assistant", content: m.tekst });
      }
    }
    var antw = await fetch("/v1/chat/completions", {
      method: "POST",
      signal: STOP.signal,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ model: el("model").value, stream: true, messages: berichten })
    });
    if (!antw.ok){
      var fout = "";
      try { fout = await antw.text(); } catch (e) {}
      throw new Error("de engine gaf " + antw.status + (fout ? ": " + fout.slice(0, 200) : ""));
    }
    var lezer = antw.body.getReader();
    var kijken = new TextDecoder();
    var rest = "";
    while (true){
      var stuk = await lezer.read();
      if (stuk.done){ break; }
      rest += kijken.decode(stuk.value, { stream: true });
      var regels = rest.split(nl());
      rest = regels.pop();
      for (var j = 0; j < regels.length; j++){
        var regel = regels[j].trim();
        if (regel.indexOf("data:") !== 0){ continue; }
        var data = regel.slice(5).trim();
        if (!data || data === "[DONE]"){ continue; }
        var brok = null;
        try { brok = JSON.parse(data); } catch (e) { continue; }
        var keuze = (brok.choices || [])[0] || {};
        var delta = keuze.delta || {};
        var deel = typeof delta.content === "string" ? delta.content : "";
        if (deel){ ai.tekst += deel; tekens += deel.length; }
      }
      tekenen();
    }
  } catch (e) {
    if (e.name === "AbortError"){
      ai.tekst += (ai.tekst ? nl() + nl() : "") + "_(gestopt)_";
    } else {
      ai.tekst += (ai.tekst ? nl() + nl() : "") + "Er ging iets mis: " + e.message;
    }
  } finally {
    BEZIG = false;
    STOP = null;
    el("stuur").disabled = false;
    el("stop").hidden = true;
    var sec = (Date.now() - t0) / 1000;
    if (ai.tekst){
      el("voet").textContent = "klaar · " + tekens + " tekens in " + sec.toFixed(1) +
        " s · " + Math.round(tekens / Math.max(sec, 0.1)) + " tekens per seconde";
    }
    tekenen();
    bewaar();
    vak.focus();
  }
}

el("stuur").addEventListener("click", stuur);
el("stop").addEventListener("click", function(){ if (STOP){ STOP.abort(); } });
el("nieuw").addEventListener("click", function(){
  GESPREK = [];
  bewaar();
  tekenen();
  el("voet").textContent = "";
  el("invoer").focus();
});
el("model").addEventListener("change", function(){
  try { localStorage.setItem("wmlm-model", el("model").value); } catch (e) {}
});
el("invoer").addEventListener("keydown", function(e){
  if (e.key === "Enter" && !e.shiftKey){ e.preventDefault(); stuur(); }
});
el("invoer").addEventListener("input", groei);
herstel();
tekenen();
laadModellen();
el("invoer").focus();
</script>
</body></html>"""


# --------------------------------------------------------------------------- #
# Configuratie (env-vars met dezelfde geest als OLLAMA_*)
# --------------------------------------------------------------------------- #
_SCRIPT_DIR = Path(__file__).resolve().parent
# De server staat in engine/; de projectmap is dan één niveau hoger, zodat
# modellen, logs en de engine-binary op de verwachte plek staan.
_STANDAARD_HOME = _SCRIPT_DIR.parent if _SCRIPT_DIR.name == "engine" else _SCRIPT_DIR
HOME = Path(os.environ.get("WLLM_HOME", _STANDAARD_HOME))
MODELS_DIR = Path(os.environ.get("WLLM_MODELS", HOME / "models"))
BLOBS_DIR = Path(os.environ.get("WLLM_BLOBS", HOME / "blobs"))
MANIFESTS_DIR = Path(os.environ.get("WLLM_MANIFESTS", HOME / "manifests"))


def gpu_info() -> dict:
    """Kijk of er een NVIDIA-GPU is en hoeveel geheugen die heeft."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return {"aanwezig": False}
    try:
        uit = subprocess.run(
            [exe, "--query-gpu=name,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, timeout=15, text=True,
        )
        regels = [r.strip() for r in uit.stdout.splitlines() if r.strip()] if uit.returncode == 0 else []
        if not regels:
            return {"aanwezig": False}
        naam, vram = (regels[0].split(",") + ["0"])[:2]
        return {
            "aanwezig": True,
            "naam": naam.strip(),
            "vram_mb": int(float(vram.strip() or 0)),
            "aantal": len(regels),
        }
    except Exception:  # noqa: BLE001
        return {"aanwezig": False}


GPU = gpu_info()


def kies_engine() -> Path:
    """Pak de juiste engine-binary. Staat er een CUDA-build en is er een GPU,
    dan die; anders de CPU-build. Zo hoef je alleen een GPU te plaatsen en te
    herstarten — de rest gaat vanzelf."""
    vast = os.environ.get("WLLM_ENGINE")
    if vast:
        return Path(vast)
    basis = HOME / "engine"
    # Windows levert llama-server.exe, Linux llama-server: beide namen zoeken,
    # zodat dezelfde code op beide systemen werkt.
    kandidaten = []
    if basis.exists():
        for naam in ("llama-server", "llama-server.exe"):
            kandidaten += sorted(basis.glob(f"llama-*/{naam}"))
            if (basis / naam).exists():
                kandidaten.append(basis / naam)
    if not kandidaten:
        return basis / "llama-b12262" / "llama-server"
    if GPU.get("aanwezig"):
        cuda = [p for p in kandidaten if "cuda" in str(p).lower()]
        if cuda:
            return cuda[-1]
    cpu = [p for p in kandidaten if "cuda" not in str(p).lower()]
    return (cpu or kandidaten)[-1]


ENGINE = kies_engine()


def _ngl() -> int:
    """Hoeveel model-lagen naar de GPU. 'auto' = alles als er een GPU is."""
    waarde = os.environ.get("WLLM_NGL", "auto").strip().lower()
    if waarde == "auto":
        return 999 if GPU.get("aanwezig") else 0
    try:
        return int(waarde)
    except ValueError:
        return 0


NGL = _ngl()


def _kv_type() -> str:
    """Op CPU is 8-bits KV-cache nodig om 64k context in het geheugen te passen;
    op een GPU is f16 sneller en nauwkeuriger."""
    vast = os.environ.get("WLLM_KV_TYPE")
    if vast:
        return vast
    return "f16" if GPU.get("aanwezig") else "q8_0"


HOST = os.environ.get("WLLM_HOST", "0.0.0.0")
PORT = int(os.environ.get("WLLM_PORT", "11434"))


def _fysieke_cores() -> int:
    """Het aantal echte cores van de machine, niet de logische. llama.cpp wordt
    langzamer als je er meer threads tegenaan gooit dan er echte cores zijn."""
    try:
        with open("/proc/cpuinfo", encoding="utf-8", errors="replace") as f:
            inhoud = f.read()
        paren = set()
        for blok in inhoud.split("\n\n"):
            fysiek = re.search(r"physical id\s*:\s*(\d+)", blok)
            core = re.search(r"core id\s*:\s*(\d+)", blok)
            if fysiek and core:
                paren.add((fysiek.group(1), core.group(1)))
        if paren:
            return len(paren)
    except OSError:
        pass
    return os.cpu_count() or 4


def _toegestane_cores() -> int:
    """Hoeveel cores deze container mag gebruiken. De host kan dat beperken, en
    dat ziet sched_getaffinity wel en /proc/cpuinfo niet."""
    try:
        return len(os.sched_getaffinity(0))
    except AttributeError:
        return os.cpu_count() or 4


def _beste_threads() -> int:
    """Hoeveel rekenkracht-threads deze machine het beste kan gebruiken.

    In een container beperkt de host het aantal cores (cpuset), en dat ziet
    sched_getaffinity wél en /proc/cpuinfo niet. Daarom het minimum van wat de
    container mag gebruiken en wat de machine echt aan cores heeft: op een host
    met hyperthreading voorkomt dat 8 threads op 4 echte cores."""
    return max(1, min(_toegestane_cores(), _fysieke_cores()))


# WLLM_THREADS=0 of leeg betekent: kijk zelf hoeveel er te gebruiken is. Dan pakt
# de engine de extra cores als de container later meer krijgt (na een herstart).
_gewenst = int(os.environ.get("WLLM_THREADS") or 0)
THREADS = _gewenst if _gewenst > 0 else _beste_threads()

# Hoe lang de engine op de rekenkern wacht voordat hij een verzoek opgeeft. Ruim
# genomen, want op een cpu kan één lange vraag met veel tekst erin makkelijk een
# kwartier duren. Valt de vragende partij eerder weg, dan grijpt de waker in en
# komt het rekenvak eerder vrij -- deze limiet is alleen voor het echte werk.
UPSTREAM_TIMEOUT = int(os.environ.get("WLLM_UPSTREAM_TIMEOUT") or 1800)
# Hermes Agent weigert modellen onder de 64.000 tokens context, dus de default
# moet daar op of boven zitten. 64k f16 KV-cache is ~2,15 GB; met q8_0 is dat
# ~1,07 GB en dat past naast Hermes op 8 GB.
CTX = int(os.environ.get("WLLM_CTX", "65536"))
KV_TYPE = _kv_type()
KEEP_ALIVE = int(os.environ.get("WLLM_KEEP_ALIVE", "300"))  # seconden
ENGINE_PORT_BASE = int(os.environ.get("WLLM_ENGINE_PORT", "18500"))
VERSION = "0.1.0"

for d in (MODELS_DIR, BLOBS_DIR, MANIFESTS_DIR):
    d.mkdir(parents=True, exist_ok=True)


# Kwaliteitstrappen: het grootste model dat op deze machine past. Hoe meer
# geheugen, hoe beter het antwoord. De drempels zijn GEMETEN met
# 'aanbevelen --meten': gewichten plus KV-cache bij 64k context, Q4_K_M, CPU en
# 8-bits KV-cache. Op een GPU telt alleen het videogeheugen en is de KV-cache
# f16 (dus ruimer), waardoor dezelfde modellen wél passen.
#
# Let op het verschil tussen de families: Qwen3 heeft 8 KV-koppen met
# kop-lengte 128, de Llama-3.2-familie 8 KV-koppen met lengte 64. Daardoor is
# het KV-geheugen van Llama-3.2 vier keer zo klein en past Llama-3.2-3B bij 64k
# waar Qwen3-4B en 8B niet passen.
MODEL_TRAPPEN = (
    (27000, "unsloth/Qwen3-32B-GGUF", "Qwen3-32B-Q4_K_M.gguf",
     "de zwaarste kwaliteit die thuis nog past"),
    (20700, "unsloth/Qwen3-30B-A3B-Instruct-2507-GGUF",
     "Qwen3-30B-A3B-Instruct-2507-Q4_K_M.gguf",
     "MoE-model: sterk én snel, want het rekent maar een deel van het netwerk"),
    (13700, "unsloth/Qwen3-14B-GGUF", "Qwen3-14B-Q4_K_M.gguf", "goede balans"),
    (9400, "unsloth/Qwen3-8B-GGUF", "Qwen3-8B-Q4_K_M.gguf", "sterk"),
    (6900, "unsloth/Qwen3-4B-Instruct-2507-GGUF", "Qwen3-4B-Instruct-2507-Q4_K_M.gguf",
     "goede kwaliteit voor een kleine machine"),
    (3100, "unsloth/Llama-3.2-3B-Instruct-GGUF", "Llama-3.2-3B-Instruct-Q4_K_M.gguf",
     "kleine KV-cache: past bij 64k context waar Qwen3-4B en 8B niet passen"),
    (1700, "unsloth/Llama-3.2-1B-Instruct-GGUF", "Llama-3.2-1B-Instruct-Q4_K_M.gguf",
     "snelst en het zuinigst met geheugen"),
)

# Op een CPU moet het besturingssysteem en de rest van de machine er ook nog
# bij passen; op een GPU telt alleen het videogeheugen.
CPU_OVERHEAD_MB = int(os.environ.get("WLLM_CPU_OVERHEAD_MB", "2000"))

# De standaardsysteemprompt. Zonder afspraken gaat een klein model fantaseren: het
# verzint een baas achter zich en antwoordt in de verkeerde taal. Clients die hun
# eigen systeemafspraken meesturen (Hermes doet dat) houden die; deze prompt vult
# alleen het gat. Leeg maken met WLLM_SYSTEEM="" zet hem uit.
STANDAARD_SYSTEEM = os.environ.get(
    "WLLM_SYSTEEM",
    "Je bent de Werdens Media LLM, een behulpzame AI-assistent die op deze machine "
    "draait. Antwoord in het Nederlands, in gewone taal, en blijf bij de vraag. "
    "Weet je iets niet, zeg dat dan eerlijk; verzin geen feiten, bronnen of namen. "
    "Stel jezelf niet voor als assistent van een ander bedrijf.",
)

# Nederlands telt ongeveer drie tekens per token.
TEKENS_PER_TOKEN = 3.0

# Hoe lang mag de rekenkern nog doorwerken nadat de vragende partij is weggevallen?
# Tijdens het verwerken van een grote prompt merkt llama-server een gesloten
# verbinding niet, dus hij blijft minutenlang rekenen voor niemand. Met één
# rekenvak houdt dat elke volgende vraag tegen. Daarom stoppen we de kern als het
# na deze seconden nog loopt; het volgende verzoek laadt hem opnieuw (seconden).
HERSTEL_NA = int(os.environ.get("WLLM_HERSTEL_NA", "20"))

# Een verzoek dat langer dan dit achter een ander verzoek in de rij staat, krijgt
# een eerlijk antwoord in plaats van een half uur wachten.
WACHT_LIMIET = int(os.environ.get("WLLM_WACHT_LIMIET", "900"))


def beschikbaar_mb() -> tuple[int, str]:
    """Het geheugen dat het model mag gebruiken, plus waar dat op gebaseerd is."""
    if GPU.get("aanwezig"):
        return int(GPU.get("vram_mb") or 0), "VRAM"
    totaal = 0
    try:
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for regel in fh:
                if regel.startswith("MemTotal:"):
                    totaal = int(regel.split()[1]) // 1024
                    break
    except Exception:  # noqa: BLE001
        pass
    return max(0, totaal - CPU_OVERHEAD_MB), f"RAM min {CPU_OVERHEAD_MB} MB voor het systeem"


def aanbevolen_model() -> dict:
    """Het beste model dat op deze machine past — dit is de knop voor kwaliteit."""
    mb, bron = beschikbaar_mb()
    for drempel, repo, bestand, opmerking in MODEL_TRAPPEN:
        if mb >= drempel:
            return {
                "repo": repo,
                "bestand": bestand,
                "opmerking": opmerking,
                "geheugen_mb": mb,
                "gebaseerd_op": bron,
                "pull": f"pull hf:{repo}/{bestand}",
            }
    return {}


# --------------------------------------------------------------------------- #
# GGUF-kop lezen: zo weten we écht hoeveel een model nodig heeft in plaats van
# te gokken op een vuistregel. De metadata staat vooraan in het bestand.
# --------------------------------------------------------------------------- #
GGUF_TYPES = {
    0: "B", 1: "b", 2: "H", 3: "h", 4: "I", 5: "i", 6: "f", 7: "?",
    10: "Q", 11: "q", 12: "d",
}
GGUF_KOP_BYTES = 4 * 1024 * 1024


def _lees(offset: int, fmt: str, data: bytes):
    grootte = struct.calcsize(fmt)
    return struct.unpack_from(fmt, data, offset)[0], offset + grootte


def _lees_tekst(offset: int, data: bytes) -> tuple[str, int]:
    n, offset = _lees(offset, "<Q", data)
    return data[offset:offset + n].decode("utf-8", "replace"), offset + n


def gguf_meta(data: bytes, stop_bij: set[str] | None = None) -> dict:
    """Lees de metadata-tabel uit de kop van een GGUF-bestand.

    ``stop_bij`` laat het lezen stoppen zodra die sleutels binnen zijn. Dat is
    nodig omdat de tokenizer-lijst (honderdduizenden teksten) in dezelfde tabel
    staat en zomaar tientallen megabytes kost — terwijl de cijfers die wij
    willen (architectuur, lagen, koppen) helemaal vooraan staan.
    """
    if data[:4] != b"GGUF":
        raise ValueError("geen GGUF-bestand")
    _, offset = _lees(4, "<I", data)          # versie
    _, offset = _lees(offset, "<Q", data)     # aantal tensors
    aantal, offset = _lees(offset, "<Q", data)
    meta: dict = {}
    for _ in range(aantal):
        try:
            sleutel, offset = _lees_tekst(offset, data)
            soort, offset = _lees(offset, "<I", data)
            if soort == 8:
                waarde, offset = _lees_tekst(offset, data)
            elif soort == 9:
                etype, offset = _lees(offset, "<I", data)
                lengte, offset = _lees(offset, "<Q", data)
                waarden = []
                for _i in range(lengte):
                    if etype == 8:
                        w, offset = _lees_tekst(offset, data)
                    else:
                        fmt = GGUF_TYPES.get(etype)
                        if fmt is None:
                            raise ValueError(f"onbekend gegevenstype {etype} in GGUF")
                        w, offset = _lees(offset, "<" + fmt, data)
                    waarden.append(w)
                waarde = waarden
            else:
                fmt = GGUF_TYPES.get(soort)
                if fmt is None:
                    raise ValueError(f"onbekend gegevenstype {soort} in GGUF")
                waarde, offset = _lees(offset, "<" + fmt, data)
        except struct.error:
            # kop te klein voor de rest; wat we al hebben is genoeg
            break
        meta[sleutel] = waarde
        if stop_bij and stop_bij <= set(meta):
            break
    return meta


def _feiten_uit_kop(kop: bytes, gewicht_mb: int, ctx: int, kv_bits: int) -> dict:
    """Bereken architectuur en geheugengebruik uit de GGUF-kop."""
    basis = gguf_meta(kop, stop_bij={"general.architecture"})
    arch = str(basis.get("general.architecture", "onbekend"))
    meta = gguf_meta(kop, stop_bij={
        f"{arch}.block_count",
        f"{arch}.attention.head_count",
        f"{arch}.attention.head_count_kv",
        f"{arch}.attention.key_length",
        f"{arch}.attention.value_length",
        f"{arch}.embedding_length",
    })
    lagen = int(meta.get(f"{arch}.block_count") or 0)
    koppen = int(meta.get(f"{arch}.attention.head_count") or 0)
    kv_koppen = int(meta.get(f"{arch}.attention.head_count_kv") or koppen)
    kop_lengte = int(
        meta.get(f"{arch}.attention.key_length")
        or (int(meta.get(f"{arch}.embedding_length") or 0) // koppen if koppen else 0)
    )
    waarde_lengte = int(meta.get(f"{arch}.attention.value_length") or kop_lengte)
    per_token = lagen * kv_koppen * (kop_lengte + waarde_lengte) * 2  # f16
    kv_mb = int(per_token * ctx * (kv_bits / 16) // 1048576)
    return {
        "architectuur": arch,
        "lagen": lagen,
        "aandachtskoppen": koppen,
        "kv_koppen": kv_koppen,
        "kop_lengte": kop_lengte,
        "waarde_lengte": waarde_lengte,
        "gewicht_mb": gewicht_mb,
        "context": ctx,
        "kv_type": f"q{kv_bits}_0" if kv_bits == 8 else "f16",
        "kv_mb": kv_mb,
        "nodig_mb": gewicht_mb + kv_mb,
        "beschikbaar_mb": beschikbaar_mb()[0],
        "kv_bytes_per_token": int(per_token * (kv_bits / 16)),
        "past": gewicht_mb + kv_mb <= beschikbaar_mb()[0],
    }


def model_feiten(pad: Path, ctx: int | None = None, kv_bits: int = 8) -> dict:
    """Architectuur en geheugengebruik van een model, uit het bestand zelf."""
    ctx = int(ctx or CTX)
    with pad.open("rb") as fh:
        kop = fh.read(GGUF_KOP_BYTES)
    feiten = _feiten_uit_kop(kop, pad.stat().st_size // 1048576, ctx, kv_bits)
    feiten["bestand"] = pad.name
    return feiten


def feiten_van_repo(repo: str, bestand: str, ctx: int | None = None, kv_bits: int = 8) -> dict:
    """Hetzelfde, maar zonder het model te downloaden: alleen de eerste
    megabytes van het bestand worden opgehaald (een Range-verzoek van 1 MB), en
    de bestandsgrootte via een HEAD-verzoek. Zo is het advies gemeten in plaats
    van geraden."""
    ctx = int(ctx or CTX)
    url = f"https://huggingface.co/{repo}/resolve/main/{bestand}"
    kop = b""
    lengte = 0
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, method="HEAD"), timeout=30
        ) as r:
            lengte = int(r.headers.get("Content-Length") or 0)
    except Exception:  # noqa: BLE001
        pass
    try:
        with urllib.request.urlopen(
            urllib.request.Request(url, headers={"Range": f"bytes=0-{GGUF_KOP_BYTES - 1}"}),
            timeout=60,
        ) as r:
            kop = r.read(GGUF_KOP_BYTES)
    except Exception as e:  # noqa: BLE001
        raise RuntimeError(f"kon de modelkop niet ophalen: {e}") from e
    feiten = _feiten_uit_kop(kop, lengte // 1048576, ctx, kv_bits)
    feiten["bestand"] = bestand
    feiten["repo"] = repo
    return feiten


def grootste_context_die_past(pad: Path, kv_bits: int = 8) -> int:
    """Welk contextvenster kan dit model halen binnen het beschikbare geheugen?"""
    try:
        feiten = model_feiten(pad, 1024, kv_bits)
    except Exception:  # noqa: BLE001
        return 0
    ruimte = feiten["beschikbaar_mb"] - feiten["gewicht_mb"] - 512
    per_token = feiten["kv_bytes_per_token"]
    if ruimte <= 0 or per_token <= 0:
        return 0
    tokens = (ruimte * 1048576) // per_token
    # afronden naar beneden op een macht van twee, zoals llama.cpp dat fijn vindt
    macht = 1024
    while macht * 2 <= tokens:
        macht *= 2
    return int(macht)


def now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime()) + (
        f".{int(time.time_ns() % 1_000_000_000):09d}Z"
    )


def sha256_file(path: Path, limit: int | None = None) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(1 << 20)
            if not chunk:
                break
            h.update(chunk)
            if limit and fh.tell() > limit:
                break
    return h.hexdigest()


# --------------------------------------------------------------------------- #
# Model-store: GGUF-bestanden + manifests (Ollama-achtige blob/manifest-opzet)
# --------------------------------------------------------------------------- #
class ModelStore:
    """Beheert modellen. Een model = een GGUF-bestand in MODELS_DIR, optioneel
    met een manifest (JSON) voor template/parameters/details."""

    def __init__(self) -> None:
        self._lock = threading.Lock()

    # -- namen -------------------------------------------------------------
    @staticmethod
    def normalize(name: str) -> str:
        n = name.strip()
        n = re.sub(r"^https?://huggingface\.co/", "", n)
        if n.endswith(":latest"):
            n = n[: -len(":latest")]
        return n

    def gguf_for(self, name: str) -> Path | None:
        n = self.normalize(name)
        if n.startswith("hf:"):
            n = n[3:]
        # exacte bestandsnaam, stam, of unieke substring
        cands = sorted(MODELS_DIR.glob("*.gguf"))
        for p in cands:
            if p.name == n or p.stem == n:
                return p
        # quant-suffix: "llama-3.2-1b:q4_k_m" -> llama-3.2-1b-*q4_k_m*.gguf
        base, _, tag = n.partition(":")
        base_l = base.lower().replace("_", "-")
        hits = [
            p
            for p in cands
            if base_l in p.stem.lower().replace("_", "-")
            and (not tag or tag.lower().replace("_", "-") in p.stem.lower().replace("_", "-"))
        ]
        if hits:
            return sorted(hits, key=lambda p: len(p.name))[0]
        return None

    def resolve(self, name: str) -> Path:
        p = self.gguf_for(name)
        if p is None:
            raise KeyError(f"model '{name}' niet gevonden in {MODELS_DIR}")
        return p

    # -- metadata ----------------------------------------------------------
    def manifest(self, name: str) -> dict:
        p = self.gguf_for(name)
        if p is None:
            return {}
        mf = MANIFESTS_DIR / f"{p.stem}.json"
        if mf.exists():
            try:
                return json.loads(mf.read_text())
            except Exception:
                return {}
        return {}

    def digest(self, path: Path) -> str:
        cached = MANIFESTS_DIR / f"{path.stem}.json"
        if cached.exists():
            try:
                d = json.loads(cached.read_text()).get("digest")
                if d and cached.stat().st_mtime >= path.stat().st_mtime:
                    return d
            except Exception:
                pass
        return sha256_file(path)

    @staticmethod
    def quant_from_name(stem: str) -> str:
        m = re.search(r"(iq\d\w*|q\d\w*|f16|bf16|f32)", stem, re.I)
        return m.group(1).upper() if m else "unknown"

    @staticmethod
    def params_from_name(stem: str) -> str:
        m = re.search(r"(\d+(?:\.\d+)?)b", stem, re.I)
        return f"{m.group(1)}B" if m else "unknown"

    @staticmethod
    def arch_from_name(stem: str) -> str:
        """Architectuur-key zoals Ollama die in model_info zet. Clients (Hermes)
        zoeken naar '<arch>.context_length' in /api/show."""
        s = stem.lower()
        for key, arch in (
            ("qwen", "qwen2"),
            ("gemma", "gemma3"),
            ("llama", "llama"),
            ("mistral", "llama"),
            ("mixtral", "llama"),
            ("phi", "phi3"),
            ("deepseek", "deepseek2"),
            ("glm", "glm4"),
            ("granite", "granite"),
            ("falcon", "falcon"),
            ("smollm", "llama"),
            ("tinyllama", "llama"),
        ):
            if key in s:
                return arch
        return "llama"

    def details(self, path: Path) -> dict:
        mf = self.manifest(path.stem)
        det = mf.get("details") or {}
        return {
            "parent_model": "",
            "format": "gguf",
            "family": det.get("family") or path.stem.split("-")[0].lower(),
            "families": det.get("families") or None,
            "parameter_size": det.get("parameter_size") or self.params_from_name(path.stem),
            "quantization_level": det.get("quantization_level") or self.quant_from_name(path.stem),
        }

    def list_models(self) -> list[dict]:
        out = []
        for p in sorted(MODELS_DIR.glob("*.gguf")):
            st = p.stat()
            mf = self.manifest(p.stem)
            out.append(
                {
                    "name": p.stem,
                    "model": p.stem,
                    "modified_at": time.strftime(
                        "%Y-%m-%dT%H:%M:%S", time.localtime(st.st_mtime)
                    )
                    + f".{st.st_mtime_ns % 1_000_000_000:09d}Z",
                    "size": st.st_size,
                    "digest": self.digest(p),
                    "details": self.details(p),
                }
            )
        return out

    # -- pull / delete -----------------------------------------------------
    def pull(self, spec: str, progress=lambda _m: None) -> dict:
        """spec: 'hf:repo/file.gguf', 'hf:repo' (kiest Q4_K_M), of een http(s) URL."""
        spec = spec.strip()
        url = None
        fname = None
        if spec.startswith("hf:"):
            rest = spec[3:]
            parts = rest.split("/")
            if len(parts) >= 2:
                repo = "/".join(parts[:2])
                fname = "/".join(parts[2:]) if len(parts) > 2 else ""
            else:
                repo, fname = rest, ""
            if not fname:
                fname = self._pick_hf_file(repo, progress)
                if not fname:
                    raise KeyError(f"geen GGUF gevonden in {repo}")
            url = f"https://huggingface.co/{repo}/resolve/main/{fname}"
            target = MODELS_DIR / Path(fname).name
        elif spec.startswith("http://") or spec.startswith("https://"):
            url = spec
            target = MODELS_DIR / Path(spec.split("?")[0]).name
        else:
            raise KeyError(
                "pull verwacht 'hf:<repo>/<bestand>.gguf' of een volledige URL "
                "(een eigen registry zit er nog niet in)"
            )
        if target.exists() and target.stat().st_size > 0:
            progress(f"al aanwezig: {target.name}")
            return {"status": "success", "path": str(target)}
        tmp = target.with_suffix(".part")
        progress(f"downloaden {url}")
        with urllib.request.urlopen(url, timeout=60) as resp, tmp.open("wb") as fh:
            total = int(resp.headers.get("Content-Length") or 0)
            got = 0
            last = 0.0
            while True:
                chunk = resp.read(1 << 20)
                if not chunk:
                    break
                fh.write(chunk)
                got += len(chunk)
                if time.time() - last > 1:
                    last = time.time()
                    pct = f"{got * 100 // total}%" if total else f"{got >> 20}MB"
                    progress(f"  {pct}")
        tmp.rename(target)
        progress(f"klaar: {target} ({target.stat().st_size >> 20}MB)")
        return {"status": "success", "path": str(target)}

    @staticmethod
    def _pick_hf_file(repo: str, progress) -> str | None:
        api = f"https://huggingface.co/api/models/{repo}"
        with urllib.request.urlopen(api, timeout=30) as r:
            data = json.load(r)
        files = [s["rfilename"] for s in data.get("siblings", [])]
        ggufs = [f for f in files if f.lower().endswith(".gguf")]
        for pref in ("Q4_K_M", "q4_k_m", "Q4_K_S", "Q5_K_M"):
            for f in ggufs:
                if pref in f:
                    progress(f"gekozen: {f}")
                    return f
        return ggufs[0] if ggufs else None

    def delete(self, name: str) -> None:
        p = self.resolve(name)
        p.unlink()
        mf = MANIFESTS_DIR / f"{p.stem}.json"
        if mf.exists():
            mf.unlink()


STORE = ModelStore()


# --------------------------------------------------------------------------- #
# Engine-manager: start/stop llama-server per model, keep-alive + idle unload
# --------------------------------------------------------------------------- #
class Engine:
    def __init__(self, model: str, gguf: Path, port: int, ctx: int | None = None):
        self.model = model
        self.gguf = gguf
        self.port = port
        self.ctx = int(ctx or CTX)
        self.proc: subprocess.Popen | None = None
        self.ready = False
        self.busy = 0
        self.bezig_sinds: float | None = None
        self.last_used = time.time()
        self.load_duration_ns = 0
        self.lock = threading.Lock()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> None:
        with self.lock:
            if self.proc and self.proc.poll() is None:
                return
            t0 = time.time()
            args = [
                str(ENGINE),
                "-m",
                str(self.gguf),
                "-c",
                str(self.ctx),
                "-t",
                str(THREADS),
                "--host",
                "127.0.0.1",
                "--port",
                str(self.port),
                # BELANGRIJK: llama-server verdeelt de KV-cache over parallelle
                # slots. Met de default (meerdere slots) krijgt een verzoek maar
                # een fractie van de context en klaagt de client over
                # "context size exceeded". Ollama draait ook met 1 slot.
                "--parallel",
                "1",
                # Flash attention + 8-bits KV-cache: halveert het KV-geheugen,
                # waardoor 64k context op een 8 GB LXC past.
                "-fa",
                "on",
                "-ctk",
                KV_TYPE,
                "-ctv",
                KV_TYPE,
                # GPU-offload: dit is de grootste kwaliteitswinst — met meer
                # rekenkracht kan er een groter en beter model op.
                "-ngl",
                str(NGL),
                # Embedding-modellen hebben deze vlag nodig; llama-server weigert
                # /v1/embeddings anders. Auto-detectie op de bestandsnaam.
                *(["--embeddings"] if "embed" in self.gguf.stem.lower() else []),
                "--jinja",
                "--no-webui",
            ]
            log = (HOME / "logs").mkdir(exist_ok=True) or (HOME / "logs" / f"{self.model}.log")
            self.proc = subprocess.Popen(
                args,
                stdout=open(log, "ab"),
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
            )
            deadline = time.time() + 300
            while time.time() < deadline:
                if self.proc.poll() is not None:
                    raise RuntimeError(
                        f"engine stopte direct; zie {log}"
                    )
                try:
                    with urllib.request.urlopen(self.base + "/health", timeout=2) as r:
                        if r.status == 200:
                            self.load_duration_ns = int((time.time() - t0) * 1e9)
                            self.ready = True
                            return
                except Exception:
                    time.sleep(0.4)
            raise RuntimeError("engine niet klaar binnen 300s")

    def begin(self) -> None:
        """Boek een verzoek in, en onthoud sinds wanneer er gewerkt wordt."""
        if self.busy == 0:
            self.bezig_sinds = time.time()
        self.busy += 1

    def eindig(self) -> None:
        self.busy = max(0, self.busy - 1)
        if self.busy == 0:
            self.bezig_sinds = None
        self.last_used = time.time()

    def te_lang_bezig(self) -> float:
        """Hoeveel seconden is deze kern al bezig (0 als hij vrij is)."""
        if self.busy and self.bezig_sinds:
            return time.time() - self.bezig_sinds
        return 0.0

    def hard_stop(self, reden: str = "herstel") -> None:
        """Breek het rekenen hard af.

        Nodig omdat llama-server een weggevallen client niet opmerkt tijdens het
        verwerken van een grote prompt: hij rekent dan minutenlang door voor
        niemand, en met één rekenvak wacht elke volgende vraag daarop. Alleen de
        kern stoppen maakt het slot echt vrij."""
        with self.lock:
            if self.proc and self.proc.poll() is None:
                sys.stderr.write(f"[wllm] rekenkern gestopt: {reden}\n")
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
            self.ready = False
            self.busy = 0
            self.bezig_sinds = None

    def stop(self) -> None:
        with self.lock:
            self.ready = False
            if self.proc and self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
            self.proc = None

    def touch(self) -> None:
        self.last_used = time.time()

    def alive(self) -> bool:
        return bool(self.proc and self.proc.poll() is None)

    def info(self) -> dict:
        return {
            "name": self.model,
            "model": self.model,
            "size": self.gguf.stat().st_size,
            "digest": STORE.digest(self.gguf),
            "details": STORE.details(self.gguf),
            "expires_at": time.strftime(
                "%Y-%m-%dT%H:%M:%S",
                time.localtime(self.last_used + KEEP_ALIVE),
            )
            + ".000000000Z",
            "size_vram": 0,
        }


class Engines:
    def __init__(self) -> None:
        self._by_model: dict[str, Engine] = {}
        self._lock = threading.Lock()
        self._next_port = ENGINE_PORT_BASE
        t = threading.Thread(target=self._reaper, daemon=True)
        t.start()

    def get(self, name: str, ctx: int | None = None) -> Engine:
        gguf = STORE.resolve(name)
        ctx = int(ctx or CTX)
        key = f"{gguf.stem}@{ctx}"
        with self._lock:
            eng = self._by_model.get(key)
            if eng is None or not eng.alive():
                port = self._next_port
                self._next_port += 1
                eng = Engine(gguf.stem, gguf, port, ctx)
                self._by_model[key] = eng
        eng.start()
        eng.touch()
        return eng

    def loaded(self) -> list[Engine]:
        with self._lock:
            return [e for e in self._by_model.values() if e.alive()]

    def _reaper(self) -> None:
        while True:
            time.sleep(5)
            for eng in self.loaded():
                # Nooit unloaden terwijl een verzoek loopt: een prompt van 14k
                # tokens op CPU duurt minuten en zou anders midden in de
                # generatie worden afgebroken.
                if eng.busy == 0 and time.time() - eng.last_used > KEEP_ALIVE:
                    eng.stop()
                    print(f"[wllm] idle unload: {eng.model}", file=sys.stderr, flush=True)

    def shutdown(self) -> None:
        for eng in self.loaded():
            eng.stop()


ENGINES = Engines()


# --------------------------------------------------------------------------- #
# Vertaling: Ollama-verzoek  ->  llama-server (OpenAI-formaat)
# --------------------------------------------------------------------------- #
def options_to_params(opts: dict) -> dict:
    """Vertaal Ollama 'options' naar OpenAI-parameters."""
    o = opts or {}
    m = {
        "temperature": "temperature",
        "top_p": "top_p",
        "top_k": "top_k",
        "min_p": "min_p",
        "repeat_penalty": "repeat_penalty",
        "presence_penalty": "presence_penalty",
        "frequency_penalty": "frequency_penalty",
        "seed": "seed",
        "stop": "stop",
    }
    out = {}
    for k, v in o.items():
        if k in m:
            out[m[k]] = v
    if "num_predict" in o:
        out["max_tokens"] = o["num_predict"]
    if "num_ctx" in o:
        out["_ctx"] = o["num_ctx"]
    return out


def engine_stream(eng: Engine, payload: dict, klant=None):
    """Genereer SSE-chunks van llama-server als dicts."""
    eng.begin()
    try:
        req = urllib.request.Request(
            eng.base + "/v1/chat/completions",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=UPSTREAM_TIMEOUT) as resp:
            klaar = threading.Event()
            if klant is not None:
                threading.Thread(
                    target=_bewaak_klant, args=(klant, resp, klaar, eng), daemon=True
                ).start()
            try:
                for raw in resp:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    try:
                        yield json.loads(data)
                    except json.JSONDecodeError:
                        continue
            finally:
                klaar.set()
    finally:
        eng.eindig()


def _klant_weg(verbinding) -> bool:
    """Is de vragende partij zijn verbinding kwijt?

    Een gesloten verbinding is 'leesbaar' en levert dan niets op; met MSG_PEEK
    kijken we dat na zonder iets uit de stroom te halen."""
    if verbinding is None:
        return False
    try:
        leesbaar, _, _ = select.select([verbinding], [], [], 0)
        if not leesbaar:
            return False
        return verbinding.recv(1, socket.MSG_PEEK) == b""
    except (OSError, ValueError):
        return True


def _bewaak_klant(verbinding, resp, klaar: threading.Event, eng: "Engine | None" = None):
    """Breek het rekenen af zodra de vragende partij wegvalt.

    Zonder dit blijft een weggevallen verzoek doorrekenen: een agent die zijn
    geduld verliest of een gesloten tabblad laat de rekenkern minutenlang
    doorwerken. De kern doet één verzoek tegelijk, dus elke volgende vraag zou
    moeten wachten tot dat af is -- en dat kan oplopen tot de tijdlimiet."""
    while not klaar.wait(1.0):
        if _klant_weg(verbinding):
            try:
                resp.close()
            except Exception:  # noqa: BLE001
                pass
            # De verbinding sluiten breekt een lopende generatie af. Tijdens het
            # verwerken van een grote prompt merkt de kern dat niet, dus wachten we
            # kort en stoppen hem daarna -- anders blijft het enige rekenvak bezet.
            if klaar.wait(HERSTEL_NA):
                return
            if eng is not None:
                eng.hard_stop("weggevallen client")
            return


def engine_once(eng: Engine, payload: dict, klant=None) -> dict:
    p = dict(payload)
    p["stream"] = False
    eng.begin()
    try:
        req = urllib.request.Request(
            eng.base + "/v1/chat/completions",
            data=json.dumps(p).encode(),
            headers={"Content-Type": "application/json"},
        )
        resp = urllib.request.urlopen(req, timeout=UPSTREAM_TIMEOUT)
        klaar = threading.Event()
        if klant is not None:
            threading.Thread(
                target=_bewaak_klant, args=(klant, resp, klaar, eng), daemon=True
            ).start()
        try:
            return json.load(resp)
        finally:
            klaar.set()
            resp.close()
    finally:
        eng.eindig()


# --------------------------------------------------------------------------- #
# HTTP-API
# --------------------------------------------------------------------------- #
def _tok(tekst: str) -> int:
    return int(len(tekst or "") / TEKENS_PER_TOKEN) + 1


def kort_in(messages: list[dict], ctx: int, max_tokens: int) -> tuple[list[dict], int]:
    """Laat een gesprek binnen het contextvenster passen.

    Zonder dit antwoordt de rekenkern met 'Context size has been exceeded' en
    krijgt de gebruiker een fout in plaats van een antwoord -- precies bij de
    lange gesprekken waarin je het meest hebt gestopt. We bewaren de
    systeemafspraken en de nieuwste berichten, en zeggen het erbij als er iets
    wegviel. Geeft (berichten, aantal weggelaten berichten)."""
    if not messages:
        return messages, 0
    ruimte = max(256, int(ctx) - int(max_tokens) - 64)
    systeem = [m for m in messages if m.get("role") == "system"]
    rest = [m for m in messages if m.get("role") != "system"]
    gebruikt = sum(_tok(m.get("content") or "") for m in systeem)
    gekozen: list[dict] = []
    for m in reversed(rest):
        n = _tok(m.get("content") or "")
        if gebruikt + n <= ruimte:
            gekozen.insert(0, m)
            gebruikt += n
            continue
        if not gekozen:
            teksten = str(m.get("content") or "")
            bewaar = max(200, int((ruimte - gebruikt) * TEKENS_PER_TOKEN))
            gekozen.insert(0, {**m, "content": "…" + teksten[-bewaar:]})
            gebruikt += _tok(gekozen[0]["content"])
        break
    weggelaten = len(rest) - len(gekozen)
    uit = list(systeem) + gekozen
    if weggelaten > 0:
        uit.append(
            {
                "role": "system",
                "content": f"Er zijn {weggelaten} eerdere berichten weggelaten omdat het "
                "gesprek te lang werd voor het contextvenster. Ga verder met wat je hier ziet.",
            }
        )
    return uit, weggelaten


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"wllm/{VERSION}"

    # -- helpers -----------------------------------------------------------
    def log_message(self, format, *args):  # noqa: A002 - stdlib-signatuur
        sys.stderr.write("[wllm] %s\n" % (format % args))

    def _json(self, obj, code: int = 200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # Client hing op (Hermes breekt langzame streams af). Geen 500 van maken.
            pass

    def _err(self, msg: str, code: int = 400):
        self._json({"error": msg}, code)

    def _html(self, tekst: str, code: int = 200):
        body = tekst.encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _png(self, body: bytes, code: int = 200, cache: str = "public, max-age=300"):
        self.send_response(code)
        self.send_header("Content-Type", "image/png")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _status_pagina(self) -> str:
        """Mensvriendelijke statuspagina. Een browser vraagt HTML; een client die
        de API aanspreekt krijgt gewoon JSON op dezelfde plek."""
        aanb = aanbevolen_model() or {}

        # Let op: STORE.list_models() berekent een sha256 over elk model (ettelijke
        # GB's) als het manifest nog niet bestaat. Een statuspagina mag dat nooit
        # triggeren, dus hier alleen de bestandsgrootte en een gecachete digest.
        rijen = ""
        heeft_digest = False
        for p in sorted(MODELS_DIR.glob("*.gguf")):
            mf = STORE.manifest(p.stem)
            dig = str(mf.get("digest") or "")
            heeft_digest = heeft_digest or bool(dig)
            rijen += (
                f'<tr><td class="naam">{p.stem}</td>'
                f'<td class="grijs">{p.stat().st_size / 1024 / 1024:,.0f} MB</td>'
                f'<td class="grijs">{STORE.quant_from_name(p.stem)}</td>'
                f'<td class="grijs">{dig[:12] if dig else "—"}</td></tr>'
            )
        if rijen and not heeft_digest:
            # Zonder gecachete digest is de kolom alleen streepjes; laat hem dan weg.
            rijen = rijen.replace('<td class="grijs">—</td>', '')
            kop = "<tr><th>Model</th><th>Grootte</th><th>Quantisatie</th></tr>"
        else:
            kop = "<tr><th>Model</th><th>Grootte</th><th>Quantisatie</th><th>Digest</th></tr>"
        rijen = rijen or '<tr><td colspan="4" class="grijs">nog geen modellen — haal er een met <code>wllm.py pull hf:&lt;repo&gt;/&lt;bestand&gt;.gguf</code></td></tr>'
        gpu_tekst = (
            f'{GPU.get("naam") or "aanwezig"} · {NGL} lagen op de GPU'
            if GPU.get("aanwezig")
            else "geen GPU gevonden — alles rekent op de CPU"
        )
        aanb_tekst = (
            f'{aanb.get("bestand", "?")} — {aanb.get("opmerking", "")}'
            if aanb
            else "nog niet te bepalen"
        )
        return (
            "<!doctype html><html lang=\"nl\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
            f"<title>Werdens Media LLM — engine {VERSION}</title><style>"
            f"{HUISSTIJL}"
            "main{padding:22px 26px 40px;display:grid;gap:16px;max-width:1120px}"
            "dl{display:grid;grid-template-columns:190px 1fr;gap:7px 14px;margin:0}"
            "dt{color:var(--muted);font-size:13px}dd{margin:0}"
            ".tabel .naam{font-family:var(--mono);color:var(--accent-d);font-weight:600}"
            ".grijs{color:var(--muted2)}"
            "footer{padding:0 26px 34px;color:var(--muted2);font-size:13px}"
            # Op een telefoon passen de labels niet naast de waarden.
            "@media(max-width:640px){"
            "main{padding:16px 16px 30px}"
            "dl{grid-template-columns:1fr;gap:2px}dt{margin-top:8px}"
            "header.kop{flex-wrap:wrap;gap:8px}"
            ".kop-rechts{margin-left:0;width:100%}"
            ".kaart-body{padding:14px}}"
            "</style></head><body>"
            "<header class=\"kop\"><div class=\"merk\">"
            "<span class=\"logo\"><img src=\"/merk.png\" alt=\"Werdens Media\" "
            "onerror=\"this.style.display='none'\"></span>"
            "<div><div class=\"naam\">Werdens Media LLM</div>"
            "<div class=\"sub\">Werdens Media · engine</div></div></div>"
            f"<div class=\"kop-rechts\">"
            "<button class=\"knop\" id=\"herstel\" type=\"button\">Herstel</button>"
            "<a class=\"knop primair\" href=\"/chat\">Chatten</a>"
            f"<span>poort {PORT} · Ollama- en OpenAI-API</span></div>"
            "</header><main>"
            "<div class=\"kaart\"><div class=\"kaart-kop\"><h2>Status</h2></div>"
            "<div class=\"kaart-body\"><dl>"
            f"<dt>Versie</dt><dd>{VERSION}</dd>"
            "<dt>Toestand</dt><dd class=\"ok\">in bedrijf</dd>"
            f"<dt>Engine</dt><dd><code>{ENGINE}</code></dd>"
            f"<dt>Contextvenster</dt><dd>{CTX:,} tokens</dd>"
            f"<dt>KV-cache</dt><dd>{KV_TYPE}</dd>"
            f"<dt>Rekenkracht</dt><dd>{THREADS} threads"
            f" <small>({_toegestane_cores()} cores beschikbaar,"
            f" {_fysieke_cores()} fysiek)</small></dd>"
            f"<dt>GPU</dt><dd>{gpu_tekst}</dd>"
            f"<dt>Aanbevolen model</dt><dd>{aanb_tekst}</dd>"
            "</dl></div></div>"
            "<div class=\"kaart\"><div class=\"kaart-kop\"><h2>Modellen in huis</h2></div>"
            "<div class=\"kaart-body\"><table class=\"tabel\">"
            f"{kop}"
            f"{rijen}</table></div></div>"
            "<div class=\"kaart\"><div class=\"kaart-kop\"><h2>Endpoints</h2></div>"
            "<div class=\"kaart-body\"><dl>"
            "<dt>Ollama-vorm</dt><dd><code>/api/tags</code> <code>/api/chat</code> "
            "<code>/api/generate</code> <code>/api/show</code> <code>/api/pull</code> "
            "<code>/api/embed</code></dd>"
            "<dt>OpenAI-vorm</dt><dd><code>/v1/models</code> "
            "<code>/v1/chat/completions</code> <code>/v1/embeddings</code></dd>"
            "<dt>Status</dt><dd><code>/health</code> (JSON)</dd>"
            "</dl></div></div>"
            "<div class=\"kaart\"><div class=\"kaart-kop\"><h2>De drie lagen</h2></div>"
            "<div class=\"kaart-body\"><dl>"
            f"<dt>Engine</dt><dd>deze pagina — poort {PORT}</dd>"
            "<dt>Werkruimte</dt><dd>documenten met bronvermelding — poort 8090</dd>"
            "<dt>Media</dt><dd>spraak naar tekst en spraak uit — poort 8091</dd>"
            "</dl></div></div>"
            "<script>"
            "var h = document.getElementById('herstel');"
            "if (h) { h.addEventListener('click', function () {"
            "  h.disabled = true; h.textContent = 'bezig';"
            "  fetch('/api/herstel', { method: 'POST' })"
            "    .then(function (r) { return r.json(); })"
            "    .then(function (d) {"
            "      h.textContent = d.gestopt ? 'opgeruimd (' + d.gestopt + ')' : 'niets te herstellen';"
            "      setTimeout(function () { h.disabled = false; h.textContent = 'Herstel'; }, 4000);"
            "    })"
            "    .catch(function () { h.textContent = 'mislukt'; h.disabled = false; });"
            "}); }"
            "</script>"
            "</main><footer>Werdens Media LLM · eigen code, alle rechten voorbehouden · "
            "gebruikte onderdelen van derden houden hun eigen licentie</footer>"
            "</body></html>"
        )

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return {}

    def _stream_start(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Connection", "close")
        self.end_headers()

    def _nd(self, obj) -> bool:
        try:
            self.wfile.write((json.dumps(obj) + "\n").encode())
            self.wfile.flush()
            return True
        except (BrokenPipeError, ConnectionResetError):
            return False

    def _herstel(self):
        """Maak het rekenvak vrij zonder in de container te hoeven.

        Een verzoek dat blijft hangen (een client die wegvalt tijdens het
        verwerken van een grote prompt) houdt het enige rekenvak bezet. Met deze
        route stop je de rekenkernen en begint schoon."""
        gestopt = 0
        for e in ENGINES.loaded():
            e.hard_stop("herstel gevraagd")
            gestopt += 1
        _ruim_achterblijvers_op()
        return self._json({"hersteld": True, "gestopt": gestopt})

    # -- routing -----------------------------------------------------------
    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/logo.png", "/merk.png"):
            # /merk.png is de nieuwe naam: een browser die het oude /logo.png nog
            # met de lege terugval in zijn cache heeft, haalt deze vanzelf opnieuw
            # op. De terugval krijgt nooit een cachekop.
            body, terugval = logo_info()
            return self._png(body, cache="no-store" if terugval else "public, max-age=300")
        if path in ("/chat", "/chat/"):
            return self._html(CHAT_PAGINA.replace("__HUISSTIJL__", HUISSTIJL))
        if path in ("/api/version", "/version"):
            return self._json({"version": VERSION})
        if path == "/api/tags":
            return self._json({"models": STORE.list_models()})
        if path == "/api/herstel":
            return self._herstel()
        if path == "/api/ps":
            return self._json({"models": [e.info() for e in ENGINES.loaded()]})
        if path == "/v1/models":
            return self._json(
                {
                    "object": "list",
                    "data": [
                        {
                            "id": m["name"],
                            "object": "model",
                            "created": int(time.time()),
                            "owned_by": "wllm",
                            # Clients (o.a. Hermes) lezen dit om hun prompt te
                            # doseren en te comprimeren. Zonder deze waarde gaan
                            # ze van een te groot venster uit en laten ze een
                            # te-lange prompt gewoon mislukken.
                            "context_length": CTX,
                            "max_model_len": CTX,
                            "meta": {"n_ctx_train": CTX, "n_ctx": CTX},
                        }
                        for m in STORE.list_models()
                    ],
                }
            )
        if path == "/" and "text/html" in (self.headers.get("Accept") or ""):
            return self._html(self._status_pagina())
        if path in ("/", "/health"):
            return self._json(
                {
                    "status": "ok",
                    "version": VERSION,
                    "engine": str(ENGINE),
                    "gpu": GPU,
                    "n_gpu_layers": NGL,
                    "context": CTX,
                    "kv_type": KV_TYPE,
                    "threads": THREADS,
                    "cores": {
                        "toegestaan": _toegestane_cores(),
                        "fysiek": _fysieke_cores(),
                    },
                    "aanbevolen_model": aanbevolen_model(),
                }
            )
        return self._err("not found", 404)

    def do_POST(self):
        path = self.path.split("?")[0]
        try:
            if path == "/api/herstel":
                return self._herstel()
            if path == "/api/show":
                return self._show()
            if path == "/api/pull":
                return self._pull()
            if path == "/api/delete":
                return self._delete()
            if path == "/api/chat":
                return self._chat()
            if path == "/api/generate":
                return self._generate()
            if path in ("/api/embed", "/api/embeddings"):
                return self._embed(path)
            if path == "/v1/chat/completions":
                return self._openai_chat()
            if path == "/v1/completions":
                return self._openai_complete()
            if path == "/v1/embeddings":
                return self._openai_embed()
            return self._err("not found", 404)
        except KeyError as e:
            return self._err(str(e), 404)
        except urllib.error.URLError as e:
            return self._err(f"engine-fout: {e}", 502)
        except Exception as e:  # noqa: BLE001
            return self._err(f"{type(e).__name__}: {e}", 500)

    # -- modelbeheer -------------------------------------------------------
    def _show(self):
        b = self._body()
        name = b.get("model") or b.get("name") or ""
        p = STORE.resolve(name)
        mf = STORE.manifest(p.stem)
        arch = STORE.arch_from_name(p.stem)
        return self._json(
            {
                "details": STORE.details(p),
                "modelfile": mf.get("modelfile", f"FROM {p}"),
                "parameters": mf.get("parameters", ""),
                "template": mf.get("template", ""),
                "system": mf.get("system", ""),
                "license": mf.get("license", ""),
                "size": p.stat().st_size,
                "modified_at": now_iso(),
                # Ollama-vorm: clients (o.a. Hermes) lezen de context uit
                # model_info['<arch>.context_length'] en uit parameters.
                "model_info": {
                    "general.architecture": arch,
                    f"{arch}.context_length": CTX,
                    "wllm.path": str(p),
                    "wllm.engine": str(ENGINE),
                },
                "parameters": f"num_ctx {CTX}",
                "capabilities": ["completion", "tools"],
                "context_length": CTX,
            }
        )

    def _pull(self):
        b = self._body()
        spec = b.get("model") or b.get("name") or ""
        stream = b.get("stream", True)
        if not stream:
            return self._json(STORE.pull(spec))
        self._stream_start()
        msgs: list[str] = []
        done = threading.Event()

        def prog(m: str):
            msgs.append(m)

        def work():
            try:
                STORE.pull(spec, prog)
                msgs.append("__ok__")
            except Exception as e:  # noqa: BLE001
                msgs.append(f"__err__{e}")
            finally:
                done.set()

        threading.Thread(target=work, daemon=True).start()
        seen = 0
        while not done.is_set() or seen < len(msgs):
            while seen < len(msgs):
                m = msgs[seen]
                seen += 1
                if m == "__ok__":
                    self._nd({"status": "success"})
                elif m.startswith("__err__"):
                    self._nd({"error": m[7:]})
                else:
                    self._nd({"status": m})
            time.sleep(0.3)
        self._nd({"status": "success"})

    def _delete(self):
        b = self._body()
        STORE.delete(b.get("model") or b.get("name") or "")
        return self._json({"status": "success"})

    # -- genereren ---------------------------------------------------------
    def _gereed(self, b: dict):
        """Bereid een verzoek voor, of weiger het eerlijk.

        Staat er al te lang een verzoek op de rekenkern te wachten, dan is een
        antwoord binnen seconden beter dan een half uur wachten zonder uitleg."""
        eng, msgs, payload = self._prepare(b)
        wacht = eng.te_lang_bezig()
        if wacht > WACHT_LIMIET:
            self._json(
                {
                    "error": f"De rekenkern is al {int(wacht)} seconden bezig met een "
                    "ander verzoek. Probeer het opnieuw, of gebruik Herstel op de "
                    "statuspagina."
                },
                503,
            )
            return None
        return eng, msgs, payload

    def _prepare(self, b: dict) -> tuple[Engine, list[dict], dict]:
        name = b.get("model") or b.get("name")
        if not name:
            raise KeyError("veld 'model' ontbreekt")
        # Ollama-gedrag: num_ctx per verzoek bepaalt de context van de engine
        eng = ENGINES.get(name, (b.get("options") or {}).get("num_ctx"))
        mf = STORE.manifest(eng.model)
        messages = b.get("messages")
        if messages is None:
            prompt = b.get("prompt") or b.get("input") or ""
            sys_msg = b.get("system") or mf.get("system") or ""
            messages = []
            if sys_msg:
                messages.append({"role": "system", "content": sys_msg})
            messages.append({"role": "user", "content": prompt})
        params = options_to_params(b.get("options") or {})
        max_tokens = int(b.get("max_tokens") or params.pop("max_tokens", 512) or 512)
        # Zonder systeemafspraken vult de engine een Nederlandse standaard in; een
        # client met eigen afspraken (Hermes) merkt hier niets van.
        if STANDAARD_SYSTEEM and not any(m.get("role") == "system" for m in messages):
            messages = [{"role": "system", "content": STANDAARD_SYSTEEM}, *messages]
        messages, weggelaten = kort_in(messages, eng.ctx, max_tokens)
        if weggelaten:
            sys.stderr.write(f"[wllm] {weggelaten} bericht(en) weggelaten voor het contextvenster\n")
        payload = {
            "model": eng.model,
            "messages": messages,
            "stream": bool(b.get("stream", True)),
            "max_tokens": max_tokens,
        }
        payload.update({k: v for k, v in params.items() if not k.startswith("_")})
        return eng, messages, payload

    def _bewaak_venster(self, eng: Engine, payload: dict) -> dict:
        """Zet de standaardsysteemprompt en de inkorting ook op de OpenAI-route.

        Die route bouwt zijn verzoek zelf uit de body en ging daardoor buiten de
        gewone voorbereiding om: geen systeemafspraken en geen inkorting, waardoor
        een lang gesprek alsnog op 'Context size exceeded' stukliep -- precies de
        route die clients als Hermes gebruiken."""
        msgs = payload.get("messages")
        mt = int(payload.get("max_tokens") or payload.get("max_completion_tokens") or 512)
        if msgs:
            if STANDAARD_SYSTEEM and not any(m.get("role") == "system" for m in msgs):
                msgs = [{"role": "system", "content": STANDAARD_SYSTEEM}, *msgs]
            msgs, weggelaten = kort_in(msgs, eng.ctx, mt)
            if weggelaten:
                sys.stderr.write(
                    f"[wllm] {weggelaten} bericht(en) weggelaten voor het contextvenster\n"
                )
            payload["messages"] = msgs
        else:
            prompt = payload.get("prompt")
            if isinstance(prompt, str) and prompt:
                ruimte = max(256, int(eng.ctx) - mt - 64)
                max_tekens = int(ruimte * TEKENS_PER_TOKEN)
                if len(prompt) > max_tekens:
                    payload["prompt"] = "…" + prompt[-max_tekens:]
                    sys.stderr.write("[wllm] prompt ingekort voor het contextvenster\n")
        return payload

    def _chat(self):
        b = self._body()
        klaar = self._gereed(b)
        if klaar is None:
            return
        eng, _msgs, payload = klaar
        stream = payload["stream"]
        t0 = time.time()
        if not stream:
            r = engine_once(eng, payload, self.connection)
            eng.touch()
            msg = r["choices"][0]["message"]
            usage = r.get("usage") or {}
            total = int((time.time() - t0) * 1e9)
            return self._json(
                {
                    "model": eng.model,
                    "created_at": now_iso(),
                    "message": {
                        "role": "assistant",
                        "content": msg.get("content") or "",
                        "thinking": msg.get("reasoning_content") or None,
                    },
                    "done": True,
                    "done_reason": r["choices"][0].get("finish_reason") or "stop",
                    "total_duration": total,
                    "load_duration": eng.load_duration_ns,
                    "prompt_eval_count": usage.get("prompt_tokens", 0),
                    "eval_count": usage.get("completion_tokens", 0),
                    "eval_duration": total,
                }
            )
        self._stream_start()
        usage, finish = {}, "stop"
        for chunk in engine_stream(eng, payload, self.connection):
            if chunk.get("usage"):
                usage = chunk["usage"]
            for ch in chunk.get("choices") or []:
                d = ch.get("delta") or {}
                if ch.get("finish_reason"):
                    finish = ch["finish_reason"]
                piece = d.get("content")
                think = d.get("reasoning_content")
                if not piece and not think:
                    continue
                if not self._nd(
                    {
                        "model": eng.model,
                        "created_at": now_iso(),
                        "message": {
                            "role": "assistant",
                            "content": piece or "",
                            **({"thinking": think} if think else {}),
                        },
                        "done": False,
                    }
                ):
                    break
        eng.touch()
        total = int((time.time() - t0) * 1e9)
        self._nd(
            {
                "model": eng.model,
                "created_at": now_iso(),
                "message": {"role": "assistant", "content": ""},
                "done": True,
                "done_reason": finish,
                "total_duration": total,
                "load_duration": eng.load_duration_ns,
                "prompt_eval_count": usage.get("prompt_tokens", 0),
                "eval_count": usage.get("completion_tokens", 0),
                "eval_duration": total,
            }
        )

    def _generate(self):
        b = self._body()
        klaar = self._gereed(b)
        if klaar is None:
            return
        eng, _msgs, payload = klaar
        stream = payload["stream"]
        t0 = time.time()
        if not stream:
            r = engine_once(eng, payload, self.connection)
            eng.touch()
            usage = r.get("usage") or {}
            total = int((time.time() - t0) * 1e9)
            return self._json(
                {
                    "model": eng.model,
                    "created_at": now_iso(),
                    "response": r["choices"][0]["message"].get("content") or "",
                    "done": True,
                    "done_reason": r["choices"][0].get("finish_reason") or "stop",
                    "total_duration": total,
                    "load_duration": eng.load_duration_ns,
                    "prompt_eval_count": usage.get("prompt_tokens", 0),
                    "eval_count": usage.get("completion_tokens", 0),
                    "eval_duration": total,
                }
            )
        self._stream_start()
        usage, finish = {}, "stop"
        for chunk in engine_stream(eng, payload, self.connection):
            if chunk.get("usage"):
                usage = chunk["usage"]
            for ch in chunk.get("choices") or []:
                d = ch.get("delta") or {}
                if ch.get("finish_reason"):
                    finish = ch["finish_reason"]
                piece = d.get("content")
                if not piece:
                    continue
                if not self._nd(
                    {
                        "model": eng.model,
                        "created_at": now_iso(),
                        "response": piece,
                        "done": False,
                    }
                ):
                    break
        eng.touch()
        total = int((time.time() - t0) * 1e9)
        self._nd(
            {
                "model": eng.model,
                "created_at": now_iso(),
                "response": "",
                "done": True,
                "done_reason": finish,
                "total_duration": total,
                "load_duration": eng.load_duration_ns,
                "prompt_eval_count": usage.get("prompt_tokens", 0),
                "eval_count": usage.get("completion_tokens", 0),
                "eval_duration": total,
            }
        )

    # -- embeddings --------------------------------------------------------
    def _embed(self, path: str):
        b = self._body()
        eng = ENGINES.get(b.get("model") or b.get("name") or "")
        inp = b.get("input") or b.get("prompt") or ""
        req = urllib.request.Request(
            eng.base + "/v1/embeddings",
            data=json.dumps({"input": inp, "model": eng.model}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=600) as r:
            data = json.load(r)
        eng.touch()
        vecs = [d["embedding"] for d in data.get("data", [])]
        if path == "/api/embeddings":
            return self._json({"embedding": vecs[0] if vecs else []})
        return self._json({"model": eng.model, "embeddings": vecs})

    # -- OpenAI-compatible doorgeefluik ------------------------------------
    def _openai_chat(self):
        b = self._body()
        eng = ENGINES.get(b.get("model") or "")
        payload = self._bewaak_venster(eng, dict(b))
        payload["model"] = eng.model
        if payload.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            for chunk in engine_stream(eng, payload, self.connection):
                try:
                    self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    break
            eng.touch()
            try:
                self.wfile.write(b"data: [DONE]\n\n")
                self.wfile.flush()
            except Exception:
                pass
            return
        r = engine_once(eng, payload, self.connection)
        eng.touch()
        r["model"] = eng.model
        r["id"] = "chatcmpl-" + hashlib.sha1(
            (r["choices"][0]["message"].get("content") or "").encode()
        ).hexdigest()[:24]
        r["object"] = "chat.completion"
        r["created"] = int(time.time())
        return self._json(r)

    def _openai_complete(self):
        b = self._body()
        eng = ENGINES.get(b.get("model") or "")
        prompt = b.get("prompt") or ""
        if isinstance(prompt, list):
            prompt = "\n".join(prompt)
        payload = self._bewaak_venster(
            eng,
            {
                "model": eng.model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "max_tokens": b.get("max_tokens") or 512,
                "temperature": b.get("temperature", 0.8),
            },
        )
        r = engine_once(eng, payload, self.connection)
        eng.touch()
        txt = r["choices"][0]["message"].get("content") or ""
        return self._json(
            {
                "id": "cmpl-" + hashlib.sha1(txt.encode()).hexdigest()[:24],
                "object": "text_completion",
                "created": int(time.time()),
                "model": eng.model,
                "choices": [{"text": txt, "index": 0, "finish_reason": "stop"}],
                "usage": r.get("usage") or {},
            }
        )

    def _openai_embed(self):
        b = self._body()
        eng = ENGINES.get(b.get("model") or "")
        req = urllib.request.Request(
            eng.base + "/v1/embeddings",
            data=json.dumps(b).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=600) as r:
            data = json.load(r)
        eng.touch()
        return self._json(data)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def api(path: str, payload: dict | None = None, stream: bool = False, host: str = "127.0.0.1"):
    url = f"http://{host}:{PORT}{path}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=3600) as r:
        if stream:
            for raw in r:
                line = raw.decode().strip()
                if line:
                    yield json.loads(line)
        else:
            yield json.load(r)


def _ruim_achterblijvers_op() -> int:
    """Ruim rekenkernen op die van een vorige start zijn achtergebleven.

    Een harde stop (kill -9, stroomstoring, crash) laat de rekenkern van
    llama.cpp achter. Die houdt zijn poort én zijn geheugen vast: de nieuwe start
    krijgt dan geen verbinding meer (de poort is bezet) en op een kleine machine
    loopt het geheugen vol. Alleen processen die van deze engine afkomstig zijn en
    waarvan de ouder geen draaiende engine meer is, worden opgeruimd -- twee
    engines naast elkaar blijven dus met rust gelaten."""
    if not os.path.isdir("/proc"):
        # Windows heeft geen /proc; daar ruimt stop-werdens-media-llm.ps1 de
        # achtergebleven rekenkernen op.
        return 0
    levende_engines = set()
    for naam in os.listdir("/proc"):
        if not naam.isdigit():
            continue
        try:
            with open(f"/proc/{naam}/cmdline", "rb") as f:
                cmd = f.read().decode("utf-8", "replace")
        except OSError:
            continue
        if "wllm.py" in cmd:
            levende_engines.add(int(naam))

    opgeruimd = 0
    for naam in os.listdir("/proc"):
        if not naam.isdigit():
            continue
        pid = int(naam)
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                cmd = f.read().decode("utf-8", "replace")
        except OSError:
            continue
        if str(ENGINE) not in cmd or "llama-server" not in cmd:
            continue
        try:
            with open(f"/proc/{pid}/stat", "rb") as f:
                stat = f.read().decode("utf-8", "replace")
            ppid = int(stat.rsplit(")", 1)[1].split()[1])
        except (OSError, IndexError, ValueError):
            continue
        if ppid in levende_engines:
            continue
        try:
            os.kill(pid, signal.SIGTERM)
            opgeruimd += 1
        except OSError:
            pass
    return opgeruimd


def cmd_serve(_a):
    opgeruimd = _ruim_achterblijvers_op()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"wllm {VERSION} — engine: {ENGINE}")
    print(f"  Ollama-API : http://{HOST}:{PORT}/api/...")
    print(f"  OpenAI-API : http://{HOST}:{PORT}/v1/...")
    print(f"  modellen   : {MODELS_DIR}")
    print(f"  threads={THREADS} ctx={CTX} keep_alive={KEEP_ALIVE}s")
    if opgeruimd:
        print(f"  opgeruimd  : {opgeruimd} achtergebleven rekenkern(en) van een vorige start")
    if GPU.get("aanwezig"):
        print(f"  GPU        : {GPU.get('naam')} ({GPU.get('vram_mb')} MB) — "
              f"n_gpu_layers={NGL}, kv={KV_TYPE}")
    else:
        print(f"  GPU        : geen — alles op CPU, n_gpu_layers={NGL}, kv={KV_TYPE}")
    advies = aanbevolen_model()
    if advies:
        print(f"  beste model: {advies['bestand']} ({advies['opmerking']})")

    def bye(*_s):
        # Hier géén srv.shutdown(): dat wacht tot de serveer-lus stopt, en die
        # loopt in dezélfde draad -- de afsluiting wacht dus op zichzelf en hangt.
        # Een herstart van de dienst liep daardoor in de tijdlimiet van systemd en
        # werd uiteindelijk hard afgebroken. Rekenkernen netjes stoppen en dan weg.
        try:
            ENGINES.shutdown()
        finally:
            sys.stdout.flush()
            os._exit(0)

    signal.signal(signal.SIGINT, bye)
    signal.signal(signal.SIGTERM, bye)
    srv.serve_forever()


def cmd_list(_a):
    for m in STORE.list_models():
        d = m["details"]
        print(
            f"{m['name']:<48} {m['size'] / 1e9:6.2f} GB  {d['parameter_size']:<8} "
            f"{d['quantization_level']:<8} {m['digest'][:12]}"
        )


def cmd_ps(_a):
    for e in ENGINES.loaded():
        print(f"{e.model:<48} poort {e.port}  actief")


def cmd_pull(a):
    for line in api("/api/pull", {"model": a.model, "stream": True}, stream=True):
        print(line.get("status") or line.get("error") or line)


def cmd_run(a):
    payload = {
        "model": a.model,
        "prompt": " ".join(a.prompt),
        "stream": True,
        "options": {"temperature": a.temperature},
    }
    for line in api("/api/generate", payload, stream=True):
        sys.stdout.write(line.get("response") or "")
        sys.stdout.flush()
    print()


def cmd_gpu(_a):
    print(json.dumps(
        {"gpu": GPU, "n_gpu_layers": NGL, "kv_type": KV_TYPE, "context": CTX,
         "threads": THREADS, "engine": str(ENGINE), "aanbevolen_model": aanbevolen_model()},
        ensure_ascii=False, indent=2,
    ))


def _aanbevelen_meten() -> None:
    """Haal van elk model in de tabel de echte architectuur op (ongeveer 1 MB per
    model) en bepaal wat er op deze machine écht past. Geen vuistregels."""
    mb, bron = beschikbaar_mb()
    print(f"Deze machine : {mb} MB bruikbaar ({bron})")
    print(f"Context      : {CTX} tokens, KV-cache {KV_TYPE}")
    if GPU.get("aanwezig"):
        print(f"GPU          : {GPU.get('naam')} ({GPU.get('vram_mb')} MB)")
    print()
    print(f"{'model':<46}{'gewichten':>11}{'KV-cache':>10}{'nodig':>9}{'past':>6}")
    beste = None
    for _drempel, repo, bestand, opmerking in MODEL_TRAPPEN:
        try:
            feiten = feiten_van_repo(repo, bestand, CTX)
        except Exception as e:  # noqa: BLE001
            print(f"{bestand:<46}{'?':>11}{'?':>10}{'?':>9}   ({e})")
            continue
        print(
            f"{bestand:<46}{str(feiten['gewicht_mb']) + ' MB':>11}"
            f"{str(feiten['kv_mb']) + ' MB':>10}{str(feiten['nodig_mb']) + ' MB':>9}"
            f"{('ja' if feiten['past'] else 'nee'):>6}"
        )
        if feiten["past"] and beste is None:
            beste = (repo, bestand, opmerking, feiten)
    print()
    if beste:
        repo, bestand, opmerking, feiten = beste
        print(f"Beste model dat past: {bestand} — {opmerking}")
        print(f"  {feiten['gewicht_mb']} MB gewichten + {feiten['kv_mb']} MB KV-cache "
              f"= {feiten['nodig_mb']} MB van {feiten['beschikbaar_mb']} MB")
        print(f"  {feiten['lagen']} lagen, {feiten['kv_koppen']} KV-koppen, "
              f"kop-lengte {feiten['kop_lengte']}")
        print(f"  ophalen: python3 {Path(__file__).name} pull hf:{repo}/{bestand}")
    else:
        print("Geen enkel model uit de lijst past bij dit contextvenster.")
        print("Zet WLLM_CTX lager, geef de machine meer geheugen, of zet er een GPU in.")


def cmd_modelinfo(a):
    pad = STORE.resolve(a.model)
    feiten = model_feiten(pad, a.context)
    print(json.dumps(feiten, ensure_ascii=False, indent=2))
    past = feiten["nodig_mb"] <= feiten["beschikbaar_mb"]
    print(f"\n{'PAST' if past else 'PAST NIET'}: {feiten['nodig_mb']} MB nodig, "
          f"{feiten['beschikbaar_mb']} MB beschikbaar")
    groot = grootste_context_die_past(pad)
    if groot:
        print(f"Grootste contextvenster dat op deze machine past: {groot} tokens")
    if not past:
        print("Zet WLLM_CTX lager, gebruik een kleiner model, of zet er een GPU in.")


def cmd_aanbevelen(a):
    if getattr(a, "meten", False):
        return _aanbevelen_meten()

    advies = aanbevolen_model()
    mb, bron = advies.get("geheugen_mb"), advies.get("gebaseerd_op")
    if GPU.get("aanwezig"):
        print(f"GPU gevonden: {GPU.get('naam')} met {GPU.get('vram_mb')} MB VRAM")
    else:
        print(f"Geen GPU gevonden; er wordt op CPU gerekend met {mb} MB {bron}")
    if not advies:
        print("Kon geen advies bepalen.")
        return
    print(f"Beste model dat hier past: {advies['bestand']}")
    print(f"  {advies['opmerking']}")
    print(f"  ophalen: python3 {Path(__file__).name} {advies['pull']}")
    if getattr(a, "ophalen", False):
        for regel in api("/api/pull", {"model": f"hf:{advies['repo']}/{advies['bestand']}",
                                       "stream": True}, stream=True):
            print(regel.get("status") or regel.get("error") or regel)


def main():
    p = argparse.ArgumentParser(prog="wllm", description="Eigen Ollama-compatible LLM-server")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("serve", help="start de server").set_defaults(fn=cmd_serve)
    sub.add_parser("list", help="toon modellen").set_defaults(fn=cmd_list)
    sub.add_parser("ps", help="toon geladen modellen").set_defaults(fn=cmd_ps)
    pl = sub.add_parser("pull", help="download een GGUF-model")
    pl.add_argument("model")
    pl.set_defaults(fn=cmd_pull)
    rn = sub.add_parser("run", help="eenmalige prompt")
    rn.add_argument("model")
    rn.add_argument("prompt", nargs="+")
    rn.add_argument("--temperature", type=float, default=0.7)
    rn.set_defaults(fn=cmd_run)
    sub.add_parser("gpu", help="toon GPU, instellingen en het beste model").set_defaults(fn=cmd_gpu)
    ad = sub.add_parser("aanbevelen", help="advies: het beste model voor deze machine")
    ad.add_argument("--ophalen", action="store_true", help="het advies meteen downloaden")
    ad.add_argument("--meten", action="store_true",
                    help="de echte cijfers van elk model ophalen (1 MB per model)")
    ad.set_defaults(fn=cmd_aanbevelen)
    mi = sub.add_parser("model-info", help="architectuur en geheugengebruik van een model")
    mi.add_argument("model")
    mi.add_argument("--context", type=int, default=None)
    mi.set_defaults(fn=cmd_modelinfo)
    a = p.parse_args()
    if not getattr(a, "fn", None):
        p.print_help()
        return
    a.fn(a)


if __name__ == "__main__":
    main()
