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
import ast
import datetime
import gzip
import math
import zoneinfo
import base64
import hashlib
import html
import json
import os
import re
import select
import signal
import shutil
import socket
import sqlite3
import subprocess
import struct
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

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


# ---------------------------------------------------------------------------
# Gereedschap: dingen die een taalmodel niet uit zichzelf kan weten
#
# Een taalmodel maakt woorden op basis van wat het ooit gelezen heeft. Het weet
# niet wat het vandaag in Hilversum doet, wat er in het nieuws is of hoe laat
# het is. Daarvoor haalt de engine zelf gegevens op -- weer, nieuws, naslag en
# zoeken -- en zet die in de vraag. Het model hoeft dus niets te beslissen: het
# krijgt de gegevens kant-en-klaar aangeleverd, wat ook voor een klein model
# werkt.
#
# Alles is sleutelvrij: Open-Meteo (weer), nl.wikipedia (naslag), de NOS-feed
# (nieuws) en DuckDuckGo (zoeken). Uitzetten kan met WLLM_GEREEDSCHAP=0.
# ---------------------------------------------------------------------------

GEREEDSCHAP = os.environ.get("WLLM_GEREEDSCHAP", "1").strip().lower() not in (
    "0",
    "nee",
    "uit",
    "false",
    "off",
)
GEREEDSCHAP_TIMEOUT = float(os.environ.get("WLLM_GEREEDSCHAP_TIMEOUT", "15"))
GEREEDSCHAP_PLAATS = os.environ.get("WLLM_GEREEDSCHAP_PLAATS", "").strip()
GEREEDSCHAP_UA = "WerdensMediaLLM/1.0 (eigen installatie)"

# WMO-weercodes van Open-Meteo naar gewoon Nederlands.
WMO = {
    0: "onbewolkt",
    1: "licht bewolkt",
    2: "half bewolkt",
    3: "zwaar bewolkt",
    45: "mist",
    48: "mist met rijp",
    51: "lichte motregen",
    53: "motregen",
    55: "zware motregen",
    56: "lichte ijzel",
    57: "ijzel",
    61: "lichte regen",
    63: "regen",
    65: "zware regen",
    66: "lichte ijzelregen",
    67: "ijzelregen",
    71: "lichte sneeuw",
    73: "sneeuw",
    75: "zware sneeuw",
    77: "sneeuwkorrels",
    80: "lichte buien",
    81: "buien",
    82: "zware buien",
    85: "lichte sneeuwbuien",
    86: "zware sneeuwbuien",
    95: "onweer",
    96: "onweer met hagel",
    99: "zwaar onweer met hagel",
}


def _haal(url: str, gegevens: bytes | None = None, koppen: dict | None = None,
          limiet: float | None = None) -> bytes:
    """Haal een adres op. Korte tijd, eigen naam, en nooit stil blijven hangen."""
    koppen = dict(koppen or {})
    koppen.setdefault("User-Agent", GEREEDSCHAP_UA)
    koppen.setdefault("Accept-Language", "nl-NL,nl;q=0.9,en;q=0.5")
    verzoek = urllib.request.Request(url, data=gegevens, headers=koppen)
    with urllib.request.urlopen(verzoek, timeout=limiet or GEREEDSCHAP_TIMEOUT) as antwoord:
        return antwoord.read()


def _haal_json(url: str, gegevens: bytes | None = None, koppen: dict | None = None,
               limiet: float | None = None) -> Any:
    return json.loads(_haal(url, gegevens, koppen, limiet).decode("utf-8", "replace"))


def _schoon(tekst: str) -> str:
    """HTML eraf en overtollige witruimte eruit."""
    tekst = re.sub(r"<[^>]+>", " ", tekst or "")
    tekst = html.unescape(tekst)
    return re.sub(r"\s+", " ", tekst).strip()


def _kort(tekst: str, lengte: int) -> str:
    tekst = (tekst or "").strip()
    if len(tekst) <= lengte:
        return tekst
    geknipt = tekst[:lengte]
    if " " in geknipt:
        geknipt = geknipt[: geknipt.rfind(" ")]
    return geknipt + "…"


def _getal(waarde: object, eenheid: str = "", komma: bool = True) -> str:
    try:
        cijfer = float(waarde)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return ""
    tekst = f"{cijfer:.1f}".rstrip("0").rstrip(".") if komma else f"{cijfer:.0f}"
    if komma:
        tekst = tekst.replace(".", ",")
    return f"{tekst}{eenheid}"


# ---- de afzonderlijke stukken gereedschap ---------------------------------


def gereedschap_tijd() -> dict:
    """Datum en tijd van deze machine."""
    nu = time.localtime()
    dagen = ["maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag"]
    tekst = (
        f"Het is nu {dagen[nu.tm_wday]} {nu.tm_mday}-{nu.tm_mon:02d}-{nu.tm_year}, "
        f"{nu.tm_hour:02d}:{nu.tm_min:02d} uur."
    )
    return {"naam": "tijd", "bron": "de klok van deze machine", "tekst": tekst}


def gereedschap_weer(plaats: str) -> dict:
    """Actueel weer en de verwachting voor drie dagen (Open-Meteo, zonder sleutel)."""
    plaats = (plaats or "").strip()
    if not plaats:
        return {"naam": "weer", "bron": "open-meteo.com", "tekst": "", "fout": "geen plaats opgegeven"}
    zoek = urllib.parse.quote(plaats)
    gevonden = _haal_json(
        f"https://geocoding-api.open-meteo.com/v1/search?name={zoek}&count=1&language=nl&format=json"
    )
    treffers = (gevonden or {}).get("results") or [] if isinstance(gevonden, dict) else []
    if not treffers:
        return {
            "naam": "weer",
            "bron": "open-meteo.com",
            "tekst": "",
            "fout": f"de plaats '{plaats}' is niet gevonden",
        }
    plek = treffers[0]
    naam = plek.get("name") or plaats
    land = plek.get("country") or ""
    waar = f"{naam}, {land}" if land else naam
    gegevens = _haal_json(
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={plek['latitude']}&longitude={plek['longitude']}"
        "&current=temperature_2m,apparent_temperature,relative_humidity_2m,precipitation,"
        "weather_code,wind_speed_10m"
        "&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max"
        "&timezone=auto&forecast_days=3"
    )
    if not isinstance(gegevens, dict):
        return {"naam": "weer", "bron": "open-meteo.com", "tekst": "", "fout": "geen gegevens terug"}
    nu = gegevens.get("current") or {}
    dagen = gegevens.get("daily") or {}
    regels = [
        f"Weer in {waar}: nu {_getal(nu.get('temperature_2m'), ' °C')} "
        f"({WMO.get(nu.get('weather_code'), 'onbekend')}), "
        f"gevoel {_getal(nu.get('apparent_temperature'), ' °C')}, "
        f"vochtigheid {_getal(nu.get('relative_humidity_2m'), '%', komma=False)}, "
        f"wind {_getal(nu.get('wind_speed_10m'), ' km/u', komma=False)}."
    ]
    datums = dagen.get("time") or []
    for i, datum in enumerate(datums[:3]):
        maximum = (dagen.get("temperature_2m_max") or [None])[i]
        minimum = (dagen.get("temperature_2m_min") or [None])[i]
        code = (dagen.get("weather_code") or [None])[i]
        kans = (dagen.get("precipitation_probability_max") or [None])[i]
        wanneer = "vandaag" if i == 0 else ("morgen" if i == 1 else "overmorgen")
        regels.append(
            f"  {wanneer} ({datum}): {_getal(minimum, ' °C')} tot {_getal(maximum, ' °C')}, "
            f"{WMO.get(code, 'onbekend')}"
            + (f", {_getal(kans, '% neerslag', komma=False)}" if kans is not None else "")
        )
    return {"naam": "weer", "bron": "open-meteo.com", "tekst": "\n".join(regels), "plaats": waar}


def gereedschap_wiki(onderwerp: str) -> dict:
    """Korte samenvatting uit de Nederlandse Wikipedia."""
    onderwerp = (onderwerp or "").strip()
    if not onderwerp:
        return {"naam": "naslag", "bron": "nl.wikipedia.org", "tekst": "", "fout": "geen onderwerp"}
    zoek = urllib.parse.quote(onderwerp)
    gevonden = _haal_json(
        "https://nl.wikipedia.org/w/api.php?action=query&list=search"
        f"&srsearch={zoek}&format=json&srlimit=1&utf8=1"
    )
    treffers = []
    if isinstance(gevonden, dict):
        treffers = ((gevonden.get("query") or {}).get("search") or [])
    if not treffers:
        return {
            "naam": "naslag",
            "bron": "nl.wikipedia.org",
            "tekst": "",
            "fout": f"niets gevonden over '{onderwerp}'",
        }
    titel = treffers[0].get("title") or onderwerp
    samenvatting = _haal_json(
        f"https://nl.wikipedia.org/api/rest_v1/page/summary/{urllib.parse.quote(titel)}"
    )
    tekst = ""
    if isinstance(samenvatting, dict):
        tekst = samenvatting.get("extract") or ""
    if not tekst:
        tekst = _schoon(treffers[0].get("snippet") or "")
    if not tekst:
        return {
            "naam": "naslag",
            "bron": "nl.wikipedia.org",
            "tekst": "",
            "fout": f"geen samenvatting over '{titel}'",
        }
    return {
        "naam": "naslag",
        "bron": f"nl.wikipedia.org/wiki/{titel.replace(' ', '_')}",
        "tekst": f"{titel}: {_kort(tekst, 900)}",
        "titel": titel,
    }


def gereedschap_nieuws(aantal: int = 6) -> dict:
    """De koppen van het NOS Nieuws (RSS, zonder sleutel)."""
    ruw = _haal("https://feeds.nos.nl/nosnieuwsalgemeen").decode("utf-8", "replace")
    koppen = []
    for blok in re.findall(r"<item>(.*?)</item>", ruw, re.S)[:aantal]:
        titel = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", blok, re.S)
        if titel:
            koppen.append(_schoon(titel.group(1)))
    if not koppen:
        return {"naam": "nieuws", "bron": "nos.nl", "tekst": "", "fout": "geen nieuws terug"}
    regels = [f"{i}. {kop}" for i, kop in enumerate(koppen, 1)]
    return {
        "naam": "nieuws",
        "bron": "nos.nl (NOS Nieuws)",
        "tekst": "Koppen van het NOS Nieuws:\n" + "\n".join(regels),
    }


def _ddg_lite(vraag: str, post: bool) -> list[dict]:
    """DuckDuckGo zonder poespas. Lukt het niet, dan geeft hij niets terug."""
    adres = "https://lite.duckduckgo.com/lite/"
    if post:
        ruw = _haal(
            adres,
            urllib.parse.urlencode({"q": vraag}).encode(),
            {"Content-Type": "application/x-www-form-urlencoded"},
        ).decode("utf-8", "replace")
    else:
        ruw = _haal(f"{adres}?q={urllib.parse.quote(vraag)}").decode("utf-8", "replace")
    treffers = []
    for stuk in re.finditer(r"<a([^>]*)>(.*?)</a>", ruw, re.S):
        attributen, tekst = stuk.group(1), _schoon(stuk.group(2))
        if "result-link" not in attributen or not tekst:
            continue
        adres_gevonden = re.search(r"href=[\"']([^\"']+)[\"']", attributen)
        adres_gevonden = html.unescape(adres_gevonden.group(1)) if adres_gevonden else ""
        # DuckDuckGo verstopt het echte adres in een omleiding; die halen we eruit.
        omgeleid = re.search(r"[?&]uddg=([^&]+)", adres_gevonden)
        if omgeleid:
            adres_gevonden = urllib.parse.unquote(omgeleid.group(1))
        if adres_gevonden.startswith("//"):
            adres_gevonden = "https:" + adres_gevonden
        treffers.append({"titel": tekst, "url": adres_gevonden})
    stukken = re.findall(r"class=[\"']result-snippet[\"'][^>]*>(.*?)</td>", ruw, re.S)
    for i, stuk in enumerate(stukken[: len(treffers)]):
        treffers[i]["tekst"] = _kort(_schoon(stuk), 240)
    return treffers[:5]


def _ddg_antwoord(vraag: str) -> dict | None:
    """De instant-answer-API van DuckDuckGo: vaak een kant-en-klaar antwoord."""
    gegevens = _haal_json(
        "https://api.duckduckgo.com/?q="
        f"{urllib.parse.quote(vraag)}&format=json&no_html=1&skip_disambig=1"
    )
    if not isinstance(gegevens, dict):
        return None
    for veld in ("Answer", "Abstract", "Definition"):
        waarde = (gegevens.get(veld) or "").strip()
        if waarde:
            bron = gegevens.get("AbstractURL") or gegevens.get("DefinitionURL") or "duckduckgo.com"
            return {"tekst": _kort(waarde, 700), "bron": bron}
    return None


def gereedschap_zoek(vraag: str) -> dict:
    """Zoeken op internet. Meerdere routes, want geen enkele is altijd bereikbaar.

    Lukt geen enkele route, dan zegt de engine dat eerlijk -- een verzonnen
    antwoord is erger dan 'ik kon het niet opzoeken'.
    """
    vraag = (vraag or "").strip()
    if not vraag:
        return {"naam": "zoeken", "bron": "duckduckgo.com", "tekst": "", "fout": "geen zoekvraag"}
    for post in (False, True):
        try:
            treffers = _ddg_lite(vraag, post)
        except Exception:
            treffers = []
        if treffers:
            regels = []
            for i, treffer in enumerate(treffers, 1):
                regel = f"{i}. {treffer['titel']}"
                if treffer.get("tekst"):
                    regel += f" -- {treffer['tekst']}"
                if treffer.get("url"):
                    regel += f" ({treffer['url']})"
                regels.append(regel)
            return {
                "naam": "zoeken",
                "bron": "duckduckgo.com",
                "tekst": f"Zoekresultaten voor '{vraag}':\n" + "\n".join(regels),
            }
    try:
        antwoord = _ddg_antwoord(vraag)
    except Exception:
        antwoord = None
    if antwoord:
        return {
            "naam": "zoeken",
            "bron": antwoord["bron"],
            "tekst": f"Over '{vraag}': {antwoord['tekst']}",
        }
    naslag = gereedschap_wiki(vraag)
    if naslag.get("tekst"):
        naslag["naam"] = "zoeken"
        naslag["tekst"] = f"Over '{vraag}' (uit de encyclopedie): {naslag['tekst']}"
        return naslag
    return {
        "naam": "zoeken",
        "bron": "duckduckgo.com",
        "tekst": "",
        "fout": f"geen zoekresultaten voor '{vraag}'",
    }


# ---- tweede reeks gereedschap ---------------------------------------------
# Rekenen, eenheden, datum, feestdagen en tijdzones werken zonder internet.
# De rest haalt gegevens op bij bronnen die geen sleutel vragen: PDOK
# (postcodes en wegen), Buienradar (neerslag), Open-Meteo (lucht en zon), de
# Europese Centrale Bank (valuta), CoinGecko (crypto), WikiWoordenboek,
# MyMemory (vertalen), RDAP (domeinnamen), NOS/Tweakers/NU (nieuws) en
# NDW/Rijkswaterstaat (verkeer). De laatste groep leest je eigen apps.


def _coordinaten(plaats: str) -> tuple[float, float, str]:
    """Plaats naar breedte, lengte en de nette naam."""
    gevonden = _haal_json(
        "https://geocoding-api.open-meteo.com/v1/search?"
        f"name={urllib.parse.quote(plaats)}&count=1&language=nl&format=json"
    )
    treffers = (gevonden or {}).get("results") or [] if isinstance(gevonden, dict) else []
    if not treffers:
        raise ValueError(f"plaats '{plaats}' niet gevonden")
    eerste = treffers[0]
    return float(eerste["latitude"]), float(eerste["longitude"]), str(eerste.get("name") or plaats)


# ---- A. zonder internet ---------------------------------------------------


def _reken(uitdrukking: str) -> float:
    """Rekenen zonder eval op willekeurige code: alleen getallen en rekenwerk."""
    schoon = (uitdrukking or "").strip().replace(",", ".").replace("^", "**")
    schoon = re.sub(r"(\d+(?:\.\d+)?)\s*%\s*(?:van|procent van)\s*(\d+(?:\.\d+)?)", r"(\1/100)*\2", schoon)
    schoon = re.sub(r"(\d+(?:\.\d+)?)\s*(?:%|procent)\s*(?=$|[+\-*/)])", r"(\1/100)", schoon)
    schoon = re.sub(r"(?<=\d)\s*x\s*(?=\d)", "*", schoon)
    schoon = re.sub(r"\bkeer\b", "*", schoon)
    schoon = re.sub(r"\bgedeeld door\b", "/", schoon)
    schoon = re.sub(r"\bplus\b", "+", schoon)
    schoon = re.sub(r"\bmin\b", "-", schoon)
    if not schoon or len(schoon) > 200:
        raise ValueError("geen bruikbare som")
    knoop = ast.parse(schoon, mode="eval").body
    toestaan = (
        ast.BinOp, ast.UnaryOp, ast.Constant, ast.Add, ast.Sub, ast.Mult, ast.Div,
        ast.Pow, ast.Mod, ast.FloorDiv, ast.USub, ast.UAdd, ast.Call, ast.Name, ast.Load,
    )
    for deel in ast.walk(knoop):
        if not isinstance(deel, toestaan):
            raise ValueError("alleen rekenen, geen tekst")
        if isinstance(deel, ast.Constant) and not isinstance(deel.value, (int, float)):
            raise ValueError("alleen getallen")
        if isinstance(deel, ast.Name) and deel.id not in (
            "wortel", "sqrt", "afronden", "round", "abs", "macht", "min", "max"
        ):
            raise ValueError(f"onbekende naam '{deel.id}'")
    functies = {
        "wortel": math.sqrt, "sqrt": math.sqrt, "afronden": round, "round": round,
        "abs": abs, "macht": pow, "min": min, "max": max,
    }
    return float(eval(compile(ast.Expression(knoop), "<rekenen>", "eval"), {"__builtins__": {}}, functies))


def gereedschap_rekenen(uitdrukking: str) -> dict:
    """Een som uitrekenen. De engine rekent zelf, dus geen gok van het model."""
    try:
        uitkomst = _reken(uitdrukking)
    except Exception as fout:
        return {"naam": "rekenen", "bron": "de engine zelf", "tekst": "",
                "fout": f"{type(fout).__name__}: {fout}"}
    netjes = f"{uitkomst:,.6f}".rstrip("0").rstrip(".").replace(",", "_").replace(".", ",").replace("_", ".")
    return {
        "naam": "rekenen",
        "bron": "de engine zelf",
        "tekst": f"{uitdrukking.strip()} = {netjes}",
    }


EENHEDEN = {
    # lengte, in meters
    "mm": ("lengte", 0.001), "millimeter": ("lengte", 0.001),
    "cm": ("lengte", 0.01), "centimeter": ("lengte", 0.01),
    "dm": ("lengte", 0.1), "m": ("lengte", 1.0), "meter": ("lengte", 1.0),
    "km": ("lengte", 1000.0), "kilometer": ("lengte", 1000.0),
    "inch": ("lengte", 0.0254), "duim": ("lengte", 0.0254),
    "voet": ("lengte", 0.3048), "foot": ("lengte", 0.3048), "feet": ("lengte", 0.3048),
    "yard": ("lengte", 0.9144),
    "mijl": ("lengte", 1609.344), "mile": ("lengte", 1609.344), "miles": ("lengte", 1609.344),
    "zeemijl": ("lengte", 1852.0),
    # gewicht, in grammen
    "mg": ("gewicht", 0.001), "g": ("gewicht", 1.0), "gram": ("gewicht", 1.0),
    "ons": ("gewicht", 100.0), "kg": ("gewicht", 1000.0), "kilo": ("gewicht", 1000.0),
    "kilogram": ("gewicht", 1000.0), "pond": ("gewicht", 453.59237), "lb": ("gewicht", 453.59237),
    "ton": ("gewicht", 1000000.0),
    # inhoud, in milliliters
    "ml": ("inhoud", 1.0), "cl": ("inhoud", 10.0), "dl": ("inhoud", 100.0),
    "l": ("inhoud", 1000.0), "liter": ("inhoud", 1000.0), "litre": ("inhoud", 1000.0),
    "m3": ("inhoud", 1000000.0), "gallon": ("inhoud", 3785.41), "pint": ("inhoud", 473.18),
    # snelheid, in meters per seconde
    "km/u": ("snelheid", 0.277778), "kmh": ("snelheid", 0.277778), "km/h": ("snelheid", 0.277778),
    "m/s": ("snelheid", 1.0), "mph": ("snelheid", 0.44704), "mijl/u": ("snelheid", 0.44704),
    "knoop": ("snelheid", 0.514444), "knopen": ("snelheid", 0.514444),
    # tijd, in seconden
    "seconde": ("tijd", 1.0), "sec": ("tijd", 1.0), "minuut": ("tijd", 60.0), "min": ("tijd", 60.0),
    "uur": ("tijd", 3600.0), "dag": ("tijd", 86400.0), "week": ("tijd", 604800.0),
    "jaar": ("tijd", 31557600.0),
}

TEMPERATUUR = {"c", "celsius", "graden", "f", "fahrenheit", "k", "kelvin"}


def gereedschap_eenheden(waarde: float, van: str, naar: str) -> dict:
    """Eenheden omrekenen: lengte, gewicht, inhoud, snelheid, tijd en temperatuur."""
    van = (van or "").strip().lower().rstrip(".")
    naar = (naar or "").strip().lower().rstrip(".")
    try:
        getal = float(str(waarde).replace(",", "."))
    except (TypeError, ValueError):
        return {"naam": "eenheden", "bron": "de engine zelf", "tekst": "", "fout": "geen getal"}
    if van in TEMPERATUUR and naar in TEMPERATUUR:
        celsius = getal
        if van.startswith("f"):
            celsius = (getal - 32) * 5 / 9
        elif van.startswith("k"):
            celsius = getal - 273.15
        uitkomst = celsius
        if naar.startswith("f"):
            uitkomst = celsius * 9 / 5 + 32
        elif naar.startswith("k"):
            uitkomst = celsius + 273.15
        return {"naam": "eenheden", "bron": "de engine zelf",
                "tekst": f"{_getal(getal)} {van} = {_getal(uitkomst)} {naar}"}
    if van not in EENHEDEN or naar not in EENHEDEN:
        onbekend = van if van not in EENHEDEN else naar
        return {"naam": "eenheden", "bron": "de engine zelf", "tekst": "",
                "fout": f"eenheid '{onbekend}' ken ik niet"}
    soort_van, factor_van = EENHEDEN[van]
    soort_naar, factor_naar = EENHEDEN[naar]
    if soort_van != soort_naar:
        return {"naam": "eenheden", "bron": "de engine zelf", "tekst": "",
                "fout": f"'{van}' en '{naar}' meten niet hetzelfde"}
    uitkomst = getal * factor_van / factor_naar
    return {"naam": "eenheden", "bron": "de engine zelf",
            "tekst": f"{_getal(getal)} {van} = {_getal(uitkomst)} {naar}"}


MAANDEN = {
    "januari": 1, "februari": 2, "maart": 3, "april": 4, "mei": 5, "juni": 6, "juli": 7,
    "augustus": 8, "september": 9, "oktober": 10, "november": 11, "december": 12,
}
DAGEN = ["maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag"]


def _pasen(jaar: int) -> datetime.date:
    """Eerste paasdag (Meeus-algoritme), want daar hangen de andere feestdagen aan."""
    a = jaar % 19
    b, c = divmod(jaar, 100)
    d, e = divmod(b, 4)
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = divmod(c, 4)
    l = (32 + 2 * e + 2 * i - h - k) % 7  # noqa: E741
    m = (a + 11 * h + 22 * l) // 451
    maand, dag = divmod(h + l - 7 * m + 114, 31)
    return datetime.date(jaar, maand, dag + 1)


def _feestdagen(jaar: int) -> list[tuple[datetime.date, str]]:
    pasen = _pasen(jaar)
    koningsdag = datetime.date(jaar, 4, 27)
    if koningsdag.weekday() == 6:  # zondag -> een dag eerder
        koningsdag = datetime.date(jaar, 4, 26)
    return [
        (datetime.date(jaar, 1, 1), "Nieuwjaarsdag"),
        (pasen - datetime.timedelta(days=2), "Goede Vrijdag"),
        (pasen, "Eerste Paasdag"),
        (pasen + datetime.timedelta(days=1), "Tweede Paasdag"),
        (koningsdag, "Koningsdag"),
        (datetime.date(jaar, 5, 5), "Bevrijdingsdag"),
        (pasen + datetime.timedelta(days=39), "Hemelvaartsdag"),
        (pasen + datetime.timedelta(days=49), "Eerste Pinksterdag"),
        (pasen + datetime.timedelta(days=50), "Tweede Pinksterdag"),
        (datetime.date(jaar, 12, 25), "Eerste Kerstdag"),
        (datetime.date(jaar, 12, 26), "Tweede Kerstdag"),
    ]


def gereedschap_feestdagen(jaar: int | None = None) -> dict:
    """De Nederlandse feestdagen van een jaar, berekend en niet opgezocht."""
    jaar = int(jaar or time.localtime().tm_year)
    regels = [f"{dag.strftime('%d-%m')} ({DAGEN[dag.weekday()]}) {naam}" for dag, naam in _feestdagen(jaar)]
    return {
        "naam": "feestdagen",
        "bron": "berekend uit de paasdatum (geen opzoeking)",
        "tekst": f"Feestdagen {jaar}: " + "; ".join(regels),
    }


def _datum_uit(tekst: str) -> datetime.date | None:
    """Een datum uit gewone taal peuteren: 25 december, 25-12, 25/12/2027, 2026-12-25."""
    laag = (tekst or "").lower()
    vandaag = datetime.date.today()
    for patroon, volgorde in (
        (r"(\d{4})-(\d{1,2})-(\d{1,2})", "jmd"),
        (r"(\d{1,2})[-/](\d{1,2})[-/](\d{4})", "dmj"),
        (r"(\d{1,2})\s+(" + "|".join(MAANDEN) + r")(?:\s+(\d{4}))?", "dmj-woord"),
    ):
        treffer = re.search(patroon, laag)
        if not treffer:
            continue
        groepen = treffer.groups()
        try:
            if volgorde == "jmd":
                return datetime.date(int(groepen[0]), int(groepen[1]), int(groepen[2]))
            if volgorde == "dmj":
                return datetime.date(int(groepen[2]), int(groepen[1]), int(groepen[0]))
            dag = int(groepen[0])
            maand = MAANDEN[groepen[1]]
            jaar = int(groepen[2]) if groepen[2] else vandaag.year
            datum = datetime.date(jaar, maand, dag)
            if not groepen[2] and datum < vandaag:
                datum = datetime.date(jaar + 1, maand, dag)
            return datum
        except ValueError:
            return None
    for woord, dagen in (("vandaag", 0), ("morgen", 1), ("overmorgen", 2), ("gisteren", -1)):
        if re.search(rf"\b{woord}\b", laag):
            return vandaag + datetime.timedelta(days=dagen)
    return None


def gereedschap_datum(vraag: str) -> dict:
    """Rekenen met datums: hoeveel dagen tot, of welke dag was het."""
    vandaag = datetime.date.today()
    datum = _datum_uit(vraag)
    if not datum:
        return {"naam": "datum", "bron": "de engine zelf",
                "tekst": f"Vandaag is het {DAGEN[vandaag.weekday()]} {vandaag.strftime('%d-%m-%Y')}."}
    verschil = (datum - vandaag).days
    richting = "over" if verschil > 0 else ("vandaag" if verschil == 0 else "geleden")
    deel = "" if verschil == 0 else f" Dat is {abs(verschil)} dagen {richting}."
    weken = ""
    if abs(verschil) >= 7:
        weken = f" ({abs(verschil) // 7} weken en {abs(verschil) % 7} dagen)"
    return {
        "naam": "datum",
        "bron": "de engine zelf",
        "tekst": (
            f"{datum.strftime('%d-%m-%Y')} is een {DAGEN[datum.weekday()]}."
            f"{deel}{weken} Vandaag is {vandaag.strftime('%d-%m-%Y')}."
        ),
    }


TIJDZONES = {
    "amsterdam": "Europe/Amsterdam", "nederland": "Europe/Amsterdam", "brussel": "Europe/Brussels",
    "londen": "Europe/London", "parijs": "Europe/Paris", "berlijn": "Europe/Berlin",
    "madrid": "Europe/Madrid", "rome": "Europe/Rome", "athene": "Europe/Athens",
    "moskou": "Europe/Moscow", "istanbul": "Europe/Istanbul", "dubai": "Asia/Dubai",
    "new york": "America/New_York", "los angeles": "America/Los_Angeles",
    "chicago": "America/Chicago", "toronto": "America/Toronto", "mexico": "America/Mexico_City",
    "sao paulo": "America/Sao_Paulo", "buenos aires": "America/Argentina/Buenos_Aires",
    "tokio": "Asia/Tokyo", "seoul": "Asia/Seoul", "sjanghai": "Asia/Shanghai",
    "shanghai": "Asia/Shanghai", "hongkong": "Asia/Hong_Kong", "singapore": "Asia/Singapore",
    "mumbai": "Asia/Kolkata", "bangkok": "Asia/Bangkok", "jakarta": "Asia/Jakarta",
    "sydney": "Australia/Sydney", "auckland": "Pacific/Auckland", "kaapstad": "Africa/Johannesburg",
    "kaapstad ": "Africa/Johannesburg", "cairo": "Africa/Cairo", "nairobi": "Africa/Nairobi",
    "utc": "UTC", "gmt": "UTC", "reu": "UTC",
}


def gereedschap_tijdzone(plaats: str) -> dict:
    """Hoe laat is het daar? Berekend met de tijdzonedatabase van het systeem."""
    plaats = (plaats or "").strip().lower()
    zone = TIJDZONES.get(plaats)
    if not zone:
        for naam, kandidate in TIJDZONES.items():
            if naam in plaats:
                zone, plaats = kandidate, naam
                break
    if not zone:
        return {"naam": "tijdzone", "bron": "tijdzonedatabase", "tekst": "",
                "fout": f"ik ken geen tijdzone voor '{plaats}'"}
    try:
        daar = datetime.datetime.now(zoneinfo.ZoneInfo(zone))
    except Exception as fout:
        return {"naam": "tijdzone", "bron": "tijdzonedatabase", "tekst": "",
                "fout": f"{type(fout).__name__}: {fout}"}
    hier = datetime.datetime.now()
    uur = daar.utcoffset().total_seconds() / 3600 if daar.utcoffset() else 0
    verschil = uur - (hier.utcoffset().total_seconds() / 3600 if hier.utcoffset() else 0)
    teken = "+" if verschil >= 0 else ""
    return {
        "naam": "tijdzone",
        "bron": "tijdzonedatabase (IANA)",
        "tekst": (
            f"In {plaats.title()} is het nu {DAGEN[daar.weekday()]} {daar.strftime('%d-%m-%Y')} "
            f"{daar.strftime('%H:%M')} uur ({zone}, {teken}{verschil:g} uur verschil met hier)."
        ),
    }


# ---- B. bronnen zonder sleutel --------------------------------------------


def gereedschap_postcode(zoekterm: str) -> dict:
    """Opzoeken in de officiële basisregistratie: postcode, adres, weg of gemeente (PDOK)."""
    zoekterm = (zoekterm or "").strip()
    if not zoekterm:
        return {"naam": "postcode", "bron": "api.pdok.nl", "tekst": "", "fout": "niets opgegeven"}
    gegevens = _haal_json(
        "https://api.pdok.nl/bzk/locatieserver/search/v3_1/free?"
        f"q={urllib.parse.quote(zoekterm)}&rows=4&fl=*"
    )
    documenten = ((gegevens or {}).get("response") or {}).get("docs") or []
    if not documenten:
        return {"naam": "postcode", "bron": "api.pdok.nl", "tekst": "",
                "fout": f"niets gevonden voor '{zoekterm}'"}
    regels = []
    for document in documenten:
        delen = [str(document.get("weergavenaam") or "")]
        if document.get("gemeentenaam"):
            delen.append(f"gemeente {document['gemeentenaam']}")
        if document.get("provincienaam"):
            delen.append(str(document["provincienaam"]))
        if document.get("postcode"):
            delen.append(f"postcode {document['postcode']}")
        regels.append(f"{document.get('type', '?')}: " + ", ".join(delen))
    return {
        "naam": "postcode",
        "bron": "api.pdok.nl (BAG, Kadaster)",
        "tekst": " | ".join(regels),
    }


def gereedschap_buien(plaats: str) -> dict:
    """Valt er de komende twee uur neerslag? (Buienradar, met bronvermelding)"""
    plaats = (plaats or "").strip() or GEREEDSCHAP_PLAATS
    if not plaats:
        return {"naam": "buien", "bron": "buienradar.nl", "tekst": "", "fout": "geen plaats opgegeven"}
    try:
        breedte, lengte, nette = _coordinaten(plaats)
    except Exception as fout:
        return {"naam": "buien", "bron": "buienradar.nl", "tekst": "", "fout": str(fout)}
    ruw = _haal(
        "https://gpsgadget.buienradar.nl/data/raintext/?"
        f"lat={breedte:.2f}&lon={lengte:.2f}"
    ).decode("utf-8", "replace")
    metingen = []
    for regel in ruw.splitlines():
        if "|" not in regel:
            continue
        waarde, tijd = regel.split("|", 1)
        try:
            metingen.append((int(waarde.strip()), tijd.strip()))
        except ValueError:
            continue
    if not metingen:
        return {"naam": "buien", "bron": "buienradar.nl", "tekst": "",
                "fout": "geen neerslagverwachting ontvangen"}
    nat = [(waarde, tijd) for waarde, tijd in metingen if waarde > 0]
    if not nat:
        return {
            "naam": "buien",
            "bron": "buienradar.nl (bronvermelding verplicht)",
            "tekst": (
                f"Neerslag bij {nette} (buienradar): de komende twee uur blijft het droog "
                f"(verwachting t/m {metingen[-1][1]})."
            ),
        }
    sterkste = max(nat, key=lambda paar: paar[0])
    mm_per_uur = 10 ** ((sterkste[0] - 109) / 32)
    return {
        "naam": "buien",
        "bron": "buienradar.nl (bronvermelding verplicht)",
        "tekst": (
            f"Neerslag bij {nette} (buienradar): het gaat regenen vanaf {nat[0][1]}, "
            f"het sterkst rond {sterkste[1]} (ongeveer {_getal(mm_per_uur)} mm per uur). "
            f"Verwachting loopt t/m {metingen[-1][1]}."
        ),
    }


def gereedschap_lucht(plaats: str) -> dict:
    """Luchtkwaliteit: fijnstof en de Europese luchtkwaliteitsindex (Open-Meteo)."""
    plaats = (plaats or "").strip() or GEREEDSCHAP_PLAATS
    if not plaats:
        return {"naam": "lucht", "bron": "open-meteo.com", "tekst": "", "fout": "geen plaats opgegeven"}
    try:
        breedte, lengte, nette = _coordinaten(plaats)
    except Exception as fout:
        return {"naam": "lucht", "bron": "open-meteo.com", "tekst": "", "fout": str(fout)}
    gegevens = _haal_json(
        "https://air-quality-api.open-meteo.com/v1/air-quality?"
        f"latitude={breedte}&longitude={lengte}&current=pm10,pm2_5,european_aqi&timezone=Europe/Amsterdam"
    )
    nu = (gegevens or {}).get("current") or {}
    if not nu:
        return {"naam": "lucht", "bron": "open-meteo.com", "tekst": "", "fout": "geen gegevens"}
    index = nu.get("european_aqi")
    oordeel = ""
    if isinstance(index, (int, float)):
        oordeel = (
            " goed" if index <= 20 else " redelijk" if index <= 40 else
            " matig" if index <= 60 else " slecht" if index <= 80 else " zeer slecht"
        )
    return {
        "naam": "lucht",
        "bron": "open-meteo.com (CAMS)",
        "tekst": (
            f"Luchtkwaliteit bij {nette}: fijnstof PM2,5 {_getal(nu.get('pm2_5'), ' µg/m³')}, "
            f"PM10 {_getal(nu.get('pm10'), ' µg/m³')}, Europese index {index}{oordeel}."
        ),
    }


def gereedschap_zon(plaats: str) -> dict:
    """Hoe laat gaat de zon op en onder, en hoe lang is de dag (Open-Meteo)."""
    plaats = (plaats or "").strip() or GEREEDSCHAP_PLAATS
    if not plaats:
        return {"naam": "zon", "bron": "open-meteo.com", "tekst": "", "fout": "geen plaats opgegeven"}
    try:
        breedte, lengte, nette = _coordinaten(plaats)
    except Exception as fout:
        return {"naam": "zon", "bron": "open-meteo.com", "tekst": "", "fout": str(fout)}
    gegevens = _haal_json(
        "https://api.open-meteo.com/v1/forecast?"
        f"latitude={breedte}&longitude={lengte}&daily=sunrise,sunset,daylight_duration"
        "&forecast_days=2&timezone=Europe/Amsterdam"
    )
    dagen = (gegevens or {}).get("daily") or {}
    tijden = dagen.get("time") or []
    if not tijden:
        return {"naam": "zon", "bron": "open-meteo.com", "tekst": "", "fout": "geen gegevens"}
    regels = []
    for nummer, dag in enumerate(tijden[:2]):
        op = str((dagen.get("sunrise") or [""])[nummer])[-5:]
        onder = str((dagen.get("sunset") or [""])[nummer])[-5:]
        duur = (dagen.get("daylight_duration") or [0])[nummer]
        regels.append(f"{dag}: op {op}, onder {onder} ({_getal((duur or 0) / 3600, ' uur licht')})")
    return {"naam": "zon", "bron": "open-meteo.com", "tekst": f"Zon bij {nette} — " + " | ".join(regels)}


MUNTEN = {
    "euro": "EUR", "eur": "EUR", "€": "EUR", "dollar": "USD", "usd": "USD", "$": "USD",
    "pond": "GBP", "gbp": "GBP", "frank": "CHF", "chf": "CHF", "yen": "JPY", "jpy": "JPY",
    "kroon": "SEK", "sek": "SEK", "zloty": "PLN", "pln": "PLN", "canadese dollar": "CAD",
    "aud": "AUD", "nok": "NOK", "dkk": "DKK",
}


def gereedschap_valuta(vraag: str) -> dict:
    """Valuta omrekenen met de dagelijkse koersen van de Europese Centrale Bank."""
    laag = (vraag or "").lower()
    bedrag = 1.0
    treffer = re.search(r"(\d+[.,]?\d*)", laag)
    if treffer:
        bedrag = float(treffer.group(1).replace(",", "."))
    gevonden = [code for woord, code in MUNTEN.items() if woord in laag]
    van, naar = "EUR", "USD"
    if len(gevonden) >= 2:
        van, naar = gevonden[0], gevonden[1]
    elif len(gevonden) == 1:
        van, naar = ("EUR", gevonden[0]) if "euro" in laag or "eur" in laag else (gevonden[0], "EUR")
    if van == naar:
        naar = "USD" if van == "EUR" else "EUR"
    gegevens = _haal_json(f"https://api.frankfurter.app/latest?from={van}&to={naar}")
    koersen = (gegevens or {}).get("rates") or {}
    if naar not in koersen:
        return {"naam": "valuta", "bron": "frankfurter.app (ECB)", "tekst": "",
                "fout": f"geen koers {van} naar {naar}"}
    koers = float(koersen[naar])
    return {
        "naam": "valuta",
        "bron": f"frankfurter.app, koersen van de ECB ({(gegevens or {}).get('date', '?')})",
        "tekst": (
            f"{_getal(bedrag)} {van} = {_getal(bedrag * koers)} {naar} "
            f"(1 {van} = {_getal(koers)} {naar})"
        ),
    }


CRYPTO = {
    "bitcoin": "bitcoin", "btc": "bitcoin", "ethereum": "ethereum", "eth": "ethereum",
    "solana": "solana", "sol": "solana", "cardano": "cardano", "ada": "cardano",
    "dogecoin": "dogecoin", "doge": "dogecoin", "ripple": "ripple", "xrp": "ripple",
    "litecoin": "litecoin", "polkadot": "polkadot", "bnb": "binancecoin",
}


def gereedschap_crypto(munt: str) -> dict:
    """De koers van een cryptomunt in euro (CoinGecko)."""
    laag = (munt or "").lower()
    gevraagd = []
    for woord, sleutel in CRYPTO.items():
        if re.search(rf"\b{re.escape(woord)}\b", laag) and sleutel not in gevraagd:
            gevraagd.append(sleutel)
    gevraagd = (gevraagd or ["bitcoin", "ethereum"])[:3]
    gegevens = _haal_json(
        "https://api.coingecko.com/api/v3/simple/price?ids="
        + ",".join(gevraagd) + "&vs_currencies=eur&include_24hr_change=true"
    )
    if not isinstance(gegevens, dict) or not gegevens:
        return {"naam": "crypto", "bron": "coingecko.com", "tekst": "", "fout": "geen koersen"}
    regels = []
    for sleutel in gevraagd:
        prijs = (gegevens.get(sleutel) or {}).get("eur")
        verandering = (gegevens.get(sleutel) or {}).get("eur_24h_change")
        if prijs is None:
            continue
        regel = f"{sleutel}: € " + f"{prijs:,.2f}".replace(",", "_").replace(".", ",").replace("_", ".")
        if isinstance(verandering, (int, float)):
            regel += f" ({verandering:+.1f}% in 24 uur)".replace(".", ",")
        regels.append(regel)
    return {"naam": "crypto", "bron": "coingecko.com", "tekst": " | ".join(regels)}


def _zonder_wikikoppen(tekst: str) -> str:
    """Wiki-opmaak eruit: koppen, lijntjes en overtollige tekens."""
    schoon = re.sub(r"\{\{[^}]*\}\}", " ", tekst or "")
    schoon = re.sub(r"\[\[[^\]|]*\|([^\]]*)\]\]", r"\1", schoon)
    schoon = re.sub(r"\[\[([^\]]*)\]\]", r"\1", schoon)
    schoon = re.sub(r"'{2,}", "", schoon)
    schoon = re.sub(r"=+\s*([^=\n]+?)\s*=+", r"\1:", schoon)
    schoon = re.sub(r"^[\s:=#*]+", "", schoon, flags=re.M)
    return _schoon(schoon)


def gereedschap_woordenboek(woord: str) -> dict:
    """Wat betekent een woord? Uit het WikiWoordenboek (nl.wiktionary)."""
    woord = (woord or "").strip()
    if not woord:
        return {"naam": "woordenboek", "bron": "nl.wiktionary.org", "tekst": "", "fout": "geen woord"}
    gegevens = _haal_json(
        "https://nl.wiktionary.org/w/api.php?action=query&format=json&prop=extracts"
        f"&explaintext=1&exintro=1&titles={urllib.parse.quote(woord)}&redirects=1"
    )
    pagina = ((gegevens or {}).get("query") or {}).get("pages") or {}
    for inhoud in pagina.values():
        tekst = (inhoud or {}).get("extract") or ""
        if tekst and tekst.strip():
            return {
                "naam": "woordenboek",
                "bron": f"nl.wiktionary.org/wiki/{woord}",
                "tekst": f"{woord}: {_kort(_schoon(tekst), 600)}",
            }
    # geen samenvatting: eerst zoeken welk artikel het is, dan de tekst ophalen
    gezocht = _haal_json(
        "https://nl.wiktionary.org/w/api.php?action=query&format=json&list=search"
        f"&srlimit=1&srsearch={urllib.parse.quote(woord)}"
    )
    treffers = ((gezocht or {}).get("query") or {}).get("search") or []
    if treffers:
        titel = str(treffers[0].get("title") or woord)
        tweede = _haal_json(
            "https://nl.wiktionary.org/w/api.php?action=query&format=json&prop=extracts"
            f"&explaintext=1&titles={urllib.parse.quote(titel)}&redirects=1"
        )
        for inhoud in ((tweede or {}).get("query") or {}).get("pages", {}).values():
            tekst = (inhoud or {}).get("extract") or ""
            if tekst.strip():
                return {
                    "naam": "woordenboek",
                    "bron": f"nl.wiktionary.org/wiki/{titel}",
                    "tekst": f"{titel}: {_kort(_zonder_wikikoppen(tekst), 700)}",
                }
    return {"naam": "woordenboek", "bron": "nl.wiktionary.org", "tekst": "",
            "fout": f"'{woord}' staat niet in het WikiWoordenboek"}


def gereedschap_vertaal(tekst: str) -> dict:
    """Een korte tekst vertalen (MyMemory, gratis zonder sleutel)."""
    tekst = (tekst or "").strip()
    if not tekst:
        return {"naam": "vertalen", "bron": "mymemory.translated.net", "tekst": "",
                "fout": "geen tekst opgegeven"}
    laag = tekst.lower()
    naar, van = "en", "nl"
    if "frans" in laag:
        naar = "fr"
    elif "duits" in laag:
        naar = "de"
    elif "spaans" in laag:
        naar = "es"
    elif "nederlands" in laag:
        van, naar = "en", "nl"
    elif re.search(r"\b(engels|english|naar het engels)\b", laag):
        naar, van = "en", "nl"
    schoon = re.sub(
        r"\b(vertaal|vertalen|in het|naar het|engels|nederlands|duits|frans|spaans|english|dutch)\b",
        " ",
        tekst,
        flags=re.I,
    ).strip(" ?!.:")
    if not schoon:
        return {"naam": "vertalen", "bron": "mymemory.translated.net", "tekst": "",
                "fout": "geen tekst om te vertalen"}
    gegevens = _haal_json(
        "https://api.mymemory.translated.net/get?q="
        f"{urllib.parse.quote(schoon)}&langpair={van}|{naar}"
    )
    uitkomst = ((gegevens or {}).get("responseData") or {}).get("translatedText") or ""
    if not uitkomst:
        return {"naam": "vertalen", "bron": "mymemory.translated.net", "tekst": "",
                "fout": "geen vertaling ontvangen"}
    return {
        "naam": "vertalen",
        "bron": "mymemory.translated.net",
        "tekst": f"'{schoon}' ({van} → {naar}): {uitkomst}",
    }


def gereedschap_domein(domein: str) -> dict:
    """Van wie is een domeinnaam, en sinds wanneer (RDAP, het officiële register)."""
    domein = (domein or "").strip().lower().removeprefix("http://").removeprefix("https://")
    domein = domein.split("/")[0].removeprefix("www.")
    if not domein or "." not in domein:
        return {"naam": "domein", "bron": "rdap.org", "tekst": "", "fout": "geen geldige domeinnaam"}
    gegevens = _haal_json(f"https://rdap.org/domain/{urllib.parse.quote(domein)}")
    if not isinstance(gegevens, dict) or not gegevens.get("ldhName"):
        return {"naam": "domein", "bron": "rdap.org", "tekst": "",
                "fout": f"geen gegevens over {domein}"}
    registrar = ""
    for entiteit in gegevens.get("entities") or []:
        if "registrar" in (entiteit.get("roles") or []):
            for naam in entiteit.get("vcardArray", [[], []])[1] or []:
                if isinstance(naam, list) and naam and naam[0] == "fn":
                    registrar = str(naam[3])
                    break
    datums = {}
    for gebeurtenis in gegevens.get("events") or []:
        if gebeurtenis.get("eventAction") and gebeurtenis.get("eventDate"):
            datums[str(gebeurtenis["eventAction"])] = str(gebeurtenis["eventDate"])[:10]
    return {
        "naam": "domein",
        "bron": "rdap.org",
        "tekst": (
            f"{gegevens.get('ldhName')}: "
            + (f"registrar {registrar}, " if registrar else "")
            + (f"aangemaakt {datums['registration']}, " if datums.get("registration") else "")
            + (f"verloopt {datums['expiration']}" if datums.get("expiration") else "geen vervaldatum bekend")
            + (f", laatst gewijzigd {datums['last changed']}" if datums.get("last changed") else "")
        ).rstrip(", "),
    }


NIEUWSBRONNEN = {
    "nos": ("NOS Nieuws", "https://feeds.nos.nl/nosnieuwsalgemeen"),
    "algemeen": ("NOS Nieuws", "https://feeds.nos.nl/nosnieuwsalgemeen"),
    "techniek": ("Tweakers", "https://tweakers.net/feeds/nieuws.xml"),
    "tweakers": ("Tweakers", "https://tweakers.net/feeds/nieuws.xml"),
    "nu": ("NU.nl", "https://www.nu.nl/rss/Algemeen"),
    "rtl": ("RTL Nieuws", "https://www.rtlnieuws.nl/rss.xml"),
}


def _koppen_uit_feed(ruw: str, aantal: int = 6) -> list[str]:
    """Koppen uit een RSS-feed halen zonder extra bibliotheek."""
    koppen = []
    for blok in re.findall(r"<item[ >](.*?)</item>", ruw, re.S)[:aantal]:
        titel = re.search(r"<title[^>]*>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", blok, re.S)
        if titel:
            schoon = _schoon(titel.group(1))
            if schoon:
                koppen.append(schoon)
    return koppen


def gereedschap_nieuws_breed(bron: str) -> dict:
    """Koppen van NOS, Tweakers, NU.nl of RTL Nieuws."""
    laag = (bron or "").lower()
    gekozen = "nos"
    for sleutel in NIEUWSBRONNEN:
        if sleutel in laag:
            gekozen = sleutel
            break
    naam, adres = NIEUWSBRONNEN[gekozen]
    ruw = _haal(adres).decode("utf-8", "replace")
    koppen = _koppen_uit_feed(ruw)
    if not koppen:
        return {"naam": "nieuws", "bron": adres, "tekst": "", "fout": f"geen koppen bij {naam}"}
    return {
        "naam": "nieuws",
        "bron": adres,
        "tekst": f"Laatste koppen van {naam}: " + " | ".join(koppen),
    }


# ---- F. verkeer (NDW / Rijkswaterstaat) ------------------------------------

NDW_ACTUEEL = "https://opendata.ndw.nu/actueel_beeld.xml.gz"
NDW_CACHE: dict = {"tijd": 0.0, "ruw": ""}
WEG_CACHE: dict = {}
VERKEER_SOORTEN = {
    "abnormalTraffic": "file",
    "accident": "ongeluk",
    "vehicleObstruction": "voorwerp op de weg",
    "roadOrCarriagewayOrLaneManagement": "afsluiting of rijstrook dicht",
    "speedManagement": "snelheidsbeperking",
    "poorEnvironmentConditions": "weersomstandigheid",
}


def _ndw_ruw() -> str:
    """Het actuele verkeersbeeld van NDW (Rijkswaterstaat), vijf minuten bewaard."""
    nu = time.time()
    if NDW_CACHE["ruw"] and nu - NDW_CACHE["tijd"] < 300:
        return str(NDW_CACHE["ruw"])
    ruw = gzip.decompress(_haal(NDW_ACTUEEL)).decode("utf-8", "replace")
    NDW_CACHE["tijd"] = nu
    NDW_CACHE["ruw"] = ruw
    return ruw


def _weg_en_plaats(breedte: float, lengte: float) -> str:
    """Bij welke weg en plaats hoort dit punt? (PDOK, de officiële kaartgegevens)"""
    sleutel = (round(breedte, 3), round(lengte, 3))
    if sleutel in WEG_CACHE:
        return WEG_CACHE[sleutel]
    antwoord = ""
    try:
        gegevens = _haal_json(
            "https://api.pdok.nl/bzk/locatieserver/search/v3_1/reverse?"
            f"lat={breedte}&lon={lengte}&rows=1&type=weg"
        )
        documenten = ((gegevens or {}).get("response") or {}).get("docs") or []
        if documenten:
            antwoord = str(documenten[0].get("weergavenaam") or "")
    except Exception:
        antwoord = ""
    WEG_CACHE[sleutel] = antwoord
    return antwoord


def _verkeersmeldingen(weg: str = "", aantal: int = 8) -> list[dict]:
    """De actuele meldingen uit het NDW-bestand, nieuwste eerst."""
    ruw = _ndw_ruw()
    gezocht = re.sub(r"[^A-Za-z0-9]", "", (weg or "").upper())
    meldingen = []
    nu = datetime.datetime.now(datetime.timezone.utc)
    for blok in re.findall(r"<sit:situationRecord\b.*?</sit:situationRecord>", ruw, re.S):
        soort = ""
        kop = blok[:400].lower()
        for naam, leesbaar in VERKEER_SOORTEN.items():
            if f"sit:{naam}".lower() in kop:
                soort = leesbaar
                break
        if not soort:
            continue
        if soort == "afsluiting of rijstrook dicht" and not gezocht:
            continue  # te veel meldingen; alleen als er op een weg gezocht wordt
        begin = re.search(r"<com:overallStartTime>([^<]+)", blok)
        moment = None
        if begin:
            try:
                moment = datetime.datetime.fromisoformat(begin.group(1).replace("Z", "+00:00"))
            except ValueError:
                moment = None
        if moment and (nu - moment).total_seconds() > 6 * 3600:
            continue
        punt = re.search(r"<loc:latitude>([\d.\-]+)</loc:latitude>.*?<loc:longitude>([\d.\-]+)</loc:longitude>", blok, re.S)
        if not punt:
            continue
        detail = ""
        if "stationaryTraffic" in blok:
            detail = "stilstaand verkeer"
        elif "slowTraffic" in blok:
            detail = "langzaam rijdend verkeer"
        elif "queuingTraffic" in blok:
            detail = "langzaam rijdend tot stilstaand verkeer"
        meldingen.append({
            "soort": soort,
            "detail": detail,
            "breedte": float(punt.group(1)),
            "lengte": float(punt.group(2)),
            "sinds": moment.strftime("%H:%M") if moment else "?",
        })
    # eerst de files en ongelukken, dan de rest; nieuwste eerst binnen de soort
    rang = {"file": 0, "ongeluk": 1, "voorwerp op de weg": 2}
    meldingen.sort(key=lambda m: (rang.get(m["soort"], 3), m["sinds"]))
    if gezocht:
        gekozen = []
        for melding in meldingen:
            weg_plaats = _weg_en_plaats(melding["breedte"], melding["lengte"])
            hoofdletters = weg_plaats.upper()
            if not re.search(rf"(?<![A-Z0-9]){re.escape(gezocht)}(?![0-9])", hoofdletters):
                continue
            melding["waar"] = weg_plaats
            gekozen.append(melding)
            if len(gekozen) >= aantal:
                break
        return gekozen
    for melding in meldingen[:aantal]:
        melding["waar"] = _weg_en_plaats(melding["breedte"], melding["lengte"])
    return meldingen[:aantal]


def gereedschap_verkeer(weg: str) -> dict:
    """Files, ongelukken en afsluitingen van Rijkswaterstaat (via NDW)."""
    try:
        meldingen = _verkeersmeldingen(weg or "")
    except Exception as fout:
        return {"naam": "verkeer", "bron": "opendata.ndw.nu", "tekst": "",
                "fout": f"{type(fout).__name__}: {fout}"}
    if not meldingen:
        return {
            "naam": "verkeer",
            "bron": "opendata.ndw.nu (NDW / Rijkswaterstaat)",
            "tekst": (
                f"Geen actuele meldingen{' op ' + weg if weg else ''} "
                f"(gekeken op {datetime.datetime.now().strftime('%H:%M')})."
            ),
        }
    regels = []
    for melding in meldingen:
        stuk = melding["soort"] + (f" ({melding['detail']})" if melding["detail"] else "")
        waar = melding.get("waar") or "onbekende plek"
        regels.append(f"{stuk} bij {waar} sinds {melding['sinds']}")
    kop = f"Actueel verkeer{' op ' + weg.upper() if weg else ''} ({len(regels)} meldingen)"
    return {
        "naam": "verkeer",
        "bron": "opendata.ndw.nu (NDW / Rijkswaterstaat) en api.pdok.nl",
        "tekst": kop + ": " + " | ".join(regels),
    }


# ---- C. je eigen apps ------------------------------------------------------

VOORRAAD_MAP = "/home/hermes/voorraadthuis/recipe-data"
AKKOORD_BESTAND = "/home/hermes/akkoordkast/song-data/liedjes.json"
ZITTESJ_BESTAND = "/home/hermes/zittesj-app/app/src/main/assets/www/app-core.js"
NOTITIES_MAP = os.environ.get("WLLM_NOTITIES", "").strip()
_RECEPTEN_CACHE: dict = {"tijd": 0.0, "lijst": []}


def _recepten() -> list[dict]:
    """Alle recepten van VoorraadThuis inlezen (een uur bewaard)."""
    nu = time.time()
    if _RECEPTEN_CACHE["lijst"] and nu - _RECEPTEN_CACHE["tijd"] < 3600:
        return list(_RECEPTEN_CACHE["lijst"])
    alles: list[dict] = []
    map_ = Path(VOORRAAD_MAP)
    if map_.is_dir():
        for bestand in sorted(map_.glob("*.json")):
            if bestand.name.startswith(("translations", "source-lock")):
                continue
            try:
                inhoud = json.loads(bestand.read_text(encoding="utf-8", errors="replace"))
            except Exception:
                continue
            recepten = inhoud.get("recipes") if isinstance(inhoud, dict) else inhoud
            if isinstance(recepten, list):
                for recept in recepten:
                    if isinstance(recept, dict) and recept.get("title"):
                        alles.append(recept)
    _RECEPTEN_CACHE["tijd"] = nu
    _RECEPTEN_CACHE["lijst"] = alles
    return alles


def gereedschap_recepten(zoektermen: str) -> dict:
    """Zoeken in de recepten van VoorraadThuis (eigen verzameling, met bron)."""
    zoektermen = (zoektermen or "").strip()
    if not zoektermen:
        return {"naam": "recepten", "bron": "VoorraadThuis", "tekst": "", "fout": "geen zoekterm"}
    woorden = [w for w in re.split(r"[,\s]+", zoektermen.lower()) if len(w) > 2]
    if not woorden:
        return {"naam": "recepten", "bron": "VoorraadThuis", "tekst": "", "fout": "geen bruikbare zoekterm"}
    treffers = []
    for recept in _recepten():
        titel = str(recept.get("title") or "").lower()
        ingredienten = " ".join(str(i) for i in (recept.get("ingredients") or [])).lower()
        score = sum(1 for woord in woorden if woord in titel) * 3
        score += sum(1 for woord in woorden if woord in ingredienten)
        if score <= 0:
            continue
        if str(recept.get("language") or "nl") != "nl":
            score -= 1
        treffers.append((score, recept))
    treffers.sort(key=lambda paar: -paar[0])
    if not treffers:
        return {
            "naam": "recepten",
            "bron": "VoorraadThuis",
            "tekst": f"Geen recept gevonden met '{zoektermen}' in de verzameling van VoorraadThuis.",
        }
    regels = []
    for score, recept in treffers[:4]:
        ingredienten = [str(i) for i in (recept.get("ingredients") or [])][:6]
        regels.append(
            f"{recept.get('title')} ({recept.get('servings') or '?'} personen, "
            f"{recept.get('language') or 'nl'}): " + "; ".join(ingredienten)
        )
    return {
        "naam": "recepten",
        "bron": f"VoorraadThuis ({len(_recepten())} recepten, bron per recept: wikibooks/unitools)",
        "tekst": f"Recepten met '{zoektermen}': " + " || ".join(regels),
    }


def gereedschap_akkoorden(titel: str) -> dict:
    """Akkoorden en speelwijze van een liedje uit AkkoordKast."""
    titel = (titel or "").strip()
    if not titel:
        return {"naam": "akkoorden", "bron": "AkkoordKast", "tekst": "", "fout": "geen titel"}
    bestand = Path(AKKOORD_BESTAND)
    if not bestand.is_file():
        return {"naam": "akkoorden", "bron": "AkkoordKast", "tekst": "",
                "fout": "de liedjeslijst van AkkoordKast is niet gevonden"}
    try:
        liedjes = json.loads(bestand.read_text(encoding="utf-8", errors="replace"))
    except Exception as fout:
        return {"naam": "akkoorden", "bron": "AkkoordKast", "tekst": "",
                "fout": f"{type(fout).__name__}: {fout}"}
    if isinstance(liedjes, dict):
        liedjes = list(liedjes.values())
    laag = titel.lower()
    treffer = None
    for liedje in liedjes:
        if not isinstance(liedje, dict):
            continue
        if laag in str(liedje.get("titel") or "").lower() or laag in str(liedje.get("artiest") or "").lower():
            treffer = liedje
            break
    if not treffer:
        titels = [str(l.get("titel")) for l in liedjes if isinstance(l, dict)][:12]
        return {
            "naam": "akkoorden",
            "bron": "AkkoordKast",
            "tekst": f"'{titel}' staat niet in AkkoordKast. Wel: " + ", ".join(titels),
        }
    chordpro = str(treffer.get("chordpro") or "")
    akkoorden = sorted(set(re.findall(r"\[([A-G][#b]?m?(?:aj|aj7|7|sus|dim|maj|add)?[0-9a-z/]*)\]", chordpro)))[:12]
    regels = []
    for regel in chordpro.splitlines():
        kaal = _schoon(re.sub(r"\{[^}]*\}", " ", regel))
        if kaal:
            regels.append(kaal)
    return {
        "naam": "akkoorden",
        "bron": "AkkoordKast (eigen songbook)",
        "tekst": (
            f"{treffer.get('titel')} — {treffer.get('artiest') or 'onbekend'}. "
            f"Toonsoort {treffer.get('toonsoort') or '?'}, tempo {treffer.get('tempo') or '?'}, "
            f"aanslag {treffer.get('strum') or '?'}, moeilijkheid {treffer.get('moeilijkheid') or '?'}/5. "
            f"Akkoorden: {', '.join(akkoorden) or '?'}. "
            f"Tekst: " + " / ".join(regels[:8])
        ),
    }


def gereedschap_zittesj(woord: str) -> dict:
    """Een woord opzoeken in de Zittesj-woordenlijst."""
    woord = (woord or "").strip().lower()
    if not woord:
        return {"naam": "zittesj", "bron": "Zittesj-app", "tekst": "", "fout": "geen woord"}
    bestand = Path(ZITTESJ_BESTAND)
    if not bestand.is_file():
        return {"naam": "zittesj", "bron": "Zittesj-app", "tekst": "",
                "fout": "de woordenlijst van Zittesj is niet gevonden"}
    ruw = bestand.read_text(encoding="utf-8", errors="replace")
    treffers = []
    for blok in re.findall(r"\{[^{}]*\}", ruw):
        if "sittards" not in blok and "dutch" not in blok:
            continue
        velden = dict(re.findall(r"(\w+)\s*:\s*'((?:[^'\\]|\\.)*)'", blok))
        if not velden:
            continue
        if woord in velden.get("sittards", "").lower() or woord in velden.get("dutch", "").lower():
            treffers.append(velden)
    if not treffers:
        return {
            "naam": "zittesj",
            "bron": "Zittesj-app (woordenlijst)",
            "tekst": f"'{woord}' staat niet in de Zittesj-woordenlijst.",
        }
    regels = [
        f"{v.get('sittards')} = {v.get('dutch')}" + (f" — {v.get('note')}" if v.get("note") else "")
        for v in treffers[:3]
    ]
    return {
        "naam": "zittesj",
        "bron": "Zittesj-app (eigen woordenlijst, bron per woord vermeld)",
        "tekst": " | ".join(regels),
    }


def gereedschap_notities(zoekterm: str) -> dict:
    """Zoeken in je eigen notitiemap (zet WLLM_NOTITIES op het pad van je map)."""
    if not NOTITIES_MAP:
        return {
            "naam": "notities",
            "bron": "eigen notitiemap",
            "tekst": "",
            "fout": "geen notitiemap ingesteld (zet WLLM_NOTITIES op het pad van je map)",
        }
    zoekterm = (zoekterm or "").strip().lower()
    if not zoekterm:
        return {"naam": "notities", "bron": "eigen notitiemap", "tekst": "", "fout": "geen zoekterm"}
    treffers = []
    for stuk in NOTITIES_MAP.split(":"):
        map_ = Path(stuk.strip()).expanduser()
        if not map_.is_dir():
            continue
        for bestand in list(map_.rglob("*.md")) + list(map_.rglob("*.txt")):
            try:
                inhoud = bestand.read_text(encoding="utf-8", errors="replace")
            except Exception:
                continue
            if zoekterm in inhoud.lower() or zoekterm in bestand.name.lower():
                plek = inhoud.lower().find(zoekterm)
                stuk_tekst = _schoon(inhoud[max(0, plek - 120): plek + 220]) if plek >= 0 else ""
                treffers.append(f"{bestand.name}: {stuk_tekst}")
            if len(treffers) >= 5:
                break
        if len(treffers) >= 5:
            break
    if not treffers:
        return {"naam": "notities", "bron": "eigen notitiemap",
                "tekst": f"Niets gevonden over '{zoekterm}' in je notities."}
    return {"naam": "notities", "bron": "eigen notitiemap", "tekst": " || ".join(treffers)}


def gereedschap_status() -> dict:
    """Hoe staat deze machine ervoor: geheugen, belasting, schijf en temperatuur."""
    delen = []
    try:
        with open("/proc/meminfo", encoding="utf-8") as bestand:
            meminfo = {}
            for regel in bestand:
                sleutel, _, waarde = regel.partition(":")
                meminfo[sleutel.strip()] = waarde.strip()
        totaal = float(meminfo.get("MemTotal", "0").split()[0]) / 1048576
        vrij = float(meminfo.get("MemAvailable", "0").split()[0]) / 1048576
        delen.append(f"geheugen {_getal(vrij)} GB vrij van {_getal(totaal)} GB")
    except Exception:
        pass
    try:
        with open("/proc/loadavg", encoding="utf-8") as bestand:
            last = bestand.read().split()[:3]
        kernen = os.cpu_count() or 1
        delen.append(f"belasting {last[0]} ({kernen} kernen)")
    except Exception:
        pass
    try:
        with open("/proc/uptime", encoding="utf-8") as bestand:
            seconden = float(bestand.read().split()[0])
        dagen, rest = divmod(int(seconden), 86400)
        uren = rest // 3600
        delen.append(f"aan sinds {dagen} dagen en {uren} uur")
    except Exception:
        pass
    try:
        schijf = shutil.disk_usage("/")
        delen.append(f"schijf {_getal(schijf.free / 1073741824)} GB vrij van {_getal(schijf.total / 1073741824)} GB")
    except Exception:
        pass
    try:
        metingen = []
        for pad in Path("/sys/class/thermal").glob("thermal_zone*/temp"):
            try:
                metingen.append(int(pad.read_text().strip()) / 1000)
            except Exception:
                continue
        if metingen:
            delen.append(f"temperatuur {_getal(max(metingen))} °C")
    except Exception:
        pass
    return {
        "naam": "status",
        "bron": "deze machine (/proc)",
        "tekst": "Status van deze machine: " + ", ".join(delen) + ".",
    }


def gereedschap_eenheden_tekst(tekst: str) -> dict:
    """'10 km in mijl' of 'hoeveel pond is 2 kilo' uit gewone taal."""
    laag = (tekst or "").strip().lower()
    getal = re.search(r"(-?\d+[.,]?\d*)", laag)
    if not getal:
        return {"naam": "eenheden", "bron": "de engine zelf", "tekst": "", "fout": "geen getal gevonden"}
    waarde = float(getal.group(1).replace(",", "."))
    woorden = re.findall(r"[a-zA-Z/]+", laag)
    temperatuur = [w for w in woorden if w in ("celsius", "fahrenheit", "kelvin")]
    if len(temperatuur) >= 2:
        return gereedschap_eenheden(waarde, temperatuur[0], temperatuur[1])
    # de eenheid die direct achter het getal staat hoort bij het getal
    van = ""
    for woord in re.findall(r"[a-zA-Z/]+", laag[getal.end():]):
        if woord in EENHEDEN:
            van = woord
            break
    if not van:
        van = next((w for w in woorden if w in EENHEDEN), "")
    naar = next((w for w in woorden if w in EENHEDEN and w != van), "")
    if len(temperatuur) == 1:
        van, naar = (temperatuur[0], naar) if not van else (van, temperatuur[0])
    if not van or not naar:
        return {"naam": "eenheden", "bron": "de engine zelf", "tekst": "",
                "fout": "noem twee eenheden, bijvoorbeeld '10 km in mijl'"}
    return gereedschap_eenheden(waarde, van, naar)


def gereedschap_feestdagen_tekst(tekst: str) -> dict:
    """Feestdagen van dit jaar, of van het jaar dat je noemt."""
    jaar = re.search(r"(20\d{2})", tekst or "")
    return gereedschap_feestdagen(int(jaar.group(1)) if jaar else None)


# ---- D. water, energie, afstand, wetten, CBS, boeken, vliegverkeer en documenten

WATER_WFS = "https://geo.rijkswaterstaat.nl/services/ogc/hws/DDAPI20/ows"
WATER_WWS = "https://ddapi20-waterwebservices.rijkswaterstaat.nl/ONLINEWAARNEMINGENSERVICES/OphalenWaarnemingen"
ENERGIE_API = "https://api.energyzero.nl/v1/energyprices"
VOORRAAD_DB = os.environ.get("WLLM_VOORRAAD_DB", "/home/hermes/voorraadthuis/data/voorraad.db")
DOCUMENTEN_MAP = os.environ.get("WLLM_DOCUMENTEN", "").strip()
SRU_WETTEN = "https://zoekservice.overheid.nl/sru/Search"


def _waterlocaties(plaats: str) -> list[tuple[str, str]]:
    """De meetlocaties van Rijkswaterstaat die bij deze naam passen, beste eerst."""
    filter_ = f"LOCATIE_NAAM ILIKE '%{plaats}%'"
    gegevens = _haal_json(
        f"{WATER_WFS}?SERVICE=WFS&VERSION=1.1.0&REQUEST=GetFeature&TYPENAMES=DDAPI20:locaties"
        f"&OUTPUTFORMAT=application/json&CQL_FILTER={urllib.parse.quote(filter_)}"
    )
    kandidaten = []
    for kenmerk in (gegevens or {}).get("features", []):
        eigenschappen = kenmerk.get("properties") or {}
        code = str(eigenschappen.get("LOCATIE_CODE") or "")
        naam = str(eigenschappen.get("LOCATIE_NAAM") or "")
        if code and naam:
            kandidaten.append((code, naam))
    if not kandidaten:
        raise ValueError(f"geen meetlocatie gevonden voor '{plaats}'")
    # liefst de kale naam ("Hoek van Holland"), dan de kortste namen: dat zijn
    # de hoofdmeetpunten, en die hebben bijna altijd een waterstand
    kandidaten.sort(key=lambda paar: (paar[1].lower() != plaats.lower(), len(paar[1]), len(paar[0])))
    return kandidaten[:20]


def _watermeting(code: str) -> dict:
    """De laatste metingen van één meetlocatie ophalen."""
    nu = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=1)))
    gevraagd = {
        "Locatie": {"Code": code},
        "AquoPlusWaarnemingMetadata": {
            "AquoMetadata": {
                "Compartiment": {"Code": "OW"},
                "Grootheid": {"Code": "WATHTE"},
                "Hoedanigheid": {"Code": "NAP"},
            },
            "KwaliteitswaardeCode": "NVT",
        },
        "Periode": {
            "Begindatumtijd": (nu - datetime.timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%S.000+01:00"),
            "Einddatumtijd": nu.strftime("%Y-%m-%dT%H:%M:%S.000+01:00"),
        },
    }
    return _haal_json(
        WATER_WWS,
        gegevens=json.dumps(gevraagd).encode("utf-8"),
        koppen={"Content-Type": "application/json"},
    )


def gereedschap_water(plaats: str) -> dict:
    """De waterstand bij een meetlocatie van Rijkswaterstaat (in cm boven NAP)."""
    plaats = (plaats or "").strip()
    if not plaats:
        return {"naam": "water", "bron": "rijkswaterstaat.nl", "tekst": "", "fout": "geen plaats opgegeven"}
    try:
        kandidaten = _waterlocaties(plaats)
    except Exception as fout:
        return {"naam": "water", "bron": "rijkswaterstaat.nl", "tekst": "",
                "fout": f"{type(fout).__name__}: {fout}"}
    antwoord = None
    naam = ""
    for kandidaat_code, kandidaat_naam in kandidaten:
        try:
            poging = _watermeting(kandidaat_code)
        except Exception:
            continue
        if (poging or {}).get("WaarnemingenLijst"):
            antwoord, naam = poging, kandidaat_naam
            break
    if not antwoord:
        return {"naam": "water", "bron": "rijkswaterstaat.nl", "tekst": "",
                "fout": f"geen waterstand beschikbaar voor '{plaats}'"}
    metingen = []
    for waarneming in antwoord.get("WaarnemingenLijst") or []:
        for meting in waarneming.get("MetingenLijst") or []:
            waarde = (meting.get("Meetwaarde") or {}).get("Waarde_Numeriek")
            tijd = str(meting.get("Tijdstip") or "")
            if isinstance(waarde, (int, float)) and tijd:
                metingen.append((tijd, float(waarde)))
    if not metingen:
        return {"naam": "water", "bron": "rijkswaterstaat.nl", "tekst": "",
                "fout": f"geen metingen voor {naam}"}
    metingen.sort()
    laatste_tijd, laatste = metingen[-1]
    eerste_tijd, eerste = metingen[0]
    verschil = laatste - eerste
    richting = "stijgend" if verschil > 2 else "dalend" if verschil < -2 else "vrijwel gelijk"
    verandering = f"{verschil:+.0f}"
    return {
        "naam": "water",
        "bron": "Rijkswaterstaat (waterwebservices, AQUO-standaard)",
        "tekst": (
            f"Waterstand bij {naam}: {_getal(laatste, ' cm')} boven NAP om {laatste_tijd[11:16]}. "
            f"Sinds {eerste_tijd[11:16]} {richting} ({verandering} cm). "
            f"{len(metingen)} metingen in de laatste uren."
        ),
    }


def gereedschap_stroomprijs() -> dict:
    """De stroomprijs per uur voor vandaag en morgen (EnergyZero, met btw)."""
    nu = datetime.datetime.now(datetime.timezone.utc)
    begin = nu.replace(hour=0, minute=0, second=0, microsecond=0)
    eind = begin + datetime.timedelta(days=2)
    gegevens = _haal_json(
        f"{ENERGIE_API}?fromDate={begin.strftime('%Y-%m-%dT%H:%M:%S.000Z')}"
        f"&tillDate={eind.strftime('%Y-%m-%dT%H:%M:%S.000Z')}"
        "&interval=4&usageType=1&inclBtw=true"
    )
    prijzen = []
    for punt in (gegevens or {}).get("Prices") or []:
        moment = str(punt.get("readingDate") or "")
        bedrag = punt.get("price")
        if moment and isinstance(bedrag, (int, float)):
            prijzen.append((moment, float(bedrag)))
    if not prijzen:
        return {"naam": "stroomprijs", "bron": "energyzero.nl", "tekst": "", "fout": "geen prijzen ontvangen"}
    prijzen.sort()
    nu_iso = nu.strftime("%Y-%m-%dT%H:00")
    huidig = next((prijs for moment, prijs in prijzen if moment[:13] == nu_iso), prijzen[0][1])
    vandaag = [paar for paar in prijzen if paar[0][:10] == nu.strftime("%Y-%m-%d")]
    goedkoopst = sorted(prijzen, key=lambda paar: paar[1])[:3]
    duurste = max(prijzen, key=lambda paar: paar[1])
    def _prijs(bedrag: float) -> str:
        return f"{bedrag:.3f}".replace(".", ",") + " euro"

    regels = [
        f"nu {_prijs(huidig)} per kWh",
        "goedkoopste uren: " + ", ".join(f"{moment[11:16]} ({_prijs(prijs)})" for moment, prijs in goedkoopst),
        f"duurste uur: {duurste[0][11:16]} ({_prijs(duurste[1])})",
    ]
    if vandaag:
        gemiddeld = sum(prijs for _, prijs in vandaag) / len(vandaag)
        regels.append(f"gemiddeld vandaag {_prijs(gemiddeld)}")
    return {
        "naam": "stroomprijs",
        "bron": "energyzero.nl (uurprijzen, inclusief btw)",
        "tekst": "Stroomprijs: " + "; ".join(regels) + ".",
    }


def gereedschap_afstand(vraag: str) -> dict:
    """Afstand en reistijd met de auto tussen twee plaatsen (OpenStreetMap/OSRM)."""
    laag = (vraag or "").strip()
    stukken = re.split(r"\bnaar\b|\ben van\b|\bvan\b|\btussen\b", laag, flags=re.I)
    stukken = [stuk.strip(" ?.,!") for stuk in stukken if stuk.strip(" ?.,!")]
    if len(stukken) < 2:
        return {"naam": "afstand", "bron": "openstreetmap.org (OSRM)", "tekst": "",
                "fout": "noem twee plaatsen, bijvoorbeeld 'van Utrecht naar Maastricht'"}
    van, naar = stukken[0], stukken[1]
    try:
        van_breedte, van_lengte, van_net = _coordinaten(van)
        naar_breedte, naar_lengte, naar_net = _coordinaten(naar)
    except Exception as fout:
        return {"naam": "afstand", "bron": "openstreetmap.org (OSRM)", "tekst": "", "fout": str(fout)}
    route = _haal_json(
        f"https://router.project-osrm.org/route/v1/driving/"
        f"{van_lengte},{van_breedte};{naar_lengte},{naar_breedte}?overview=false"
    )
    routes = (route or {}).get("routes") or []
    if not routes:
        return {"naam": "afstand", "bron": "openstreetmap.org (OSRM)", "tekst": "",
                "fout": f"geen route van {van_net} naar {naar_net}"}
    afstand = float(routes[0].get("distance") or 0) / 1000
    seconden = float(routes[0].get("duration") or 0)
    uren, minuten = divmod(int(seconden // 60), 60)
    duur = f"{uren} uur en {minuten} minuten" if uren else f"{minuten} minuten"
    return {
        "naam": "afstand",
        "bron": "openstreetmap.org (OSRM) en open-meteo geocoding",
        "tekst": (
            f"Van {van_net} naar {naar_net}: {_getal(afstand, ' km')} met de auto, "
            f"ongeveer {duur} rijden (zonder files)."
        ),
    }


def gereedschap_boodschappen(zoektermen: str) -> dict:
    """Wat je al in huis hebt en wat je nog moet kopen voor een recept (VoorraadThuis)."""
    zoektermen = (zoektermen or "").strip()
    if not zoektermen:
        return {"naam": "boodschappen", "bron": "VoorraadThuis", "tekst": "", "fout": "geen gerecht opgegeven"}
    recept = None
    woorden = [w for w in re.split(r"[,\s]+", zoektermen.lower()) if len(w) > 2]
    beste = 0
    for kandidaten in _recepten():
        titel = str(kandidaten.get("title") or "").lower()
        score = sum(1 for woord in woorden if woord in titel)
        if score > beste:
            beste, recept = score, kandidaten
    if not recept:
        return {"naam": "boodschappen", "bron": "VoorraadThuis", "tekst": "",
                "fout": f"geen recept gevonden voor '{zoektermen}'"}
    ingredienten = [str(i) for i in (recept.get("ingredients") or [])]
    in_huis = []
    try:
        verbinding = sqlite3.connect(f"file:{VOORRAAD_DB}?mode=ro", uri=True)
        namen = [str(rij[0]).lower() for rij in verbinding.execute("select name from products")]
        verbinding.close()
    except Exception as fout:
        return {"naam": "boodschappen", "bron": "VoorraadThuis", "tekst": "",
                "fout": f"voorraad niet leesbaar ({type(fout).__name__})"}
    mis = []
    for ingredient in ingredienten:
        kaal = re.sub(r"[0-9½¼¾.,/]| gram| kilo| ml| liter| eetlepel[s]?| theelepel[s]?| stuk[s]?| teen[tje]*| snufje", " ", ingredient.lower())
        delen = [w for w in re.findall(r"[a-zà-ÿ]{3,}", kaal)]
        gevonden = any(deel in naam for naam in namen for deel in delen)
        (in_huis if gevonden else mis).append(ingredient)
    return {
        "naam": "boodschappen",
        "bron": f"VoorraadThuis ({recept.get('title')})",
        "tekst": (
            f"Voor {recept.get('title')} ({recept.get('servings') or '?'} personen) "
            f"heb je al: {', '.join(in_huis) if in_huis else 'niets'}. "
            f"Je mist nog: {', '.join(mis) if mis else 'niets'}."
        ),
    }


def gereedschap_wet(zoekterm: str) -> dict:
    """Zoeken in de Nederlandse wetten (basiswettenbestand, SRU)."""
    zoekterm = (zoekterm or "").strip()
    if not zoekterm:
        return {"naam": "wetten", "bron": "wetten.overheid.nl", "tekst": "", "fout": "geen zoekterm"}
    term = re.sub(r"^(wat zegt|wat is|zoek|zoek op)\s+", "", zoekterm, flags=re.I).strip(" ?!.")
    adres = (
        f"{SRU_WETTEN}?operation=searchRetrieve&x-connection=bwb&version=1.2&maximumRecords=3"
        f"&query={urllib.parse.quote('titel=' + term, safe='')}"
    )
    ruw = _haal(adres).decode("utf-8", "replace")
    titels = re.findall(r"<dcterms:title>([^<]+)</dcterms:title>", ruw) or re.findall(
        r"<title>([^<]+)</title>", ruw
    )
    nummers = re.findall(r"<dcterms:identifier>([^<]+)</dcterms:identifier>", ruw)
    links = re.findall(r"(https?://wetten\.overheid\.nl/id/[A-Z0-9]+)", ruw)
    if not titels:
        return {"naam": "wetten", "bron": "wetten.overheid.nl", "tekst": "",
                "fout": f"geen wet gevonden met '{term}' in de titel"}
    regels = []
    gezien = set()
    for nummer, titel in enumerate(titels[:6]):
        schone_titel = titel.strip()
        nummer_wet = nummers[nummer].strip() if nummer < len(nummers) else ""
        if nummer_wet in gezien or schone_titel in gezien:
            continue
        gezien.update({nummer_wet, schone_titel})
        stuk = f"{schone_titel} ({nummer_wet})" if nummer_wet else schone_titel
        if nummer < len(links):
            stuk += f" — {links[nummer]}"
        regels.append(stuk)
        if len(regels) >= 3:
            break
    return {
        "naam": "wetten",
        "bron": "wetten.overheid.nl (basiswettenbestand, SRU)",
        "tekst": f"Wetten met '{term}': " + " | ".join(regels),
    }


def gereedschap_cbs(plaats: str) -> dict:
    """Inwoners van een gemeente (CBS Open Data)."""
    plaats = (plaats or "").strip()
    if not plaats:
        return {"naam": "cbs", "bron": "opendata.cbs.nl", "tekst": "", "fout": "geen gemeente opgegeven"}
    filter_ = urllib.parse.quote(f"startswith(Title,'{plaats}')", safe="()'")
    regio = _haal_json(f"https://opendata.cbs.nl/ODataApi/odata/70072ned/RegioS?$filter={filter_}&$top=10")
    kandidaten = [rij for rij in ((regio or {}).get("value") or []) if str(rij.get("Key") or "").startswith("GM")]
    if not kandidaten:
        return {"naam": "cbs", "bron": "opendata.cbs.nl", "tekst": "",
                "fout": f"geen gemeente gevonden die begint met '{plaats}'"}
    # de naam die het beste past eerst (Sittard-Geleen gaat voor het oude Sittard)
    kandidaten.sort(key=lambda rij: (str(rij.get("Title")) != plaats, -len(str(rij.get("Title")))))
    grens = datetime.date.today().year - 3
    beste = None
    for kandidaat in kandidaten[:3]:
        code = str(kandidaat.get("Key") or "")
        gegevens = _haal_json(
            "https://opendata.cbs.nl/ODataApi/odata/70072ned/TypedDataSet?"
            f"%24filter=RegioS%20eq%20%27{code}%27&%24select=Perioden%2CTotaleBevolking_1%2CMannen_2%2CVrouwen_3"
        )
        waarden = [rij for rij in ((gegevens or {}).get("value") or [])
                   if isinstance(rij.get("TotaleBevolking_1"), (int, float))]
        if not waarden:
            continue
        rij = max(waarden, key=lambda r: str(r.get("Perioden") or ""))
        jaar = int(str(rij.get("Perioden") or "0")[:4] or 0)
        if beste is None or jaar > beste[0]:
            beste = (jaar, str(kandidaat.get("Title") or plaats), rij)
        if jaar >= grens:
            break
    if not beste:
        return {"naam": "cbs", "bron": "opendata.cbs.nl", "tekst": "",
                "fout": f"geen inwonertal voor {plaats}"}
    jaar, naam, rij = beste
    periode = str(rij.get("Perioden") or "")
    return {
        "naam": "cbs",
        "bron": f"opendata.cbs.nl (tabel 70072ned, {periode})",
        "tekst": (
            f"{naam} heeft {_getal(rij.get('TotaleBevolking_1'), komma=False)} inwoners "
            f"({jaar}, bron CBS)."
            + (f" Mannen {_getal(rij.get('Mannen_2'), komma=False)}, vrouwen {_getal(rij.get('Vrouwen_3'), komma=False)}."
               if isinstance(rij.get("Mannen_2"), (int, float)) else "")
        ),
    }


def gereedschap_boek(titel: str) -> dict:
    """Een boek opzoeken: schrijver, jaar en druk (Open Library)."""
    titel = (titel or "").strip()
    if not titel:
        return {"naam": "boek", "bron": "openlibrary.org", "tekst": "", "fout": "geen titel opgegeven"}
    gegevens = _haal_json(
        "https://openlibrary.org/search.json?fields=title,author_name,first_publish_year,publisher"
        f"&limit=3&q={urllib.parse.quote(titel)}"
    )
    boeken = (gegevens or {}).get("docs") or []
    if not boeken:
        return {"naam": "boek", "bron": "openlibrary.org", "tekst": "",
                "fout": f"geen boek gevonden voor '{titel}'"}
    regels = []
    for boek in boeken[:3]:
        schrijvers = ", ".join((boek.get("author_name") or [])[:2]) or "onbekende schrijver"
        jaar = boek.get("first_publish_year") or "?"
        regels.append(f"{boek.get('title')} — {schrijvers} ({jaar})")
    return {
        "naam": "boek",
        "bron": "openlibrary.org",
        "tekst": f"Boeken bij '{titel}': " + " | ".join(regels),
    }


def gereedschap_vliegtuigen(gebied: str) -> dict:
    """Hoeveel vliegtuigen er nu boven Nederland hangen (OpenSky)."""
    vak = {"lamin": 50.7, "lomin": 3.3, "lamax": 53.6, "lomax": 7.2}
    gegevens = _haal_json(
        "https://opensky-network.org/api/states/all?"
        + "&".join(f"{sleutel}={waarde}" for sleutel, waarde in vak.items())
    )
    vliegtuigen = (gegevens or {}).get("states") or []
    if not vliegtuigen:
        return {"naam": "vliegtuigen", "bron": "opensky-network.org", "tekst": "",
                "fout": "geen vliegtuigen ontvangen (de dienst is soms druk)"}
    namen = []
    for toestel in vliegtuigen:
        roepnaam = str(toestel[1] or "").strip()
        land = str(toestel[2] or "").strip()
        hoogte = toestel[7] or toestel[13]
        if roepnaam:
            namen.append(f"{roepnaam} ({land}, {_getal((hoogte or 0) * 3.281, komma=False)} voet)")
    return {
        "naam": "vliegtuigen",
        "bron": "opensky-network.org (gratis, momentopname)",
        "tekst": (
            f"Er hangen nu {len(vliegtuigen)} vliegtuigen boven Nederland. "
            "Een paar: " + "; ".join(namen[:6])
        ),
    }


def _pdf_tekst(pad: Path) -> str:
    """Tekst uit een eenvoudige pdf halen (zonder extra bibliotheek)."""
    ruw = pad.read_bytes()
    stukken = []
    for blok in re.finditer(rb"stream\r?\n(.*?)endstream", ruw, re.S):
        try:
            inhoud = zlib.decompress(blok.group(1))
        except Exception:
            continue
        # alleen paginastromen met tekstobjecten; afbeeldingen slaan we over
        if b"BT" not in inhoud or b"ET" not in inhoud:
            continue
        for tekstblok in re.finditer(rb"BT(.*?)ET", inhoud, re.S):
            for stuk in re.finditer(rb"\((?:[^()\\]|\\.)*\)", tekstblok.group(1)):
                tekst = stuk.group(0)[1:-1]
                tekst = re.sub(rb"\\([nrt])", b" ", tekst)
                tekst = re.sub(rb"\\(.)", rb"\1", tekst)
                schoon = tekst.decode("latin-1", "replace")
                leesbaar = sum(1 for teken in schoon if teken.isprintable() or teken == " ")
                if len(schoon) > 1 and leesbaar >= len(schoon) * 0.9:
                    stukken.append(schoon)
    return " ".join(stukken)


def _docx_tekst(pad: Path) -> str:
    """Tekst uit een Word-bestand halen (docx is een zip met xml)."""
    with zipfile.ZipFile(pad) as archief:
        ruw = archief.read("word/document.xml").decode("utf-8", "replace")
    ruw = re.sub(r"</w:p>", "\n", ruw)
    return _schoon(ruw)


def gereedschap_document(zoekterm: str) -> dict:
    """Een document uit je eigen documentenmap lezen of doorzoeken (WLLM_DOCUMENTEN)."""
    if not DOCUMENTEN_MAP:
        return {
            "naam": "document",
            "bron": "eigen documentenmap",
            "tekst": "",
            "fout": "geen documentenmap ingesteld (zet WLLM_DOCUMENTEN op het pad van je map)",
        }
    zoekterm = (zoekterm or "").strip()
    if not zoekterm:
        return {"naam": "document", "bron": "eigen documentenmap", "tekst": "", "fout": "geen zoekterm"}
    laag = zoekterm.lower()
    bestanden = []
    for stuk in DOCUMENTEN_MAP.split(":"):
        map_ = Path(stuk.strip()).expanduser()
        if not map_.is_dir():
            continue
        for patroon in ("*.txt", "*.md", "*.pdf", "*.docx"):
            bestanden.extend(sorted(map_.rglob(patroon)))
    if not bestanden:
        return {"naam": "document", "bron": "eigen documentenmap", "tekst": "",
                "fout": "geen leesbare documenten gevonden (txt, md, pdf, docx)"}
    # eerst een bestand waarvan de naam al klopt, anders de inhoud doorzoeken
    for bestand in bestanden:
        if laag in bestand.name.lower():
            try:
                tekst = _docx_tekst(bestand) if bestand.suffix == ".docx" else (
                    _pdf_tekst(bestand) if bestand.suffix == ".pdf"
                    else bestand.read_text(encoding="utf-8", errors="replace")
                )
            except Exception as fout:
                return {"naam": "document", "bron": str(bestand), "tekst": "",
                        "fout": f"{type(fout).__name__}: {fout}"}
            schoon = _schoon(tekst)
            if len(schoon) < 40:
                return {
                    "naam": "document",
                    "bron": f"eigen document: {bestand.name}",
                    "tekst": "",
                    "fout": (
                        f"{bestand.name} is een pdf met een eigen tekencoder; "
                        "daar kan ik de tekst niet uit halen. Zet er een txt- of docx-versie naast."
                    ),
                }
            return {
                "naam": "document",
                "bron": f"eigen document: {bestand.name}",
                "tekst": _kort(schoon, 1200),
            }
    for bestand in bestanden:
        try:
            tekst = _docx_tekst(bestand) if bestand.suffix == ".docx" else (
                _pdf_tekst(bestand) if bestand.suffix == ".pdf"
                else bestand.read_text(encoding="utf-8", errors="replace")
            )
        except Exception:
            continue
        if laag in tekst.lower():
            plek = tekst.lower().find(laag)
            return {
                "naam": "document",
                "bron": f"eigen document: {bestand.name}",
                "tekst": _kort(_schoon(tekst[max(0, plek - 250): plek + 500]), 800),
            }
    return {"naam": "document", "bron": "eigen documentenmap",
            "tekst": f"Niets gevonden over '{zoekterm}' in {len(bestanden)} documenten."}

# ---- E. aardbevingen, hooikoorts, bruggen, verwachting, gas, horeca,
#         weeralarm, buurtcijfers en ontwikkelaarshulpjes

EMSC_API = "https://www.seismicportal.eu/fdsnws/event/1/query"
METEOALARM = "https://feeds.meteoalarm.org/api/v1/warnings/feeds-netherlands"
BRUGGEN_FEED = "https://opendata.ndw.nu/planningsfeed_brugopeningen.xml.gz"
BRUGGEN_CACHE: dict = {"tijd": 0.0, "ruw": ""}


def gereedschap_aardbevingen(gebied: str) -> dict:
    """Aardbevingen in Nederland en omstreken, laatste drie maanden (EMSC)."""
    nu = datetime.datetime.now(datetime.timezone.utc)
    begin = (nu - datetime.timedelta(days=90)).strftime("%Y-%m-%d")
    gegevens = _haal_json(
        f"{EMSC_API}?format=json&minlatitude=50.6&maxlatitude=53.7&minlongitude=3.2&maxlongitude=7.3"
        f"&starttime={begin}&minmagnitude=0.5&limit=8&orderby=time"
    )
    bevingen = (gegevens or {}).get("features") or []
    if not bevingen:
        return {
            "naam": "aardbevingen",
            "bron": "seismicportal.eu (EMSC)",
            "tekst": "Geen aardbevingen boven magnitude 0,5 in Nederland en omstreken in de laatste drie maanden.",
        }
    regels = []
    for beving in bevingen[:6]:
        eigenschappen = beving.get("properties") or {}
        moment = str(eigenschappen.get("time") or "")[:10]
        regels.append(
            f"M{_getal(eigenschappen.get('mag'))} {eigenschappen.get('flynn_region') or '?'} ({moment})"
        )
    return {
        "naam": "aardbevingen",
        "bron": "seismicportal.eu (EMSC, Europees seismologisch centrum)",
        "tekst": f"Aardbevingen in Nederland en omstreken, laatste drie maanden: " + " | ".join(regels),
    }


def gereedschap_hooikoorts(plaats: str) -> dict:
    """Hooikoorts (pollen) en de UV-index (Open-Meteo)."""
    plaats = (plaats or "").strip() or GEREEDSCHAP_PLAATS
    if not plaats:
        return {"naam": "hooikoorts", "bron": "open-meteo.com", "tekst": "", "fout": "geen plaats opgegeven"}
    try:
        breedte, lengte, nette = _coordinaten(plaats)
    except Exception as fout:
        return {"naam": "hooikoorts", "bron": "open-meteo.com", "tekst": "", "fout": str(fout)}
    gegevens = _haal_json(
        "https://air-quality-api.open-meteo.com/v1/air-quality?"
        f"latitude={breedte}&longitude={lengte}"
        "&current=uv_index,grass_pollen,birch_pollen,alder_pollen,mugwort_pollen,ragweed_pollen,olive_pollen"
        "&timezone=Europe/Amsterdam"
    )
    nu = (gegevens or {}).get("current") or {}
    if not nu:
        return {"naam": "hooikoorts", "bron": "open-meteo.com", "tekst": "", "fout": "geen gegevens"}
    namen = {
        "grass_pollen": "gras", "birch_pollen": "berk", "alder_pollen": "els",
        "mugwort_pollen": "bijvoet", "ragweed_pollen": "ambrosia", "olive_pollen": "olijf",
    }
    actief = []
    for sleutel, naam in namen.items():
        waarde = nu.get(sleutel)
        if isinstance(waarde, (int, float)) and waarde > 0:
            actief.append(f"{naam} {_getal(waarde, ' korrels/m³')}")
    uv = nu.get("uv_index")
    uv_tekst = ""
    if isinstance(uv, (int, float)):
        oordeel = "laag" if uv < 3 else "matig" if uv < 6 else "hoog" if uv < 8 else "zeer hoog"
        uv_tekst = f" UV-index {_getal(uv)} ({oordeel})."
    return {
        "naam": "hooikoorts",
        "bron": "open-meteo.com (CAMS-pollenmodel)",
        "tekst": (
            f"Pollen bij {nette}: " + (", ".join(actief) if actief else "geen noemenswaardige pollen") + "."
            + uv_tekst
        ),
    }


def _bruggen_ruw() -> str:
    nu = time.time()
    if BRUGGEN_CACHE["ruw"] and nu - BRUGGEN_CACHE["tijd"] < 600:
        return str(BRUGGEN_CACHE["ruw"])
    ruw = gzip.decompress(_haal(BRUGGEN_FEED)).decode("utf-8", "replace")
    BRUGGEN_CACHE["tijd"] = nu
    BRUGGEN_CACHE["ruw"] = ruw
    return ruw


def gereedschap_bruggen(plaats: str) -> dict:
    """Brugopeningen in de komende uren (NDW / Rijkswaterstaat)."""
    try:
        ruw = _bruggen_ruw()
    except Exception as fout:
        return {"naam": "bruggen", "bron": "opendata.ndw.nu", "tekst": "",
                "fout": f"{type(fout).__name__}: {fout}"}
    nu = datetime.datetime.now(datetime.timezone.utc)
    grens = nu + datetime.timedelta(hours=8)
    gezocht = (plaats or "").strip().lower()
    openingen = []
    for blok in re.findall(r"<sit:situationRecord\b.*?</sit:situationRecord>", ruw, re.S):
        if "bridgeSwingInOperation" not in blok and "bridgeOpen" not in blok:
            continue
        begin = re.search(r"<com:overallStartTime>([^<]+)", blok)
        eind = re.search(r"<com:overallEndTime>([^<]+)", blok)
        punt = re.search(r"<loc:latitude>([\d.\-]+)</loc:latitude>.*?<loc:longitude>([\d.\-]+)</loc:longitude>", blok, re.S)
        if not (begin and punt):
            continue
        try:
            start = datetime.datetime.fromisoformat(begin.group(1).replace("Z", "+00:00"))
            stop = datetime.datetime.fromisoformat(eind.group(1).replace("Z", "+00:00")) if eind else start
        except ValueError:
            continue
        if stop < nu or start > grens:
            continue
        openingen.append((start, stop, float(punt.group(1)), float(punt.group(2))))
    openingen.sort()
    if not openingen:
        return {
            "naam": "bruggen",
            "bron": "opendata.ndw.nu (NDW / Rijkswaterstaat)",
            "tekst": "Geen geplande brugopeningen in de komende acht uur.",
        }
    regels = []
    for start, stop, breedte, lengte in openingen[:6]:
        waar = _weg_en_plaats(breedte, lengte) or "onbekende plek"
        if gezocht and gezocht not in waar.lower():
            continue
        regels.append(f"{waar}: {start.astimezone().strftime('%H:%M')} tot {stop.astimezone().strftime('%H:%M')}")
    if not regels:
        return {
            "naam": "bruggen",
            "bron": "opendata.ndw.nu (NDW / Rijkswaterstaat)",
            "tekst": f"Geen brugopeningen gevonden bij '{plaats}' in de komende acht uur.",
        }
    return {
        "naam": "bruggen",
        "bron": "opendata.ndw.nu (NDW / Rijkswaterstaat) en api.pdok.nl",
        "tekst": "Brugopeningen: " + " | ".join(regels),
    }


def gereedschap_verwachting(plaats: str) -> dict:
    """De verwachting voor veertien dagen (Open-Meteo)."""
    plaats = (plaats or "").strip() or GEREEDSCHAP_PLAATS
    if not plaats:
        return {"naam": "verwachting", "bron": "open-meteo.com", "tekst": "", "fout": "geen plaats opgegeven"}
    try:
        breedte, lengte, nette = _coordinaten(plaats)
    except Exception as fout:
        return {"naam": "verwachting", "bron": "open-meteo.com", "tekst": "", "fout": str(fout)}
    gegevens = _haal_json(
        "https://api.open-meteo.com/v1/forecast?"
        f"latitude={breedte}&longitude={lengte}"
        "&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_sum,precipitation_probability_max"
        "&forecast_days=14&timezone=Europe/Amsterdam"
    )
    dagen = (gegevens or {}).get("daily") or {}
    tijden = dagen.get("time") or []
    if not tijden:
        return {"naam": "verwachting", "bron": "open-meteo.com", "tekst": "", "fout": "geen gegevens"}
    regels = []
    for nummer, dag in enumerate(tijden):
        code = (dagen.get("weather_code") or [0])[nummer]
        regels.append(
            f"{dag[8:10]}-{dag[5:7]}: {WMO.get(int(code or 0), 'onbekend')}, "
            f"{_getal((dagen.get('temperature_2m_min') or [0])[nummer])} tot "
            f"{_getal((dagen.get('temperature_2m_max') or [0])[nummer], ' °C')}, "
            f"{_getal((dagen.get('precipitation_sum') or [0])[nummer], ' mm')} regen"
        )
    return {
        "naam": "verwachting",
        "bron": "open-meteo.com",
        "tekst": f"Verwachting voor {nette}, veertien dagen: " + " | ".join(regels),
    }


def gereedschap_gasprijs() -> dict:
    """De gasprijs per kuub, vandaag en morgen (EnergyZero, met btw)."""
    nu = datetime.datetime.now(datetime.timezone.utc)
    begin = nu.replace(hour=0, minute=0, second=0, microsecond=0)
    eind = begin + datetime.timedelta(days=2)
    gegevens = _haal_json(
        f"{ENERGIE_API}?fromDate={begin.strftime('%Y-%m-%dT%H:%M:%S.000Z')}"
        f"&tillDate={eind.strftime('%Y-%m-%dT%H:%M:%S.000Z')}"
        "&interval=4&usageType=2&inclBtw=true"
    )
    prijzen = []
    for punt in (gegevens or {}).get("Prices") or []:
        moment = str(punt.get("readingDate") or "")
        bedrag = punt.get("price")
        if moment and isinstance(bedrag, (int, float)):
            prijzen.append((moment, float(bedrag)))
    if not prijzen:
        return {"naam": "gasprijs", "bron": "energyzero.nl", "tekst": "", "fout": "geen prijzen ontvangen"}
    prijzen.sort()
    nu_iso = nu.strftime("%Y-%m-%dT%H:00")
    huidig = next((prijs for moment, prijs in prijzen if moment[:13] == nu_iso), prijzen[0][1])
    laagste = min(prijs for _, prijs in prijzen)
    hoogste = max(prijs for _, prijs in prijzen)
    return {
        "naam": "gasprijs",
        "bron": "energyzero.nl (inclusief btw)",
        "tekst": (
            f"Gasprijs: nu {huidig:.2f}".replace(".", ",") + " euro per m³, "
            + f"laagste {laagste:.2f}".replace(".", ",") + f", hoogste {hoogste:.2f}".replace(".", ",")
            + " (vandaag en morgen)."
        ),
    }


AMENITY = {
    "pizzeria": "restaurant", "pizza": "restaurant", "restaurant": "restaurant",
    "eetcafe": "cafe", "café": "cafe", "kroeg": "pub", "bar": "bar",
    "bakker": "bakery", "bakkerij": "bakery", "slager": "butcher", "supermarkt": "supermarket",
    "apotheek": "pharmacy", "huisarts": "doctors", "tandarts": "dentist",
    "hotel": "hotel", "cafeetje": "cafe", "koffie": "cafe", "snackbar": "fast_food",
    "frituur": "fast_food", "fastfood": "fast_food", "ijs": "ice_cream", "ijssalon": "ice_cream",
}


def gereedschap_horeca(zoekterm: str) -> dict:
    """Wat er in de buurt zit: horeca en winkels (OpenStreetMap)."""
    laag = (zoekterm or "").strip().lower()
    if not laag:
        return {"naam": "horeca", "bron": "openstreetmap.org", "tekst": "",
                "fout": "noem wat je zoekt, bijvoorbeeld 'pizzeria in Utrecht'"}
    soort = ""
    for woord, waarde in AMENITY.items():
        if woord in laag:
            soort = waarde
            break
    if not soort:
        soort = "restaurant"
    plaats = _plaats_uit(zoekterm) or GEREEDSCHAP_PLAATS
    if not plaats:
        return {"naam": "horeca", "bron": "openstreetmap.org", "tekst": "",
                "fout": "noem ook een plaats, bijvoorbeeld 'pizzeria in Utrecht'"}
    try:
        breedte, lengte, nette = _coordinaten(plaats)
    except Exception as fout:
        return {"naam": "horeca", "bron": "openstreetmap.org", "tekst": "", "fout": str(fout)}
    vraag = f'[out:json][timeout:20];node["amenity"="{soort}"](around:1500,{breedte},{lengte});out 12;'
    try:
        gegevens = _overpass(vraag)
    except Exception as fout:
        return {"naam": "horeca", "bron": "openstreetmap.org", "tekst": "", "fout": str(fout)}
    plekken = []
    for element in (gegevens or {}).get("elements") or []:
        naam = (element.get("tags") or {}).get("name")
        if not naam:
            continue
        punt_lat = element.get("lat")
        punt_lon = element.get("lon")
        afstand = ""
        if isinstance(punt_lat, (int, float)) and isinstance(punt_lon, (int, float)):
            dy = (punt_lat - breedte) * 111
            dx = (punt_lon - lengte) * 111 * 0.62
            afstand = f" ({_getal((dy * dy + dx * dx) ** 0.5, ' km')})"
        plekken.append(f"{naam}{afstand}")
    if not plekken:
        return {
            "naam": "horeca",
            "bron": "openstreetmap.org (Overpass)",
            "tekst": f"Geen {soort} gevonden binnen 1,5 km van {nette}.",
        }
    return {
        "naam": "horeca",
        "bron": "openstreetmap.org (Overpass, ODbL)",
        "tekst": f"{soort.capitalize()} binnen 1,5 km van {nette}: " + ", ".join(plekken[:8]),
    }


def gereedschap_weeralarm(plaats: str) -> dict:
    """Officiële weerswaarschuwingen voor Nederland (MeteoAlarm)."""
    gegevens = _haal_json(METEOALARM)
    waarschuwingen = (gegevens or {}).get("warnings") or []
    nu = datetime.datetime.now(datetime.timezone.utc)
    regels = []
    gezien = set()
    for waarschuwing in waarschuwingen:
        for info in (waarschuwing.get("alert") or {}).get("info") or []:
            if str(info.get("language") or "nl").lower().startswith("en"):
                continue
            afloop = str(info.get("expires") or info.get("onset") or "")
            try:
                if afloop and datetime.datetime.fromisoformat(afloop.replace("Z", "+00:00")) < nu:
                    continue
            except ValueError:
                pass
            gebieden = ", ".join(
                str(gebied.get("areaDesc") or "") for gebied in (info.get("area") or [])
            )[:110]
            sleutel = (info.get("event"), gebieden, info.get("severity"))
            if sleutel in gezien:
                continue
            gezien.add(sleutel)
            regels.append(
                f"{info.get('event') or 'waarschuwing'} "
                f"({info.get('severity') or '?'}) voor {gebieden or 'Nederland'}"
            )
    if not regels:
        return {
            "naam": "weeralarm",
            "bron": "meteoalarm.org (KNMI levert de Nederlandse waarschuwingen)",
            "tekst": "Er zijn nu geen officiële weerswaarschuwingen voor Nederland.",
        }
    return {
        "naam": "weeralarm",
        "bron": "meteoalarm.org (KNMI levert de Nederlandse waarschuwingen)",
        "tekst": f"Weerswaarschuwingen ({len(regels)}): " + " | ".join(regels[:5]),
    }


def gereedschap_buurtcijfers(plaats: str) -> dict:
    """Inwoners, leeftijdsopbouw en dichtheid van een gemeente (CBS)."""
    plaats = (plaats or "").strip()
    if not plaats:
        return {"naam": "buurtcijfers", "bron": "opendata.cbs.nl", "tekst": "", "fout": "geen gemeente opgegeven"}
    filter_ = urllib.parse.quote(f"startswith(Title,'{plaats}')", safe="()'")
    regio = _haal_json(f"https://opendata.cbs.nl/ODataApi/odata/70072ned/RegioS?$filter={filter_}&$top=10")
    kandidaten = [rij for rij in ((regio or {}).get("value") or []) if str(rij.get("Key") or "").startswith("GM")]
    if not kandidaten:
        return {"naam": "buurtcijfers", "bron": "opendata.cbs.nl", "tekst": "",
                "fout": f"geen gemeente gevonden die begint met '{plaats}'"}
    kandidaten.sort(key=lambda rij: (str(rij.get("Title")) != plaats, -len(str(rij.get("Title")))))
    code = str(kandidaten[0].get("Key") or "")
    naam = str(kandidaten[0].get("Title") or plaats)
    velden = "Perioden,TotaleBevolking_1,k_0Tot15Jaar_4,k_65Tot80Jaar_8,k_80JaarOfOuder_9,Bevolkingsdichtheid_27"
    gegevens = _haal_json(
        "https://opendata.cbs.nl/ODataApi/odata/70072ned/TypedDataSet?"
        f"%24filter=RegioS%20eq%20%27{code}%27&%24select={velden}"
    )
    waarden = [rij for rij in ((gegevens or {}).get("value") or [])
               if isinstance(rij.get("TotaleBevolking_1"), (int, float))]
    if not waarden:
        return {"naam": "buurtcijfers", "bron": "opendata.cbs.nl", "tekst": "",
                "fout": f"geen cijfers voor {naam}"}
    rij = max(waarden, key=lambda r: str(r.get("Perioden") or ""))
    totaal = float(rij.get("TotaleBevolking_1") or 0)
    jong = float(rij.get("k_0Tot15Jaar_4") or 0)
    oud = float(rij.get("k_65Tot80Jaar_8") or 0) + float(rij.get("k_80JaarOfOuder_9") or 0)
    dichtheid = rij.get("Bevolkingsdichtheid_27")
    return {
        "naam": "buurtcijfers",
        "bron": f"opendata.cbs.nl (tabel 70072ned, {rij.get('Perioden')})",
        "tekst": (
            f"{naam}: {_getal(totaal, komma=False)} inwoners, "
            f"{_getal(jong / totaal * 100 if totaal else 0, '% onder 15')}, "
            f"{_getal(oud / totaal * 100 if totaal else 0, '% 65 en ouder')}"
            + (f", {_getal(dichtheid, komma=False)} inwoners per km²" if isinstance(dichtheid, (int, float)) else "")
            + "."
        ),
    }


def gereedschap_devhulp(verzoek: str) -> dict:
    """Hulpjes voor ontwikkelwerk: hashes, base64, json, kleurcontrast en QR."""
    verzoek = (verzoek or "").strip()
    if not verzoek:
        return {"naam": "devhulp", "bron": "de engine zelf", "tekst": "",
                "fout": "zeg wat je wilt, bijvoorbeeld 'sha256 van hallo'"}
    laag = verzoek.lower()
    inhoud = re.sub(
        r"^(maak|bereken|geef|doe)?\s*(een\s+)?(sha256|sha1|md5|base64|json|contrast|qr)\s*"
        r"(van|voor|tussen|formatteren|decoderen|coderen)?\s*",
        "",
        verzoek,
        flags=re.I,
    ).strip(" :?")
    if "sha256" in laag or "sha1" in laag or "md5" in laag:
        soort = "sha256" if "sha256" in laag else ("sha1" if "sha1" in laag else "md5")
        waarde = hashlib.new(soort, inhoud.encode("utf-8")).hexdigest()
        return {"naam": "devhulp", "bron": "de engine zelf", "tekst": f"{soort} van '{inhoud}': {waarde}"}
    if "base64" in laag:
        if "decode" in laag or "decoderen" in laag:
            try:
                tekst = base64.b64decode(inhoud + "=" * (-len(inhoud) % 4)).decode("utf-8", "replace")
            except Exception as fout:
                return {"naam": "devhulp", "bron": "de engine zelf", "tekst": "",
                        "fout": f"{type(fout).__name__}: {fout}"}
            return {"naam": "devhulp", "bron": "de engine zelf", "tekst": f"base64 gedecodeerd: {tekst}"}
        waarde = base64.b64encode(inhoud.encode("utf-8")).decode("ascii")
        return {"naam": "devhulp", "bron": "de engine zelf", "tekst": f"base64 van '{inhoud}': {waarde}"}
    if "json" in laag:
        try:
            gegevens = json.loads(inhoud)
        except Exception as fout:
            return {"naam": "devhulp", "bron": "de engine zelf", "tekst": "",
                    "fout": f"geen geldige json ({type(fout).__name__})"}
        return {
            "naam": "devhulp",
            "bron": "de engine zelf",
            "tekst": json.dumps(gegevens, ensure_ascii=False, indent=2)[:900],
        }
    if "contrast" in laag:
        kleuren = re.findall(r"#?([0-9a-f]{6}|[0-9a-f]{3})\b", laag)
        if len(kleuren) < 2:
            return {"naam": "devhulp", "bron": "de engine zelf", "tekst": "",
                    "fout": "noem twee kleuren, bijvoorbeeld 'contrast tussen #ffffff en #1e73be'"}

        def _helderheid(waarde: str) -> float:
            waarde = waarde if len(waarde) == 6 else "".join(teken * 2 for teken in waarde)
            rood, groen, blauw = (int(waarde[i:i + 2], 16) / 255 for i in (0, 2, 4))
            kanalen = [k / 12.92 if k <= 0.03928 else ((k + 0.055) / 1.055) ** 2.4 for k in (rood, groen, blauw)]
            return 0.2126 * kanalen[0] + 0.7152 * kanalen[1] + 0.0722 * kanalen[2]

        licht_a, licht_b = _helderheid(kleuren[0]), _helderheid(kleuren[1])
        verhouding = (max(licht_a, licht_b) + 0.05) / (min(licht_a, licht_b) + 0.05)
        oordeel = "voldoet aan AA" if verhouding >= 4.5 else "te weinig contrast voor tekst"
        return {
            "naam": "devhulp",
            "bron": "de engine zelf (WCAG-formule)",
            "tekst": f"Contrast tussen #{kleuren[0]} en #{kleuren[1]}: {_getal(verhouding)}:1 — {oordeel}.",
        }
    if "qr" in laag:
        na_voor = re.split(r"\bvoor\b", verzoek, maxsplit=1, flags=re.I)
        doel = (na_voor[1].strip(" :?") if len(na_voor) > 1 else "") or "https://example.com"
        return {
            "naam": "devhulp",
            "bron": "api.qrserver.com",
            "tekst": (
                f"QR-code voor '{doel}': https://api.qrserver.com/v1/create-qr-code/?size=300x300&data="
                + urllib.parse.quote(doel)
            ),
        }
    return {
        "naam": "devhulp",
        "bron": "de engine zelf",
        "tekst": "",
        "fout": "ik ken alleen sha256, sha1, md5, base64, json, contrast en qr",
    }

# ---- F. uurverloop, laadpalen, parkeren, vorig jaar, btw, inflatie,
#         wikipedia, marktplaats-prijswacht, afsluitingen en feeds

MARKTPLAATS_LRP = "https://www.marktplaats.nl/lrp/api/search"
AFSLUITINGEN_FEED = "https://opendata.ndw.nu/tijdelijke_verkeersmaatregelen_afsluitingen.xml.gz"
AFSLUITINGEN_CACHE: dict = {"tijd": 0.0, "ruw": ""}


def gereedschap_uurverloop(plaats: str) -> dict:
    """Het weer per uur voor vandaag: wanneer is het droog (Open-Meteo)."""
    plaats = (plaats or "").strip() or GEREEDSCHAP_PLAATS
    if not plaats:
        return {"naam": "uurverloop", "bron": "open-meteo.com", "tekst": "", "fout": "geen plaats opgegeven"}
    try:
        breedte, lengte, nette = _coordinaten(plaats)
    except Exception as fout:
        return {"naam": "uurverloop", "bron": "open-meteo.com", "tekst": "", "fout": str(fout)}
    gegevens = _haal_json(
        "https://api.open-meteo.com/v1/forecast?"
        f"latitude={breedte}&longitude={lengte}"
        "&hourly=temperature_2m,precipitation,precipitation_probability,weather_code"
        "&forecast_days=1&timezone=Europe/Amsterdam"
    )
    per_uur = (gegevens or {}).get("hourly") or {}
    tijden = per_uur.get("time") or []
    if not tijden:
        return {"naam": "uurverloop", "bron": "open-meteo.com", "tekst": "", "fout": "geen gegevens"}
    nu = datetime.datetime.now().strftime("%Y-%m-%dT%H:00")
    regels, droog, nat = [], [], []
    temperaturen = []
    for nummer, moment in enumerate(tijden):
        if moment < nu:
            continue
        temperatuur = (per_uur.get("temperature_2m") or [None])[nummer]
        regen = (per_uur.get("precipitation") or [0])[nummer] or 0
        kans = (per_uur.get("precipitation_probability") or [0])[nummer] or 0
        if isinstance(temperatuur, (int, float)):
            temperaturen.append(temperatuur)
        (nat if regen > 0.1 or kans > 55 else droog).append(moment[11:16])
        if len(regels) < 8:
            regels.append(f"{moment[11:16]} {_getal(temperatuur, ' °C')} {_getal(kans, '% regen')}")
        if len(regels) >= 8 and len(droog) + len(nat) > 12:
            break
    droog_tekst = (
        f"Droog van {droog[0]} tot {droog[-1]}." if len(droog) >= 2
        else "Geen duidelijk droog blok vandaag."
    )
    nat_tekst = f" Regen verwacht rond {nat[0]}." if nat else " Geen regen verwacht."
    bereik = (
        f" {_getal(min(temperaturen))} tot {_getal(max(temperaturen), '°C')}."
        if temperaturen else ""
    )
    return {
        "naam": "uurverloop",
        "bron": "open-meteo.com",
        "tekst": f"Weer per uur bij {nette}: " + " | ".join(regels) + f". {droog_tekst}{nat_tekst}{bereik}",
    }


def _overpass(vraag: str) -> dict:
    """Een Overpass-vraag doen. De spiegel die het eerst echt antwoord geeft wint."""
    gegevens = urllib.parse.urlencode({"data": vraag}).encode("utf-8")
    koppen = {"Content-Type": "application/x-www-form-urlencoded"}
    laatste: dict | None = None
    for adres in ("https://overpass-api.de/api/interpreter",
                  "https://overpass.kumi.systems/api/interpreter",
                  "https://overpass.private.coffee/api/interpreter"):
        try:
            antwoord = json.loads(
                _haal(adres, gegevens=gegevens, koppen=koppen, limiet=15).decode("utf-8", "replace")
            )
        except Exception:
            continue
        # een leeg antwoord kan ook betekenen dat deze spiegel jouw land niet kent
        if antwoord.get("elements"):
            return antwoord
        laatste = antwoord
    if laatste is None:
        raise ValueError("openstreetmap is even overbelast; probeer het over een minuut opnieuw")
    return laatste


def _afstand_km(breedte: float, lengte: float, lat: float, lon: float) -> float:
    dy = (lat - breedte) * 111
    dx = (lon - lengte) * 111 * 0.62
    return (dy * dy + dx * dx) ** 0.5


def gereedschap_laadpalen(plaats: str) -> dict:
    """Laadpalen in de buurt (OpenStreetMap)."""
    plaats = (plaats or "").strip() or GEREEDSCHAP_PLAATS
    if not plaats:
        return {"naam": "laadpalen", "bron": "openstreetmap.org", "tekst": "", "fout": "geen plaats opgegeven"}
    try:
        breedte, lengte, nette = _coordinaten(plaats)
        gegevens = _overpass(
            f'[out:json][timeout:20];node["amenity"="charging_station"](around:1500,{breedte},{lengte});out 15;'
        )
    except Exception as fout:
        return {"naam": "laadpalen", "bron": "openstreetmap.org", "tekst": "", "fout": str(fout)}
    palen = []
    for element in (gegevens or {}).get("elements") or []:
        lat, lon = element.get("lat"), element.get("lon")
        if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
            continue
        kenmerken = element.get("tags") or {}
        naam = kenmerken.get("name") or kenmerken.get("operator") or "laadpaal"
        aantal = kenmerken.get("capacity") or ""
        palen.append((_afstand_km(breedte, lengte, lat, lon), naam, aantal))
    if not palen:
        return {"naam": "laadpalen", "bron": "openstreetmap.org",
                "tekst": f"Geen laadpalen gevonden binnen 1,5 km van {nette}."}
    palen.sort()
    regels = [f"{naam} ({_getal(afstand, ' km')}" + (f", {aantal} punten)" if aantal else ")")
              for afstand, naam, aantal in palen[:6]]
    return {
        "naam": "laadpalen",
        "bron": "openstreetmap.org (Overpass, ODbL)",
        "tekst": f"{len(palen)} laadpalen binnen 1,5 km van {nette}: " + ", ".join(regels),
    }


def gereedschap_parkeren(plaats: str) -> dict:
    """Parkeergarages en -terreinen in de buurt (OpenStreetMap)."""
    plaats = (plaats or "").strip() or GEREEDSCHAP_PLAATS
    if not plaats:
        return {"naam": "parkeren", "bron": "openstreetmap.org", "tekst": "", "fout": "geen plaats opgegeven"}
    try:
        breedte, lengte, nette = _coordinaten(plaats)
        gegevens = _overpass(
            f'[out:json][timeout:25];node["amenity"="parking"](around:1500,{breedte},{lengte});out 30;'
        )
    except Exception as fout:
        return {"naam": "parkeren", "bron": "openstreetmap.org", "tekst": "", "fout": str(fout)}
    plekken = []
    for element in (gegevens or {}).get("elements") or []:
        lat, lon = element.get("lat"), element.get("lon")
        if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
            continue
        kenmerken = element.get("tags") or {}
        naam = kenmerken.get("name")
        if not naam:
            continue
        tarief = {"yes": "betaald", "no": "gratis"}.get(str(kenmerken.get("fee") or "").lower(), "")
        overdekt = "garage" if str(kenmerken.get("parking") or "") == "multi-storey" else ""
        extra = ", ".join(stuk for stuk in (tarief, overdekt) if stuk)
        plekken.append((_afstand_km(breedte, lengte, lat, lon), naam, extra))
    if not plekken:
        return {"naam": "parkeren", "bron": "openstreetmap.org",
                "tekst": f"Geen parkeerplaatsen met een naam gevonden binnen 1,5 km van {nette}."}
    plekken.sort()
    regels = [f"{naam} ({_getal(afstand, ' km')}{', ' + extra if extra else ''})"
              for afstand, naam, extra in plekken[:6]]
    return {
        "naam": "parkeren",
        "bron": "openstreetmap.org (Overpass, ODbL)",
        "tekst": f"Parkeren bij {nette}: " + ", ".join(regels) + ". Tarieven staan er alleen bij als iemand ze invulde.",
    }


def gereedschap_vorigjaar(plaats: str) -> dict:
    """Het weer op deze dag vorig jaar (Open-Meteo archief)."""
    plaats = (plaats or "").strip() or GEREEDSCHAP_PLAATS
    if not plaats:
        return {"naam": "vorigjaar", "bron": "open-meteo.com", "tekst": "", "fout": "geen plaats opgegeven"}
    try:
        breedte, lengte, nette = _coordinaten(plaats)
    except Exception as fout:
        return {"naam": "vorigjaar", "bron": "open-meteo.com", "tekst": "", "fout": str(fout)}
    vandaag = datetime.date.today()
    regels = []
    for jaar in (vandaag.year - 1, vandaag.year - 2):
        dag = vandaag.replace(year=jaar)
        gegevens = _haal_json(
            "https://archive-api.open-meteo.com/v1/archive?"
            f"latitude={breedte}&longitude={lengte}&start_date={dag.isoformat()}&end_date={dag.isoformat()}"
            "&daily=temperature_2m_max,temperature_2m_min,precipitation_sum&timezone=Europe/Amsterdam"
        )
        dagcijfers = (gegevens or {}).get("daily") or {}
        hoog = (dagcijfers.get("temperature_2m_max") or [None])[0]
        laag = (dagcijfers.get("temperature_2m_min") or [None])[0]
        regen = (dagcijfers.get("precipitation_sum") or [None])[0]
        if isinstance(hoog, (int, float)):
            regels.append(
                f"{jaar}: {_getal(laag)} tot {_getal(hoog, '°C')}"
                + (f", {_getal(regen, ' mm regen')}" if isinstance(regen, (int, float)) else "")
            )
    if not regels:
        return {"naam": "vorigjaar", "bron": "open-meteo.com (archief)", "tekst": "",
                "fout": f"geen archiefgegevens voor {nette}"}
    return {
        "naam": "vorigjaar",
        "bron": "open-meteo.com (archief, ERA5)",
        "tekst": f"Op deze dag in {nette}: " + " | ".join(regels) + ".",
    }


def gereedschap_btw(verzoek: str) -> dict:
    """Btw en percentages uitrekenen, ook andersom."""
    verzoek = (verzoek or "").strip()
    laag = verzoek.lower().replace(",", ".")
    bedragen = [float(getal) for getal in re.findall(r"\d+(?:\.\d+)?", laag)]
    if not bedragen:
        return {"naam": "btw", "bron": "de engine zelf", "tekst": "",
                "fout": "noem een bedrag, bijvoorbeeld 'btw over 250 euro'"}
    bedrag = bedragen[0]
    tarief = 21.0
    for kandidaat in (21, 9, 0):
        if re.search(rf"\b{kandidaat}\s*%", laag):
            tarief = float(kandidaat)
            break
    if "inclusief" in laag or "incl." in laag or "met btw" in laag:
        kaal = bedrag / (1 + tarief / 100)
        btw = bedrag - kaal
        tekst = (
            f"{_getal(bedrag, ' euro')} inclusief {tarief:.0f}% btw is "
            f"{_getal(kaal, ' euro')} exclusief btw; de btw is {_getal(btw, ' euro')}."
        )
    elif "% van" in laag and len(bedragen) >= 2:
        deel = bedragen[0] / 100 * bedragen[1]
        tekst = f"{_getal(bedragen[0], '%')} van {_getal(bedragen[1], ' euro')} is {_getal(deel, ' euro')}."
    else:
        btw = bedrag * tarief / 100
        tekst = (
            f"Btw over {_getal(bedrag, ' euro')} bij {tarief:.0f}% is {_getal(btw, ' euro')}; "
            f"in totaal {_getal(bedrag + btw, ' euro')}."
        )
    return {"naam": "btw", "bron": "de engine zelf", "tekst": tekst}


def gereedschap_inflatie() -> dict:
    """De inflatie in Nederland volgens het CBS."""
    gegevens = _haal_json(
        "https://opendata.cbs.nl/ODataApi/odata/83131NED/TypedDataSet?"
        "%24filter=startswith(Bestedingscategorieen%2C%27T001112%27)"
        "&%24select=Perioden%2CCPI_1%2CJaarmutatieCPI_5%2CMaandmutatieCPI_3"
    )
    rijen = [rij for rij in ((gegevens or {}).get("value") or [])
             if isinstance(rij.get("JaarmutatieCPI_5"), (int, float))]
    if not rijen:
        return {"naam": "inflatie", "bron": "opendata.cbs.nl", "tekst": "", "fout": "geen cijfers ontvangen"}
    rijen.sort(key=lambda rij: str(rij.get("Perioden") or ""))
    laatste = rijen[-1]
    vorige = rijen[-2] if len(rijen) > 1 else laatste
    periode = str(laatste.get("Perioden") or "")
    maanden = ["januari", "februari", "maart", "april", "mei", "juni", "juli",
               "augustus", "september", "oktober", "november", "december"]
    maand = periode
    of_maand = re.search(r"(\d{4})MM(\d{2})", periode)
    if of_maand:
        maand = f"{maanden[int(of_maand.group(2)) - 1]} {of_maand.group(1)}"
    return {
        "naam": "inflatie",
        "bron": f"opendata.cbs.nl (tabel 83131NED, {periode})",
        "tekst": (
            f"Inflatie in Nederland ({maand}): {_getal(laatste.get('JaarmutatieCPI_5'), '%')} "
            f"ten opzichte van een jaar eerder"
            + (f"; vorige maand {_getal(vorige.get('JaarmutatieCPI_5'), '%')}." if vorige is not laatste else ".")
            + f" Prijsindex {_getal(laatste.get('CPI_1'))} (2015 = 100)."
        ),
    }


def gereedschap_wikipedia(onderwerp: str) -> dict:
    """Een samenvatting van een artikel op Wikipedia (Nederlands)."""
    onderwerp = (onderwerp or "").strip()
    if not onderwerp:
        return {"naam": "wikipedia", "bron": "nl.wikipedia.org", "tekst": "", "fout": "geen onderwerp opgegeven"}
    gegevens = _haal_json(
        "https://nl.wikipedia.org/w/api.php?action=query&format=json&generator=search"
        f"&gsrsearch={urllib.parse.quote(onderwerp)}&gsrlimit=1&prop=extracts&exintro=1&explaintext=1"
    )
    paginas = ((gegevens or {}).get("query") or {}).get("pages") or {}
    for pagina in paginas.values():
        tekst = _schoon(str(pagina.get("extract") or ""))
        if tekst:
            return {
                "naam": "wikipedia",
                "bron": f"nl.wikipedia.org/wiki/{urllib.parse.quote(str(pagina.get('title') or onderwerp))}",
                "tekst": f"{pagina.get('title')}: {_kort(tekst, 900)}",
            }
    return {"naam": "wikipedia", "bron": "nl.wikipedia.org", "tekst": "",
            "fout": f"geen artikel gevonden over '{onderwerp}'"}


def gereedschap_prijswacht(zoekterm: str) -> dict:
    """Wat iets tweedehands kost op Marktplaats (goedkoopste advertenties)."""
    zoekterm = (zoekterm or "").strip()
    if not zoekterm:
        return {"naam": "prijswacht", "bron": "marktplaats.nl", "tekst": "",
                "fout": "noem wat je zoekt, bijvoorbeeld 'rtx 3060'"}
    gegevens = _haal_json(
        f"{MARKTPLAATS_LRP}?query={urllib.parse.quote(zoekterm)}&limit=100",
        koppen={"Accept": "application/json"},
    )
    woorden = [woord.lower() for woord in re.findall(r"[a-z0-9]+", zoekterm.lower()) if len(woord) > 1]
    advertenties = []
    for advertentie in (gegevens or {}).get("listings") or []:
        centen = (advertentie.get("priceInfo") or {}).get("priceCents")
        if not isinstance(centen, (int, float)) or centen <= 0:
            continue
        titel_laag = str(advertentie.get("title") or "").lower()
        if woorden and not all(woord in titel_laag for woord in woorden):
            continue
        advertenties.append(advertentie)
    if not advertenties:
        return {"naam": "prijswacht", "bron": "marktplaats.nl", "tekst": "",
                "fout": f"geen advertenties met een prijs waarin alle woorden van '{zoekterm}' staan"}
    advertenties.sort(key=lambda advertentie: (advertentie.get("priceInfo") or {}).get("priceCents") or 0)
    if not advertenties:
        return {"naam": "prijswacht", "bron": "marktplaats.nl", "tekst": "",
                "fout": f"geen advertenties gevonden voor '{zoekterm}'"}
    regels = []
    for advertentie in advertenties:
        prijsinfo = advertentie.get("priceInfo") or {}
        centen = prijsinfo.get("priceCents")
        if not isinstance(centen, (int, float)) or centen <= 0:
            continue
        plaats = (advertentie.get("location") or {}).get("cityName") or "?"
        titel = _kort(str(advertentie.get("title") or "?"), 52)
        regels.append(f"{_getal(centen / 100, ' euro')} — {titel} ({plaats})")
        if len(regels) >= 5:
            break
    if not regels:
        return {"naam": "prijswacht", "bron": "marktplaats.nl", "tekst": "",
                "fout": f"geen advertenties met een prijs voor '{zoekterm}'"}
    totaal = (gegevens or {}).get("totalResultCount")
    prijzen = sorted(
        (advertentie.get("priceInfo") or {}).get("priceCents") / 100 for advertentie in advertenties
    )
    mediaan = prijzen[len(prijzen) // 2] if prijzen else 0
    return {
        "naam": "prijswacht",
        "bron": f"marktplaats.nl (lrp-api, {totaal} advertenties gevonden)",
        "tekst": (
            f"Marktplaats voor '{zoekterm}': mediaan {_getal(mediaan, ' euro')} "
            f"over {len(prijzen)} advertenties. Goedkoopste: " + " | ".join(regels)
        ),
    }


def _afsluitingen_ruw() -> str:
    nu = time.time()
    if AFSLUITINGEN_CACHE["ruw"] and nu - AFSLUITINGEN_CACHE["tijd"] < 900:
        return str(AFSLUITINGEN_CACHE["ruw"])
    ruw = gzip.decompress(_haal(AFSLUITINGEN_FEED)).decode("utf-8", "replace")
    AFSLUITINGEN_CACHE["tijd"] = nu
    AFSLUITINGEN_CACHE["ruw"] = ruw
    return ruw


def gereedschap_afsluitingen(weg: str) -> dict:
    """Wegen die nu dicht zijn (NDW / Rijkswaterstaat)."""
    try:
        ruw = _afsluitingen_ruw()
    except Exception as fout:
        return {"naam": "afsluitingen", "bron": "opendata.ndw.nu", "tekst": "",
                "fout": f"{type(fout).__name__}: {fout}"}
    nu = datetime.datetime.now(datetime.timezone.utc)
    gezocht = (weg or "").strip().upper()
    gevonden = []
    for blok in re.findall(r"<sit:situationRecord\b.*?</sit:situationRecord>", ruw, re.S):
        if "roadClosed" not in blok and "closed" not in blok.lower():
            continue
        begin = re.search(r"<com:overallStartTime>([^<]+)", blok)
        eind = re.search(r"<com:overallEndTime>([^<]+)", blok)
        punt = re.search(r"<loc:latitude>([\d.\-]+)</loc:latitude>.*?<loc:longitude>([\d.\-]+)</loc:longitude>", blok, re.S)
        if not punt:
            continue
        try:
            start = datetime.datetime.fromisoformat(begin.group(1).replace("Z", "+00:00")) if begin else nu
            stop = datetime.datetime.fromisoformat(eind.group(1).replace("Z", "+00:00")) if eind else nu
        except ValueError:
            continue
        if stop < nu or start > nu + datetime.timedelta(days=2):
            continue
        waar = _weg_en_plaats(float(punt.group(1)), float(punt.group(2))) or "onbekende plek"
        if gezocht and not re.search(rf"(?<![A-Z0-9]){re.escape(gezocht)}(?![0-9])", waar.upper()):
            continue
        tot = stop.astimezone().strftime("%d-%m %H:%M") if eind else "onbekend"
        gevonden.append(f"{waar} tot {tot}")
        if len(gevonden) >= 8:
            break
    if not gevonden:
        return {
            "naam": "afsluitingen",
            "bron": "opendata.ndw.nu (NDW / Rijkswaterstaat)",
            "tekst": f"Geen afsluitingen gevonden{' op ' + weg if weg else ''}.",
        }
    return {
        "naam": "afsluitingen",
        "bron": "opendata.ndw.nu (NDW / Rijkswaterstaat) en api.pdok.nl",
        "tekst": f"Afgesloten: " + " | ".join(gevonden[:6]),
    }


def gereedschap_feed(adres: str) -> dict:
    """De laatste berichten uit een rss- of atom-feed."""
    adres = (adres or "").strip()
    if not adres.startswith(("http://", "https://")):
        return {"naam": "feed", "bron": "rss", "tekst": "",
                "fout": "geef een volledig webadres van een feed, bijvoorbeeld https://feeds.nos.nl/nosnieuwsalgemeen"}
    try:
        ruw = _haal(adres, limiet=20).decode("utf-8", "replace")
    except Exception as fout:
        return {"naam": "feed", "bron": "rss", "tekst": "", "fout": f"{type(fout).__name__}: {fout}"}
    titels = re.findall(r"<title[^>]*>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", ruw, re.S)
    titels = [_schoon(re.sub(r"<[^>]+>", " ", titel)) for titel in titels]
    titels = [titel for titel in titels if titel and len(titel) > 8][:6]
    if not titels:
        return {"naam": "feed", "bron": "rss", "tekst": "",
                "fout": "geen berichten gevonden in deze feed"}
    return {
        "naam": "feed",
        "bron": adres,
        "tekst": "Laatste berichten: " + " | ".join(titels),
    }

# ---- G. brandstof, zon, synoniemen, koken, prijsgeschiedenis, artikel,
#         persberichten, reistijd en spelling

CBS_BRANDSTOF = "https://opendata.cbs.nl/ODataApi/odata/80416ned/TypedDataSet"
GOOGLE_NIEUWS = "https://news.google.com/rss/search"
WOORDENLIJST_URL = ("https://raw.githubusercontent.com/OpenTaal/opentaal-hunspell/"
                    "master/nl.dic")
PRIJS_DB = os.environ.get(
    "WLLM_PRIJS_DB", os.path.join(os.path.expanduser("~"), ".werdens-media-llm", "prijzen.db")
)


def gereedschap_brandstof() -> dict:
    """De landelijke pompprijzen van deze week (CBS)."""
    gegevens = _haal_json(
        f"{CBS_BRANDSTOF}?%24select=Perioden%2CBenzineEuro95_1%2CDiesel_2%2CLpg_3"
    )
    rijen = [rij for rij in ((gegevens or {}).get("value") or [])
             if isinstance(rij.get("BenzineEuro95_1"), (int, float))]
    if not rijen:
        return {"naam": "brandstof", "bron": "opendata.cbs.nl", "tekst": "", "fout": "geen prijzen ontvangen"}
    rijen.sort(key=lambda rij: str(rij.get("Perioden") or ""))
    nieuwste = rijen[-1]
    periode = str(nieuwste.get("Perioden") or "")
    week = f"{periode[6:8]}-{periode[4:6]}-{periode[:4]}" if len(periode) == 8 else periode
    ouder = rijen[-2] if len(rijen) > 1 else nieuwste

    def _per_liter(waarde: object) -> str:
        return f"{float(waarde):.3f}".replace(".", ",") if isinstance(waarde, (int, float)) else "?"

    richting = ""
    if isinstance(ouder.get("BenzineEuro95_1"), (int, float)) and isinstance(nieuwste.get("BenzineEuro95_1"), (int, float)):
        verschil = nieuwste["BenzineEuro95_1"] - ouder["BenzineEuro95_1"]
        if abs(verschil) >= 0.005:
            richting = f" Dat is {abs(verschil):.3f}".replace(".", ",") + " euro " + ("duurder" if verschil > 0 else "goedkoper") + " dan vorige week."
    return {
        "naam": "brandstof",
        "bron": f"opendata.cbs.nl (tabel 80416ned, week van {week})",
        "tekst": (
            f"Pompprijzen (landelijk gemiddelde): Euro95 {_per_liter(nieuwste.get('BenzineEuro95_1'))} euro, "
            f"diesel {_per_liter(nieuwste.get('Diesel_2'))} euro, "
            f"lpg {_per_liter(nieuwste.get('Lpg_3'))} euro per liter." + richting
        ),
    }


def gereedschap_zon(plaats: str) -> dict:
    """Zonsopkomst, zonsondergang en het gouden uur (Open-Meteo)."""
    plaats = (plaats or "").strip() or GEREEDSCHAP_PLAATS
    if not plaats:
        return {"naam": "zon", "bron": "open-meteo.com", "tekst": "", "fout": "geen plaats opgegeven"}
    try:
        breedte, lengte, nette = _coordinaten(plaats)
    except Exception as fout:
        return {"naam": "zon", "bron": "open-meteo.com", "tekst": "", "fout": str(fout)}
    gegevens = _haal_json(
        "https://api.open-meteo.com/v1/forecast?"
        f"latitude={breedte}&longitude={lengte}"
        "&daily=sunrise,sunset,daylight_duration&forecast_days=3&timezone=Europe/Amsterdam"
    )
    dagen = (gegevens or {}).get("daily") or {}
    tijden = dagen.get("time") or []
    if not tijden:
        return {"naam": "zon", "bron": "open-meteo.com", "tekst": "", "fout": "geen gegevens"}
    regels = []
    gouden = ""
    for nummer, dag in enumerate(tijden):
        op = str((dagen.get("sunrise") or [""])[nummer])[11:16]
        onder = str((dagen.get("sunset") or [""])[nummer])[11:16]
        licht = (dagen.get("daylight_duration") or [0])[nummer]
        if not op or not onder:
            continue
        if nummer == 0:
            uur, minuut = (int(deel) for deel in onder.split(":"))
            gouden = f" Gouden uur van {(uur - 1) % 24:02d}:{minuut:02d} tot {onder}."
        regels.append(f"{dag[8:10]}-{dag[5:7]}: op {op}, onder {onder}"
                      + (f" ({_getal((licht or 0) / 3600, ' uur licht')})" if licht else ""))
    if not regels:
        return {"naam": "zon", "bron": "open-meteo.com", "tekst": "", "fout": "geen zonsgegevens"}
    return {
        "naam": "zon",
        "bron": "open-meteo.com",
        "tekst": f"Zon bij {nette}: " + " | ".join(regels) + "." + (gouden if len(regels) else ""),
    }


def gereedschap_synoniemen(woord: str) -> dict:
    """Synoniemen en betekenis van een woord (WikiWoordenboek)."""
    woord = (woord or "").strip().split()[-1] if (woord or "").strip() else ""
    if not woord:
        return {"naam": "synoniemen", "bron": "nl.wiktionary.org", "tekst": "", "fout": "geen woord opgegeven"}
    gegevens = _haal_json(
        "https://nl.wiktionary.org/w/api.php?action=query&format=json&prop=revisions&rvprop=content"
        f"&rvslots=main&titles={urllib.parse.quote(woord)}&redirects=1"
    )
    paginas = ((gegevens or {}).get("query") or {}).get("pages") or {}
    inhoud = ""
    for pagina in paginas.values():
        try:
            inhoud = pagina["revisions"][0]["slots"]["main"]["*"]
        except (KeyError, IndexError, TypeError):
            inhoud = ""
    if not inhoud:
        return {"naam": "synoniemen", "bron": "nl.wiktionary.org", "tekst": "",
                "fout": f"geen lemma gevonden voor '{woord}'"}
    blok = re.search(r"\{\{-syn-\}\}(.*?)(?=\{\{-|\Z)", inhoud, re.S)
    if not blok:
        blok = re.search(r"\{\{-?synoniem(?:en)?\|(.*?)\}\}", inhoud, re.S | re.I)
    if blok:
        stukken = []
        for link in re.findall(r"\[\[([^\]|#]+)", blok.group(1)):
            schoon = link.strip()
            if schoon and schoon not in stukken:
                stukken.append(schoon)
        if not stukken:
            for stuk in re.split(r"[,\n]", re.sub(r"\*\[\d+\]|\[\d+\]", "", blok.group(1))):
                stuk = re.sub(r"[\[\]']", "", stuk).strip(" .;")
                if stuk and len(stuk) > 1 and stuk not in stukken:
                    stukken.append(stuk)
        if stukken:
            return {
                "naam": "synoniemen",
                "bron": f"nl.wiktionary.org/wiki/{urllib.parse.quote(woord)}",
                "tekst": f"Synoniemen van '{woord}': " + ", ".join(stukken[:12]),
            }
    betekenis = re.search(r"#\s*(?:''')?([^'\n#{}]{15,220})", inhoud)
    if betekenis:
        return {
            "naam": "synoniemen",
            "bron": f"nl.wiktionary.org/wiki/{urllib.parse.quote(woord)}",
            "tekst": f"'{woord}': {_schoon(betekenis.group(1))} (geen synoniemenlijst op deze pagina).",
        }
    return {"naam": "synoniemen", "bron": "nl.wiktionary.org", "tekst": "",
            "fout": f"geen synoniemen of betekenis gevonden voor '{woord}'"}


KOKEN = {
    "ons": 100.0, "pond": 500.0, "gram": 1.0, "kilo": 1000.0, "kilogram": 1000.0,
    "milligram": 0.001, "ml": 1.0, "milliliter": 1.0, "cl": 10.0, "dl": 100.0,
    "liter": 1000.0, "l": 1000.0, "cup": 240.0, "cups": 240.0, "eetlepel": 15.0,
    "el": 15.0, "theelepel": 5.0, "tl": 5.0, "kopje": 150.0, "snufje": 0.5,
    "blikje": 330.0, "glas": 200.0, "eetlepels": 15.0, "theelepels": 5.0,
}


def gereedschap_koken(verzoek: str) -> dict:
    """Maten omrekenen voor het koken (ons, cup, eetlepel)."""
    laag = (verzoek or "").lower().replace(",", ".")
    paren = re.findall(r"(\d+(?:\.\d+)?)\s*([a-z]+)", laag)
    if not paren:
        return {"naam": "koken", "bron": "de engine zelf", "tekst": "",
                "fout": "noem een aantal en een maat, bijvoorbeeld '3 ons in gram'"}
    aantal = float(paren[0][0])
    van = paren[0][1]
    if van not in KOKEN:
        return {"naam": "koken", "bron": "de engine zelf", "tekst": "",
                "fout": f"'{van}' ken ik niet; ik ken ons, pond, gram, kilo, ml, cl, dl, liter, cup, eetlepel, theelepel, kopje, glas en snufje"}
    gram = aantal * KOKEN[van]
    naar = ""
    for _, maat in paren[1:]:
        if maat in KOKEN and maat != van:
            naar = maat
            break
    if not naar:
        # 'in ml' heeft geen getal voor de maat staan
        for maat in re.findall(r"[a-z]+", laag.split(van, 1)[-1]):
            if maat in KOKEN and maat != van:
                naar = maat
                break
    if naar:
        uitkomst = gram / KOKEN[naar]
        tekst = (
            f"{_getal(aantal)} {van} is {_getal(uitkomst)} {naar}"
            + (f" ({_getal(gram, ' gram')})" if naar not in ("gram", "kilo", "kilogram") else "")
            + "."
        )
    else:
        tekst = f"{_getal(aantal)} {van} is {_getal(gram, ' gram')} (ongeveer {_getal(gram / 15, ' eetlepels')})."
    return {
        "naam": "koken",
        "bron": "de engine zelf (1 ons = 100 g, 1 cup = 240 ml, 1 el = 15 ml, 1 tl = 5 ml)",
        "tekst": tekst + " Let op: een eetlepel bloem weegt minder dan een eetlepel water.",
    }


def _prijs_meten(zoekterm: str) -> tuple[float, int]:
    """De mediaanprijs en het aantal advertenties ophalen bij Marktplaats."""
    gegevens = _haal_json(
        f"{MARKTPLAATS_LRP}?query={urllib.parse.quote(zoekterm)}&limit=100",
        koppen={"Accept": "application/json"},
    )
    woorden = [woord.lower() for woord in re.findall(r"[a-z0-9]+", zoekterm.lower()) if len(woord) > 1]
    prijzen = []
    for advertentie in (gegevens or {}).get("listings") or []:
        centen = (advertentie.get("priceInfo") or {}).get("priceCents")
        if not isinstance(centen, (int, float)) or centen <= 0:
            continue
        titel_laag = str(advertentie.get("title") or "").lower()
        if woorden and not all(woord in titel_laag for woord in woorden):
            continue
        prijzen.append(centen / 100)
    if not prijzen:
        raise ValueError(f"geen advertenties met een prijs voor '{zoekterm}'")
    prijzen.sort()
    return prijzen[len(prijzen) // 2], len(prijzen)


def gereedschap_prijsgeschiedenis(zoekterm: str) -> dict:
    """De prijs van iets tweedehands volgen in de tijd (Marktplaats)."""
    zoekterm = (zoekterm or "").strip().lower()
    if not zoekterm:
        return {"naam": "prijsgeschiedenis", "bron": "marktplaats.nl", "tekst": "",
                "fout": "noem wat je zoekt, bijvoorbeeld 'rtx 3060 12gb'"}
    try:
        mediaan, aantal = _prijs_meten(zoekterm)
    except Exception as fout:
        return {"naam": "prijsgeschiedenis", "bron": "marktplaats.nl", "tekst": "", "fout": str(fout)}
    vandaag = datetime.date.today().isoformat()
    os.makedirs(os.path.dirname(PRIJS_DB), exist_ok=True)
    verbinding = sqlite3.connect(PRIJS_DB)
    try:
        verbinding.execute(
            "CREATE TABLE IF NOT EXISTS prijzen ("
            "dag TEXT, zoekterm TEXT, mediaan REAL, aantal INTEGER, "
            "PRIMARY KEY (dag, zoekterm))"
        )
        verbinding.execute(
            "INSERT OR REPLACE INTO prijzen (dag, zoekterm, mediaan, aantal) VALUES (?,?,?,?)",
            (vandaag, zoekterm, mediaan, aantal),
        )
        verbinding.commit()
        ouder = verbinding.execute(
            "SELECT dag, mediaan, aantal FROM prijzen WHERE zoekterm = ? AND dag < ? "
            "ORDER BY dag DESC LIMIT 1",
            (zoekterm, vandaag),
        ).fetchone()
        metingen = verbinding.execute(
            "SELECT COUNT(*) FROM prijzen WHERE zoekterm = ?", (zoekterm,)
        ).fetchone()[0]
    finally:
        verbinding.close()
    if not ouder:
        return {
            "naam": "prijsgeschiedenis",
            "bron": f"marktplaats.nl (eigen prijslogboek, {PRIJS_DB})",
            "tekst": (
                f"Vandaag: mediaan {_getal(mediaan, ' euro')} over {aantal} advertenties voor '{zoekterm}'. "
                "Dit is de eerste meting; vanaf nu hou ik het per dag bij."
            ),
        }
    dag, oud_mediaan, oud_aantal = ouder
    verschil = mediaan - float(oud_mediaan)
    percentage = (verschil / float(oud_mediaan) * 100) if oud_mediaan else 0
    richting = "gestegen" if verschil > 0 else "gedaald" if verschil < 0 else "gelijk gebleven"
    return {
        "naam": "prijsgeschiedenis",
        "bron": f"marktplaats.nl (eigen prijslogboek, {metingen} metingen)",
        "tekst": (
            f"'{zoekterm}': vandaag mediaan {_getal(mediaan, ' euro')} over {aantal} advertenties; "
            f"op {dag} was dat {_getal(oud_mediaan, ' euro')} over {oud_aantal}. "
            f"Dus {richting} met {_getal(abs(percentage), '%')}."
        ),
    }


def gereedschap_artikel() -> dict:
    """Wat er vandaag op Wikipedia gelezen wordt."""
    gegevens = _haal_json("https://nl.wikipedia.org/api/rest_v1/feed/featured/" +
                          datetime.date.today().strftime("%Y/%m/%d"))
    lijst = ((gegevens or {}).get("mostread") or {}).get("articles") or []
    if not lijst:
        gegevens = _haal_json("https://nl.wikipedia.org/api/rest_v1/page/random/summary")
        if gegevens and gegevens.get("extract"):
            return {
                "naam": "artikel",
                "bron": f"nl.wikipedia.org/wiki/{urllib.parse.quote(str(gegevens.get('title') or ''))}",
                "tekst": f"{gegevens.get('title')}: {_kort(_schoon(str(gegevens.get('extract'))), 700)}",
            }
        return {"naam": "artikel", "bron": "nl.wikipedia.org", "tekst": "", "fout": "geen artikelen ontvangen"}
    regels = []
    for artikel in lijst[:5]:
        titel = (artikel.get("titles") or {}).get("normalized")
        beschrijving = _schoon(str(artikel.get("description") or ""))
        if titel:
            regels.append(f"{titel} ({beschrijving})" if beschrijving else str(titel))
    if not regels:
        return {"naam": "artikel", "bron": "nl.wikipedia.org", "tekst": "", "fout": "geen titels gevonden"}
    return {
        "naam": "artikel",
        "bron": "nl.wikipedia.org (meest gelezen vandaag)",
        "tekst": "Meest gelezen op Wikipedia: " + " | ".join(regels),
    }


def gereedschap_persberichten() -> dict:
    """Persberichten van de Rijksoverheid."""
    gegevens = _haal(
        f"{GOOGLE_NIEUWS}?q=site:rijksoverheid.nl&hl=nl&gl=NL&ceid=NL:nl", limiet=25
    ).decode("utf-8", "replace")
    titels = re.findall(r"<item>.*?<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", gegevens, re.S)
    titels = [_schoon(re.sub(r"\s+-\s+Rijksoverheid\.nl$", "", re.sub(r"<[^>]+>", " ", titel)))
              for titel in titels]
    titels = [titel for titel in titels if titel and "Google" not in titel][:6]
    if not titels:
        return {"naam": "persberichten", "bron": "rijksoverheid.nl", "tekst": "",
                "fout": "geen persberichten gevonden"}
    return {
        "naam": "persberichten",
        "bron": "rijksoverheid.nl (via de nieuwsfeed van Google Nieuws)",
        "tekst": "Van de Rijksoverheid: " + " | ".join(titels),
    }


def gereedschap_reistijd(verzoek: str) -> dict:
    """Hoe lang je onderweg bent, en of er files staan op die route."""
    punten = re.split(r"\bnaar\b", verzoek or "", maxsplit=1, flags=re.I)
    if len(punten) < 2:
        return {"naam": "reistijd", "bron": "osrm + ndw", "tekst": "",
                "fout": "vraag het als 'reistijd van Utrecht naar Maastricht'"}
    van = re.sub(r"^.*?\bvan\b", "", punten[0], flags=re.I).strip(" ,?:")
    naar = punten[1].strip(" ,?.")
    if not van or not naar:
        return {"naam": "reistijd", "bron": "osrm + ndw", "tekst": "",
                "fout": "ik kon de plaatsen niet lezen; probeer 'reistijd van Utrecht naar Maastricht'"}
    try:
        van_breedte, van_lengte, van_nette = _coordinaten(van)
        naar_breedte, naar_lengte, naar_nette = _coordinaten(naar)
    except Exception as fout:
        return {"naam": "reistijd", "bron": "osrm + ndw", "tekst": "", "fout": str(fout)}
    route = _haal_json(
        "https://router.project-osrm.org/route/v1/driving/"
        f"{van_lengte},{van_breedte};{naar_lengte},{naar_breedte}?overview=false&steps=true"
    )
    eerste = ((route or {}).get("routes") or [{}])[0]
    afstand = eerste.get("distance")
    duur = eerste.get("duration")
    if not isinstance(afstand, (int, float)) or not isinstance(duur, (int, float)):
        return {"naam": "reistijd", "bron": "osrm + ndw", "tekst": "",
                "fout": f"geen route gevonden van {van_nette} naar {naar_nette}"}
    wegen = []
    for been in eerste.get("legs") or []:
        for stap in been.get("steps") or []:
            for stuk in str(stap.get("ref") or "").split(";"):
                stuk = stuk.strip().upper()
                if stuk and stuk not in wegen:
                    wegen.append(stuk)
    files = ""
    if wegen:
        try:
            ruw = _haal("https://opendata.ndw.nu/actueel_beeld.xml.gz", limiet=40)
            beeld = gzip.decompress(ruw).decode("utf-8", "replace")
            gevonden = []
            for blok in re.findall(r"<sit:situationRecord\b.*?</sit:situationRecord>", beeld, re.S):
                for weg in wegen:
                    if re.search(rf"(?<![A-Z0-9]){re.escape(weg)}(?![0-9])", blok.upper()):
                        melding = re.search(r"<sit:value>([^<]{10,120})</sit:value>", blok)
                        if melding and weg not in gevonden:
                            gevonden.append(weg)
                        break
            if gevonden:
                files = f" Let op: meldingen op " + ", ".join(gevonden) + "."
        except Exception:
            files = ""
    return {
        "naam": "reistijd",
        "bron": "router.project-osrm.org (OpenStreetMap) en opendata.ndw.nu",
        "tekst": (
            f"{van_nette} naar {naar_nette}: {_getal((afstand or 0) / 1000, ' km')}, "
            f"ongeveer {int(round((duur or 0) / 60))} minuten zonder vertraging"
            + (f" via {', '.join(wegen[:6])}." if wegen else ".")
            + files
        ),
    }


WOORDEN_CACHE: dict = {"lijst": None}


def _woordenlijst() -> set:
    if WOORDEN_CACHE["lijst"] is not None:
        return WOORDEN_CACHE["lijst"]
    bestand = os.path.join(os.path.dirname(PRIJS_DB), "woordenlijst.txt")
    ruw = ""
    if os.path.exists(bestand) and os.path.getsize(bestand) > 100000:
        with open(bestand, encoding="utf-8", errors="replace") as invoer:
            ruw = invoer.read()
    else:
        ruw = _haal(WOORDENLIJST_URL, limiet=90).decode("utf-8", "replace")
        try:
            os.makedirs(os.path.dirname(bestand), exist_ok=True)
            with open(bestand, "w", encoding="utf-8") as uitvoer:
                uitvoer.write(ruw)
        except OSError:
            pass
    woorden = set()
    for regel in ruw.split("\n")[1:]:
        woord = regel.split("/")[0].strip().lower()
        if len(woord) > 1 and not woord.isdigit():
            woorden.add(woord)
    WOORDEN_CACHE["lijst"] = woorden
    return woorden


def gereedschap_spelling(tekst: str) -> dict:
    """Woorden die niet in de Nederlandse woordenlijst staan (OpenTaal)."""
    tekst = (tekst or "").strip()
    if len(tekst) < 3:
        return {"naam": "spelling", "bron": "OpenTaal", "tekst": "",
                "fout": "geef de tekst die ik moet controleren"}
    try:
        woordenlijst = _woordenlijst()
    except Exception as fout:
        return {"naam": "spelling", "bron": "OpenTaal", "tekst": "",
                "fout": f"woordenlijst niet op te halen ({type(fout).__name__})"}
    if not woordenlijst:
        return {"naam": "spelling", "bron": "OpenTaal", "tekst": "", "fout": "lege woordenlijst"}
    verdacht = []
    for woord in re.findall(r"[A-Za-zÀ-ÿ'’-]{2,}", tekst):
        kaal = woord.strip("-'’").lower()
        if not kaal or kaal.isdigit():
            continue
        varianten = {
            kaal, kaal.rstrip("s"), kaal + "en", kaal + "e", kaal + "s",
            kaal[:-1] if kaal.endswith("e") else kaal,
            kaal[:-2] if kaal.endswith("en") else kaal,
            kaal[:-1] if kaal.endswith("n") else kaal,
        }
        if varianten & woordenlijst:
            continue
        if kaal not in verdacht:
            verdacht.append(kaal)
    if not verdacht:
        return {
            "naam": "spelling",
            "bron": f"OpenTaal-woordenlijst ({len(woordenlijst)} woorden)",
            "tekst": "Alle woorden in deze tekst staan in de Nederlandse woordenlijst.",
        }
    return {
        "naam": "spelling",
        "bron": f"OpenTaal-woordenlijst ({len(woordenlijst)} woorden)",
        "tekst": (
            f"{len(verdacht)} {'woord staat' if len(verdacht) == 1 else 'woorden staan'} niet in de woordenlijst: "
            + ", ".join(verdacht[:15])
            + ". Dat kunnen ook namen, samenstellingen of vaktaal zijn."
        ),
    }

# ---- H. water, weer op detail, en alles wat met reizen te maken heeft

def _osm_nabij(plaats: str, tag: str, straal: int = 1500, aantal: int = 12) -> tuple[list, str]:
    """Plekken met een OSM-kenmerk in de buurt van een plaats, met afstand."""
    breedte, lengte, nette = _coordinaten(plaats)
    vraag = (f'[out:json][timeout:20];nwr[{tag}](around:{straal},{breedte},{lengte});'
             f'out center {aantal * 3};')
    gegevens = _overpass(vraag)
    plekken = []
    for element in (gegevens or {}).get("elements") or []:
        lat = element.get("lat")
        lon = element.get("lon")
        if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
            midden = element.get("center") or {}
            lat, lon = midden.get("lat"), midden.get("lon")
        if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
            continue
        kenmerken = element.get("tags") or {}
        naam = (kenmerken.get("name") or kenmerken.get("operator") or kenmerken.get("brand")
                or kenmerken.get("ref") or "")
        plekken.append((_afstand_km(breedte, lengte, lat, lon), naam, kenmerken))
    plekken.sort(key=lambda stuk: stuk[0])
    return plekken, nette


def gereedschap_regenradar(plaats: str) -> dict:
    """Waar het nu regent, volgens de neerslagmeters van Buienradar."""
    gegevens = _haal_json("https://data.buienradar.nl/2.0/feed/json")
    metingen = ((gegevens or {}).get("actual") or {}).get("stationmeasurements") or []
    if not metingen:
        return {"naam": "regenradar", "bron": "buienradar.nl", "tekst": "", "fout": "geen metingen ontvangen"}
    alles = []
    for meting in metingen:
        regen = meting.get("rainFallLastHour")
        if regen is None:
            regen = meting.get("rainfall")
        lat = meting.get("lat", meting.get("latitude"))
        lon = meting.get("lon", meting.get("longitude"))
        if isinstance(regen, (int, float)) and isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
            alles.append((float(regen), str(meting.get("stationname") or "?"), lat, lon))
    nat = [stuk for stuk in alles if stuk[0] > 0.05]
    regels = []
    eigen = ""
    plaats = (plaats or "").strip() or GEREEDSCHAP_PLAATS
    if plaats:
        try:
            breedte, lengte, nette = _coordinaten(plaats)
            dichtst = min(alles, key=lambda stuk: _afstand_km(breedte, lengte, stuk[2], stuk[3]))
            eigen = (f" Bij {nette} (meter {dichtst[1]}) valt "
                     + (f"{_getal(dichtst[0], ' mm per uur')}." if dichtst[0] > 0.05 else "niets."))
        except Exception:
            eigen = ""
    for regen, naam, _, _ in sorted(nat, reverse=True)[:5]:
        regels.append(f"{naam} {_getal(regen, ' mm/u')}")
    if not regels:
        return {
            "naam": "regenradar",
            "bron": "buienradar.nl (neerslagmeters)",
            "tekst": f"Het regent nu nergens boven Nederland volgens {len(alles)} meetstations." + eigen,
        }
    return {
        "naam": "regenradar",
        "bron": f"buienradar.nl ({len(nat)} van {len(alles)} meetstations met regen)",
        "tekst": "Natste plekken nu: " + ", ".join(regels) + "." + eigen,
    }


def gereedschap_neerslag_kwartier(plaats: str) -> dict:
    """De neerslag per vijf minuten voor de komende twee uur."""
    plaats = (plaats or "").strip() or GEREEDSCHAP_PLAATS
    if not plaats:
        return {"naam": "neerslag", "bron": "buienradar.nl", "tekst": "", "fout": "geen plaats opgegeven"}
    try:
        breedte, lengte, nette = _coordinaten(plaats)
    except Exception as fout:
        return {"naam": "neerslag", "bron": "buienradar.nl", "tekst": "", "fout": str(fout)}
    ruw = _haal(f"https://gpsgadget.buienradar.nl/data/raintext/?lat={breedte}&lon={lengte}",
                limiet=20).decode("utf-8", "replace")
    reeks = []
    for regel in ruw.split("\n"):
        if "|" not in regel:
            continue
        waarde, _, tijd = regel.partition("|")
        try:
            mm = 10 ** ((int(waarde) - 109) / 32)
        except ValueError:
            continue
        reeks.append((tijd.strip(), mm))
    if not reeks:
        return {"naam": "neerslag", "bron": "buienradar.nl", "tekst": "",
                "fout": f"geen neerslagverwachting voor {nette}"}
    nat = [(tijd, mm) for tijd, mm in reeks if mm > 0.1]
    eerste_droog = ""
    for nummer, (tijd, mm) in enumerate(reeks):
        if mm <= 0.1 and nummer > 0 and all(stuk[1] <= 0.1 for stuk in reeks[nummer:]):
            eerste_droog = tijd
            break
    regels = [f"{tijd} {mm:.1f}".replace(".", ",") + " mm/u" for tijd, mm in reeks[:8] if mm > 0.1]
    if nat:
        tekst = (f"Neerslag bij {nette}: " + " | ".join(regels[:6])
                 + (f". Daarna droog vanaf {eerste_droog}." if eerste_droog else ". Het blijft de komende twee uur nat."))
    else:
        tekst = f"Het blijft de komende twee uur droog bij {nette}."
    return {"naam": "neerslag", "bron": "buienradar.nl (5-minutenverwachting)", "tekst": tekst}


def gereedschap_windstoten(plaats: str) -> dict:
    """Hoe hard het waait, met de uitschieters erbij (Open-Meteo)."""
    plaats = (plaats or "").strip() or GEREEDSCHAP_PLAATS
    if not plaats:
        return {"naam": "windstoten", "bron": "open-meteo.com", "tekst": "", "fout": "geen plaats opgegeven"}
    try:
        breedte, lengte, nette = _coordinaten(plaats)
    except Exception as fout:
        return {"naam": "windstoten", "bron": "open-meteo.com", "tekst": "", "fout": str(fout)}
    gegevens = _haal_json(
        "https://api.open-meteo.com/v1/forecast?"
        f"latitude={breedte}&longitude={lengte}"
        "&hourly=wind_speed_10m,wind_gusts_10m&forecast_days=1&timezone=Europe/Amsterdam"
    )
    per_uur = (gegevens or {}).get("hourly") or {}
    tijden = per_uur.get("time") or []
    paren = []
    for nummer, moment in enumerate(tijden):
        wind = (per_uur.get("wind_speed_10m") or [None])[nummer]
        stoot = (per_uur.get("wind_gusts_10m") or [None])[nummer]
        if isinstance(wind, (int, float)) and isinstance(stoot, (int, float)):
            paren.append((moment, wind, stoot))
    if not paren:
        return {"naam": "windstoten", "bron": "open-meteo.com", "tekst": "", "fout": "geen windgegevens"}
    hoogste = max(paren, key=lambda stuk: stuk[2])
    nu_uur = datetime.datetime.now().strftime("%Y-%m-%dT%H:00")
    nu = next((stuk for stuk in paren if stuk[0] == nu_uur), paren[0])
    oordeel = ("krachtig" if hoogste[2] >= 75 else "hard" if hoogste[2] >= 50
               else "stevig" if hoogste[2] >= 35 else "rustig")
    return {
        "naam": "windstoten",
        "bron": "open-meteo.com",
        "tekst": (
            f"Wind bij {nette}: nu {_getal(nu[1], ' km/u')} met stoten tot {_getal(nu[2], ' km/u')}. "
            f"Vandaag het hardst rond {hoogste[0][11:16]} met {_getal(hoogste[2], ' km/u')} — dat is {oordeel}."
        ),
    }


def gereedschap_uv_uur(plaats: str) -> dict:
    """De zonkracht per uur, en wanneer hij het hoogst is."""
    plaats = (plaats or "").strip() or GEREEDSCHAP_PLAATS
    if not plaats:
        return {"naam": "uv", "bron": "open-meteo.com", "tekst": "", "fout": "geen plaats opgegeven"}
    try:
        breedte, lengte, nette = _coordinaten(plaats)
    except Exception as fout:
        return {"naam": "uv", "bron": "open-meteo.com", "tekst": "", "fout": str(fout)}
    gegevens = _haal_json(
        "https://api.open-meteo.com/v1/forecast?"
        f"latitude={breedte}&longitude={lengte}&hourly=uv_index&forecast_days=2&timezone=Europe/Amsterdam"
    )
    per_uur = (gegevens or {}).get("hourly") or {}
    tijden = per_uur.get("time") or []
    waarden = per_uur.get("uv_index") or []
    vandaag = [(tijd[11:16], waarde) for tijd, waarde in zip(tijden, waarden)
               if isinstance(waarde, (int, float)) and tijd[:10] == datetime.date.today().isoformat()]
    if not vandaag:
        return {"naam": "uv", "bron": "open-meteo.com", "tekst": "", "fout": "geen uv-gegevens"}
    piek = max(vandaag, key=lambda stuk: stuk[1])
    oordeel = ("zeer hoog" if piek[1] >= 8 else "hoog" if piek[1] >= 6
               else "matig" if piek[1] >= 3 else "laag")
    return {
        "naam": "uv",
        "bron": "open-meteo.com",
        "tekst": (
            f"Zonkracht bij {nette}: hoogst rond {piek[0]} met {_getal(piek[1])} ({oordeel}). "
            + " | ".join(f"{tijd} {_getal(waarde)}" for tijd, waarde in vandaag[::3] if waarde > 0)
        ),
    }


def gereedschap_verbrandingstijd(plaats: str) -> dict:
    """Hoe lang je in de zon kunt blijven zonder te verbranden."""
    plaats = (plaats or "").strip() or GEREEDSCHAP_PLAATS
    if not plaats:
        return {"naam": "verbrandingstijd", "bron": "open-meteo.com", "tekst": "",
                "fout": "geen plaats opgegeven"}
    try:
        breedte, lengte, nette = _coordinaten(plaats)
    except Exception as fout:
        return {"naam": "verbrandingstijd", "bron": "open-meteo.com", "tekst": "", "fout": str(fout)}
    gegevens = _haal_json(
        "https://api.open-meteo.com/v1/forecast?"
        f"latitude={breedte}&longitude={lengte}&current=uv_index&timezone=Europe/Amsterdam"
    )
    uv = ((gegevens or {}).get("current") or {}).get("uv_index")
    if not isinstance(uv, (int, float)):
        return {"naam": "verbrandingstijd", "bron": "open-meteo.com", "tekst": "", "fout": "geen uv-waarde"}
    if uv < 1:
        tekst = f"Zonkracht bij {nette} is nu {_getal(uv)} — je verbrandt nu niet."
    else:
        minuten = int(round(200 / uv))
        tekst = (
            f"Zonkracht bij {nette} is nu {_getal(uv)}. Een huid die snel verbrandt (type 2) "
            f"houdt het ongeveer {minuten} minuten vol zonder zonnebrand; met factor 20 "
            f"is dat ongeveer {minuten * 20 // 60} uur, met factor 50 ongeveer {minuten * 50 // 60} uur."
        )
    return {
        "naam": "verbrandingstijd",
        "bron": "open-meteo.com + eigen berekening (200 gedeeld door de uv-index)",
        "tekst": tekst,
    }


def gereedschap_klimaat(plaats: str) -> dict:
    """Hoe warm en nat de laatste twaalf maanden waren, per maand."""
    plaats = (plaats or "").strip() or GEREEDSCHAP_PLAATS
    if not plaats:
        return {"naam": "klimaat", "bron": "open-meteo.com archief", "tekst": "", "fout": "geen plaats opgegeven"}
    try:
        breedte, lengte, nette = _coordinaten(plaats)
    except Exception as fout:
        return {"naam": "klimaat", "bron": "open-meteo.com archief", "tekst": "", "fout": str(fout)}
    vandaag = datetime.date.today()
    begin = (vandaag.replace(day=1) - datetime.timedelta(days=365)).replace(day=1)
    gegevens = _haal_json(
        "https://archive-api.open-meteo.com/v1/archive?"
        f"latitude={breedte}&longitude={lengte}&start_date={begin.isoformat()}&end_date={vandaag.isoformat()}"
        "&daily=temperature_2m_mean,precipitation_sum&timezone=Europe/Amsterdam"
    )
    dagen = (gegevens or {}).get("daily") or {}
    tijden = dagen.get("time") or []
    if not tijden:
        return {"naam": "klimaat", "bron": "open-meteo.com archief", "tekst": "", "fout": "geen archiefgegevens"}
    per_maand: dict = {}
    for nummer, dag in enumerate(tijden):
        temperatuur = (dagen.get("temperature_2m_mean") or [None])[nummer]
        regen = (dagen.get("precipitation_sum") or [None])[nummer]
        sleutel = dag[:7]
        bak = per_maand.setdefault(sleutel, {"temp": [], "regen": 0.0})
        if isinstance(temperatuur, (int, float)):
            bak["temp"].append(temperatuur)
        if isinstance(regen, (int, float)):
            bak["regen"] += regen
    regels = []
    for maand in sorted(per_maand)[-6:]:
        bak = per_maand[maand]
        gemiddeld = sum(bak["temp"]) / len(bak["temp"]) if bak["temp"] else 0
        regels.append(f"{maand}: {_getal(gemiddeld, '°C')}, {_getal(bak['regen'], ' mm')}")
    alle_temperaturen = [w for bak in per_maand.values() for w in bak["temp"]]
    gemiddelde = sum(alle_temperaturen) / len(alle_temperaturen) if alle_temperaturen else 0
    totaal_regen = sum(bak["regen"] for bak in per_maand.values())
    return {
        "naam": "klimaat",
        "bron": "open-meteo.com (archief, ERA5)",
        "tekst": (
            f"Klimaat bij {nette}, laatste twaalf maanden: gemiddeld {_getal(gemiddelde, '°C')} "
            f"en {_getal(totaal_regen, ' mm')} neerslag. Per maand: " + " | ".join(regels) + "."
        ),
    }


def gereedschap_warmste_dag(plaats: str) -> dict:
    """De warmste en koudste dag van dit jaar tot nu toe."""
    plaats = (plaats or "").strip() or GEREEDSCHAP_PLAATS
    if not plaats:
        return {"naam": "warmste dag", "bron": "open-meteo.com archief", "tekst": "",
                "fout": "geen plaats opgegeven"}
    try:
        breedte, lengte, nette = _coordinaten(plaats)
    except Exception as fout:
        return {"naam": "warmste dag", "bron": "open-meteo.com archief", "tekst": "", "fout": str(fout)}
    vandaag = datetime.date.today()
    gegevens = _haal_json(
        "https://archive-api.open-meteo.com/v1/archive?"
        f"latitude={breedte}&longitude={lengte}&start_date={vandaag.year}-01-01&end_date={vandaag.isoformat()}"
        "&daily=temperature_2m_max,temperature_2m_min&timezone=Europe/Amsterdam"
    )
    dagen = (gegevens or {}).get("daily") or {}
    tijden = dagen.get("time") or []
    maxima = dagen.get("temperature_2m_max") or []
    minima = dagen.get("temperature_2m_min") or []
    paren = [(dag, hoog) for dag, hoog in zip(tijden, maxima) if isinstance(hoog, (int, float))]
    koud = [(dag, laag) for dag, laag in zip(tijden, minima) if isinstance(laag, (int, float))]
    if not paren:
        return {"naam": "warmste dag", "bron": "open-meteo.com archief", "tekst": "", "fout": "geen gegevens"}
    warm = max(paren, key=lambda stuk: stuk[1])
    koudst = min(koud, key=lambda stuk: stuk[1]) if koud else warm
    tropisch = len([1 for _, hoog in paren if hoog >= 30])
    vorst = len([1 for _, laag in koud if laag < 0])
    return {
        "naam": "warmste dag",
        "bron": "open-meteo.com (archief, ERA5)",
        "tekst": (
            f"In {nette} dit jaar: warmste dag {warm[0][8:10]}-{warm[0][5:7]} met {_getal(warm[1], '°C')}, "
            f"koudste nacht {koudst[0][8:10]}-{koudst[0][5:7]} met {_getal(koudst[1], '°C')}. "
            f"{tropisch} tropische dagen (30 °C of meer) en {vorst} nachten met vorst."
        ),
    }


def gereedschap_getijden(plaats: str) -> dict:
    """Eb en vloed: het verloop van de waterstand en de keerpunten."""
    plaats = (plaats or "").strip() or "Vlissingen"
    try:
        kandidaten = _waterlocaties(plaats)
    except Exception as fout:
        return {"naam": "getijden", "bron": "rijkswaterstaat.nl", "tekst": "", "fout": str(fout)}
    metingen = []
    naam = ""
    for code, kandidaat_naam in kandidaten[:8]:
        try:
            antwoord = _watermeting(code)
        except Exception:
            continue
        beste: list = []
        for waarneming in (antwoord or {}).get("WaarnemingenLijst") or []:
            locatie = str((waarneming.get("Locatie") or {}).get("Code") or "")
            if locatie and locatie.lower() != code.lower():
                continue
            reeks = []
            for meting in waarneming.get("MetingenLijst") or []:
                waarde = (meting.get("Meetwaarde") or {}).get("Waarde_Numeriek")
                tijd = str(meting.get("Tijdstip") or "")
                if isinstance(waarde, (int, float)) and tijd:
                    reeks.append((tijd, float(waarde)))
            if len(reeks) > len(beste):
                beste = reeks
        if beste:
            metingen, naam = beste, kandidaat_naam
            break
    if not metingen:
        return {"naam": "getijden", "bron": "rijkswaterstaat.nl", "tekst": "",
                "fout": f"geen waterstand bij '{plaats}'"}
    metingen.sort()
    uniek = {}
    for tijd, waarde in metingen:
        uniek[tijd] = waarde
    metingen = sorted(uniek.items())
    keerpunten = []
    for nummer in range(1, len(metingen) - 1):
        ervoor, nu, erna = metingen[nummer - 1][1], metingen[nummer][1], metingen[nummer + 1][1]
        if nu >= ervoor and nu > erna:
            keerpunten.append(("hoogwater", metingen[nummer][0], nu))
        elif nu <= ervoor and nu < erna:
            keerpunten.append(("laagwater", metingen[nummer][0], nu))
    laatste = metingen[-1]
    regels = [f"{soort} {tijd[11:16]} ({_getal(waarde, ' cm')})" for soort, tijd, waarde in keerpunten[-3:]]
    if not regels:
        return {
            "naam": "getijden",
            "bron": "Rijkswaterstaat (waterwebservices)",
            "tekst": (f"Waterstand bij {naam}: {_getal(laatste[1], ' cm')} boven NAP om {laatste[0][11:16]}. "
                      "In de laatste uren geen duidelijk keerpunt gevonden."),
        }
    return {
        "naam": "getijden",
        "bron": "Rijkswaterstaat (waterwebservices, laatste 6 uur)",
        "tekst": (
            f"Getij bij {naam}: nu {_getal(laatste[1], ' cm')} boven NAP om {laatste[0][11:16]}. "
            "Keerpunten: " + " | ".join(regels) + ". Dit zijn gemeten keerpunten, geen verwachting."
        ),
    }


def _water_meting_code(code: str, grootheid: str, hoedanigheid: str, uren: int) -> dict:
    nu = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=1)))
    gevraagd = {
        "Locatie": {"Code": code},
        "AquoPlusWaarnemingMetadata": {
            "AquoMetadata": {
                "Compartiment": {"Code": "OW"},
                "Grootheid": {"Code": grootheid},
                "Hoedanigheid": {"Code": hoedanigheid},
            },
            "KwaliteitswaardeCode": "NVT",
        },
        "Periode": {
            "Begindatumtijd": (nu - datetime.timedelta(hours=uren)).strftime("%Y-%m-%dT%H:%M:%S.000+01:00"),
            "Einddatumtijd": nu.strftime("%Y-%m-%dT%H:%M:%S.000+01:00"),
        },
    }
    return _haal_json(WATER_WWS, gegevens=json.dumps(gevraagd).encode("utf-8"),
                      koppen={"Content-Type": "application/json"})


def gereedschap_watertemperatuur(plaats: str) -> dict:
    """Hoe warm het water is (Rijkswaterstaat)."""
    plaats = (plaats or "").strip() or "Scheveningen"
    try:
        kandidaten = _waterlocaties(plaats)
    except Exception as fout:
        return {"naam": "watertemperatuur", "bron": "rijkswaterstaat.nl", "tekst": "", "fout": str(fout)}
    kandidaten = list(kandidaten[:20])
    for extra in ("vlissingen", "ijmuiden.buitenhaven", "scheveningen"):
        try:
            for code, naam in _waterlocaties(extra):
                if (code, naam) not in kandidaten:
                    kandidaten.append((code, naam))
        except Exception:
            continue
    for code, naam in kandidaten:
        for grootheid, hoedanigheid in (("T", "NVT"), ("T20", "NVT"), ("T", "DGS")):
            try:
                antwoord = _water_meting_code(code, grootheid, hoedanigheid, 24)
            except Exception:
                continue
            for waarneming in (antwoord or {}).get("WaarnemingenLijst") or []:
                for meting in waarneming.get("MetingenLijst") or []:
                    waarde = (meting.get("Meetwaarde") or {}).get("Waarde_Numeriek")
                    tijd = str(meting.get("Tijdstip") or "")
                    if isinstance(waarde, (int, float)) and -3 < waarde < 40 and tijd:
                        return {
                            "naam": "watertemperatuur",
                            "bron": "Rijkswaterstaat (waterwebservices)",
                            "tekst": (f"Watertemperatuur bij {naam}: {_getal(waarde, ' °C')} "
                                      f"om {tijd[11:16]}."),
                        }
    return {"naam": "watertemperatuur", "bron": "rijkswaterstaat.nl", "tekst": "",
            "fout": f"geen watertemperatuur gevonden bij '{plaats}'"}


def gereedschap_zwemwater(plaats: str) -> dict:
    """Zwemplekken en de waterkwaliteit (RIVM / zwemwater.nl)."""
    plaats = (plaats or "").strip()
    return {
        "naam": "zwemwater",
        "bron": "zwemwater.nl (RIVM)",
        "tekst": "",
        "fout": ("zwemwater.nl heeft geen open API meer; de kwaliteit staat alleen op de site. "
                 "Kijk op https://www.zwemwater.nl/ en zoek op " + (plaats or "je plaats") + "."),
    }


def gereedschap_maanfase(plaats: str) -> dict:
    """Hoe vol de maan is, en in welke fase hij staat."""
    nieuw = datetime.datetime(2000, 1, 6, 18, 14, tzinfo=datetime.timezone.utc)
    nu = datetime.datetime.now(datetime.timezone.utc)
    dagen = (nu - nieuw).total_seconds() / 86400
    cyclus = 29.530588853
    fase = (dagen % cyclus) / cyclus
    namen = [(0.03, "nieuwe maan"), (0.22, "wassende sikkel"), (0.28, "eerste kwartier"),
             (0.47, "wassende maan"), (0.53, "volle maan"), (0.72, "afnemende maan"),
             (0.78, "laatste kwartier"), (0.97, "afnemende sikkel"), (1.01, "nieuwe maan")]
    naam = next(label for grens, label in namen if fase < grens)
    verlicht = (1 - abs(2 * fase - 1)) * 100
    dagen_tot_vol = ((0.5 - fase) % 1) * cyclus
    return {
        "naam": "maanfase",
        "bron": "eigen berekening (synodische maand van 29,53 dagen)",
        "tekst": (
            f"De maan staat op {naam}, voor {_getal(verlicht, '%')} verlicht. "
            + (f"Over ongeveer {int(round(dagen_tot_vol))} dagen is het volle maan."
               if dagen_tot_vol < cyclus / 2 else
               f"Het is ongeveer {int(round(cyclus - dagen_tot_vol))} dagen geleden volle maan geweest.")
        ),
    }


def gereedschap_werkzaamheden(weg: str) -> dict:
    """Waar aan de weg wordt gewerkt (NDW, de actuele maatregelenfeed)."""
    try:
        beeld = _afsluitingen_ruw()
    except Exception as fout:
        return {"naam": "werkzaamheden", "bron": "opendata.ndw.nu", "tekst": "",
                "fout": f"{type(fout).__name__}: {fout}"}
    nu = datetime.datetime.now(datetime.timezone.utc)
    grens = nu + datetime.timedelta(days=14)
    gezocht = (weg or "").strip().upper()
    gevonden = []
    for blok in re.findall(r"<sit:situationRecord\b.*?</sit:situationRecord>", beeld, re.S):
        laag_blok = blok.lower()
        if not any(woord in laag_blok for woord in ("roadworks", "maintenanceworks",
                                                    "roadclosed", "constructionworks")):
            continue
        begin = re.search(r"<com:overallStartTime>([^<]+)", blok)
        eind = re.search(r"<com:overallEndTime>([^<]+)", blok)
        punt = re.search(r"<loc:latitude>([\d.\-]+)</loc:latitude>.*?<loc:longitude>([\d.\-]+)</loc:longitude>",
                         blok, re.S)
        if not (begin and punt):
            continue
        try:
            start = datetime.datetime.fromisoformat(begin.group(1).replace("Z", "+00:00"))
            stop = datetime.datetime.fromisoformat(eind.group(1).replace("Z", "+00:00")) if eind else grens
        except ValueError:
            continue
        if stop < nu or start > grens:
            continue
        waar = _weg_en_plaats(float(punt.group(1)), float(punt.group(2))) or "onbekend"
        if gezocht and not re.search(rf"(?<![A-Z0-9]){re.escape(gezocht)}(?![0-9])", waar.upper()):
            continue
        gevonden.append(f"{waar} vanaf {start.astimezone().strftime('%d-%m')}")
        if len(gevonden) >= 8:
            break
    if not gevonden:
        afsluiting = gereedschap_afsluitingen(weg or "")
        if afsluiting.get("tekst"):
            return {
                "naam": "werkzaamheden",
                "bron": afsluiting.get("bron") or "opendata.ndw.nu",
                "tekst": "Geen wegwerkzaamheden in de feed, wel dit: " + str(afsluiting["tekst"]),
            }
        return {"naam": "werkzaamheden", "bron": "opendata.ndw.nu (NDW)",
                "tekst": ("Geen werkzaamheden of afsluitingen gevonden"
                          + (f" op {weg}" if weg else "") + " in de actuele NDW-feed.")}
    return {
        "naam": "werkzaamheden",
        "bron": "opendata.ndw.nu (NDW) en api.pdok.nl",
        "tekst": f"Geplande werkzaamheden ({len(gevonden)}): " + " | ".join(dict.fromkeys(gevonden))[:700],
    }


def gereedschap_snelheidslimiet(verzoek: str) -> dict:
    """Hoe hard je op een weg of in een straat mag (OpenStreetMap)."""
    plaats = _plaats_uit(verzoek) or GEREEDSCHAP_PLAATS
    if not plaats:
        return {"naam": "snelheidslimiet", "bron": "openstreetmap.org", "tekst": "",
                "fout": "noem een straat of plaats"}
    try:
        breedte, lengte, nette = _coordinaten(plaats)
        gegevens = _overpass(
            f'[out:json][timeout:20];way["highway"]["maxspeed"](around:1200,{breedte},{lengte});'
            "out tags 40;"
        )
    except Exception as fout:
        return {"naam": "snelheidslimiet", "bron": "openstreetmap.org", "tekst": "", "fout": str(fout)}
    per_limiet: dict = {}
    for element in (gegevens or {}).get("elements") or []:
        kenmerken = element.get("tags") or {}
        limiet = str(kenmerken.get("maxspeed") or "").strip()
        weg = str(kenmerken.get("name") or kenmerken.get("ref") or "")
        if not limiet or limiet.lower() in ("signals", "none"):
            continue
        per_limiet.setdefault(limiet, [])
        if weg and weg not in per_limiet[limiet]:
            per_limiet[limiet].append(weg)
    if not per_limiet:
        return {"naam": "snelheidslimiet", "bron": "openstreetmap.org", "tekst": "",
                "fout": f"geen snelheidslimieten gevonden bij {nette}"}
    regels = []
    for limiet, wegen in sorted(per_limiet.items(), key=lambda stuk: stuk[0]):
        voorbeeld = ", ".join(wegen[:3]) if wegen else "zonder naam"
        regels.append(f"{limiet}: {voorbeeld}")
    return {
        "naam": "snelheidslimiet",
        "bron": "openstreetmap.org (Overpass)",
        "tekst": f"Snelheidslimieten bij {nette}: " + " | ".join(regels[:5]),
    }


def gereedschap_flitspalen(plaats: str) -> dict:
    """Waar vaste flitspalen en roodlichtcamera's staan (OpenStreetMap)."""
    plaats = (plaats or "").strip() or GEREEDSCHAP_PLAATS
    if not plaats:
        return {"naam": "flitspalen", "bron": "openstreetmap.org", "tekst": "", "fout": "geen plaats opgegeven"}
    try:
        breedte, lengte, nette = _coordinaten(plaats)
        gegevens = _overpass(
            f'[out:json][timeout:20];node["highway"="speed_camera"](around:4000,{breedte},{lengte});out 25;'
        )
    except Exception as fout:
        return {"naam": "flitspalen", "bron": "openstreetmap.org", "tekst": "", "fout": str(fout)}
    palen = []
    for element in (gegevens or {}).get("elements") or []:
        lat, lon = element.get("lat"), element.get("lon")
        if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
            continue
        waar = _weg_en_plaats(lat, lon) or "onbekend"
        palen.append((_afstand_km(breedte, lengte, lat, lon), waar))
    if not palen:
        return {"naam": "flitspalen", "bron": "openstreetmap.org",
                "tekst": f"Geen flitspalen gevonden binnen 4 km van {nette}."}
    palen.sort()
    regels = [f"{waar} ({_getal(afstand, ' km')})" for afstand, waar in palen[:6]]
    return {
        "naam": "flitspalen",
        "bron": "openstreetmap.org (Overpass)",
        "tekst": f"{len(palen)} flitspalen binnen 4 km van {nette}: " + ", ".join(regels),
    }


OV_CACHE: dict = {"tijd": 0.0, "lijnen": None}


def _ov_lijnen() -> dict:
    nu = time.time()
    if OV_CACHE["lijnen"] is not None and nu - OV_CACHE["tijd"] < 1800:
        return OV_CACHE["lijnen"]
    gegevens = _haal_json("http://v0.ovapi.nl/line", limiet=45)
    OV_CACHE["lijnen"] = gegevens if isinstance(gegevens, dict) else {}
    OV_CACHE["tijd"] = nu
    return OV_CACHE["lijnen"]


def gereedschap_ov_haltes(plaats: str) -> dict:
    """Welke bus-, tram- en treinlijnen er bij een plaats horen (OVapi)."""
    plaats = (plaats or "").strip() or GEREEDSCHAP_PLAATS
    if not plaats:
        return {"naam": "ov", "bron": "ovapi.nl", "tekst": "", "fout": "geen plaats opgegeven"}
    try:
        lijnen = _ov_lijnen()
    except Exception as fout:
        return {"naam": "ov", "bron": "ovapi.nl", "tekst": "", "fout": f"{type(fout).__name__}: {fout}"}
    zoek = plaats.lower()
    gevonden = []
    for waarde in lijnen.values():
        if not isinstance(waarde, dict):
            continue
        naam = str(waarde.get("LineName") or "")
        richting = str(waarde.get("DestinationName50") or "")
        if zoek in naam.lower() or zoek in richting.lower():
            nummer = str(waarde.get("LinePublicNumber") or "?")
            soort = str(waarde.get("TransportType") or "")
            stuk = f"{nummer} ({naam})"
            if stuk not in gevonden:
                gevonden.append(stuk)
    if not gevonden:
        return {"naam": "ov", "bron": "ovapi.nl", "tekst": "",
                "fout": f"geen lijnen gevonden met '{plaats}' in de naam"}
    return {
        "naam": "ov",
        "bron": f"ovapi.nl ({len(lijnen)} lijnen in de lijst)",
        "tekst": f"Openbaar vervoer bij {plaats}: " + " | ".join(gevonden[:8]),
    }


def gereedschap_vertrektijden(plaats: str) -> dict:
    """Actuele vertrektijden bij een halte."""
    return {
        "naam": "vertrektijden",
        "bron": "ovapi.nl",
        "tekst": "",
        "fout": ("de actuele vertrektijden van OVapi zijn niet meer zonder sleutel op te vragen; "
                 "de haltes en lijnen werken wel (gebruik 'ov-haltes')."),
    }


def gereedschap_treinvertragingen(plaats: str) -> dict:
    """Of de treinen op tijd rijden."""
    return {
        "naam": "treinvertragingen",
        "bron": "ns.nl",
        "tekst": "",
        "fout": ("de NS geeft geen open realtime-feed meer zonder sleutel; "
                 "de storingenpagina is alleen HTML met JavaScript."),
    }


def gereedschap_spoorwerk(plaats: str) -> dict:
    """Waar deze week aan het spoor wordt gewerkt."""
    titels = _nieuwsfeed("spoorwerkzaamheden NS", "NS-werkzaamheden")
    if not titels:
        return {"naam": "spoorwerk", "bron": "nieuwsfeed", "tekst": "",
                "fout": "geen berichten over spoorwerkzaamheden gevonden"}
    return {
        "naam": "spoorwerk",
        "bron": "nieuws over het spoor (via Google Nieuws)",
        "tekst": "Spoorwerkzaamheden in het nieuws: " + " | ".join(titels[:5]),
    }


def _route(van: str, naar: str, profiel: str, label: str) -> dict:
    try:
        van_breedte, van_lengte, van_nette = _coordinaten(van)
        naar_breedte, naar_lengte, naar_nette = _coordinaten(naar)
    except Exception as fout:
        return {"naam": label, "bron": "openstreetmap routing", "tekst": "", "fout": str(fout)}
    try:
        gegevens = _haal_json(
            f"https://routing.openstreetmap.de/routed-{profiel}/route/v1/driving/"
            f"{van_lengte},{van_breedte};{naar_lengte},{naar_breedte}?overview=false",
            limiet=25,
        )
    except Exception as fout:
        return {"naam": label, "bron": "openstreetmap routing", "tekst": "",
                "fout": f"{type(fout).__name__}: {fout}"}
    route = ((gegevens or {}).get("routes") or [{}])[0]
    afstand, duur = route.get("distance"), route.get("duration")
    if not isinstance(afstand, (int, float)) or not isinstance(duur, (int, float)):
        return {"naam": label, "bron": "openstreetmap routing", "tekst": "",
                "fout": f"geen {label} gevonden van {van_nette} naar {naar_nette}"}
    return {
        "naam": label,
        "bron": "routing.openstreetmap.de (OpenStreetMap)",
        "tekst": (f"{label.capitalize()} van {van_nette} naar {naar_nette}: "
                  f"{_getal(afstand / 1000, ' km')}, ongeveer {int(round(duur / 60))} minuten."),
    }


def _van_naar(verzoek: str) -> tuple[str, str]:
    punten = re.split(r"\bnaar\b", verzoek or "", maxsplit=1, flags=re.I)
    if len(punten) < 2:
        return "", ""
    van = re.sub(r"^.*?\bvan\b", "", punten[0], flags=re.I).strip(" ,?:")
    return van, punten[1].strip(" ,?.")


def gereedschap_fietsroute(verzoek: str) -> dict:
    """Hoe lang je fietst van A naar B."""
    van, naar = _van_naar(verzoek)
    if not van or not naar:
        return {"naam": "fietsroute", "bron": "openstreetmap routing", "tekst": "",
                "fout": "vraag het als 'fietsroute van Utrecht naar Amersfoort'"}
    return _route(van, naar, "bike", "fietsroute")


def gereedschap_wandelroute(verzoek: str) -> dict:
    """Hoe lang je loopt van A naar B."""
    van, naar = _van_naar(verzoek)
    if not van or not naar:
        return {"naam": "wandelroute", "bron": "openstreetmap routing", "tekst": "",
                "fout": "vraag het als 'wandelroute van Utrecht naar Amersfoort'"}
    return _route(van, naar, "foot", "wandelroute")


def gereedschap_tolwegen(plaats: str) -> dict:
    """Waar je onderweg moet betalen (tol, vignet, veerpont)."""
    plaats = (plaats or "").strip() or GEREEDSCHAP_PLAATS
    if not plaats:
        return {"naam": "tolwegen", "bron": "openstreetmap.org", "tekst": "", "fout": "geen plaats opgegeven"}
    try:
        breedte, lengte, nette = _coordinaten(plaats)
        gegevens = _overpass(
            f'[out:json][timeout:20];way["toll"="yes"](around:15000,{breedte},{lengte});'
            'out tags 20;'
        )
    except Exception as fout:
        return {"naam": "tolwegen", "bron": "openstreetmap.org", "tekst": "", "fout": str(fout)}
    wegen = []
    for element in (gegevens or {}).get("elements") or []:
        kenmerken = element.get("tags") or {}
        naam = kenmerken.get("name") or kenmerken.get("ref")
        if naam and str(naam) not in wegen:
            wegen.append(str(naam))
    if not wegen:
        return {"naam": "tolwegen", "bron": "openstreetmap.org",
                "tekst": f"Geen tolwegen gevonden binnen 15 km van {nette}. In Nederland is tol zeldzaam; "
                         "in het buitenland staat het er wel in."}
    return {
        "naam": "tolwegen",
        "bron": "openstreetmap.org (Overpass)",
        "tekst": f"Tolwegen binnen 15 km van {nette}: " + ", ".join(wegen[:8]),
    }


def gereedschap_veerponten(plaats: str) -> dict:
    """Welke veerponten er in de buurt varen."""
    try:
        plekken, nette = _osm_nabij(plaats or GEREEDSCHAP_PLAATS, '"route"="ferry"', 15000, 10)
    except Exception as fout:
        return {"naam": "veerponten", "bron": "openstreetmap.org", "tekst": "", "fout": str(fout)}
    if not plekken:
        return {"naam": "veerponten", "bron": "openstreetmap.org",
                "tekst": f"Geen veerponten gevonden binnen 15 km van {nette}."}
    regels = [f"{naam or 'pont'} ({_getal(afstand, ' km')})" for afstand, naam, _ in plekken[:6]]
    return {
        "naam": "veerponten",
        "bron": "openstreetmap.org (Overpass)",
        "tekst": f"Veerponten bij {nette}: " + ", ".join(regels),
    }


def gereedschap_tankstations(plaats: str) -> dict:
    """Waar je kunt tanken in de buurt en welk merk."""
    try:
        plekken, nette = _osm_nabij(plaats or GEREEDSCHAP_PLAATS, '"amenity"="fuel"', 2500, 10)
    except Exception as fout:
        return {"naam": "tankstations", "bron": "openstreetmap.org", "tekst": "", "fout": str(fout)}
    if not plekken:
        return {"naam": "tankstations", "bron": "openstreetmap.org",
                "tekst": f"Geen tankstations gevonden binnen 2,5 km van {nette}."}
    regels = []
    for afstand, naam, kenmerken in plekken[:6]:
        merk = naam or kenmerken.get("brand") or "tankstation"
        regels.append(f"{merk} ({_getal(afstand, ' km')})")
    return {
        "naam": "tankstations",
        "bron": "openstreetmap.org (Overpass)",
        "tekst": f"Tankstations bij {nette}: " + ", ".join(regels),
    }


def gereedschap_pr_parkeren(plaats: str) -> dict:
    """Parkeer-en-reis-plekken bij een stad."""
    try:
        plekken, nette = _osm_nabij(plaats or GEREEDSCHAP_PLAATS, '"park_ride"', 8000, 10)
    except Exception as fout:
        return {"naam": "p+r", "bron": "openstreetmap.org", "tekst": "", "fout": str(fout)}
    if not plekken:
        return {"naam": "p+r", "bron": "openstreetmap.org",
                "tekst": f"Geen p+r-terreinen gevonden binnen 8 km van {nette}."}
    regels = [f"{naam or 'p+r'} ({_getal(afstand, ' km')})" for afstand, naam, _ in plekken[:6]]
    return {
        "naam": "p+r",
        "bron": "openstreetmap.org (Overpass)",
        "tekst": f"Parkeer en reis bij {nette}: " + ", ".join(regels),
    }

# ---- I. geld, overheid en kennis

def _nieuwsfeed(zoek: str, label: str, aantal: int = 6) -> list[str]:
    """Titels uit de nieuwsfeed van Google voor één zoekopdracht."""
    try:
        ruw = _haal(
            "https://news.google.com/rss/search?q=" + urllib.parse.quote(zoek)
            + "&hl=nl&gl=NL&ceid=NL:nl",
            limiet=25,
        ).decode("utf-8", "replace")
    except Exception:
        return []
    titels = re.findall(r"<item>.*?<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", ruw, re.S)
    schoon = []
    for titel in titels:
        tekst = _schoon(re.sub(r"<[^>]+>", " ", titel))
        tekst = re.sub(r"\s+-\s+[^-]{2,40}$", "", tekst).strip()
        if tekst and "Google" not in tekst and len(tekst) > 12 and tekst not in schoon:
            schoon.append(tekst)
    return schoon[:aantal]


def gereedschap_valuta_geschiedenis(verzoek: str) -> dict:
    """Hoe een munt zich over een periode ontwikkelde (Frankfurter)."""
    laag = (verzoek or "").lower()
    codes = {"dollar": "USD", "usd": "USD", "pond": "GBP", "gbp": "GBP", "yen": "JPY",
             "frank": "CHF", "chf": "CHF", "zloty": "PLN", "pln": "PLN", "kroon": "SEK",
             "sek": "SEK", "noorse kroon": "NOK", "nok": "NOK", "deense kroon": "DKK",
             "dkk": "DKK", "canadese dollar": "CAD", "cad": "CAD", "ausd": "AUD", "aud": "AUD"}
    munt = next((code for woord, code in codes.items() if woord in laag), "USD")
    dagen = 30
    for aantal in (7, 30, 90, 365):
        if str(aantal) in laag:
            dagen = aantal
            break
    if "jaar" in laag:
        dagen = 365
    if "maand" in laag:
        dagen = 30
    if "week" in laag:
        dagen = 7
    begin = (datetime.date.today() - datetime.timedelta(days=dagen)).isoformat()
    gegevens = _haal_json(
        f"https://api.frankfurter.app/{begin}..{datetime.date.today().isoformat()}"
        f"?from=EUR&to={munt}"
    )
    reeks = (gegevens or {}).get("rates") or {}
    waarden = [(dag, float(stuk[munt])) for dag, stuk in sorted(reeks.items()) if munt in stuk]
    if not waarden:
        return {"naam": "valuta-geschiedenis", "bron": "frankfurter.app", "tekst": "",
                "fout": f"geen koersen voor {munt}"}
    eerste, laatste = waarden[0][1], waarden[-1][1]
    hoogste = max(waarden, key=lambda stuk: stuk[1])
    laagste = min(waarden, key=lambda stuk: stuk[1])
    verschil = (laatste - eerste) / eerste * 100 if eerste else 0
    def _koers(waarde: float) -> str:
        return f"{waarde:.4f}".replace(".", ",")

    return {
        "naam": "valuta-geschiedenis",
        "bron": "frankfurter.app (Europese Centrale Bank)",
        "tekst": (
            f"EUR/{munt} over {dagen} dagen: van {_koers(eerste)} naar {_koers(laatste)} "
            f"({verschil:+.1f}%). Hoogste {_koers(hoogste[1])} op {hoogste[0]}, "
            f"laagste {_koers(laagste[1])} op {laagste[0]}.".replace(".", ",")
        ),
    }


def gereedschap_crypto_geschiedenis(verzoek: str) -> dict:
    """Hoe een cryptomunt het deed over een periode (CoinGecko)."""
    laag = (verzoek or "").lower()
    munten = {"bitcoin": "bitcoin", "btc": "bitcoin", "ethereum": "ethereum", "eth": "ethereum",
              "solana": "solana", "sol": "solana", "cardano": "cardano", "ada": "cardano",
              "dogecoin": "dogecoin", "doge": "dogecoin", "ripple": "ripple", "xrp": "ripple",
              "polkadot": "polkadot", "dot": "polkadot", "litecoin": "litecoin", "ltc": "litecoin"}
    munt = next((naam for woord, naam in munten.items() if woord in laag), "bitcoin")
    dagen = 30
    for aantal in (7, 30, 90, 365):
        if str(aantal) in laag:
            dagen = aantal
            break
    if "jaar" in laag:
        dagen = 365
    gegevens = _haal_json(
        f"https://api.coingecko.com/api/v3/coins/{munt}/market_chart?vs_currency=eur&days={dagen}",
        limiet=25,
    )
    punten = (gegevens or {}).get("prices") or []
    if len(punten) < 2:
        return {"naam": "crypto-geschiedenis", "bron": "coingecko.com", "tekst": "",
                "fout": f"geen koersreeks voor {munt}"}
    eerste = float(punten[0][1])
    laatste = float(punten[-1][1])
    hoogste = max(float(punt[1]) for punt in punten)
    laagste = min(float(punt[1]) for punt in punten)
    verschil = (laatste - eerste) / eerste * 100 if eerste else 0
    return {
        "naam": "crypto-geschiedenis",
        "bron": "coingecko.com",
        "tekst": (
            f"{munt.capitalize()} over {dagen} dagen: van {_getal(eerste, ' euro')} naar "
            f"{_getal(laatste, ' euro')} ({verschil:+.1f}%). Hoogste {_getal(hoogste, ' euro')}, "
            f"laagste {_getal(laagste, ' euro')}.".replace(".", ",")
        ),
    }


def gereedschap_aandelenkoers(verzoek: str) -> dict:
    """De koers van een aandeel (Yahoo Finance)."""
    laag = (verzoek or "").lower()
    bekend = {"asml": "ASML.AS", "philips": "PHIA.AS", "ing": "INGA.AS", "abn": "ABN.AS",
              "heineken": "HEIA.AS", "ahold": "AD.AS", "unilever": "UNA.AS", "shell": "SHELL.AS",
              "adidas": "ADS.DE", "apple": "AAPL", "microsoft": "MSFT", "tesla": "TSLA",
              "nvidia": "NVDA", "google": "GOOGL", "amazon": "AMZN", "meta": "META",
              "aex": "^AEX", "bitcoin": "BTC-EUR"}
    ticker = next((code for naam, code in bekend.items() if naam in laag), "")
    if not ticker:
        gevonden = re.search(r"\b([A-Z]{1,5}(?:\.[A-Z]{1,3})?)\b", verzoek or "")
        ticker = gevonden.group(1) if gevonden else "ASML.AS"
    gegevens = _haal_json(
        f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(ticker)}"
        "?range=5d&interval=1d",
        limiet=25,
    )
    resultaat = (((gegevens or {}).get("chart") or {}).get("result") or [{}])[0]
    meta = resultaat.get("meta") or {}
    prijs = meta.get("regularMarketPrice")
    vorige = meta.get("chartPreviousClose") or meta.get("previousClose")
    if not isinstance(prijs, (int, float)):
        return {"naam": "aandelenkoers", "bron": "finance.yahoo.com", "tekst": "",
                "fout": f"geen koers gevonden voor {ticker}"}
    munt = meta.get("currency") or "EUR"
    verschil = ""
    if isinstance(vorige, (int, float)) and vorige:
        procent = (prijs - vorige) / vorige * 100
        verschil = f" ({procent:+.1f}% sinds de vorige slotkoers)".replace(".", ",")
    return {
        "naam": "aandelenkoers",
        "bron": "finance.yahoo.com",
        "tekst": f"{meta.get('symbol') or ticker}: {_getal(prijs)} {munt}{verschil}.",
    }


def gereedschap_hypotheekrente(plaats: str) -> dict:
    """De actuele hypotheekrente, van een vergelijkingspagina geplukt."""
    try:
        ruw = _haal("https://www.hypotheker.nl/", limiet=25).decode("utf-8", "replace")
    except Exception as fout:
        return {"naam": "hypotheekrente", "bron": "hypotheker.nl", "tekst": "",
                "fout": f"{type(fout).__name__}: {fout}"}
    rentes = re.findall(r"(\d,\d{1,2})\s*%", ruw)
    if not rentes:
        return {"naam": "hypotheekrente", "bron": "hypotheker.nl", "tekst": "",
                "fout": "geen rentepercentages op de pagina gevonden"}
    uniek = list(dict.fromkeys(rentes))[:6]
    return {
        "naam": "hypotheekrente",
        "bron": "hypotheker.nl (letterlijk van de pagina geplukt)",
        "tekst": ("Rentes die op de pagina staan: " + ", ".join(uniek) + "%. "
                  "Dit is een momentopname van de site, geen volledig overzicht — "
                  "controleer je eigen situatie bij een adviseur."),
    }


def gereedschap_spaarrente(plaats: str) -> dict:
    """Waar je spaargeld het meest opbrengt."""
    try:
        ruw = _haal("https://www.spaarrente.nl/", limiet=25).decode("utf-8", "replace")
    except Exception as fout:
        return {"naam": "spaarrente", "bron": "spaarrente.nl", "tekst": "",
                "fout": f"{type(fout).__name__}: {fout}"}
    rentes = list(dict.fromkeys(re.findall(r"(\d,\d{1,2})\s*%", ruw)))[:6]
    if not rentes:
        return {"naam": "spaarrente", "bron": "spaarrente.nl", "tekst": "",
                "fout": "geen rentepercentages op de pagina gevonden"}
    return {
        "naam": "spaarrente",
        "bron": "spaarrente.nl (letterlijk van de pagina geplukt)",
        "tekst": ("Spaarrentes op de pagina: " + ", ".join(rentes) + "%. "
                  "Kijk op de site welke bank en welke looptijd erbij horen."),
    }


def gereedschap_teruglevering() -> dict:
    """Wat je krijgt voor stroom die je teruglevert (EnergyZero)."""
    nu = datetime.datetime.now(datetime.timezone.utc)
    begin = nu.replace(hour=0, minute=0, second=0, microsecond=0)
    eind = begin + datetime.timedelta(days=2)
    gegevens = _haal_json(
        f"{ENERGIE_API}?fromDate={begin.strftime('%Y-%m-%dT%H:%M:%S.000Z')}"
        f"&tillDate={eind.strftime('%Y-%m-%dT%H:%M:%S.000Z')}"
        "&interval=4&usageType=3&inclBtw=true"
    )
    prijzen = []
    for punt in (gegevens or {}).get("Prices") or []:
        bedrag = punt.get("price")
        moment = str(punt.get("readingDate") or "")
        if isinstance(bedrag, (int, float)) and moment:
            prijzen.append((moment, float(bedrag)))
    if not prijzen:
        return {"naam": "teruglevering", "bron": "energyzero.nl", "tekst": "",
                "fout": "geen terugleverprijzen ontvangen"}
    prijzen.sort()
    nu_iso = nu.strftime("%Y-%m-%dT%H:00")
    huidig = next((prijs for moment, prijs in prijzen if moment[:13] == nu_iso), prijzen[0][1])
    laagste = min(prijs for _, prijs in prijzen)
    hoogste = max(prijs for _, prijs in prijzen)
    gemiddeld = sum(prijs for _, prijs in prijzen) / len(prijzen)
    return {
        "naam": "teruglevering",
        "bron": "energyzero.nl (inclusief btw)",
        "tekst": (
            f"Terugleververgoeding: nu {huidig:.3f}".replace(".", ",") + " euro per kWh, "
            + f"gemiddeld {gemiddeld:.3f}".replace(".", ",") + ", "
            + f"laagste {laagste:.3f}".replace(".", ",") + " en "
            + f"hoogste {hoogste:.3f}".replace(".", ",") + " (vandaag en morgen)."
        ),
    }


def gereedschap_zonnepanelen(verzoek: str) -> dict:
    """Wat zonnepanelen vandaag ongeveer opleveren."""
    laag = (verzoek or "").lower()
    vermogen = 3000.0
    gevonden = re.search(r"(\d{3,5})\s*(wp|wattpiek|w)\b", laag)
    if gevonden:
        vermogen = float(gevonden.group(1))
    plaats = _plaats_uit(verzoek) or GEREEDSCHAP_PLAATS
    if not plaats:
        return {"naam": "zonnepanelen", "bron": "open-meteo.com", "tekst": "",
                "fout": "geen plaats opgegeven"}
    try:
        breedte, lengte, nette = _coordinaten(plaats)
    except Exception as fout:
        return {"naam": "zonnepanelen", "bron": "open-meteo.com", "tekst": "", "fout": str(fout)}
    gegevens = _haal_json(
        "https://api.open-meteo.com/v1/forecast?"
        f"latitude={breedte}&longitude={lengte}&hourly=shortwave_radiation"
        "&forecast_days=1&timezone=Europe/Amsterdam"
    )
    straling = ((gegevens or {}).get("hourly") or {}).get("shortwave_radiation") or []
    uren = [waarde for waarde in straling if isinstance(waarde, (int, float))]
    if not uren:
        return {"naam": "zonnepanelen", "bron": "open-meteo.com", "tekst": "",
                "fout": "geen stralingsgegevens"}
    # kWh = Wp * (Wh/m2 / 1000) * rendement; een paneel zet ongeveer 80% van de jaaropbrengst om
    straling_wh = sum(uren)
    kWh = vermogen * (straling_wh / 1000.0) * 0.8 / 1000.0 * 1000 / 1000
    kWh = vermogen / 1000.0 * (straling_wh / 1000.0) * 0.85
    return {
        "naam": "zonnepanelen",
        "bron": "open-meteo.com (instraling) + eigen berekening",
        "tekst": (
            f"Bij {nette} valt vandaag {_getal(straling_wh / 1000, ' kWh per m²')} zon op een plat vlak. "
            f"Een set van {_getal(vermogen, ' Wp')} levert daarmee roughly "
            f"{_getal(kWh, ' kWh')} — een schatting met 85% systeemrendement, geen garantie."
        ),
    }


def gereedschap_kosten_apparaat(verzoek: str) -> dict:
    """Wat een apparaat per jaar aan stroom kost."""
    laag = (verzoek or "").lower().replace(",", ".")
    watt = None
    gevonden = re.search(r"(\d{1,5})\s*(watt|w)\b", laag)
    if gevonden:
        watt = float(gevonden.group(1))
    uren = 1.0
    uur_match = re.search(r"(\d{1,2})\s*(uur|uren)\s*(per dag|daags)?", laag)
    if uur_match:
        uren = float(uur_match.group(1))
    standaard_uren = {"vriezer": 24, "koelkast": 24, "tv": 4, "laptop": 8, "computer": 8,
                      "desktop": 8, "server": 24, "router": 24, "ledlamp": 4, "gloeilamp": 4}
    if watt is None:
        bekend = {"vriezer": 150, "koelkast": 100, "wasmachine": 1000, "droger": 2500,
                  "vaatwasser": 1200, "oven": 2000, "magnetron": 1000, "waterkoker": 2000,
                  "tv": 100, "laptop": 50, "computer": 200, "desktop": 300, "server": 150,
                  "airco": 1000, "warmtepomp": 1500, "ledlamp": 9, "gloeilamp": 60,
                  "stofzuiger": 900, "koffiezetapparaat": 1000, "quooker": 1500}
        for naam, vermogen in bekend.items():
            if naam in laag:
                watt = float(vermogen)
                if not uur_match and naam in standaard_uren:
                    uren = float(standaard_uren[naam])
                break
    if watt is None:
        return {"naam": "kosten apparaat", "bron": "eigen berekening", "tekst": "",
                "fout": "noem het vermogen of het apparaat, bijvoorbeeld 'wat kost een vriezer per jaar'"}
    prijs = 0.25
    try:
        gegevens = _haal_json(
            f"{ENERGIE_API}?fromDate={(datetime.datetime.now(datetime.timezone.utc)).strftime('%Y-%m-%dT00:00:00.000Z')}"
            f"&tillDate={(datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=1)).strftime('%Y-%m-%dT00:00:00.000Z')}"
            "&interval=4&usageType=1&inclBtw=true",
            limiet=20,
        )
        waarden = [float(punt.get("price")) for punt in (gegevens or {}).get("Prices") or []
                   if isinstance(punt.get("price"), (int, float))]
        if waarden:
            prijs = sum(waarden) / len(waarden)
    except Exception:
        pass
    jaarverbruik = {"vriezer": 250, "koelkast": 150, "wasmachine": 200, "droger": 250,
                    "vaatwasser": 250, "oven": 150, "magnetron": 60, "waterkoker": 60,
                    "tv": 120, "laptop": 50, "computer": 250, "desktop": 300, "server": 400,
                    "airco": 400, "warmtepomp": 2500, "ledlamp": 10, "gloeilamp": 60,
                    "stofzuiger": 30, "koffiezetapparaat": 100, "quooker": 500}
    bekend_verbruik = next((waarde for naam, waarde in jaarverbruik.items() if naam in laag), None)
    if bekend_verbruik and not uur_match:
        kWh_jaar = float(bekend_verbruik)
        uitleg = f"een gemiddelde {next(naam for naam in jaarverbruik if naam in laag)} gebruikt"
    else:
        kWh_jaar = watt / 1000 * uren * 365
        uitleg = f"een apparaat van {_getal(watt, ' watt')} dat {_getal(uren, ' uur per dag')} aanstaat, gebruikt"
    return {
        "naam": "kosten apparaat",
        "bron": "eigen berekening met de actuele stroomprijs",
        "tekst": (
            f"{uitleg[0].upper() + uitleg[1:]} ongeveer {_getal(kWh_jaar, ' kWh per jaar')}. Bij "
            + f"{prijs:.3f}".replace(".", ",") + " euro per kWh kost dat ongeveer "
            + f"{_getal(kWh_jaar * prijs, ' euro per jaar')}."
        ),
    }


def _box1_belasting(bruto: float) -> tuple[float, list[tuple[str, float, float]]]:
    schijven = [(38441.0, 0.3582), (76817.0, 0.3748), (float("inf"), 0.4950)]
    belasting = 0.0
    vorige_grens = 0.0
    details = []
    for grens, tarief in schijven:
        deel = min(bruto, grens) - vorige_grens
        if deel <= 0:
            break
        bedrag = deel * tarief
        belasting += bedrag
        details.append((f"{_getal(deel, ' euro')} tegen {tarief * 100:.2f}".replace(".", ",") + "%", tarief, bedrag))
        vorige_grens = grens
    return belasting, details


def gereedschap_bruto_netto(verzoek: str) -> dict:
    """Wat je van een bruto salaris ongeveer overhoudt."""
    bedragen = [float(getal.replace(".", "").replace(",", "."))
                for getal in re.findall(r"\d{3,6}(?:[.,]\d{3})?", verzoek or "")]
    if not bedragen:
        return {"naam": "bruto netto", "bron": "eigen berekening", "tekst": "",
                "fout": "noem een bruto jaarbedrag, bijvoorbeeld 'bruto netto 45000'"}
    bruto = max(bedragen)
    if bruto < 5000:
        bruto *= 12
    belasting, _ = _box1_belasting(bruto)
    heffingskorting = max(0.0, 3068.0 - 0.0663 * max(0.0, bruto - 24813.0))
    arbeidskorting = min(5599.0, 0.0815 * bruto)
    te_betalen = max(0.0, belasting - heffingskorting - arbeidskorting)
    netto = bruto - te_betalen
    return {
        "naam": "bruto netto",
        "bron": "eigen berekening met de tarieven van 2025 (indicatief)",
        "tekst": (
            f"Bruto {_getal(bruto, ' euro')} per jaar geeft ongeveer {_getal(netto, ' euro')} netto. "
            f"Daar gaat {_getal(te_betalen, ' euro')} belasting vanaf, na heffingskorting "
            f"({_getal(heffingskorting, ' euro')}) en arbeidskorting ({_getal(arbeidskorting, ' euro')}). "
            "Dit is een schatting zonder pensioenpremie, eigen huis en toeslagen."
        ),
    }


def gereedschap_belasting_box1(verzoek: str) -> dict:
    """Hoeveel belasting je per schijf betaalt."""
    bedragen = [float(getal.replace(".", "").replace(",", "."))
                for getal in re.findall(r"\d{3,7}(?:[.,]\d{3})?", verzoek or "")]
    if not bedragen:
        return {"naam": "belasting", "bron": "eigen berekening", "tekst": "",
                "fout": "noem een bruto jaarinkomen, bijvoorbeeld 'belasting over 60000'"}
    bruto = max(bedragen)
    if bruto < 5000:
        bruto *= 12
    belasting, details = _box1_belasting(bruto)
    regels = [f"{_getal(bedrag, ' euro')} belast" for _, _, bedrag in details]
    effectief = belasting / bruto * 100 if bruto else 0
    return {
        "naam": "belasting",
        "bron": "eigen berekening met de tarieven van 2025 (indicatief)",
        "tekst": (
            f"Over {_getal(bruto, ' euro')} betaal je in box 1 ongeveer {_getal(belasting, ' euro')} "
            f"({_getal(effectief, '%')} gemiddeld). Per schijf: " + " | ".join(regels) + ". "
            "De heffingskortingen zijn hier nog niet van afgetrokken."
        ),
    }


def gereedschap_reiskosten(verzoek: str) -> dict:
    """Wat een rit aan reiskostenvergoeding oplevert."""
    laag = (verzoek or "").lower().replace(",", ".")
    kilometers = re.search(r"(\d{1,4}(?:\.\d)?)\s*(km|kilometer)", laag)
    if not kilometers:
        return {"naam": "reiskosten", "bron": "eigen berekening", "tekst": "",
                "fout": "noem de afstand, bijvoorbeeld 'reiskosten 45 km'"}
    afstand = float(kilometers.group(1))
    tarief = 0.23
    eigen = re.search(r"(\d,\d{2})\s*(euro)?\s*(per km)?", laag)
    if eigen and "per km" in laag:
        tarief = float(eigen.group(1).replace(",", "."))
    rit = afstand * tarief
    return {
        "naam": "reiskosten",
        "bron": "eigen berekening (0,23 euro per km is het fiscale tarief van 2025)",
        "tekst": (
            f"{_getal(afstand, ' km')} tegen {tarief:.2f}".replace(".", ",")
            + f" euro per km is {_getal(rit, ' euro')} enkele reis, "
            + f"{_getal(rit * 2, ' euro')} heen en terug, en "
            + f"{_getal(rit * 2 * 5, ' euro')} per week als je dit vijf dagen doet."
        ),
    }


def gereedschap_rente_op_rente(verzoek: str) -> dict:
    """Wat sparen of beleggen op lange termijn oplevert."""
    getallen = [float(getal.replace(".", "").replace(",", "."))
                for getal in re.findall(r"\d{1,7}(?:[.,]\d{1,2})?", verzoek or "")]
    getallen = [getal for getal in getallen if getal > 0]
    if len(getallen) < 2:
        return {"naam": "rente op rente", "bron": "eigen berekening", "tekst": "",
                "fout": "noem bedrag en percentage, bijvoorbeeld 'rente op rente 5000 euro 3%'"}
    begin = getallen[0]
    rente = getallen[1]
    if rente > 1:
        rente = rente / 100
    jaren = 10
    for kandidaat in (5, 10, 15, 20, 25, 30):
        if f"{kandidaat} jaar" in (verzoek or "").lower() or f"{kandidaat}jaar" in (verzoek or "").lower():
            jaren = kandidaat
            break
    eind = begin * (1 + rente) ** jaren
    return {
        "naam": "rente op rente",
        "bron": "eigen berekening",
        "tekst": (
            f"{_getal(begin, ' euro')} tegen {_getal(rente * 100, '%')} per jaar is na {jaren} jaar "
            f"{_getal(eind, ' euro')}; dat is {_getal(eind - begin, ' euro')} rente. "
            "Zonder tussentijds opnemen en zonder belasting."
        ),
    }


def gereedschap_kamerstukken(plaats: str) -> dict:
    """Wat er in de Tweede Kamer speelt."""
    titels = _nieuwsfeed("site:tweedekamer.nl", "Tweede Kamer")
    if not titels:
        return {"naam": "kamerstukken", "bron": "tweedekamer.nl", "tekst": "",
                "fout": "geen Kamernieuws gevonden"}
    return {"naam": "kamerstukken", "bron": "tweedekamer.nl (via Google Nieuws)",
            "tekst": "Uit de Tweede Kamer: " + " | ".join(titels)}


def gereedschap_bekendmakingen(plaats: str) -> dict:
    """Wat gemeenten en het Rijk officieel bekendmaken."""
    zoek = f"site:officielebekendmakingen.nl {plaats}".strip()
    titels = _nieuwsfeed(zoek, "bekendmakingen")
    if not titels:
        return {"naam": "bekendmakingen", "bron": "officielebekendmakingen.nl", "tekst": "",
                "fout": f"geen bekendmakingen gevonden{' over ' + plaats if plaats else ''}"}
    return {
        "naam": "bekendmakingen",
        "bron": "officielebekendmakingen.nl (via Google Nieuws)",
        "tekst": "Officiële bekendmakingen: " + " | ".join(titels),
    }


def gereedschap_verkiezingen(plaats: str) -> dict:
    """De laatste verkiezingsuitslagen."""
    titels = _nieuwsfeed("verkiezingsuitslag uitslag Kiesraad", "verkiezingen")
    if not titels:
        return {"naam": "verkiezingen", "bron": "kiesraad.nl", "tekst": "",
                "fout": "geen uitslagen gevonden"}
    return {
        "naam": "verkiezingen",
        "bron": "kiesraad.nl (via Google Nieuws); de officiële cijfers staan op kiesraad.nl",
        "tekst": "Verkiezingen in het nieuws: " + " | ".join(titels),
    }


def gereedschap_subsidies(plaats: str) -> dict:
    """Welke subsidie of regeling in het nieuws is."""
    titels = _nieuwsfeed("site:rijksoverheid.nl subsidie regeling", "subsidies")
    if not titels:
        return {"naam": "subsidies", "bron": "rijksoverheid.nl", "tekst": "",
                "fout": "geen subsidienieuws gevonden"}
    return {"naam": "subsidies", "bron": "rijksoverheid.nl (via Google Nieuws)",
            "tekst": "Subsidies en regelingen in het nieuws: " + " | ".join(titels)}


def gereedschap_wat_verandert_er(plaats: str) -> dict:
    """Wat er per 1 januari verandert."""
    titels = _nieuwsfeed('site:rijksoverheid.nl "1 januari" wijziging', "wijzigingen")
    if not titels:
        return {"naam": "wat verandert er", "bron": "rijksoverheid.nl", "tekst": "",
                "fout": "geen berichten over wijzigingen gevonden"}
    return {"naam": "wat verandert er", "bron": "rijksoverheid.nl (via Google Nieuws)",
            "tekst": "Wat er verandert: " + " | ".join(titels)}


def gereedschap_vergunningen(plaats: str) -> dict:
    """Welke vergunning je nodig hebt."""
    zoek = f"omgevingsloket vergunning {plaats}".strip()
    titels = _nieuwsfeed(zoek, "vergunningen")
    tekst = ("Het Omgevingsloket heeft geen open API: de vergunningscheck doorloopt een "
             "vragenlijst in de browser. Kijk op https://www.omgevingsloket.nl/.")
    if titels:
        tekst += " Wat er in het nieuws over vergunningen staat: " + " | ".join(titels[:4])
    return {"naam": "vergunningen", "bron": "omgevingsloket.nl", "tekst": tekst}


def gereedschap_provincie(plaats: str) -> dict:
    """Wat je provincie besluit."""
    zoek = f"provincie {plaats} besluit".strip() if plaats else "provinciale staten besluit"
    titels = _nieuwsfeed(zoek, "provincie")
    if not titels:
        return {"naam": "provincie", "bron": "provincies", "tekst": "",
                "fout": "geen provinciaal nieuws gevonden"}
    return {"naam": "provincie", "bron": "provinciaal nieuws (via Google Nieuws)",
            "tekst": "Uit de provincie: " + " | ".join(titels)}


def gereedschap_wetten_in_werking(plaats: str) -> dict:
    """Welke wetten en regels deze week ingaan."""
    titels = _nieuwsfeed("site:officielebekendmakingen.nl inwerkingtreding", "wetten")
    if not titels:
        return {"naam": "wetten in werking", "bron": "officielebekendmakingen.nl", "tekst": "",
                "fout": "geen bekendmakingen over inwerkingtreding gevonden"}
    return {"naam": "wetten in werking", "bron": "officielebekendmakingen.nl (via Google Nieuws)",
            "tekst": "In werking getreden: " + " | ".join(titels)}


def gereedschap_gemeente_open_data(plaats: str) -> dict:
    """De kerncijfers van een gemeente uit de CBS-open-data."""
    plaats = (plaats or "").strip() or GEREEDSCHAP_PLAATS
    if not plaats:
        return {"naam": "gemeente open data", "bron": "opendata.cbs.nl", "tekst": "",
                "fout": "geen gemeente opgegeven"}
    filter_ = urllib.parse.quote(f"startswith(Title,'{plaats}')", safe="()'")
    regio = _haal_json(f"https://opendata.cbs.nl/ODataApi/odata/70072ned/RegioS?$filter={filter_}&$top=10")
    kandidaten = [rij for rij in ((regio or {}).get("value") or [])
                  if str(rij.get("Key") or "").startswith("GM")]
    if not kandidaten:
        return {"naam": "gemeente open data", "bron": "opendata.cbs.nl", "tekst": "",
                "fout": f"geen gemeente gevonden die begint met '{plaats}'"}
    kandidaten.sort(key=lambda rij: (str(rij.get("Title")) != plaats, -len(str(rij.get("Title")))))
    code = str(kandidaten[0].get("Key") or "")
    naam = str(kandidaten[0].get("Title") or plaats)
    gegevens = _haal_json(
        "https://opendata.cbs.nl/ODataApi/odata/70072ned/TypedDataSet?"
        f"%24filter=RegioS%20eq%20%27{code}%27"
        "&%24select=Perioden%2CTotaleBevolking_1%2CMannen_2%2CVrouwen_3%2CBevolkingsdichtheid_27"
    )
    rijen = [rij for rij in ((gegevens or {}).get("value") or [])
             if isinstance(rij.get("TotaleBevolking_1"), (int, float))]
    if not rijen:
        return {"naam": "gemeente open data", "bron": "opendata.cbs.nl", "tekst": "",
                "fout": f"geen cijfers voor {naam}"}
    rij = max(rijen, key=lambda stuk: str(stuk.get("Perioden") or ""))
    return {
        "naam": "gemeente open data",
        "bron": f"opendata.cbs.nl (tabel 70072ned, {rij.get('Perioden')})",
        "tekst": (
            f"{naam}: {_getal(rij.get('TotaleBevolking_1'), komma=False)} inwoners "
            f"({_getal(rij.get('Mannen_2'), komma=False)} mannen, "
            f"{_getal(rij.get('Vrouwen_3'), komma=False)} vrouwen), "
            f"{_getal(rij.get('Bevolkingsdichtheid_27'), komma=False)} inwoners per km²."
        ),
    }


def gereedschap_wikipedia_volledig(onderwerp: str) -> dict:
    """Een heel Wikipedia-artikel in plaats van alleen de inleiding."""
    onderwerp = (onderwerp or "").strip()
    if not onderwerp:
        return {"naam": "wikipedia volledig", "bron": "nl.wikipedia.org", "tekst": "",
                "fout": "geen onderwerp opgegeven"}
    gegevens = _haal_json(
        "https://nl.wikipedia.org/w/api.php?action=query&format=json&generator=search"
        f"&gsrsearch={urllib.parse.quote(onderwerp)}&gsrlimit=1&prop=extracts&explaintext=1"
    )
    paginas = ((gegevens or {}).get("query") or {}).get("pages") or {}
    for pagina in paginas.values():
        tekst = _schoon(str(pagina.get("extract") or ""))
        if tekst:
            return {
                "naam": "wikipedia volledig",
                "bron": f"nl.wikipedia.org/wiki/{urllib.parse.quote(str(pagina.get('title') or onderwerp))}",
                "tekst": f"{pagina.get('title')}: {_kort(tekst, 2200)}",
            }
    return {"naam": "wikipedia volledig", "bron": "nl.wikipedia.org", "tekst": "",
            "fout": f"geen artikel gevonden over '{onderwerp}'"}


def gereedschap_woord_van_de_dag(plaats: str) -> dict:
    """Een woord met betekenis uit het WikiWoordenboek."""
    try:
        lijst = _haal_json(
            "https://nl.wiktionary.org/w/api.php?action=query&format=json"
            "&list=random&rnnamespace=0&rnlimit=1"
        )
    except Exception as fout:
        return {"naam": "woord van de dag", "bron": "nl.wiktionary.org", "tekst": "",
                "fout": f"{type(fout).__name__}: {fout}"}
    kandidaten = ((lijst or {}).get("query") or {}).get("random") or []
    titel = str((kandidaten[0] if kandidaten else {}).get("title") or "")
    if not titel:
        return {"naam": "woord van de dag", "bron": "nl.wiktionary.org", "tekst": "",
                "fout": "geen woord ontvangen"}
    try:
        gegevens = _haal_json(
            "https://nl.wiktionary.org/w/api.php?action=query&format=json&prop=extracts"
            f"&explaintext=1&titles={urllib.parse.quote(titel)}"
        )
    except Exception:
        gegevens = {}
    paginas = ((gegevens or {}).get("query") or {}).get("pages") or {}
    tekst = ""
    for pagina in paginas.values():
        tekst = _schoon(str(pagina.get("extract") or ""))
    betekenis = ""
    blok = re.search(r"(?:Betekenis|-bet-\}\})(.*?)(?:\n==|\Z)", tekst or "", re.S)
    if blok:
        regels = []
        for regel in blok.group(1).split("\n"):
            regel = regel.strip()
            if not regel or regel.startswith(("=", "{{", "|", "*", ":", "[[Bestand")):
                continue
            regel = re.sub(r"\[\[[^\]|]*\|([^\]]*)\]\]", r"\1", regel)
            regel = re.sub(r"\[\[([^\]]*)\]\]", r"\1", regel)
            regel = re.sub(r"\{\{[^}]*\}\}", "", regel).strip()
            if regel:
                regels.append(regel)
        betekenis = " ".join(regels)[:400]
    return {
        "naam": "woord van de dag",
        "bron": f"nl.wiktionary.org/wiki/{urllib.parse.quote(titel)} "
                "(willekeurig woord; het echte woord van de dag heeft geen open feed)",
        "tekst": f"{titel}: " + (betekenis or f"geen leesbare betekenis in de samenvatting; "
                                            f"bekijk https://nl.wiktionary.org/wiki/{urllib.parse.quote(titel)}"),
    }


def gereedschap_rijmwoorden(woord: str) -> dict:
    """Woorden die rijmen, uit de Nederlandse woordenlijst."""
    woord = (woord or "").strip().lower().split()[-1] if (woord or "").strip() else ""
    if len(woord) < 2:
        return {"naam": "rijmwoorden", "bron": "OpenTaal-woordenlijst", "tekst": "",
                "fout": "geen woord opgegeven"}
    try:
        woordenlijst = _woordenlijst()
    except Exception as fout:
        return {"naam": "rijmwoorden", "bron": "OpenTaal-woordenlijst", "tekst": "",
                "fout": f"woordenlijst niet op te halen ({type(fout).__name__})"}
    staart = woord[-3:]
    kandidaten = sorted(
        (stuk for stuk in woordenlijst
         if stuk.endswith(staart) and stuk != woord and len(stuk) <= len(woord) + 4 and " " not in stuk),
        key=len,
    )
    if not kandidaten:
        return {"naam": "rijmwoorden", "bron": "OpenTaal-woordenlijst", "tekst": "",
                "fout": f"geen rijmwoorden gevonden voor '{woord}'"}
    return {
        "naam": "rijmwoorden",
        "bron": f"OpenTaal-woordenlijst ({len(woordenlijst)} woorden)",
        "tekst": f"Rijmt op '{woord}' (zelfde laatste drie letters): " + ", ".join(kandidaten[:12]),
    }


def gereedschap_synoniemen_engels(woord: str) -> dict:
    """Een Engels synoniem (Datamuse)."""
    woord = (woord or "").strip().split()[-1] if (woord or "").strip() else ""
    if not woord:
        return {"naam": "synoniemen engels", "bron": "datamuse.com", "tekst": "",
                "fout": "geen woord opgegeven"}
    gegevens = _haal_json(
        f"https://api.datamuse.com/words?rel_syn={urllib.parse.quote(woord)}&max=12"
    )
    woorden = [str(stuk.get("word")) for stuk in (gegevens or []) if stuk.get("word")]
    if not woorden:
        return {"naam": "synoniemen engels", "bron": "datamuse.com", "tekst": "",
                "fout": f"geen Engelse synoniemen voor '{woord}'"}
    return {"naam": "synoniemen engels", "bron": "datamuse.com", "tekst":
            f"Engelse synoniemen van '{woord}': " + ", ".join(woorden)}


ONREGELMATIG = {
    "zijn": ("ben", "bent", "is", "zijn", "was", "waren", "geweest"),
    "hebben": ("heb", "hebt", "heeft", "hebben", "had", "hadden", "gehad"),
    "gaan": ("ga", "gaat", "gaat", "gaan", "ging", "gingen", "gegaan"),
    "komen": ("kom", "komt", "komt", "komen", "kwam", "kwamen", "gekomen"),
    "doen": ("doe", "doet", "doet", "doen", "deed", "deden", "gedaan"),
    "zien": ("zie", "ziet", "ziet", "zien", "zag", "zagen", "gezien"),
    "lopen": ("loop", "loopt", "loopt", "lopen", "liep", "liepen", "gelopen"),
    "eten": ("eet", "eet", "eet", "eten", "at", "aten", "gegeten"),
    "geven": ("geef", "geeft", "geeft", "geven", "gaf", "gaven", "gegeven"),
    "nemen": ("neem", "neemt", "neemt", "nemen", "nam", "namen", "genomen"),
    "blijven": ("blijf", "blijft", "blijft", "blijven", "bleef", "bleven", "gebleven"),
    "worden": ("word", "wordt", "wordt", "worden", "werd", "werden", "geworden"),
    "weten": ("weet", "weet", "weet", "weten", "wist", "wisten", "geweten"),
    "zeggen": ("zeg", "zegt", "zegt", "zeggen", "zei", "zeiden", "gezegd"),
    "kunnen": ("kan", "kunt", "kan", "kunnen", "kon", "konden", "gekund"),
    "moeten": ("moet", "moet", "moet", "moeten", "moest", "moesten", "gemoeten"),
    "willen": ("wil", "wilt", "wil", "willen", "wilde", "wilden", "gewild"),
    "mogen": ("mag", "mag", "mag", "mogen", "mocht", "mochten", "gemogen"),
    "zitten": ("zit", "zit", "zit", "zitten", "zat", "zaten", "gezeten"),
    "liggen": ("lig", "ligt", "ligt", "liggen", "lag", "lagen", "gelegen"),
    "staan": ("sta", "staat", "staat", "staan", "stond", "stonden", "gestaan"),
    "denken": ("denk", "denkt", "denkt", "denken", "dacht", "dachten", "gedacht"),
    "vinden": ("vind", "vindt", "vindt", "vinden", "vond", "vonden", "gevonden"),
    "brengen": ("breng", "brengt", "brengt", "brengen", "bracht", "brachten", "gebracht"),
    "kopen": ("koop", "koopt", "koopt", "kopen", "kocht", "kochten", "gekocht"),
    "maken": ("maak", "maakt", "maakt", "maken", "maakte", "maakten", "gemaakt"),
    "werken": ("werk", "werkt", "werkt", "werken", "werkte", "werkten", "gewerkt"),
    "wonen": ("woon", "woont", "woont", "wonen", "woonde", "woonden", "gewoond"),
    "praten": ("praat", "praat", "praat", "praten", "praatte", "praatten", "gepraat"),
}


def gereedschap_werkwoorden(woord: str) -> dict:
    """Een werkwoord vervoegen in de tegenwoordige en verleden tijd."""
    woord = (woord or "").strip().lower().split()[-1] if (woord or "").strip() else ""
    if not woord:
        return {"naam": "werkwoorden", "bron": "eigen berekening", "tekst": "",
                "fout": "noem een werkwoord, bijvoorbeeld 'vervoeg lopen'"}
    if woord in ONREGELMATIG:
        ik, jij, hij, wij, verleden, verleden_meervoud, voltooid = ONREGELMATIG[woord]
        return {
            "naam": "werkwoorden",
            "bron": "eigen tabel met onregelmatige werkwoorden",
            "tekst": (
                f"{woord}: ik {ik}, jij {jij}, hij {hij}, wij {wij}. "
                f"Verleden tijd: ik {verleden}, wij {verleden_meervoud}. "
                f"Voltooid: heeft {voltooid}."
            ),
        }
    if not woord.endswith("en") or len(woord) < 4:
        return {"naam": "werkwoorden", "bron": "eigen berekening", "tekst": "",
                "fout": f"'{woord}' ziet er niet als een Nederlands werkwoord uit"}
    stam = woord[:-2]
    klinkers = "aeiou"
    if stam.endswith(("t", "k", "f", "s", "p", "ch")):
        verleden = stam + "te"
        verleden_meervoud = stam + "ten"
    else:
        verleden = stam + "de"
        verleden_meervoud = stam + "den"
    if stam[-1] in klinkers and len(stam) > 2 and stam[-2] not in klinkers:
        stam = stam + stam[-1]
    voltooid = ("ge" + stam + ("t" if verleden.endswith("te") else "d"))
    return {
        "naam": "werkwoorden",
        "bron": "eigen berekening volgens de regels van de zwakke werkwoorden",
        "tekst": (
            f"{woord}: ik {stam}, jij {stam}t, hij {stam}t, wij {woord}. "
            f"Verleden tijd: ik {verleden}, wij {verleden_meervoud}. Voltooid: heeft {voltooid}. "
            "Sterke werkwoorden staan in mijn eigen tabel; zit je woord er niet bij, dan is dit een gok."
        ),
    }


EENHEDEN_GETAL = [
    "nul", "een", "twee", "drie", "vier", "vijf", "zes", "zeven", "acht", "negen", "tien",
    "elf", "twaalf", "dertien", "veertien", "vijftien", "zestien", "zeventien", "achttien",
    "negentien",
]
TIENTALLEN = ["", "", "twintig", "dertig", "veertig", "vijftig", "zestig", "zeventig",
              "tachtig", "negentig"]


def _getal_naar_woord(waarde: int) -> str:
    if waarde < 0:
        return "min " + _getal_naar_woord(-waarde)
    if waarde < 20:
        return EENHEDEN_GETAL[waarde]
    if waarde < 100:
        tiental = waarde // 10
        rest = waarde % 10
        if rest == 0:
            return TIENTALLEN[tiental]
        tussen = "ën" if rest == 2 else "en"
        return EENHEDEN_GETAL[rest] + tussen + TIENTALLEN[tiental]
    if waarde < 1000:
        honderd = waarde // 100
        rest = waarde % 100
        kop = ("honderd" if honderd == 1 else EENHEDEN_GETAL[honderd] + "honderd")
        return kop + ("" if rest == 0 else _getal_naar_woord(rest))
    if waarde < 1000000:
        duizend = waarde // 1000
        rest = waarde % 1000
        kop = ("duizend" if duizend == 1 else _getal_naar_woord(duizend) + "duizend")
        return kop + ("" if rest == 0 else _getal_naar_woord(rest))
    miljoen = waarde // 1000000
    rest = waarde % 1000000
    kop = _getal_naar_woord(miljoen) + " miljoen"
    return kop + ("" if rest == 0 else " " + _getal_naar_woord(rest))


def gereedschap_getallen_in_woorden(verzoek: str) -> dict:
    """Een getal of bedrag in Nederlandse woorden."""
    gevonden = re.findall(r"\d{1,9}(?:[.,]\d{1,2})?", verzoek or "")
    if not gevonden:
        return {"naam": "getallen in woorden", "bron": "eigen berekening", "tekst": "",
                "fout": "noem een getal, bijvoorbeeld 'schrijf 1250 in woorden'"}
    stuk = gevonden[0].replace(".", "").replace(",", ".")
    if "." in stuk:
        heel, _, centen = stuk.partition(".")
        tekst = (f"{_getal_naar_woord(int(heel))} euro en {_getal_naar_woord(int(centen.ljust(2, '0')))} cent"
                 if len(centen) == 2 and "euro" in (verzoek or "").lower()
                 else f"{_getal_naar_woord(int(heel))} komma {_getal_naar_woord(int(centen))}")
    else:
        tekst = _getal_naar_woord(int(stuk))
    return {"naam": "getallen in woorden", "bron": "eigen berekening", "tekst":
            f"{gevonden[0]} is in woorden: {tekst}."}


def gereedschap_citaten(onderwerp: str) -> dict:
    """Bekende uitspraken over een onderwerp (Wikiquote)."""
    onderwerp = (onderwerp or "").strip()
    if not onderwerp:
        return {"naam": "citaten", "bron": "nl.wikiquote.org", "tekst": "", "fout": "geen onderwerp opgegeven"}
    gegevens = _haal_json(
        "https://nl.wikiquote.org/w/api.php?action=query&format=json&list=search"
        f"&srsearch={urllib.parse.quote(onderwerp)}&srlimit=3&srprop=snippet"
    )
    treffers = ((gegevens or {}).get("query") or {}).get("search") or []
    if not treffers:
        return {"naam": "citaten", "bron": "nl.wikiquote.org", "tekst": "",
                "fout": f"geen citaten gevonden over '{onderwerp}'"}
    regels = []
    for treffer in treffers:
        titel = str(treffer.get("title") or "")
        stuk = _schoon(re.sub(r"<[^>]+>", " ", str(treffer.get("snippet") or "")))
        regels.append(f"{titel}: {_kort(stuk, 160)}")
    return {"naam": "citaten", "bron": "nl.wikiquote.org", "tekst": " | ".join(regels)}


def gereedschap_films_en_series(titel: str) -> dict:
    """Een film of serie opzoeken met jaar en regisseur (Wikidata)."""
    titel = (titel or "").strip()
    if not titel:
        return {"naam": "films", "bron": "wikidata.org", "tekst": "", "fout": "geen titel opgegeven"}
    vraag = (
        'SELECT ?filmLabel ?jaar ?regisseurLabel WHERE {'
        ' ?film wdt:P31/wdt:P279* wd:Q11424 .'
        f' ?film rdfs:label "{titel}"@nl .'
        ' OPTIONAL { ?film wdt:P577 ?jaar . }'
        ' OPTIONAL { ?film wdt:P57 ?regisseur . }'
        ' SERVICE wikibase:label { bd:serviceParam wikibase:language "nl,en". }'
        '} LIMIT 5'
    )
    gegevens = _haal_json(
        "https://query.wikidata.org/sparql?format=json&query=" + urllib.parse.quote(vraag), limiet=30
    )
    rijen = (((gegevens or {}).get("results") or {}).get("bindings") or [])
    if not rijen:
        return {"naam": "films", "bron": "wikidata.org", "tekst": "",
                "fout": f"geen film of serie gevonden met de titel '{titel}'"}
    regels = []
    for rij in rijen[:4]:
        naam = str((rij.get("filmLabel") or {}).get("value") or "?")
        jaar = str((rij.get("jaar") or {}).get("value") or "")[:4]
        regisseur = str((rij.get("regisseurLabel") or {}).get("value") or "")
        stuk = naam + (f" ({jaar})" if jaar else "")
        if regisseur and regisseur != naam:
            stuk += f", regie {regisseur}"
        if stuk not in regels:
            regels.append(stuk)
    return {"naam": "films", "bron": "wikidata.org (CC0)", "tekst": " | ".join(regels)}


def gereedschap_muziekalbums(artiest: str) -> dict:
    """Albums van een artiest (MusicBrainz)."""
    artiest = (artiest or "").strip()
    if not artiest:
        return {"naam": "muziekalbums", "bron": "musicbrainz.org", "tekst": "", "fout": "geen artiest opgegeven"}
    gegevens = _haal_json(
        "https://musicbrainz.org/ws/2/release?query="
        + urllib.parse.quote(f'artist:"{artiest}"')
        + "&fmt=json&limit=6",
        limiet=25,
    )
    albums = (gegevens or {}).get("releases") or []
    if not albums:
        return {"naam": "muziekalbums", "bron": "musicbrainz.org", "tekst": "",
                "fout": f"geen albums gevonden van '{artiest}'"}
    regels = []
    for album in albums:
        titel = str(album.get("title") or "?")
        datum = str(album.get("date") or "")
        regels.append(f"{titel}" + (f" ({datum[:4]})" if datum else ""))
    return {"naam": "muziekalbums", "bron": "musicbrainz.org", "tekst":
            f"Albums van {artiest}: " + ", ".join(regels)}


def gereedschap_landeninfo(land: str) -> dict:
    """Hoofdstad, inwoners, oppervlakte en valuta van een land (Wikidata)."""
    land = (land or "").strip()
    if not land:
        return {"naam": "landeninfo", "bron": "wikidata.org", "tekst": "", "fout": "geen land opgegeven"}
    vraag = (
        "SELECT ?landLabel ?hoofdstadLabel ?inwoners ?oppervlakte ?valutaLabel WHERE {"
        f' ?land rdfs:label "{land}"@nl .'
        " ?land wdt:P31/wdt:P279* wd:Q6256 ."
        " OPTIONAL { ?land wdt:P36 ?hoofdstad . }"
        " OPTIONAL { ?land wdt:P1082 ?inwoners . }"
        " OPTIONAL { ?land wdt:P2046 ?oppervlakte . }"
        " OPTIONAL { ?land wdt:P38 ?valuta . }"
        ' SERVICE wikibase:label { bd:serviceParam wikibase:language "nl,en". }'
        "} LIMIT 1"
    )
    try:
        gegevens = _haal_json(
            "https://query.wikidata.org/sparql?format=json&query=" + urllib.parse.quote(vraag),
            limiet=30,
        )
    except Exception as fout:
        return {"naam": "landeninfo", "bron": "wikidata.org", "tekst": "",
                "fout": f"{type(fout).__name__}: {fout}"}
    rijen = ((gegevens or {}).get("results") or {}).get("bindings") or []
    if not rijen:
        return {"naam": "landeninfo", "bron": "wikidata.org", "tekst": "",
                "fout": f"geen land gevonden met de naam '{land}'"}
    rij = rijen[0]

    def _waarde(sleutel: str) -> str:
        return str((rij.get(sleutel) or {}).get("value") or "")

    naam = _waarde("landLabel") or land
    hoofdstad = _waarde("hoofdstadLabel") or "?"
    inwoners = _waarde("inwoners")
    oppervlakte = _waarde("oppervlakte")
    valuta = _waarde("valutaLabel") or "?"
    stukken = [f"hoofdstad {hoofdstad}"]
    if inwoners:
        try:
            stukken.append(f"{int(float(inwoners)):,} inwoners".replace(",", "."))
        except ValueError:
            pass
    if oppervlakte:
        try:
            stukken.append(f"{int(float(oppervlakte)):,} km²".replace(",", "."))
        except ValueError:
            pass
    stukken.append(f"valuta {valuta}")
    return {
        "naam": "landeninfo",
        "bron": "wikidata.org (CC0)",
        "tekst": f"{naam}: " + ", ".join(stukken) + ".",
    }


def gereedschap_nobelprijzen(plaats: str) -> dict:
    """De meest recente Nobelprijzen."""
    gegevens = _haal_json("https://api.nobelprize.org/2.1/nobelPrizes?limit=6&sort=desc", limiet=25)
    prijzen = (gegevens or {}).get("nobelPrizes") or []
    if not prijzen:
        return {"naam": "nobelprijzen", "bron": "nobelprize.org", "tekst": "",
                "fout": "geen Nobelprijzen ontvangen"}
    regels = []
    for prijs in prijzen[:5]:
        jaar = str(prijs.get("awardYear") or "?")
        categorie = ((prijs.get("category") or {}).get("nl") or (prijs.get("category") or {}).get("en") or "?")
        laureaten = []
        for laureaat in prijs.get("laureates") or []:
            naam = ((laureaat.get("knownName") or {}).get("nl")
                    or (laureaat.get("knownName") or {}).get("en")
                    or (laureaat.get("orgName") or {}).get("nl") or "")
            if naam:
                laureaten.append(naam)
        regels.append(f"{jaar} {categorie}: " + (", ".join(laureaten) or "?"))
    return {"naam": "nobelprijzen", "bron": "nobelprize.org", "tekst": " | ".join(regels)}


def gereedschap_vandaag_in_het_jaar(plaats: str) -> dict:
    """Wat er op deze dag in de geschiedenis gebeurde."""
    vandaag = datetime.date.today()
    gegevens = _haal_json(
        f"https://en.wikipedia.org/api/rest_v1/feed/onthisday/events/{vandaag.month}/{vandaag.day}",
        limiet=25,
    )
    gebeurtenissen = (gegevens or {}).get("events") or []
    if not gebeurtenissen:
        return {"naam": "vandaag in het jaar", "bron": "en.wikipedia.org", "tekst": "",
                "fout": "geen gebeurtenissen ontvangen"}
    regels = []
    for gebeurtenis in gebeurtenissen[:5]:
        jaar = gebeurtenis.get("year")
        tekst = _schoon(str(gebeurtenis.get("text") or ""))
        if tekst:
            regels.append(f"{jaar}: {_kort(tekst, 150)}")
    return {
        "naam": "vandaag in het jaar",
        "bron": "en.wikipedia.org (on this day)",
        "tekst": f"Op {vandaag.day}-{vandaag.month:02d} in de geschiedenis: " + " | ".join(regels),
    }


def gereedschap_definitie(woord: str) -> dict:
    """De betekenis van een woord volgens het WikiWoordenboek."""
    woord = (woord or "").strip().split()[-1] if (woord or "").strip() else ""
    if not woord:
        return {"naam": "definitie", "bron": "nl.wiktionary.org", "tekst": "", "fout": "geen woord opgegeven"}
    gegevens = _haal_json(
        "https://nl.wiktionary.org/w/api.php?action=query&format=json&prop=extracts&explaintext=1"
        f"&titles={urllib.parse.quote(woord)}&redirects=1"
    )
    paginas = ((gegevens or {}).get("query") or {}).get("pages") or {}
    for pagina in paginas.values():
        tekst = _schoon(str(pagina.get("extract") or ""))
        if tekst:
            return {
                "naam": "definitie",
                "bron": f"nl.wiktionary.org/wiki/{urllib.parse.quote(woord)}",
                "tekst": f"{woord}: {_kort(tekst, 700)}",
            }
    return {"naam": "definitie", "bron": "nl.wiktionary.org", "tekst": "",
            "fout": f"geen lemma gevonden voor '{woord}'"}

# ---- J. nieuws, gezondheid en de buurt

def gereedschap_nieuws_onderwerp(onderwerp: str) -> dict:
    """Het nieuws over één onderwerp of bedrijf."""
    onderwerp = (onderwerp or "").strip()
    if not onderwerp:
        return {"naam": "nieuws onderwerp", "bron": "nieuwsfeed", "tekst": "",
                "fout": "noem een onderwerp, bijvoorbeeld 'nieuws over ASML'"}
    titels = _nieuwsfeed(onderwerp, "onderwerp")
    if not titels:
        return {"naam": "nieuws onderwerp", "bron": "nieuwsfeed", "tekst": "",
                "fout": f"geen nieuws gevonden over '{onderwerp}'"}
    return {"naam": "nieuws onderwerp", "bron": "Google Nieuws", "tekst":
            f"Nieuws over {onderwerp}: " + " | ".join(titels)}


def gereedschap_nieuws_gemeente(plaats: str) -> dict:
    """Wat er in jouw plaats gebeurt."""
    plaats = (plaats or "").strip() or GEREEDSCHAP_PLAATS
    if not plaats:
        return {"naam": "nieuws gemeente", "bron": "nieuwsfeed", "tekst": "", "fout": "geen plaats opgegeven"}
    titels = _nieuwsfeed(plaats, "gemeente")
    if not titels:
        return {"naam": "nieuws gemeente", "bron": "nieuwsfeed", "tekst": "",
                "fout": f"geen nieuws gevonden over {plaats}"}
    return {"naam": "nieuws gemeente", "bron": "Google Nieuws", "tekst":
            f"Nieuws uit {plaats}: " + " | ".join(titels)}


def gereedschap_sportuitslagen(plaats: str) -> dict:
    """De sportuitslagen van vandaag (NOS)."""
    try:
        ruw = _haal("https://feeds.nos.nl/nossportalgemeen", limiet=20).decode("utf-8", "replace")
    except Exception as fout:
        return {"naam": "sportuitslagen", "bron": "nos.nl", "tekst": "", "fout": f"{type(fout).__name__}: {fout}"}
    titels = [_schoon(re.sub(r"<[^>]+>", " ", stuk))
              for stuk in re.findall(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", ruw, re.S)]
    titels = [titel for titel in titels if titel and len(titel) > 10][:7]
    if not titels:
        return {"naam": "sportuitslagen", "bron": "nos.nl", "tekst": "", "fout": "geen sportberichten gevonden"}
    return {"naam": "sportuitslagen", "bron": "feeds.nos.nl/nossportalgemeen", "tekst":
            "Sport van vandaag: " + " | ".join(titels)}


def gereedschap_tv_gids(plaats: str) -> dict:
    """Wat er vanavond op tv is."""
    try:
        ruw = _haal("https://www.tvgids.nl/", limiet=25).decode("utf-8", "replace")
    except Exception as fout:
        return {"naam": "tv-gids", "bron": "tvgids.nl", "tekst": "", "fout": f"{type(fout).__name__}: {fout}"}
    blokken = re.findall(r'<script type="application/ld\+json">(.*?)</script>', ruw, re.S)
    namen = []
    for blok in blokken:
        try:
            gegevens = json.loads(blok)
        except Exception:
            continue
        stapel = [gegevens]
        while stapel:
            stuk = stapel.pop()
            if isinstance(stuk, dict):
                if stuk.get("@type") in ("TVEpisode", "Movie", "CreativeWork") and stuk.get("name"):
                    namen.append(str(stuk["name"]))
                stapel.extend(stuk.values())
            elif isinstance(stuk, list):
                stapel.extend(stuk)
    namen = list(dict.fromkeys(namen))[:8]
    if not namen:
        return {
            "naam": "tv-gids",
            "bron": "tvgids.nl",
            "tekst": ("tvgids.nl heeft geen open API en de programmalijst staat niet in de pagina. "
                      "Kijk op https://www.tvgids.nl/ voor vanavond."),
        }
    return {"naam": "tv-gids", "bron": "tvgids.nl", "tekst": "Vanavond op tv: " + ", ".join(namen)}


def gereedschap_podcasts(zoek: str) -> dict:
    """Podcasts zoeken (iTunes)."""
    zoek = (zoek or "").strip()
    if not zoek:
        return {"naam": "podcasts", "bron": "itunes.apple.com", "tekst": "",
                "fout": "noem een onderwerp, bijvoorbeeld 'podcasts over geschiedenis'"}
    gegevens = _haal_json(
        "https://itunes.apple.com/search?media=podcast&entity=podcast&limit=8&country=NL&term="
        + urllib.parse.quote(zoek),
        limiet=20,
    )
    podcasts = (gegevens or {}).get("results") or []
    if not podcasts:
        return {"naam": "podcasts", "bron": "itunes.apple.com", "tekst": "",
                "fout": f"geen podcasts gevonden over '{zoek}'"}
    regels = []
    for podcast in podcasts[:6]:
        naam = str(podcast.get("collectionName") or podcast.get("trackName") or "?")
        maker = str(podcast.get("artistName") or "")
        regels.append(naam + (f" ({maker})" if maker else ""))
    return {"naam": "podcasts", "bron": "itunes.apple.com", "tekst":
            f"Podcasts over {zoek}: " + " | ".join(regels)}


def gereedschap_radiostations(plaats: str) -> dict:
    """De Nederlandse radiostations met hun stream."""
    zenders = [
        ("NPO Radio 1", "https://icecast.omroep.nl/radio1-bb-mp3"),
        ("NPO Radio 2", "https://icecast.omroep.nl/radio2-bb-mp3"),
        ("NPO 3FM", "https://icecast.omroep.nl/3fm-bb-mp3"),
        ("NPO Radio 4", "https://icecast.omroep.nl/radio4-bb-mp3"),
        ("NPO Radio 5", "https://icecast.omroep.nl/radio5-bb-mp3"),
        ("FunX", "https://icecast.omroep.nl/funx-bb-mp3"),
    ]
    return {
        "naam": "radiostations",
        "bron": "icecast.omroep.nl (NPO)",
        "tekst": "Nederlandse radiostreams: " + " | ".join(f"{naam}: {adres}" for naam, adres in zenders),
    }


def gereedschap_film_op_tv(plaats: str) -> dict:
    """Welke films er vanavond op tv zijn."""
    titels = _nieuwsfeed("film op tv vanavond", "film op tv")
    tekst = ("tvgids.nl heeft geen open filmlijst; kijk op https://www.tvgids.nl/films voor vanavond.")
    if titels:
        tekst += " In het nieuws: " + " | ".join(titels[:3])
    return {"naam": "film op tv", "bron": "tvgids.nl", "tekst": tekst}


def gereedschap_nieuwsarchief(onderwerp: str) -> dict:
    """Zoeken in ouder nieuws van de laatste maand."""
    onderwerp = (onderwerp or "").strip()
    if not onderwerp:
        return {"naam": "nieuwsarchief", "bron": "nieuwsfeed", "tekst": "",
                "fout": "noem een onderwerp om in te zoeken"}
    titels = _nieuwsfeed(f"{onderwerp} when:30d", "archief")
    if not titels:
        return {"naam": "nieuwsarchief", "bron": "nieuwsfeed", "tekst": "",
                "fout": f"geen berichten over '{onderwerp}' in de laatste maand"}
    return {"naam": "nieuwsarchief", "bron": "Google Nieuws (laatste 30 dagen)", "tekst":
            f"Over {onderwerp}: " + " | ".join(titels)}


def _openfood(barcode: str) -> dict:
    return _haal_json(
        f"https://world.openfoodfacts.org/api/v2/product/{urllib.parse.quote(barcode)}.json",
        limiet=25,
    )


def _openfood_zoek(zoek: str) -> list:
    gegevens = _haal_json(
        "https://world.openfoodfacts.org/api/v2/search?search_terms="
        + urllib.parse.quote(zoek) + "&page_size=5&fields=product_name,brands,nutriments,allergens_tags,code",
        limiet=30,
    )
    return (gegevens or {}).get("products") or []


def gereedschap_calorieen(zoek: str) -> dict:
    """Hoeveel calorieën iets bevat (Open Food Facts)."""
    zoek = (zoek or "").strip()
    if not zoek:
        return {"naam": "calorieen", "bron": "openfoodfacts.org", "tekst": "",
                "fout": "noem een product, bijvoorbeeld 'calorieen van melk'"}
    try:
        producten = _openfood_zoek(zoek)
    except Exception as fout:
        return {"naam": "calorieen", "bron": "openfoodfacts.org", "tekst": "",
                "fout": f"de voedingsdatabase is even niet bereikbaar ({type(fout).__name__})"}
    regels = []
    for product in producten[:5]:
        naam = str(product.get("product_name") or "").strip()
        voedingsstoffen = product.get("nutriments") or {}
        kcal = voedingsstoffen.get("energy-kcal_100g")
        if naam and isinstance(kcal, (int, float)):
            regels.append(f"{naam}: {_getal(kcal, ' kcal per 100 gram')}")
    if not regels:
        return {"naam": "calorieen", "bron": "openfoodfacts.org", "tekst": "",
                "fout": f"geen voedingswaarde gevonden voor '{zoek}'"}
    return {"naam": "calorieen", "bron": "openfoodfacts.org (ODbL)", "tekst":
            "Calorieën per 100 gram: " + " | ".join(regels)}


def gereedschap_voedingswaarde(barcode: str) -> dict:
    """Eiwit, vet en koolhydraten van een product."""
    barcode = (barcode or "").strip()
    gevonden = re.search(r"\d{8,14}", barcode)
    code = gevonden.group(0) if gevonden else barcode
    if not code:
        return {"naam": "voedingswaarde", "bron": "openfoodfacts.org", "tekst": "",
                "fout": "noem een barcode of een productnaam"}
    try:
        gegevens = _openfood(code)
    except Exception as fout:
        return {"naam": "voedingswaarde", "bron": "openfoodfacts.org", "tekst": "",
                "fout": f"de voedingsdatabase is even niet bereikbaar ({type(fout).__name__})"}
    product = (gegevens or {}).get("product") or {}
    if not product:
        return {"naam": "voedingswaarde", "bron": "openfoodfacts.org", "tekst": "",
                "fout": f"geen product gevonden met barcode {code}"}
    voedingsstoffen = product.get("nutriments") or {}
    naam = str(product.get("product_name") or code)
    return {
        "naam": "voedingswaarde",
        "bron": f"openfoodfacts.org/{code}",
        "tekst": (
            f"{naam} per 100 gram: {_getal(voedingsstoffen.get('energy-kcal_100g'), ' kcal')}, "
            f"eiwit {_getal(voedingsstoffen.get('proteins_100g'), ' g')}, "
            f"vet {_getal(voedingsstoffen.get('fat_100g'), ' g')}, "
            f"koolhydraten {_getal(voedingsstoffen.get('carbohydrates_100g'), ' g')}, "
            f"suiker {_getal(voedingsstoffen.get('sugars_100g'), ' g')}."
        ),
    }


def gereedschap_allergenen(barcode: str) -> dict:
    """Of er melk, noten of gluten in een product zitten."""
    code = re.search(r"\d{8,14}", barcode or "")
    if not code:
        return {"naam": "allergenen", "bron": "openfoodfacts.org", "tekst": "",
                "fout": "noem een barcode van het product"}
    try:
        gegevens = _openfood(code.group(0))
    except Exception as fout:
        return {"naam": "allergenen", "bron": "openfoodfacts.org", "tekst": "",
                "fout": f"de voedingsdatabase is even niet bereikbaar ({type(fout).__name__})"}
    product = (gegevens or {}).get("product") or {}
    if not product:
        return {"naam": "allergenen", "bron": "openfoodfacts.org", "tekst": "",
                "fout": f"geen product gevonden met barcode {code.group(0)}"}
    allergenen = [str(stuk).replace("en:", "") for stuk in product.get("allergens_tags") or []]
    sporen = [str(stuk).replace("en:", "") for stuk in product.get("traces_tags") or []]
    naam = str(product.get("product_name") or code.group(0))
    tekst = (f"{naam} bevat: " + (", ".join(allergenen) if allergenen else "geen opgegeven allergenen") + ".")
    if sporen:
        tekst += " Kan sporen bevatten van: " + ", ".join(sporen) + "."
    return {"naam": "allergenen", "bron": f"openfoodfacts.org/{code.group(0)}", "tekst": tekst}


def gereedschap_barcode(zoek: str) -> dict:
    """Een product opzoeken met de streepjescode."""
    zoek = (zoek or "").strip()
    if not zoek:
        return {"naam": "barcode", "bron": "openfoodfacts.org", "tekst": "", "fout": "noem een barcode"}
    code = re.search(r"\d{8,14}", zoek)
    if code:
        try:
            gegevens = _openfood(code.group(0))
        except Exception as fout:
            return {"naam": "barcode", "bron": "openfoodfacts.org", "tekst": "",
                    "fout": f"de voedingsdatabase is even niet bereikbaar ({type(fout).__name__})"}
        product = (gegevens or {}).get("product") or {}
        if product:
            return {
                "naam": "barcode",
                "bron": f"openfoodfacts.org/{code.group(0)}",
                "tekst": (f"Barcode {code.group(0)} is {product.get('product_name') or '?'} "
                          f"van {product.get('brands') or 'onbekend merk'}."),
            }
    try:
        producten = _openfood_zoek(zoek)
    except Exception:
        producten = []
    for product in producten[:3]:
        if product.get("code"):
            return {
                "naam": "barcode",
                "bron": "openfoodfacts.org",
                "tekst": (f"{product.get('product_name') or zoek} "
                          f"({product.get('brands') or 'onbekend merk'}): barcode {product.get('code')}."),
            }
    return {"naam": "barcode", "bron": "openfoodfacts.org", "tekst": "",
            "fout": f"geen product gevonden voor '{zoek}'"}


def gereedschap_griepcijfers(plaats: str) -> dict:
    """Hoeveel griep er in Nederland is (RIVM)."""
    try:
        ruw = _haal("https://www.rivm.nl/griepprik", limiet=25).decode("utf-8", "replace").lower()
    except Exception as fout:
        return {"naam": "griepcijfers", "bron": "rivm.nl", "tekst": "",
                "fout": f"{type(fout).__name__}: {fout}"}
    cijfers = re.findall(r"(\d{1,4})\s*(?:patiënten|patienten|gevallen)", ruw)
    if cijfers:
        return {"naam": "griepcijfers", "bron": "rivm.nl",
                "tekst": f"Op de RIVM-pagina staan {cijfers[0]} gevallen genoemd."}
    return {
        "naam": "griepcijfers",
        "bron": "rivm.nl",
        "tekst": ("Het RIVM publiceert de griepcijfers niet als open data met een API; "
                  "de cijfers staan in het wekelijkse overzicht op https://www.rivm.nl/griepprik."),
    }


def gereedschap_corona_cijfers(plaats: str) -> dict:
    """De actuele coronacijfers."""
    return {
        "naam": "corona-cijfers",
        "bron": "data.rivm.nl",
        "tekst": ("Het RIVM levert de coronacijfers alleen als downloadbestanden, niet als API. "
                  "De bestanden staan op https://data.rivm.nl/covid-19/."),
    }


def gereedschap_bijwerkingen(zoek: str) -> dict:
    """Wat een medicijn met je kan doen."""
    zoek = (zoek or "").strip()
    tekst = ("De Geneesmiddeleninformatiebank heeft geen open API; zoeken gaat via een formulier op "
             "https://www.geneesmiddeleninformatiebank.nl/.")
    if zoek:
        tekst = f"Voor '{zoek}': " + tekst
    tekst += " Bij ernstige klachten altijd de huisarts of apotheek bellen."
    return {"naam": "bijwerkingen", "bron": "geneesmiddeleninformatiebank.nl", "tekst": tekst}


EHBO = {
    "brandwond": ("Koel de wond minimaal tien minuten met lauw stromend water. Verwijder geen kleding die "
                  "vastzit. Dek de wond los af met steriel verband. Bel 112 bij een grote of diepe wond, "
                  "bij brandwonden aan gezicht, handen of geslacht, of bij een kind."),
    "verslikking": ("Laat de persoon hoesten. Kan hij niet hoesten, niet praten of niet ademen: geef tot "
                    "vijf slagen tussen de schouderbladen, daarna tot vijf buikstoten. Bel 112."),
    "reanimatie": ("Controleer of iemand reageert en ademt. Ademlt hij niet normaal: bel 112, start met "
                   "borstcompressies (100 tot 120 per minuut, 5 tot 6 cm diep) en haal een AED."),
    "hartinfarct": ("Bel direct 112 bij pijn op de borst die langer dan vijf minuten duurt, vooral met "
                    "uitstraling naar arm, kaak of rug, zweten en benauwdheid. Laat de persoon zitten en "
                    "niets eten of drinken."),
    "beroerte": ("Denk aan een beroerte bij een scheve mond, een verlamde arm of verwarde spraak. Bel 112, "
                 "noteer hoe laat de klachten begonnen en laat de persoon niet alleen."),
    "bloedneus": ("Laat de persoon rechtop zitten, het hoofd iets voorover, en knijp de neus tien minuten "
                  "dicht. Niet achterover leggen. Naar de huisarts als het bloeden niet stopt."),
    "vergiftiging": ("Bel het Nationaal Vergiftigingen Informatie Centrum via 030-2748888 of 112 bij "
                     "ernstige klachten. Neem de verpakking mee en laat de persoon niet braken."),
    "tekenbeet": ("Verwijder de teek met een puntige pincet, zo dicht mogelijk bij de huid, zonder te "
                  "draaien. Noteer de datum. Let de weken daarna op een rode ring of griepverschijnselen."),
}


def gereedschap_ehbo(verzoek: str) -> dict:
    """Wat je moet doen bij een ongeluk."""
    laag = (verzoek or "").lower()
    for sleutel, tekst in EHBO.items():
        if sleutel in laag:
            return {"naam": "ehbo", "bron": "eigen naslag (Rode Kruis-richtlijnen in het kort)", "tekst":
                    f"{sleutel.capitalize()}: {tekst}"}
    return {
        "naam": "ehbo",
        "bron": "eigen naslag",
        "tekst": ("Ik ken deze situaties: " + ", ".join(EHBO) + ". Noem er één, bijvoorbeeld "
                  "'ehbo bij een brandwond'. Bij twijfel altijd 112 bellen."),
    }


def gereedschap_wachttijden(plaats: str) -> dict:
    """Hoe lang je wacht bij een ziekenhuis."""
    return {
        "naam": "wachttijden",
        "bron": "ziekenhuizentransparant.nl",
        "tekst": ("De wachttijden van ziekenhuizen zijn niet als open API beschikbaar; ze staan per "
                  "behandeling op https://www.ziekenhuizentransparant.nl/."),
    }


VERBRANDING = {
    "wandelen": 4.0, "fietsen": 6.0, "hardlopen": 11.0, "zwemmen": 7.0, "tuinieren": 4.0,
    "stofzuigen": 3.5, "trap lopen": 8.0, "sporten": 8.0, "yoga": 3.0, "dansen": 6.0,
}


def gereedschap_verbranding(verzoek: str) -> dict:
    """Hoeveel calorieën je verbrandt met bewegen."""
    laag = (verzoek or "").lower()
    minuten = 30.0
    gevonden = re.search(r"(\d{1,3})\s*min", laag)
    if gevonden:
        minuten = float(gevonden.group(1))
    gewicht = 75.0
    kilo = re.search(r"(\d{2,3})\s*(kg|kilo)", laag)
    if kilo:
        gewicht = float(kilo.group(1))
    activiteit = next((naam for naam in VERBRANDING if naam in laag), "wandelen")
    met = VERBRANDING[activiteit]
    calorieen = met * gewicht * (minuten / 60)
    return {
        "naam": "verbranding",
        "bron": "eigen berekening met MET-waarden",
        "tekst": (
            f"{activiteit.capitalize()} van {_getal(minuten, ' minuten')} bij {_getal(gewicht, ' kilo')} "
            f"verbrandt ongeveer {_getal(calorieen, ' kcal')}. Dat is een schatting; "
            "de echte waarde hangt af van tempo en conditie."
        ),
    }


def gereedschap_supermarkten(plaats: str) -> dict:
    """Welke supermarkt het dichtst in de buurt is."""
    try:
        plekken, nette = _osm_nabij(plaats or GEREEDSCHAP_PLAATS, '"shop"="supermarket"', 2500, 10)
    except Exception as fout:
        return {"naam": "supermarkten", "bron": "openstreetmap.org", "tekst": "", "fout": str(fout)}
    if not plekken:
        return {"naam": "supermarkten", "bron": "openstreetmap.org",
                "tekst": f"Geen supermarkten gevonden binnen 2,5 km van {nette}."}
    regels = []
    for afstand, naam, kenmerken in plekken[:6]:
        merk = naam or kenmerken.get("brand") or "supermarkt"
        regels.append(f"{merk} ({_getal(afstand, ' km')})")
    return {"naam": "supermarkten", "bron": "openstreetmap.org (Overpass)", "tekst":
            f"Supermarkten bij {nette}: " + ", ".join(regels)}


def gereedschap_huizenprijzen(plaats: str) -> dict:
    """Wat een huis in een gemeente gemiddeld kost (CBS)."""
    plaats = (plaats or "").strip() or GEREEDSCHAP_PLAATS
    if not plaats:
        return {"naam": "huizenprijzen", "bron": "opendata.cbs.nl", "tekst": "", "fout": "geen gemeente opgegeven"}
    filter_ = urllib.parse.quote(f"startswith(Title,'{plaats}')", safe="()'")
    regio = _haal_json(f"https://opendata.cbs.nl/ODataApi/odata/83625NED/RegioS?$filter={filter_}&$top=10")
    kandidaten = [rij for rij in ((regio or {}).get("value") or [])
                  if str(rij.get("Key") or "").startswith("GM")]
    if not kandidaten:
        return {"naam": "huizenprijzen", "bron": "opendata.cbs.nl", "tekst": "",
                "fout": f"geen gemeente gevonden die begint met '{plaats}'"}
    kandidaten.sort(key=lambda rij: (str(rij.get("Title")) != plaats, -len(str(rij.get("Title")))))
    code = str(kandidaten[0].get("Key") or "")
    naam = str(kandidaten[0].get("Title") or plaats)
    gegevens = _haal_json(
        "https://opendata.cbs.nl/ODataApi/odata/83625NED/TypedDataSet?"
        f"%24filter=RegioS%20eq%20%27{code}%27&%24select=Perioden%2CGemiddeldeVerkoopprijs_1"
    )
    rijen = [rij for rij in ((gegevens or {}).get("value") or [])
             if isinstance(rij.get("GemiddeldeVerkoopprijs_1"), (int, float))]
    if not rijen:
        return {"naam": "huizenprijzen", "bron": "opendata.cbs.nl", "tekst": "",
                "fout": f"geen verkoopprijzen voor {naam}"}
    rijen.sort(key=lambda rij: str(rij.get("Perioden") or ""))
    laatste = rijen[-1]
    periode = str(laatste.get("Perioden") or "")
    jaar = int(periode[:4]) if periode[:4].isdigit() else 0
    waarschuwing = ""
    if jaar and jaar < datetime.date.today().year - 2:
        waarschuwing = (f" Let op: dit is het laatste jaar in deze CBS-tabel ({jaar}); "
                        "recentere prijzen staan in de CBS-statline.")
    return {
        "naam": "huizenprijzen",
        "bron": f"opendata.cbs.nl (tabel 83625NED, {periode})",
        "tekst": (f"{naam}: gemiddelde verkoopprijs van een bestaande woning "
                  f"{_getal(laatste.get('GemiddeldeVerkoopprijs_1'), ' euro')} "
                  "(alle woningtypen door elkaar)." + waarschuwing),
    }


def gereedschap_hoogte(plaats: str) -> dict:
    """Hoeveel meter een plek boven zeeniveau ligt."""
    plaats = (plaats or "").strip() or GEREEDSCHAP_PLAATS
    if not plaats:
        return {"naam": "hoogte", "bron": "open-meteo.com", "tekst": "", "fout": "geen plaats opgegeven"}
    try:
        breedte, lengte, nette = _coordinaten(plaats)
    except Exception as fout:
        return {"naam": "hoogte", "bron": "open-meteo.com", "tekst": "", "fout": str(fout)}
    gegevens = _haal_json(
        f"https://api.open-meteo.com/v1/elevation?latitude={breedte}&longitude={lengte}"
    )
    hoogte = ((gegevens or {}).get("elevation") or [None])[0]
    if not isinstance(hoogte, (int, float)):
        return {"naam": "hoogte", "bron": "open-meteo.com", "tekst": "", "fout": "geen hoogte ontvangen"}
    return {
        "naam": "hoogte",
        "bron": "open-meteo.com (Copernicus DEM)",
        "tekst": (f"{nette} ligt op {_getal(hoogte, ' meter')} boven zeeniveau."
                  + (" Dat is onder de zeespiegel." if hoogte < 0 else "")),
    }


def gereedschap_evenementen(plaats: str) -> dict:
    """Wat er deze week in de buurt te doen is."""
    plaats = (plaats or "").strip() or GEREEDSCHAP_PLAATS
    if not plaats:
        return {"naam": "evenementen", "bron": "nieuwsfeed", "tekst": "", "fout": "geen plaats opgegeven"}
    titels = _nieuwsfeed(f"evenement {plaats}", "evenementen")
    if not titels:
        return {"naam": "evenementen", "bron": "nieuwsfeed", "tekst": "",
                "fout": f"geen evenementen gevonden in {plaats}"}
    return {"naam": "evenementen", "bron": "Google Nieuws", "tekst":
            f"Te doen in {plaats}: " + " | ".join(titels)}


def gereedschap_afval_ophalen(plaats: str) -> dict:
    """Wanneer welke afvalbak aan de straat moet."""
    plaats = (plaats or "").strip()
    return {
        "naam": "afval ophalen",
        "bron": "gemeente",
        "tekst": ("De afvalkalender is per gemeente anders en bijna geen enkele gemeente heeft er een "
                  "open API voor. Kijk in de afvalapp van je gemeente"
                  + (f" (gemeente {plaats})" if plaats else "") + "."),
    }


def gereedschap_zon_op_je_dak(plaats: str) -> dict:
    """Hoeveel zon je dak per jaar vangt."""
    return {
        "naam": "zon op je dak",
        "bron": "eigen advies",
        "tekst": ("Voor een schatting van de opbrengst kun je het gereedschap 'zonnepanelen' gebruiken: "
                  "dat rekent met de instraling van vandaag en je aantal Wp. Een echte daksimulatie "
                  "(helling, schaduw, oriëntatie) vraagt de Zonatlas van je gemeente of een installateur."),
    }


def gereedschap_bereikbaarheid(plaats: str) -> dict:
    """Hoeveel openbaar vervoer er in de buurt is."""
    plaats = (plaats or "").strip() or GEREEDSCHAP_PLAATS
    if not plaats:
        return {"naam": "bereikbaarheid", "bron": "openstreetmap.org", "tekst": "", "fout": "geen plaats opgegeven"}
    try:
        breedte, lengte, nette = _coordinaten(plaats)
    except Exception as fout:
        return {"naam": "bereikbaarheid", "bron": "openstreetmap.org", "tekst": "", "fout": str(fout)}
    try:
        gegevens = _overpass(
            f'[out:json][timeout:20];nwr["public_transport"~"platform|stop_position"]'
            f'(around:1000,{breedte},{lengte});out count;'
        )
    except Exception as fout:
        return {"naam": "bereikbaarheid", "bron": "openstreetmap.org", "tekst": "", "fout": str(fout)}
    aantal = 0
    for element in (gegevens or {}).get("elements") or []:
        tags = element.get("tags") or {}
        if "total" in tags:
            try:
                aantal = int(tags["total"])
            except (TypeError, ValueError):
                aantal = 0
    if not aantal:
        aantal = len((gegevens or {}).get("elements") or [])
    if aantal >= 20:
        oordeel = "goed"
    elif aantal >= 8:
        oordeel = "redelijk"
    elif aantal:
        oordeel = "beperkt"
    else:
        oordeel = "slecht"
    return {
        "naam": "bereikbaarheid",
        "bron": "openstreetmap.org (Overpass)",
        "tekst": (f"Binnen een kilometer van {nette} liggen {aantal} haltes of perrons. "
                  f"Dat is {oordeel} openbaar vervoer."),
    }

# Welke route bij welk gereedschap hoort: (/api/<naam>?q=...)
GEREEDSCHAP_ROUTES = {
    "rekenen": (gereedschap_rekenen, "q"),
    "eenheden": (gereedschap_eenheden_tekst, "q"),
    "datum": (gereedschap_datum, "q"),
    "feestdagen": (gereedschap_feestdagen_tekst, "q"),
    "tijdzone": (gereedschap_tijdzone, "q"),
    "postcode": (gereedschap_postcode, "q"),
    "buien": (gereedschap_buien, "plaats"),
    "lucht": (gereedschap_lucht, "plaats"),
    "zon": (gereedschap_zon, "plaats"),
    "valuta": (gereedschap_valuta, "q"),
    "crypto": (gereedschap_crypto, "q"),
    "woordenboek": (gereedschap_woordenboek, "q"),
    "vertalen": (gereedschap_vertaal, "q"),
    "domein": (gereedschap_domein, "q"),
    "verkeer": (gereedschap_verkeer, "weg"),
    "recepten": (gereedschap_recepten, "q"),
    "akkoorden": (gereedschap_akkoorden, "q"),
    "zittesj": (gereedschap_zittesj, "q"),
    "notities": (gereedschap_notities, "q"),
    "status": (gereedschap_status, None),
    "water": (gereedschap_water, "plaats"),
    "stroomprijs": (gereedschap_stroomprijs, None),
    "afstand": (gereedschap_afstand, "q"),
    "boodschappen": (gereedschap_boodschappen, "q"),
    "wetten": (gereedschap_wet, "q"),
    "cbs": (gereedschap_cbs, "gemeente"),
    "boek": (gereedschap_boek, "q"),
    "vliegtuigen": (gereedschap_vliegtuigen, "q"),
    "document": (gereedschap_document, "q"),
    "aardbevingen": (gereedschap_aardbevingen, "q"),
    "hooikoorts": (gereedschap_hooikoorts, "plaats"),
    "bruggen": (gereedschap_bruggen, "plaats"),
    "verwachting": (gereedschap_verwachting, "plaats"),
    "gasprijs": (gereedschap_gasprijs, None),
    "horeca": (gereedschap_horeca, "q"),
    "weeralarm": (gereedschap_weeralarm, "q"),
    "buurtcijfers": (gereedschap_buurtcijfers, "gemeente"),
    "devhulp": (gereedschap_devhulp, "q"),
    "uurverloop": (gereedschap_uurverloop, "plaats"),
    "laadpalen": (gereedschap_laadpalen, "plaats"),
    "parkeren": (gereedschap_parkeren, "plaats"),
    "vorigjaar": (gereedschap_vorigjaar, "plaats"),
    "btw": (gereedschap_btw, "q"),
    "inflatie": (gereedschap_inflatie, None),
    "wikipedia": (gereedschap_wikipedia, "q"),
    "prijswacht": (gereedschap_prijswacht, "q"),
    "afsluitingen": (gereedschap_afsluitingen, "weg"),
    "feed": (gereedschap_feed, "url"),
    "brandstof": (gereedschap_brandstof, None),
    "zon": (gereedschap_zon, "plaats"),
    "synoniemen": (gereedschap_synoniemen, "q"),
    "koken": (gereedschap_koken, "q"),
    "prijsgeschiedenis": (gereedschap_prijsgeschiedenis, "q"),
    "artikel": (gereedschap_artikel, None),
    "persberichten": (gereedschap_persberichten, None),
    "reistijd": (gereedschap_reistijd, "q"),
    "spelling": (gereedschap_spelling, "q"),
    "regenradar": (gereedschap_regenradar, "plaats"),
    "neerslag": (gereedschap_neerslag_kwartier, "plaats"),
    "windstoten": (gereedschap_windstoten, "plaats"),
    "uv": (gereedschap_uv_uur, "plaats"),
    "verbrandingstijd": (gereedschap_verbrandingstijd, "plaats"),
    "klimaat": (gereedschap_klimaat, "plaats"),
    "warmste dag": (gereedschap_warmste_dag, "plaats"),
    "getijden": (gereedschap_getijden, "plaats"),
    "watertemperatuur": (gereedschap_watertemperatuur, "plaats"),
    "zwemwater": (gereedschap_zwemwater, "plaats"),
    "maanfase": (gereedschap_maanfase, "plaats"),
    "werkzaamheden": (gereedschap_werkzaamheden, "weg"),
    "snelheidslimiet": (gereedschap_snelheidslimiet, "q"),
    "flitspalen": (gereedschap_flitspalen, "plaats"),
    "ov": (gereedschap_ov_haltes, "plaats"),
    "vertrektijden": (gereedschap_vertrektijden, "plaats"),
    "treinvertragingen": (gereedschap_treinvertragingen, "plaats"),
    "spoorwerk": (gereedschap_spoorwerk, "plaats"),
    "fietsroute": (gereedschap_fietsroute, "q"),
    "wandelroute": (gereedschap_wandelroute, "q"),
    "tolwegen": (gereedschap_tolwegen, "plaats"),
    "veerponten": (gereedschap_veerponten, "plaats"),
    "tankstations": (gereedschap_tankstations, "plaats"),
    "p+r": (gereedschap_pr_parkeren, "plaats"),
    "valuta-geschiedenis": (gereedschap_valuta_geschiedenis, "q"),
    "crypto-geschiedenis": (gereedschap_crypto_geschiedenis, "q"),
    "aandelenkoers": (gereedschap_aandelenkoers, "q"),
    "hypotheekrente": (gereedschap_hypotheekrente, "plaats"),
    "spaarrente": (gereedschap_spaarrente, "plaats"),
    "teruglevering": (gereedschap_teruglevering, None),
    "zonnepanelen": (gereedschap_zonnepanelen, "q"),
    "kosten apparaat": (gereedschap_kosten_apparaat, "q"),
    "bruto netto": (gereedschap_bruto_netto, "q"),
    "belasting": (gereedschap_belasting_box1, "q"),
    "reiskosten": (gereedschap_reiskosten, "q"),
    "rente op rente": (gereedschap_rente_op_rente, "q"),
    "kamerstukken": (gereedschap_kamerstukken, "plaats"),
    "bekendmakingen": (gereedschap_bekendmakingen, "plaats"),
    "verkiezingen": (gereedschap_verkiezingen, "plaats"),
    "subsidies": (gereedschap_subsidies, "plaats"),
    "wat verandert er": (gereedschap_wat_verandert_er, "plaats"),
    "vergunningen": (gereedschap_vergunningen, "plaats"),
    "provincie": (gereedschap_provincie, "plaats"),
    "wetten in werking": (gereedschap_wetten_in_werking, "plaats"),
    "gemeente open data": (gereedschap_gemeente_open_data, "gemeente"),
    "wikipedia volledig": (gereedschap_wikipedia_volledig, "q"),
    "woord van de dag": (gereedschap_woord_van_de_dag, "plaats"),
    "rijmwoorden": (gereedschap_rijmwoorden, "q"),
    "synoniemen engels": (gereedschap_synoniemen_engels, "q"),
    "werkwoorden": (gereedschap_werkwoorden, "q"),
    "getallen in woorden": (gereedschap_getallen_in_woorden, "q"),
    "citaten": (gereedschap_citaten, "q"),
    "film op tv": (gereedschap_film_op_tv, "plaats"),
    "films": (gereedschap_films_en_series, "q"),
    "muziekalbums": (gereedschap_muziekalbums, "q"),
    "landeninfo": (gereedschap_landeninfo, "q"),
    "nobelprijzen": (gereedschap_nobelprijzen, "plaats"),
    "vandaag in het jaar": (gereedschap_vandaag_in_het_jaar, "plaats"),
    "definitie": (gereedschap_definitie, "q"),
    "nieuws onderwerp": (gereedschap_nieuws_onderwerp, "q"),
    "nieuws gemeente": (gereedschap_nieuws_gemeente, "plaats"),
    "sportuitslagen": (gereedschap_sportuitslagen, "plaats"),
    "tv-gids": (gereedschap_tv_gids, "plaats"),
    "podcasts": (gereedschap_podcasts, "q"),
    "radiostations": (gereedschap_radiostations, "plaats"),
    "nieuwsarchief": (gereedschap_nieuwsarchief, "q"),
    "calorieen": (gereedschap_calorieen, "q"),
    "voedingswaarde": (gereedschap_voedingswaarde, "q"),
    "allergenen": (gereedschap_allergenen, "q"),
    "barcode": (gereedschap_barcode, "q"),
    "griepcijfers": (gereedschap_griepcijfers, "plaats"),
    "corona-cijfers": (gereedschap_corona_cijfers, "plaats"),
    "bijwerkingen": (gereedschap_bijwerkingen, "q"),
    "ehbo": (gereedschap_ehbo, "q"),
    "wachttijden": (gereedschap_wachttijden, "plaats"),
    "verbranding": (gereedschap_verbranding, "q"),
    "supermarkten": (gereedschap_supermarkten, "plaats"),
    "huizenprijzen": (gereedschap_huizenprijzen, "plaats"),
    "hoogte": (gereedschap_hoogte, "plaats"),
    "evenementen": (gereedschap_evenementen, "plaats"),
    "afval ophalen": (gereedschap_afval_ophalen, "plaats"),
    "zon op je dak": (gereedschap_zon_op_je_dak, "plaats"),
    "bereikbaarheid": (gereedschap_bereikbaarheid, "plaats"),
}


GEREEDSCHAP_SOORTEN = {
    "weer": "actueel weer en verwachting (open-meteo.com)",
    "nieuws": "koppen van het NOS Nieuws",
    "naslag": "samenvatting uit de Nederlandse Wikipedia",
    "zoeken": "zoeken op internet (duckduckgo.com)",
    "tijd": "datum en tijd van deze machine",
    "rekenen": "een som uitrekenen (de engine zelf, zonder internet)",
    "eenheden": "eenheden omrekenen (de engine zelf, zonder internet)",
    "datum": "rekenen met datums (de engine zelf, zonder internet)",
    "feestdagen": "Nederlandse feestdagen, berekend uit de paasdatum",
    "tijdzone": "hoe laat is het elders (tijdzonedatabase)",
    "postcode": "postcode, adres, weg of gemeente (PDOK/BAG)",
    "buien": "neerslag voor de komende twee uur (buienradar.nl)",
    "lucht": "luchtkwaliteit en fijnstof (open-meteo.com)",
    "zon": "zon op en zon onder (open-meteo.com)",
    "valuta": "valuta omrekenen met de koersen van de ECB",
    "crypto": "cryptokoersen in euro (coingecko.com)",
    "woordenboek": "betekenis van een woord (nl.wiktionary.org)",
    "vertalen": "korte tekst vertalen (mymemory.translated.net)",
    "domein": "van wie is een domeinnaam (rdap.org)",
    "verkeer": "files, ongelukken en afsluitingen (NDW/Rijkswaterstaat)",
    "recepten": "zoeken in de recepten van VoorraadThuis",
    "akkoorden": "akkoorden en speelwijze uit AkkoordKast",
    "zittesj": "een woord opzoeken in de Zittesj-woordenlijst",
    "notities": "zoeken in je eigen notitiemap (WLLM_NOTITIES)",
    "status": "geheugen, belasting, schijf en temperatuur van deze machine",
    "water": "waterstand in cm boven NAP (Rijkswaterstaat)",
    "stroomprijs": "stroomprijs per uur, inclusief btw (energyzero.nl)",
    "afstand": "afstand en reistijd met de auto (OpenStreetMap/OSRM)",
    "boodschappen": "wat je al hebt en mist voor een recept (VoorraadThuis)",
    "wetten": "zoeken in de Nederlandse wetten (wetten.overheid.nl)",
    "cbs": "inwoners en kerncijfers van een gemeente (CBS)",
    "boek": "schrijver en jaar van een boek (openlibrary.org)",
    "vliegtuigen": "hoeveel vliegtuigen er nu boven Nederland hangen (OpenSky)",
    "document": "een document uit je eigen map lezen (WLLM_DOCUMENTEN)",
    "aardbevingen": "aardbevingen in Nederland en omstreken (EMSC)",
    "hooikoorts": "pollen en UV-index (open-meteo.com)",
    "bruggen": "geplande brugopeningen (NDW/Rijkswaterstaat)",
    "verwachting": "de verwachting voor veertien dagen (open-meteo.com)",
    "gasprijs": "de gasprijs per kuub (energyzero.nl)",
    "horeca": "horeca en winkels in de buurt (openstreetmap.org)",
    "weeralarm": "officiële weerswaarschuwingen (meteoalarm.org)",
    "buurtcijfers": "leeftijdsopbouw en dichtheid van een gemeente (CBS)",
    "devhulp": "hashes, base64, json, kleurcontrast en QR (de engine zelf)",
    "uurverloop": "het weer per uur vandaag (open-meteo.com)",
    "laadpalen": "laadpalen in de buurt (openstreetmap.org)",
    "parkeren": "parkeergarages en -terreinen in de buurt (openstreetmap.org)",
    "vorigjaar": "het weer op deze dag vorig jaar (open-meteo.com archief)",
    "btw": "btw en percentages uitrekenen (de engine zelf)",
    "inflatie": "de inflatie in Nederland (CBS)",
    "wikipedia": "een samenvatting van Wikipedia (nl.wikipedia.org)",
    "prijswacht": "wat iets tweedehands kost op Marktplaats",
    "afsluitingen": "wegen die nu dicht zijn (NDW/Rijkswaterstaat)",
    "feed": "de laatste berichten uit een rss-feed",
    "brandstof": "de pompprijzen van deze week (CBS)",
    "zon": "zonsopkomst, zonsondergang en het gouden uur (open-meteo.com)",
    "synoniemen": "synoniemen en betekenis van een woord (WikiWoordenboek)",
    "koken": "maten omrekenen voor het koken (de engine zelf)",
    "prijsgeschiedenis": "de prijs van iets tweedehands volgen in de tijd (Marktplaats)",
    "artikel": "wat er vandaag op Wikipedia gelezen wordt",
    "persberichten": "persberichten van de Rijksoverheid",
    "reistijd": "reistijd met files op de route (OSRM en NDW)",
    "spelling": "woorden controleren met de OpenTaal-woordenlijst",
    "regenradar": "waar het nu regent (buienradar.nl)",
    "neerslag": "neerslag per vijf minuten (buienradar.nl)",
    "windstoten": "wind en windstoten (open-meteo.com)",
    "uv": "de zonkracht per uur (open-meteo.com)",
    "verbrandingstijd": "hoe lang je in de zon kunt (eigen berekening)",
    "klimaat": "temperatuur en neerslag per maand (open-meteo archief)",
    "warmste dag": "warmste en koudste dag van het jaar (open-meteo archief)",
    "getijden": "eb en vloed met keerpunten (rijkswaterstaat)",
    "watertemperatuur": "hoe warm het water is (rijkswaterstaat)",
    "zwemwater": "zwemplekken en waterkwaliteit (rivm)",
    "maanfase": "de stand van de maan (eigen berekening)",
    "werkzaamheden": "geplande wegwerkzaamheden (ndw)",
    "snelheidslimiet": "hoe hard je ergens mag (openstreetmap)",
    "flitspalen": "waar flitspalen staan (openstreetmap)",
    "ov": "lijnen en haltes bij een plaats (ovapi.nl)",
    "vertrektijden": "actuele vertrektijden (ovapi.nl)",
    "treinvertragingen": "of de treinen op tijd rijden (ns)",
    "spoorwerk": "werkzaamheden aan het spoor (nieuwsfeed)",
    "fietsroute": "hoe lang je fietst van a naar b (osm routing)",
    "wandelroute": "hoe lang je loopt van a naar b (osm routing)",
    "tolwegen": "tol en vignetten (openstreetmap)",
    "veerponten": "veerponten in de buurt (openstreetmap)",
    "tankstations": "tankstations in de buurt (openstreetmap)",
    "p+r": "parkeer en reis (openstreetmap)",
    "valuta-geschiedenis": "hoe een munt zich ontwikkelde (frankfurter)",
    "crypto-geschiedenis": "hoe een munt het deed (coingecko)",
    "aandelenkoers": "de koers van een aandeel (yahoo finance)",
    "hypotheekrente": "de actuele hypotheekrente (hypotheker.nl)",
    "spaarrente": "waar spaargeld het meest opbrengt (spaarrente.nl)",
    "teruglevering": "wat je krijgt voor teruggeleverde stroom (energyzero)",
    "zonnepanelen": "wat panelen vandaag opleveren (open-meteo)",
    "kosten apparaat": "stroomkosten van een apparaat (eigen berekening)",
    "bruto netto": "wat je van je salaris overhoudt (eigen berekening)",
    "belasting": "belasting per schijf (eigen berekening)",
    "reiskosten": "reiskostenvergoeding per kilometer (eigen berekening)",
    "rente op rente": "wat sparen op termijn oplevert (eigen berekening)",
    "kamerstukken": "wat er in de Tweede Kamer speelt",
    "bekendmakingen": "officiele bekendmakingen",
    "verkiezingen": "verkiezingsuitslagen",
    "subsidies": "subsidies en regelingen",
    "wat verandert er": "wat er per 1 januari verandert",
    "vergunningen": "welke vergunning je nodig hebt",
    "provincie": "wat je provincie besluit",
    "wetten in werking": "welke wetten ingaan",
    "gemeente open data": "kerncijfers van een gemeente (cbs)",
    "wikipedia volledig": "een heel Wikipedia-artikel",
    "woord van de dag": "het woord van de dag (wiktionary)",
    "rijmwoorden": "woorden die rijmen (datamuse)",
    "synoniemen engels": "een Engels synoniem (datamuse)",
    "werkwoorden": "een werkwoord vervoegen (eigen berekening)",
    "getallen in woorden": "een getal in woorden (eigen berekening)",
    "citaten": "bekende uitspraken (wikiquote)",
    "film op tv": "welke film er op tv is",
    "films": "een film of serie opzoeken (wikidata)",
    "muziekalbums": "albums van een artiest (musicbrainz)",
    "landeninfo": "hoofdstad en inwoners van een land (restcountries)",
    "nobelprijzen": "de Nobelprijzen (nobelprize.org)",
    "vandaag in het jaar": "wat er op deze dag gebeurde",
    "definitie": "de betekenis van een woord (wiktionary)",
    "nieuws onderwerp": "nieuws over een onderwerp",
    "nieuws gemeente": "nieuws uit een plaats",
    "sportuitslagen": "de sportuitslagen (nos)",
    "tv-gids": "wat er op tv is",
    "podcasts": "podcasts zoeken (itunes)",
    "radiostations": "radiozenders met stream (npo)",
    "nieuwsarchief": "zoeken in ouder nieuws",
    "calorieen": "calorieen van eten (open food facts)",
    "voedingswaarde": "eiwit, vet en koolhydraten (open food facts)",
    "allergenen": "allergenen in een product (open food facts)",
    "barcode": "een product opzoeken met de streepjescode",
    "griepcijfers": "hoeveel griep er is (rivm)",
    "corona-cijfers": "de coronacijfers (rivm)",
    "bijwerkingen": "bijwerkingen van een medicijn",
    "ehbo": "eerste hulp bij ongelukken (eigen naslag)",
    "wachttijden": "wachttijden in het ziekenhuis",
    "verbranding": "calorieen verbranden met bewegen (eigen berekening)",
    "supermarkten": "supermarkten in de buurt (openstreetmap)",
    "huizenprijzen": "wat een huis gemiddeld kost (cbs)",
    "hoogte": "hoe hoog een plek ligt (open-meteo)",
    "evenementen": "wat er in de buurt te doen is",
    "afval ophalen": "wanneer het afval aan de straat moet",
    "zon op je dak": "hoeveel zon je dak vangt",
    "bereikbaarheid": "hoeveel ov er in de buurt is (openstreetmap)",
}


# ---- kiezen, uitvoeren en in de vraag zetten ------------------------------


def _plaats_uit(tekst: str) -> str:
    """De plaats uit een vraag peuteren: 'in Hilversum', 'voor Utrecht', 'Hilversum'."""
    for patroon in (
        r"\bin\s+([A-Za-zÀ-ÿ' \-]{2,30})",
        r"\bvoor\s+([A-Za-zÀ-ÿ' \-]{2,30})",
        r"\bte\s+([A-Za-zÀ-ÿ' \-]{2,30})",
    ):
        treffer = re.search(patroon, tekst)
        if treffer:
            plaats = treffer.group(1).strip(" ?.,!")
            plaats = re.split(r"\s+(?:vandaag|morgen|overmorgen|nu|deze|dit|het|de)\b", plaats)[0]
            plaats = re.sub(r"^(?:de|het|een|mijn|onze|jouw)\s+", "", plaats.strip(), flags=re.I)
            woorden = plaats.lower().split()
            if woorden and woorden[0] in ("blijven", "gaan", "komen", "doen", "zijn", "worden",
                                          "staan", "liggen", "lopen", "fietsen", "rijden", "wonen",
                                          "werken", "zwemmen", "tanken", "parkeren", "zon"):
                continue
            if len(plaats) >= 2 and len(woorden) <= 3:
                return plaats.strip()
    for woord in re.findall(r"\b[A-ZÀ-Ý][a-zà-ÿ]{2,}\b", tekst):
        if woord.lower() not in ("wat", "hoe", "waar", "welke", "wie", "geef", "vertel", "het", "weer"):
            return woord
    return ""


def _weg_uit(tekst: str) -> str:
    """Een wegnummer uit de vraag halen (A2, A76, N242)."""
    gevonden = re.search(r"\b([AN]\s?\d{1,3})\b", tekst.upper())
    return gevonden.group(1).replace(" ", "") if gevonden else ""


def _adres_uit(tekst: str) -> str:
    """Een webadres uit de vraag halen."""
    gevonden = re.search(r"https?://[^\s,;]+", tekst)
    return gevonden.group(0) if gevonden else ""


def gereedschap_kies(tekst: str) -> list[tuple[str, str]]:
    """Welk gereedschap hoort bij deze vraag? Opdrachten met '/' gaan voor."""
    tekst = (tekst or "").strip()
    if not tekst:
        return []
    laag = tekst.lower()

    for gebod, soort in (
        ("/weeralarm", "weeralarm"),
        ("/weer", "weer"),
        ("/nieuws", "nieuws"),
        ("/wiki", "naslag"),
        ("/naslag", "naslag"),
        ("/zoek", "zoeken"),
        ("/tijdzone", "tijdzone"),
        ("/tijd", "tijd"),
        ("/reken", "rekenen"),
        ("/bereken", "rekenen"),
        ("/eenheden", "eenheden"),
        ("/omrekenen", "eenheden"),
        ("/datum", "datum"),
        ("/feestdagen", "feestdagen"),
        ("/postcode", "postcode"),
        ("/buien", "buien"),
        ("/lucht", "lucht"),
        ("/zon", "zon"),
        ("/valuta", "valuta"),
        ("/crypto", "crypto"),
        ("/woordenboek", "woordenboek"),
        ("/woord", "woordenboek"),
        ("/vertaal", "vertalen"),
        ("/domein", "domein"),
        ("/whois", "domein"),
        ("/verkeer", "verkeer"),
        ("/files", "verkeer"),
        ("/recepten", "recepten"),
        ("/recept", "recepten"),
        ("/akkoorden", "akkoorden"),
        ("/akkoord", "akkoorden"),
        ("/zittesj", "zittesj"),
        ("/notities", "notities"),
        ("/status", "status"),
        ("/water", "water"),
        ("/waterstand", "water"),
        ("/stroom", "stroomprijs"),
        ("/stroomprijs", "stroomprijs"),
        ("/afstand", "afstand"),
        ("/boodschappen", "boodschappen"),
        ("/wet", "wetten"),
        ("/wetten", "wetten"),
        ("/cbs", "cbs"),
        ("/boek", "boek"),
        ("/vliegtuigen", "vliegtuigen"),
        ("/document", "document"),
        ("/aardbevingen", "aardbevingen"),
        ("/bevingen", "aardbevingen"),
        ("/hooikoorts", "hooikoorts"),
        ("/pollen", "hooikoorts"),
        ("/bruggen", "bruggen"),
        ("/verwachting", "verwachting"),
        ("/gasprijs", "gasprijs"),
        ("/horeca", "horeca"),
        ("/buurtcijfers", "buurtcijfers"),
        ("/devhulp", "devhulp"),
        ("/uurverloop", "uurverloop"),
        ("/laadpalen", "laadpalen"),
        ("/parkeren", "parkeren"),
        ("/vorigjaar", "vorigjaar"),
        ("/btw", "btw"),
        ("/inflatie", "inflatie"),
        ("/wiki2", "wikipedia"),
        ("/prijswacht", "prijswacht"),
        ("/afsluitingen", "afsluitingen"),
        ("/feed", "feed"),
        ("/brandstof", "brandstof"),
        ("/zon", "zon"),
        ("/synoniemen", "synoniemen"),
        ("/koken", "koken"),
        ("/prijsgeschiedenis", "prijsgeschiedenis"),
        ("/artikel", "artikel"),
        ("/persberichten", "persberichten"),
        ("/reistijd", "reistijd"),
        ("/spelling", "spelling"),
    ):
        if laag.startswith(gebod):
            argument = tekst[len(gebod) :].strip(" :?")
            if soort == "weer" and not argument:
                argument = GEREEDSCHAP_PLAATS
            return [(soort, argument)]

    # de specifieke gereedschappen eerst, dan de algemene hieronder
    if re.fullmatch(r"[\d\s.,+\-*/x^()]+", laag) or re.search(
        r"^\s*(reken|bereken)\b|\bhoeveel is\b|\bwat is de uitkomst\b|\btel (eens )?op\b|\bdeel door\b", laag
    ):
        return [("rekenen", tekst)]
    if re.search(
        r"\d\s*(km|m|cm|mm|mijl|inch|voet|kg|kilo|gram|pond|ons|liter|ml|graden|celsius|fahrenheit|kelvin"
        r"|km/u|km/h|mph|knoop)\b.*\b(in|naar)\b",
        laag,
    ) or re.search(
        r"\bhoeveel (pond|ons|kilo|gram|km|kilometer|mijl|meter|graden|celsius|fahrenheit|liter|ml)\b",
        laag,
    ) or re.search(r"\b(omrekenen|reken om)\b", laag):
        return [("eenheden", tekst)]
    if re.search(r"\bhoeveel dagen (tot|geleden|naar)|\bwelke dag (is|was)|\bhoeveel nachtjes\b", laag):
        return [("datum", tekst)]
    if re.search(r"\bfeestdagen?\b", laag):
        return [("feestdagen", tekst)]
    if re.search(r"\bhoe laat is het in\b", laag):
        return [("tijdzone", tekst)]
    if re.search(r"\bpostcode\b", laag):
        return [("postcode", tekst)]
    if re.search(r"\b(regen\w*|buien|neerslag|motregen|wordt het nat|komt er regen)\b", laag):
        return [("buien", _plaats_uit(tekst) or GEREEDSCHAP_PLAATS)]
    if re.search(r"\b(luchtkwaliteit|fijnstof|smog|ozon)\b", laag):
        return [("lucht", _plaats_uit(tekst) or GEREEDSCHAP_PLAATS)]
    if re.search(r"\b(zon op|zon onder|zonsopgang|zonsondergang|hoe laat gaat de zon)\b", laag):
        return [("zon", _plaats_uit(tekst) or GEREEDSCHAP_PLAATS)]
    if (re.search(r"\b(valuta|wisselkoers|dollar|pond|yen|kronen|euro)\b", laag)
            and re.search(r"\d", laag) and not re.search(r"\b(btw|exclusief|inclusief)\b", laag)):
        return [("valuta", tekst)]
    if re.search(r"\b(bitcoin|ethereum|solana|dogecoin|crypto|litecoin|xrp)\b", laag):
        return [("crypto", tekst)]
    if re.search(r"\b(vertaal|vertaling|vertellen in het engels)\b", laag):
        return [("vertalen", tekst)]
    if re.search(r"\b(van wie is|domein|whois|domeinnaam)\b", laag):
        return [("domein", tekst)]
    if re.search(r"\b(files?|verkeersinfo|verkeersinformatie|ongeluk|afsluiting|wegwerkzaamheden|verkeer)\b", laag):
        return [("verkeer", tekst)]
    if re.search(r"\b(recept|recepten|wat kan ik maken|koken met|te eten maken)\b", laag):
        return [("recepten", tekst)]
    if re.search(r"\b(akkoorden|capo|hoe speel ik|songbook|gitaar)\b", laag):
        return [("akkoorden", tekst)]
    if re.search(r"\b(zittesj|sittards|sittardse)\b", laag):
        return [("zittesj", tekst)]
    if re.search(r"\b(mijn notities|notities|opgeschreven|mijn aantekeningen)\b", laag):
        return [("notities", tekst)]
    if re.search(r"\b(hoe staat de server|serverstatus|geheugen vrij|hoeveel geheugen|cpu-temperatuur|hoe warm is de server)\b", laag):
        return [("status", "")]
    if re.search(r"\bwat betekent\b", laag) and len(laag.split()) <= 5:
        return [("woordenboek", tekst)]
    if re.search(r"\b(waterstand|waterhoogte|hoogwater|hoe hoog staat het water)\b", laag):
        return [("water", _plaats_uit(tekst) or GEREEDSCHAP_PLAATS)]
    if re.search(r"\b(stroomprijs|energieprijs|stroomtarief|kwh-prijs|wat kost stroom)\b", laag):
        return [("stroomprijs", "")]
    if re.search(r"\b(hoe ver|hoe lang rijden|afstand|reisafstand|reistijd)\b", laag) or re.search(
        r"\bvan\b.+\bnaar\b", laag
    ):
        return [("afstand", tekst)]
    if re.search(r"\b(boodschappen|wat moet ik kopen|heb ik alles in huis|ontbreekt er nog)\b", laag):
        return [("boodschappen", tekst)]
    if re.search(r"\b(\w+wet|wetten|wetboek|artikel \d|welke wet)\b", laag):
        return [("wetten", tekst)]
    if re.search(r"\b(inwoners|hoeveel mensen wonen|gemeentecijfers|\bcbs\b)\b", laag):
        return [("cbs", tekst)]
    if re.search(r"\b(boek|schrijver|auteur|schreef|geschreven door|verscheen)\b", laag):
        return [("boek", tekst)]
    if re.search(r"\b(vliegtuig\w*|vlucht\w*|luchtruim)\b", laag):
        return [("vliegtuigen", "")]
    if re.search(r"\b(mijn documenten|document|pdf|word-bestand|lees eens)\b", laag):
        return [("document", tekst)]
    if re.search(r"\b(aardbeving\w*|beving\w*|trilling\w*|seismisch)\b", laag):
        return [("aardbevingen", tekst)]
    if re.search(r"\b(hooikoorts\w*|pollen|uv-index|zonkracht|verbranden)\b", laag):
        return [("hooikoorts", _plaats_uit(tekst) or GEREEDSCHAP_PLAATS)]
    if re.search(r"\b(brug\w*|bascule|ophaalbrug)\b", laag):
        return [("bruggen", _plaats_uit(tekst) or "")]
    if re.search(r"\b(14 dagen|veertien dagen|veertiendaagse|twee weken vooruit|langetermijnverwachting)\b", laag):
        return [("verwachting", _plaats_uit(tekst) or GEREEDSCHAP_PLAATS)]
    if re.search(r"\b(gasprijs|gas\b|gasrekening|kubieke meter gas)\b", laag):
        return [("gasprijs", "")]
    if re.search(r"\b(pizzeria|restaurant|eetcaf|kroeg|snackbar|frituur|bakker|slager|supermarkt|apotheek|huisarts|in de buurt|dichtstbijzijnde)\b", laag):
        return [("horeca", tekst)]
    if re.search(r"\b(weeralarm|code geel|code oranje|code rood|weerswaarschuwing)\b", laag):
        return [("weeralarm", "")]
    if re.search(r"\b(leeftijdsopbouw|bevolkingsdichtheid|hoeveel 65-plussers|hoeveel kinderen wonen)\b", laag):
        return [("buurtcijfers", tekst)]
    if re.search(r"\b(sha256|sha1|md5|base64|json formatteren|kleurcontrast|qr-code|qrcode)\b", laag):
        return [("devhulp", tekst)]
    if re.search(r"\b(per uur|uurverloop|wanneer is het droog|komende uren|vanavond droog)\b", laag):
        return [("uurverloop", _plaats_uit(tekst) or GEREEDSCHAP_PLAATS)]
    if re.search(r"\b(laadpaal\w*|laadpunt\w*|oplad\w*|auto laden|mijn auto|accu laden|stekker)\b", laag):
        return [("laadpalen", _plaats_uit(tekst) or GEREEDSCHAP_PLAATS)]
    if re.search(r"\b(parkeren|parkeergarage\w*|parkeerplaats\w*|parkeertarief\w*)\b", laag):
        return [("parkeren", _plaats_uit(tekst) or GEREEDSCHAP_PLAATS)]
    if re.search(r"\b(vorig jaar|vorige jaar|een jaar geleden|toen (was|het))\b", laag):
        return [("vorigjaar", _plaats_uit(tekst) or GEREEDSCHAP_PLAATS)]
    if re.search(r"\b(btw|21%|9%|exclusief|inclusief)\b", laag) and re.search(r"\d", laag):
        return [("btw", tekst)]
    if re.search(r"\b(inflatie|prijsstijging|duurder geworden|cpi)\b", laag):
        return [("inflatie", "")]
    if (re.search(r"\b(wikipedia|encyclopedie|wie is|wat is een)\b", laag)
            and not re.search(r"\b(ander woord|synoniem|betekenis van)\b", laag)):
        return [("wikipedia", tekst)]
    if re.search(r"\b(marktplaats|tweedehands|wat kost een gebruikt|prijzen van)\b", laag):
        return [("prijswacht", tekst)]
    if re.search(r"\b(afgesloten|afsluiting\w*|dicht|wegwerkzaamheden)\b", laag):
        return [("afsluitingen", _weg_uit(tekst))]
    if re.search(r"\b(rss|feed|laatste berichten van)\b", laag):
        return [("feed", _adres_uit(tekst))]
    if re.search(r"\b(brandstofprijs\w*|benzineprijs\w*|benzine|diesel|lpg|pomp\w*|tanken)\b", laag):
        return [("brandstof", "")]
    if re.search(r"\b(zonsondergang|zonsopkomst|gouden uur|hoe laat wordt het donker|daglicht)\b", laag):
        return [("zon", _plaats_uit(tekst) or GEREEDSCHAP_PLAATS)]
    if re.search(r"\b(synoniem\w*|ander woord voor|betekenis van)\b", laag):
        return [("synoniemen", tekst)]
    if re.search(r"\b(ons|pond|cup|eetlepel\w*|theelepel\w*|kopje|milliliter|ml|gram|kilo)\b", laag) and re.search(r"\d", laag):
        return [("koken", tekst)]
    if re.search(r"(prijsgeschiedenis|prijsverloop|prijsontwikkeling|wordt .{0,25}(goedkoper|duurder)|(goedkoper|duurder) geworden)", laag):
        return [("prijsgeschiedenis", tekst)]
    if re.search(r"\b(artikel van de dag|wat leest men|meest gelezen|leuk weetje)\b", laag):
        return [("artikel", "")]
    if re.search(r"\b(rijksoverheid|persbericht\w*|wat heeft de overheid|ministerie)\b", laag):
        return [("persberichten", "")]
    if re.search(r"(reistijd|hoe lang doe ik (er)?over|hoeveel uur rijden|van .{2,30} naar .{2,30} rijden)", laag):
        return [("reistijd", tekst)]
    if re.search(r"\b(spellingscontrole|spelling|taalfout\w*|controleer (deze|mijn) tekst)\b", laag):
        return [("spelling", tekst)]

    if re.search(r"regenradar|waar regent het|regent het nu", laag):
        return [("regenradar", _plaats_uit(tekst) or GEREEDSCHAP_PLAATS)]
    if re.search(r"neerslag|komende twee uur|eerste twee uur|buien de komende", laag):
        return [("neerslag", _plaats_uit(tekst) or GEREEDSCHAP_PLAATS)]
    if re.search(r"windstoot|windkracht|hoe hard waait|waait het", laag):
        return [("windstoten", _plaats_uit(tekst) or GEREEDSCHAP_PLAATS)]
    if re.search(r"uv-index per uur|zonkracht per uur|uv per uur|zonkracht vandaag", laag):
        return [("uv", _plaats_uit(tekst) or GEREEDSCHAP_PLAATS)]
    if re.search(r"verbrand|zonnebrand|in de zon|hoe lang in de zon", laag):
        return [("verbrandingstijd", _plaats_uit(tekst) or GEREEDSCHAP_PLAATS)]
    if re.search(r"klimaat|gemiddelde temperatuur per maand|normaal voor de tijd van het jaar", laag):
        return [("klimaat", _plaats_uit(tekst) or GEREEDSCHAP_PLAATS)]
    if re.search(r"warmste dag|koudste dag|tropische dagen|vorstnachten", laag):
        return [("warmste dag", _plaats_uit(tekst) or GEREEDSCHAP_PLAATS)]
    if re.search(r"getij|eb en vloed|hoogwater|laagwater|vloed", laag):
        return [("getijden", _plaats_uit(tekst) or GEREEDSCHAP_PLAATS)]
    if re.search(r"watertemperatuur|hoe warm is het water|zeewater", laag):
        return [("watertemperatuur", _plaats_uit(tekst) or GEREEDSCHAP_PLAATS)]
    if re.search(r"zwemwater|zwemplek|zwemmen in", laag):
        return [("zwemwater", _plaats_uit(tekst) or GEREEDSCHAP_PLAATS)]
    if re.search(r"maanfase|volle maan|nieuwe maan|maan\b", laag):
        return [("maanfase", _plaats_uit(tekst) or GEREEDSCHAP_PLAATS)]
    if re.search(r"werkzaamheden|wegwerk|aan de weg gewerkt|wordt er gewerkt", laag):
        return [("werkzaamheden", _weg_uit(tekst))]
    if re.search(r"snelheidslimiet|hoe hard mag ik|maximumsnelheid", laag):
        return [("snelheidslimiet", tekst)]
    if re.search(r"flitspa|flitser|snelheidscontrole", laag):
        return [("flitspalen", _plaats_uit(tekst) or GEREEDSCHAP_PLAATS)]
    if re.search(r"ov-halte|bushalte|welke lijn|openbaar vervoer in", laag):
        return [("ov", _plaats_uit(tekst) or GEREEDSCHAP_PLAATS)]
    if re.search(r"vertrektijd|wanneer gaat de bus|wanneer gaat de tram", laag):
        return [("vertrektijden", _plaats_uit(tekst) or GEREEDSCHAP_PLAATS)]
    if re.search(r"treinvertraging|rijden de treinen|trein op tijd", laag):
        return [("treinvertragingen", _plaats_uit(tekst) or GEREEDSCHAP_PLAATS)]
    if re.search(r"spoorwerk|werkzaamheden aan het spoor", laag):
        return [("spoorwerk", _plaats_uit(tekst) or GEREEDSCHAP_PLAATS)]
    if re.search(r"fietsroute|hoe lang fiets ik|fietsen naar", laag):
        return [("fietsroute", tekst)]
    if re.search(r"wandelroute|hoe lang loop ik|lopen naar", laag):
        return [("wandelroute", tekst)]
    if re.search(r"tolweg|tol\b|vignet", laag):
        return [("tolwegen", _plaats_uit(tekst) or GEREEDSCHAP_PLAATS)]
    if re.search(r"veerpont|pont\b|veerboot", laag):
        return [("veerponten", _plaats_uit(tekst) or GEREEDSCHAP_PLAATS)]
    if re.search(r"tankstation|waar kan ik tanken|benzinepomp", laag):
        return [("tankstations", _plaats_uit(tekst) or GEREEDSCHAP_PLAATS)]
    if re.search(r"p\+r|parkeer en reis|park and ride", laag):
        return [("p+r", _plaats_uit(tekst) or GEREEDSCHAP_PLAATS)]
    if re.search(r"valutakoers|koersverloop|hoe stond de dollar|hoe deed de (dollar|pond|euro)|dollar vorig jaar|pond vorig jaar", laag):
        return [("valuta-geschiedenis", tekst)]
    if re.search(r"crypto-geschiedenis|hoe deed bitcoin|bitcoin vorig jaar|bitcoin in (januari|februari|maart|april|mei|juni|juli|augustus|september|oktober|november|december)", laag):
        return [("crypto-geschiedenis", tekst)]
    if re.search(r"aandelenkoers|aandeel|koers van|beurs|asml|philips|heineken", laag):
        return [("aandelenkoers", tekst)]
    if re.search(r"hypotheekrente|hypotheek", laag):
        return [("hypotheekrente", _plaats_uit(tekst) or GEREEDSCHAP_PLAATS)]
    if re.search(r"spaarrente|spaargeld", laag):
        return [("spaarrente", _plaats_uit(tekst) or GEREEDSCHAP_PLAATS)]
    if re.search(r"teruglever|salderen|terugleververgoeding", laag):
        return [("teruglevering", )]
    if re.search(r"zonnepaneel|wp\b|opbrengst van mijn panelen", laag):
        return [("zonnepanelen", tekst)]
    if re.search(r"kost .{0,25}per jaar|kwh per jaar|stroomverbruik|wat kost een (vriezer|koelkast|wasmachine|droger|oven|tv|computer|server)", laag):
        return [("kosten apparaat", tekst)]
    if re.search(r"bruto netto|netto salaris|houd ik .{0,12}over|bruto salaris|netto over", laag):
        return [("bruto netto", tekst)]
    if re.search(r"belasting over|box 1|inkomstenbelasting", laag):
        return [("belasting", tekst)]
    if re.search(r"reiskosten|kilometervergoeding", laag):
        return [("reiskosten", tekst)]
    if re.search(r"rente op rente|samengestelde rente|spaardoel", laag):
        return [("rente op rente", tekst)]
    if re.search(r"kamerstuk|tweede kamer", laag):
        return [("kamerstukken", _plaats_uit(tekst) or GEREEDSCHAP_PLAATS)]
    if re.search(r"bekendmaking|gemeenteblad|wat heeft de gemeente besloten", laag):
        return [("bekendmakingen", _plaats_uit(tekst) or GEREEDSCHAP_PLAATS)]
    if re.search(r"verkiezing|verkiezingsuitslag|kiesraad", laag):
        return [("verkiezingen", _plaats_uit(tekst) or GEREEDSCHAP_PLAATS)]
    if re.search(r"subsidie", laag):
        return [("subsidies", _plaats_uit(tekst) or GEREEDSCHAP_PLAATS)]
    if re.search(r"wat verandert er|nieuwe regels per|per 1 januari", laag):
        return [("wat verandert er", _plaats_uit(tekst) or GEREEDSCHAP_PLAATS)]
    if re.search(r"vergunning|omgevingsloket", laag):
        return [("vergunningen", _plaats_uit(tekst) or GEREEDSCHAP_PLAATS)]
    if re.search(r"provincie|provinciale staten", laag):
        return [("provincie", _plaats_uit(tekst) or GEREEDSCHAP_PLAATS)]
    if re.search(r"wetten in werking|in werking getreden|nieuwe wet", laag):
        return [("wetten in werking", _plaats_uit(tekst) or GEREEDSCHAP_PLAATS)]
    if re.search(r"gemeentecijfers|kerncijfers|inwoners van de gemeente", laag):
        return [("gemeente open data", _plaats_uit(tekst) or GEREEDSCHAP_PLAATS)]
    if re.search(r"volledig(e)? artikel|heel artikel|wikipedia-artikel", laag):
        return [("wikipedia volledig", tekst)]
    if re.search(r"woord van de dag", laag):
        return [("woord van de dag", _plaats_uit(tekst) or GEREEDSCHAP_PLAATS)]
    if re.search(r"rijmwoord|wat rijmt|rijmt op", laag):
        return [("rijmwoorden", tekst)]
    if re.search(r"engels synoniem|synoniem in het engels", laag):
        return [("synoniemen engels", tekst)]
    if re.search(r"vervoeg|werkwoord", laag):
        return [("werkwoorden", tekst)]
    if re.search(r"in woorden|uitschrijven|hoe schrijf je \d", laag):
        return [("getallen in woorden", tekst)]
    if re.search(r"citaat|quote|uitspraak van", laag):
        return [("citaten", tekst)]
    if re.search(r"film op tv", laag):
        return [("film op tv", _plaats_uit(tekst) or GEREEDSCHAP_PLAATS)]
    if re.search(r"film\b|serie\b|wie speelt er in", laag):
        return [("films", tekst)]
    if re.search(r"album|muziek van|liedjes van", laag):
        return [("muziekalbums", tekst)]
    if re.search(r"hoofdstad|welk land|land .{0,20}inwoners", laag):
        return [("landeninfo", tekst)]
    if re.search(r"nobelprijs", laag):
        return [("nobelprijzen", _plaats_uit(tekst) or GEREEDSCHAP_PLAATS)]
    if re.search(r"vandaag in de geschiedenis|op deze dag", laag):
        return [("vandaag in het jaar", _plaats_uit(tekst) or GEREEDSCHAP_PLAATS)]
    if re.search(r"definitie|wat betekent", laag):
        return [("definitie", tekst)]
    if re.search(r"nieuws over|wat is er met", laag):
        return [("nieuws onderwerp", tekst)]
    if re.search(r"nieuws uit|wat gebeurt er in", laag):
        return [("nieuws gemeente", _plaats_uit(tekst) or GEREEDSCHAP_PLAATS)]
    if re.search(r"sportuitslag|uitslagen van|voetbal|eredivisie", laag):
        return [("sportuitslagen", _plaats_uit(tekst) or GEREEDSCHAP_PLAATS)]
    if re.search(r"tv-gids|op tv\b|televisie", laag):
        return [("tv-gids", _plaats_uit(tekst) or GEREEDSCHAP_PLAATS)]
    if re.search(r"podcast", laag):
        return [("podcasts", tekst)]
    if re.search(r"radiozender|radiostation|radio luisteren|welke zender", laag):
        return [("radiostations", _plaats_uit(tekst) or GEREEDSCHAP_PLAATS)]
    if re.search(r"nieuwsarchief|oud nieuws|afgelopen maand", laag):
        return [("nieuwsarchief", tekst)]
    if re.search(r"calorie|kcal", laag):
        return [("calorieen", tekst)]
    if re.search(r"voedingswaarde|eiwit|koolhydraten", laag):
        return [("voedingswaarde", tekst)]
    if re.search(r"allerge|gluten|noten in|melk in", laag):
        return [("allergenen", tekst)]
    if re.search(r"barcode|streepjescode", laag):
        return [("barcode", tekst)]
    if re.search(r"griep", laag):
        return [("griepcijfers", _plaats_uit(tekst) or GEREEDSCHAP_PLAATS)]
    if re.search(r"corona|covid", laag):
        return [("corona-cijfers", _plaats_uit(tekst) or GEREEDSCHAP_PLAATS)]
    if re.search(r"bijwerking|medicijn", laag):
        return [("bijwerkingen", tekst)]
    if re.search(r"ehbo|eerste hulp|brandwond|verslik|reanimatie|hartinfarct|beroerte|bloedneus|tekenbeet", laag):
        return [("ehbo", tekst)]
    if re.search(r"wachtlijst|wachttijd|ziekenhuis", laag):
        return [("wachttijden", _plaats_uit(tekst) or GEREEDSCHAP_PLAATS)]
    if re.search(r"calorie.{0,12}verbrand|verbrand.{0,20}calorie|hoeveel kcal", laag):
        return [("verbranding", tekst)]
    if re.search(r"supermarkt|albert heijn|jumbo|lidl|aldi\b", laag):
        return [("supermarkten", _plaats_uit(tekst) or GEREEDSCHAP_PLAATS)]
    if re.search(r"huizenprijs|woningprijs|wat kost een huis|verkoopprijs", laag):
        return [("huizenprijzen", _plaats_uit(tekst) or GEREEDSCHAP_PLAATS)]
    if re.search(r"hoe hoog ligt|hoogte boven zeeniveau|boven nap", laag):
        return [("hoogte", _plaats_uit(tekst) or GEREEDSCHAP_PLAATS)]
    if re.search(r"evenement|wat is er te doen", laag):
        return [("evenementen", _plaats_uit(tekst) or GEREEDSCHAP_PLAATS)]
    if re.search(r"\bafval\b|kliko|container aan de straat", laag):
        return [("afval ophalen", _plaats_uit(tekst) or GEREEDSCHAP_PLAATS)]
    if re.search(r"zon op mijn dak|dakopbrengst|zonne-opbrengst", laag):
        return [("zon op je dak", _plaats_uit(tekst) or GEREEDSCHAP_PLAATS)]
    if re.search(r"bereikbaar|ov in de buurt|haltes in de buurt", laag):
        return [("bereikbaarheid", _plaats_uit(tekst) or GEREEDSCHAP_PLAATS)]

    keuzes: list[tuple[str, str]] = []
    if re.search(r"\b(weer|weersverwachting|weersvoorspelling|temperatuur|graden|regen|sneeuw|zonnig|bewolkt|waait|fris|koud|warm)\b", laag):
        keuzes.append(("weer", _plaats_uit(tekst) or GEREEDSCHAP_PLAATS))
    if re.search(r"\b(nieuws|actualiteit|actualiteiten|laatste ontwikkelingen|wat speelt er)\b", laag):
        keuzes.append(("nieuws", ""))
    if re.search(r"\b(hoe laat|welke dag|welke datum|datum van vandaag|vandaag de dag)\b", laag):
        keuzes.append(("tijd", ""))
    if not keuzes:
        if re.search(r"\b(wie is|wie was|wie zijn|wat is|wat zijn|waar ligt|waar staat|geschiedenis van|uitleg over|wat betekent|hoe werkt)\b", laag):
            onderwerp = re.sub(
                r"^.*?\b(wie is|wie was|wie zijn|wat is|wat zijn|waar ligt|waar staat|"
                r"geschiedenis van|uitleg over|wat betekent|hoe werkt)\b",
                "",
                laag,
            ).strip(" ?!.:")
            keuzes.append(("naslag", onderwerp))
        elif re.search(r"\b(zoek|zoek op|zoek eens|opzoeken|op te zoeken|search|google)\b", laag):
            keuzes.append(("zoeken", tekst))
    return keuzes[:3]


def gereedschap_uitvoeren(keuzes: list[tuple[str, str]]) -> list[dict]:
    """Voer het gekozen gereedschap uit. Fouten zijn geen crash: ze worden gemeld."""
    uitkomsten = []
    for soort, argument in keuzes:
        try:
            if soort == "weer":
                uitkomsten.append(gereedschap_weer(argument))
            elif soort == "nieuws":
                uitkomsten.append(gereedschap_nieuws())
            elif soort == "naslag":
                uitkomsten.append(gereedschap_wiki(argument))
            elif soort == "zoeken":
                uitkomsten.append(gereedschap_zoek(argument))
            elif soort == "tijd":
                uitkomsten.append(gereedschap_tijd())
            elif soort == "rekenen":
                uitkomsten.append(gereedschap_rekenen(argument))
            elif soort == "eenheden":
                uitkomsten.append(gereedschap_eenheden_tekst(argument))
            elif soort == "datum":
                uitkomsten.append(gereedschap_datum(argument))
            elif soort == "feestdagen":
                uitkomsten.append(gereedschap_feestdagen_tekst(argument))
            elif soort == "tijdzone":
                uitkomsten.append(gereedschap_tijdzone(argument))
            elif soort == "postcode":
                uitkomsten.append(gereedschap_postcode(argument))
            elif soort == "buien":
                uitkomsten.append(gereedschap_buien(argument))
            elif soort == "lucht":
                uitkomsten.append(gereedschap_lucht(argument))
            elif soort == "zon":
                uitkomsten.append(gereedschap_zon(argument))
            elif soort == "valuta":
                uitkomsten.append(gereedschap_valuta(argument))
            elif soort == "crypto":
                uitkomsten.append(gereedschap_crypto(argument))
            elif soort == "woordenboek":
                uitkomsten.append(gereedschap_woordenboek(argument))
            elif soort == "vertalen":
                uitkomsten.append(gereedschap_vertaal(argument))
            elif soort == "domein":
                uitkomsten.append(gereedschap_domein(argument))
            elif soort == "verkeer":
                uitkomsten.append(gereedschap_verkeer(argument))
            elif soort == "recepten":
                uitkomsten.append(gereedschap_recepten(argument))
            elif soort == "akkoorden":
                uitkomsten.append(gereedschap_akkoorden(argument))
            elif soort == "zittesj":
                uitkomsten.append(gereedschap_zittesj(argument))
            elif soort == "notities":
                uitkomsten.append(gereedschap_notities(argument))
            elif soort == "status":
                uitkomsten.append(gereedschap_status())
            elif soort == "water":
                uitkomsten.append(gereedschap_water(argument))
            elif soort == "stroomprijs":
                uitkomsten.append(gereedschap_stroomprijs())
            elif soort == "afstand":
                uitkomsten.append(gereedschap_afstand(argument))
            elif soort == "boodschappen":
                uitkomsten.append(gereedschap_boodschappen(argument))
            elif soort == "wetten":
                uitkomsten.append(gereedschap_wet(argument))
            elif soort == "cbs":
                uitkomsten.append(gereedschap_cbs(argument))
            elif soort == "boek":
                uitkomsten.append(gereedschap_boek(argument))
            elif soort == "vliegtuigen":
                uitkomsten.append(gereedschap_vliegtuigen(argument))
            elif soort == "document":
                uitkomsten.append(gereedschap_document(argument))
            elif soort == "aardbevingen":
                uitkomsten.append(gereedschap_aardbevingen(argument))
            elif soort == "hooikoorts":
                uitkomsten.append(gereedschap_hooikoorts(argument))
            elif soort == "bruggen":
                uitkomsten.append(gereedschap_bruggen(argument))
            elif soort == "verwachting":
                uitkomsten.append(gereedschap_verwachting(argument))
            elif soort == "gasprijs":
                uitkomsten.append(gereedschap_gasprijs())
            elif soort == "horeca":
                uitkomsten.append(gereedschap_horeca(argument))
            elif soort == "weeralarm":
                uitkomsten.append(gereedschap_weeralarm(argument))
            elif soort == "buurtcijfers":
                uitkomsten.append(gereedschap_buurtcijfers(argument))
            elif soort == "devhulp":
                uitkomsten.append(gereedschap_devhulp(argument))
            elif soort == "uurverloop":
                uitkomsten.append(gereedschap_uurverloop(argument))
            elif soort == "laadpalen":
                uitkomsten.append(gereedschap_laadpalen(argument))
            elif soort == "parkeren":
                uitkomsten.append(gereedschap_parkeren(argument))
            elif soort == "vorigjaar":
                uitkomsten.append(gereedschap_vorigjaar(argument))
            elif soort == "btw":
                uitkomsten.append(gereedschap_btw(argument))
            elif soort == "inflatie":
                uitkomsten.append(gereedschap_inflatie())
            elif soort == "wikipedia":
                uitkomsten.append(gereedschap_wikipedia(argument))
            elif soort == "prijswacht":
                uitkomsten.append(gereedschap_prijswacht(argument))
            elif soort == "afsluitingen":
                uitkomsten.append(gereedschap_afsluitingen(argument))
            elif soort == "feed":
                uitkomsten.append(gereedschap_feed(argument))
            elif soort == "brandstof":
                uitkomsten.append(gereedschap_brandstof())
            elif soort == "zon":
                uitkomsten.append(gereedschap_zon(argument))
            elif soort == "synoniemen":
                uitkomsten.append(gereedschap_synoniemen(argument))
            elif soort == "koken":
                uitkomsten.append(gereedschap_koken(argument))
            elif soort == "prijsgeschiedenis":
                uitkomsten.append(gereedschap_prijsgeschiedenis(argument))
            elif soort == "artikel":
                uitkomsten.append(gereedschap_artikel())
            elif soort == "persberichten":
                uitkomsten.append(gereedschap_persberichten())
            elif soort == "reistijd":
                uitkomsten.append(gereedschap_reistijd(argument))
            elif soort == "spelling":
                uitkomsten.append(gereedschap_spelling(argument))
            elif soort in GEREEDSCHAP_ROUTES:
                functie, veld = GEREEDSCHAP_ROUTES[soort]
                uitkomsten.append(functie(argument) if veld else functie())
        except Exception as fout:  # nooit de vraag laten stuklopen op gereedschap
            uitkomsten.append(
                {"naam": soort, "bron": "", "tekst": "", "fout": f"{type(fout).__name__}: {fout}"}
            )
    return uitkomsten


def gereedschap_blok(uitkomsten: list[dict]) -> str:
    """De opgehaalde gegevens als blok boven de vraag."""
    bruikbaar = [u for u in uitkomsten if u.get("tekst")]
    if not bruikbaar:
        return ""
    nu = time.localtime()
    koppen = [f"[Actuele gegevens, opgehaald {nu.tm_mday}-{nu.tm_mon:02d}-{nu.tm_year} "
              f"{nu.tm_hour:02d}:{nu.tm_min:02d}]"]
    for uitkomst in bruikbaar:
        koppen.append(uitkomst["tekst"])
        if uitkomst.get("bron"):
            koppen.append(f"(bron: {uitkomst['bron']})")
    koppen.append(
        "Gebruik deze gegevens als ze bij de vraag passen, en verzin niets bij wat er "
        "niet staat. Klopt het antwoord niet met de gegevens, zeg dan wat er wel staat."
    )
    return "\n".join(koppen)


def verrijk(messages: list[dict]) -> tuple[list[dict], list[dict]]:
    """Zet de opgehaalde gegevens in het laatste bericht van de gebruiker.

    Bewust in dat bericht en niet als extra systeembericht: bij een lang gesprek
    laat de inkorting oudere berichten weg, maar het laatste bericht blijft altijd
    staan -- en juist daar hoort de actuele vraag bij.
    """
    if not GEREEDSCHAP or not messages:
        return messages, []
    laatste = None
    for bericht in reversed(messages):
        if bericht.get("role") == "user":
            laatste = bericht
            break
    if laatste is None:
        return messages, []
    inhoud = laatste.get("content")
    if not isinstance(inhoud, str) or not inhoud.strip():
        return messages, []
    keuzes = gereedschap_kies(inhoud)
    if not keuzes:
        return messages, []
    uitkomsten = gereedschap_uitvoeren(keuzes)
    blok = gereedschap_blok(uitkomsten)
    gelukt = [u["naam"] for u in uitkomsten if u.get("tekst")]
    mislukt = [f"{u['naam']} ({u.get('fout')})" for u in uitkomsten if not u.get("tekst")]
    if gelukt:
        sys.stderr.write(f"[wllm] gereedschap gebruikt: {', '.join(gelukt)}\n")
    if mislukt:
        sys.stderr.write(f"[wllm] gereedschap mislukt: {', '.join(mislukt)}\n")
    if not blok:
        return messages, uitkomsten
    nieuw = dict(laatste)
    nieuw["content"] = f"{blok}\n\n{inhoud}"
    vervangen = list(messages)
    for i in range(len(vervangen) - 1, -1, -1):
        if vervangen[i] is laatste:
            vervangen[i] = nieuw
            break
    return vervangen, uitkomsten


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
            # Wat het model niet zelf kan weten, haalt de engine op.
            f"<dt>Gereedschap</dt><dd>{'aan' if GEREEDSCHAP else 'uit'}"
            f" <small>({', '.join(GEREEDSCHAP_SOORTEN)})</small></dd>"
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

    @property
    def _schrijf_slot(self) -> threading.Lock:
        """Eén slot per verbinding: de hartslag en het antwoord mogen elkaar nooit
        midden in een regel raken."""
        slot = getattr(self, "_slot", None)
        if slot is None:
            slot = threading.Lock()
            self._slot = slot
        return slot

    def _wakker_houden(self) -> threading.Event:
        """Stuur elke 10 s een leeg bericht zolang het antwoord nog niet begint.

        Nodig voor verbindingen met een tussenschakel die na honderd seconden
        stilte afbreekt: Cloudflare antwoordt dan met 524, ook al rekent de
        rekenkern gewoon door. Een regel die met ':' begint is een leeg
        SSE-bericht dat elke client negeert, maar houdt de verbinding wel open."""
        stop = threading.Event()

        def tik() -> None:
            while not stop.wait(10.0):
                # Zodra er echt geantwoord wordt is de hartslag niet meer nodig.
                if getattr(self, "_geschreven", False):
                    return
                with self._schrijf_slot:
                    try:
                        self.wfile.write(b": rekenen\n\n")
                        self.wfile.flush()
                    except Exception:
                        return

        threading.Thread(target=tik, daemon=True).start()
        return stop

    def _stream_start(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/x-ndjson")
        self.send_header("Connection", "close")
        self.end_headers()

    def _nd(self, obj) -> bool:
        try:
            with self._schrijf_slot:
                self.wfile.write((json.dumps(obj) + "\n").encode())
                self.wfile.flush()
                self._geschreven = True
            return True
        except (BrokenPipeError, ConnectionResetError):
            return False

    def _sse(self, obj) -> bool:
        """Eén regel van de OpenAI-stream. Ook hier met het schrijfslot, want de
        hartslag mag er nooit middenin vallen."""
        try:
            with self._schrijf_slot:
                self.wfile.write(f"data: {json.dumps(obj)}\n\n".encode())
                self.wfile.flush()
                self._geschreven = True
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
    def _vraag(self, naam: str) -> str:
        """Eén waarde uit de queryreeks, bijvoorbeeld ?plaats=Hilversum."""
        query = self.path.split("?", 1)[1] if "?" in self.path else ""
        waarden = urllib.parse.parse_qs(query).get(naam) or [""]
        return (waarden[0] or "").strip()

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
        if path == "/api/gereedschap":
            return self._json(
                {
                    "aan": GEREEDSCHAP,
                    "soorten": GEREEDSCHAP_SOORTEN,
                    "standaard_plaats": GEREEDSCHAP_PLAATS,
                    "tijd": gereedschap_tijd()["tekst"],
                }
            )
        if path == "/api/weer":
            return self._json(gereedschap_weer(self._vraag("plaats") or GEREEDSCHAP_PLAATS))
        if path == "/api/wiki":
            return self._json(gereedschap_wiki(self._vraag("q") or self._vraag("onderwerp")))
        if path == "/api/zoek":
            return self._json(gereedschap_zoek(self._vraag("q") or self._vraag("vraag")))
        if path == "/api/nieuws":
            return self._json(gereedschap_nieuws())
        if path == "/api/tijd":
            return self._json(gereedschap_tijd())
        if path.startswith("/api/"):
            soort = urllib.parse.unquote(path[len("/api/") :].strip("/"))
            invoer = GEREEDSCHAP_ROUTES.get(soort)
            if invoer:
                functie, veld = invoer
                return self._json(functie(self._vraag(veld) or "") if veld else functie())
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
        # Gereedschap: actuele gegevens (weer, nieuws, naslag) boven de vraag zetten.
        messages, _gebruikt = verrijk(messages)
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
            # Ook op deze route gereedschap: de OpenAI-route is de route die
            # clients als Hermes gebruiken.
            msgs, _gebruikt = verrijk(msgs)
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
        # Hartslag: houd de verbinding open terwijl de rekenkern nog rekent.
        self._wakker_houden()
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
        # Hartslag: houd de verbinding open terwijl de rekenkern nog rekent.
        self._wakker_houden()
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
            # Hartslag: houd de verbinding open terwijl de rekenkern nog rekent.
            self._wakker_houden()
            for chunk in engine_stream(eng, payload, self.connection):
                if not self._sse(chunk):
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
