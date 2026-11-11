#!/usr/bin/env python3
"""
Werdens Media LLM — werkruimtelaag.

Werkruimtes per project, documenten erin met bronvermelding, vectordatabase op
SQLite, gesprekken met geschiedenis, een REST-API met sleutels en een
embed-widget voor je eigen websites.

Pure Python: alleen de standaardbibliotheek. De taal- en embeddingmodellen komen
van de eigen engine (zie ../engine/wllm.py).

CLI:
  wmlm_workspace.py serve
  wmlm_workspace.py workspace-aanmaken "Mijn project"
  wmlm_workspace.py document-toevoegen <werkruimte> --titel "..." --bestand pad.md
  wmlm_workspace.py vraag <werkruimte> "je vraag"

API:
  GET  /                          webinterface
  GET  /health
  GET  /api/workspaces            POST /api/workspaces
  GET  /api/workspaces/<slug>/documents
  POST /api/workspaces/<slug>/documents     {title, text, source}
  POST /api/workspaces/<slug>/documents/bestand  {title, url}  (webpagina ophalen)
  POST /api/workspaces/<slug>/query         {question, top_k}
  GET  /api/workspaces/<slug>/chats
  GET  /api/chats/<id>
  POST /api/workspaces/<slug>/sleutels      nieuwe API-sleutel
  POST /v1/chat/completions   model="werkruimte:<slug>"  → antwoord met bronnen
  GET  /widget.js             embed-widget
  GET  /embed?werkruimte=<slug>   pagina voor in een iframe
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import math
import os
import re
import secrets
import sqlite3
import struct
import sys
import threading
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# --------------------------------------------------------------------------- #
# Configuratie
# --------------------------------------------------------------------------- #
BASIS = Path(__file__).resolve().parent
DATA = Path(os.environ.get("WMLM_DATA", BASIS.parent / "data"))
DB = DATA / "werkruimte.db"
ENGINE = os.environ.get("WMLM_ENGINE_URL", "http://127.0.0.1:11434")
EMBED_MODEL = os.environ.get("WMLM_EMBED_MODEL", "nomic-embed-text-v1.5.Q8_0")
CHAT_MODEL = os.environ.get("WMLM_CHAT_MODEL", "qwen3-4b-instruct-2507-q4_k_m")
HOST = os.environ.get("WMLM_HOST", "0.0.0.0")
PORT = int(os.environ.get("WMLM_PORT", "8090"))
CHUNK_CHARS = int(os.environ.get("WMLM_CHUNK_CHARS", "900"))
CHUNK_OVERLAP = int(os.environ.get("WMLM_CHUNK_OVERLAP", "120"))
TOP_K = int(os.environ.get("WMLM_TOP_K", "5"))
VERSIE = "0.1.0"

# Merk: het logo van Werdens Media staat in assets/ naast de drie lagen. Mist het
# bestand, dan gaat er een lege PNG van 1x1 over de lijn zodat geen pagina een
# kapot beeld toont.
def _logo_pad() -> Path:
    """Zoek het merklogo. In de repo staat dit script in workspace/, in de
    container in de projectmap; probeer daarom beide niveau's."""
    hier = Path(__file__).resolve().parent
    for basis in (hier.parent, hier):
        kandidaat = basis / "assets" / "werdens-media-logo.png"
        if kandidaat.exists():
            return kandidaat
    return hier.parent / "assets" / "werdens-media-logo.png"


LOGO = _logo_pad()
LEGE_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


def logo_bytes() -> bytes:
    try:
        return LOGO.read_bytes()
    except OSError:
        return LEGE_PNG


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

DATA.mkdir(parents=True, exist_ok=True)
_db_lock = threading.Lock()


def nu() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def slugify(s: str) -> str:
    s = s.strip().lower()
    s = re.sub(r"[^a-z0-9\s-]", "", s)
    s = re.sub(r"[\s-]+", "-", s).strip("-")
    return s or "werkruimte"


# --------------------------------------------------------------------------- #
# Database
# --------------------------------------------------------------------------- #
SCHEMA = """
CREATE TABLE IF NOT EXISTS workspaces (
  id INTEGER PRIMARY KEY, slug TEXT UNIQUE NOT NULL, naam TEXT NOT NULL,
  systeem_prompt TEXT DEFAULT '', chat_model TEXT DEFAULT '',
  embed_model TEXT DEFAULT '', aangemaakt TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS documents (
  id INTEGER PRIMARY KEY, workspace_id INTEGER NOT NULL, titel TEXT NOT NULL,
  bron TEXT DEFAULT '', sha256 TEXT, tekens INTEGER DEFAULT 0,
  toegevoegd TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chunks (
  id INTEGER PRIMARY KEY, document_id INTEGER NOT NULL, workspace_id INTEGER NOT NULL,
  volgnr INTEGER NOT NULL, tekst TEXT NOT NULL, vector BLOB
);
CREATE INDEX IF NOT EXISTS idx_chunks_ws ON chunks(workspace_id);
CREATE TABLE IF NOT EXISTS chats (
  id INTEGER PRIMARY KEY, workspace_id INTEGER NOT NULL, titel TEXT DEFAULT '',
  aangemaakt TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS berichten (
  id INTEGER PRIMARY KEY, chat_id INTEGER NOT NULL, rol TEXT NOT NULL,
  inhoud TEXT NOT NULL, bronnen TEXT DEFAULT '', tijd TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sleutels (
  sleutel TEXT PRIMARY KEY, workspace_id INTEGER NOT NULL, label TEXT DEFAULT '',
  aangemaakt TEXT NOT NULL
);
"""


@contextmanager
def db():
    con = sqlite3.connect(DB, timeout=30)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA foreign_keys=ON")
        yield con
        con.commit()
    finally:
        con.close()


def init_db() -> None:
    with _db_lock, db() as con:
        con.executescript(SCHEMA)


# --------------------------------------------------------------------------- #
# De engine aanspreken
# --------------------------------------------------------------------------- #
class EngineFout(RuntimeError):
    pass


def _post(pad: str, data: dict, timeout: int = 900) -> dict:
    req = urllib.request.Request(
        ENGINE + pad, data=json.dumps(data).encode(), headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read()[:300].decode("utf-8", "replace")
        raise EngineFout(f"engine gaf HTTP {e.code}: {detail}") from e
    except urllib.error.URLError as e:
        raise EngineFout(f"engine niet bereikbaar op {ENGINE}: {e}") from e


def embed(teksten: list[str], model: str | None = None) -> list[list[float]]:
    """Vectoren via de eigen engine. Normaliseert, zodat zoeken een inproduct is."""
    if not teksten:
        return []
    uit = _post("/v1/embeddings", {"model": model or EMBED_MODEL, "input": teksten})
    rijen = uit.get("data") or []
    if len(rijen) != len(teksten):
        raise EngineFout(f"engine gaf {len(rijen)} vectoren voor {len(teksten)} teksten")
    vecs = []
    for rij in rijen:
        v = [float(x) for x in rij["embedding"]]
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        vecs.append([x / n for x in v])
    return vecs


def chat(messages: list[dict], model: str | None = None, temperatuur: float = 0.3) -> str:
    uit = _post(
        "/api/chat",
        {
            "model": model or CHAT_MODEL,
            "messages": messages,
            "stream": False,
            "options": {"temperature": temperatuur},
        },
    )
    return (uit.get("message") or {}).get("content", "")


def engine_online() -> dict:
    try:
        with urllib.request.urlopen(ENGINE + "/api/version", timeout=4) as r:
            return {"online": True, "versie": json.load(r).get("version")}
    except Exception as e:  # noqa: BLE001
        return {"online": False, "fout": str(e)}


# --------------------------------------------------------------------------- #
# Vectoren: opslaan en zoeken
# --------------------------------------------------------------------------- #
def vec_naar_blob(v: list[float]) -> bytes:
    return struct.pack(f"<{len(v)}f", *v)


def blob_naar_vec(b: bytes) -> list[float]:
    return list(struct.unpack(f"<{len(b) // 4}f", b))


def inproduct(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def lexicaal_score(vraag: str, tekst: str) -> float:
    """Terugval als er geen embeddingmodel is: eenvoudige woordoverlap."""
    woorden = {w for w in re.findall(r"[a-zA-ZÀ-ÿ0-9]{3,}", vraag.lower())}
    if not woorden:
        return 0.0
    doel = tekst.lower()
    hits = sum(doel.count(w) for w in woorden)
    return hits / (1 + len(tekst) / 1000)


# --------------------------------------------------------------------------- #
# Documenten inlezen en opdelen
# --------------------------------------------------------------------------- #
def html_naar_tekst(html: str) -> str:
    html = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?is)<br\s*/?>|</p>|</div>|</li>|</h[1-6]>", "\n", html)
    html = re.sub(r"(?s)<[^>]+>", " ", html)
    html = (
        html.replace("&nbsp;", " ")
        .replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&quot;", '"')
        .replace("&#39;", "'")
    )
    html = re.sub(r"[ \t]+", " ", html)
    html = re.sub(r"\n\s*\n\s*\n+", "\n\n", html)
    return html.strip()


def bestand_naar_tekst(pad: Path) -> str:
    ext = pad.suffix.lower()
    if ext in (".html", ".htm"):
        return html_naar_tekst(pad.read_text(encoding="utf-8", errors="replace"))
    if ext == ".pdf":
        import shutil
        import subprocess

        if not shutil.which("pdftotext"):
            raise ValueError(
                "PDF lezen vraagt pdftotext. Installeer poppler-utils, of plak de tekst zelf."
            )
        uit = subprocess.run(
            ["pdftotext", "-layout", str(pad), "-"], capture_output=True, timeout=120
        )
        if uit.returncode != 0:
            raise ValueError("pdftotext kon dit bestand niet lezen.")
        return uit.stdout.decode("utf-8", "replace")
    if ext in (".docx", ".odt"):
        raise ValueError(
            "Word- en ODT-bestanden: converteer eerst naar tekst of PDF "
            "(bijvoorbeeld met pandoc of LibreOffice)."
        )
    return pad.read_text(encoding="utf-8", errors="replace")


def haal_url(url: str, max_bytes: int = 3_000_000) -> tuple[str, str]:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "WerdensMediaLLM/0.1 (+eigen server)",
            "Accept": "text/html,text/plain,application/xhtml+xml",
        },
    )
    with urllib.request.urlopen(req, timeout=45) as r:
        ruw = r.read(max_bytes)
        ctype = r.headers.get("Content-Type", "")
    tekst = ruw.decode("utf-8", "replace")
    if "html" in ctype.lower() or tekst.lstrip().lower().startswith(("<!doctype", "<html")):
        tekst = html_naar_tekst(tekst)
    titel = ""
    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", ruw.decode("utf-8", "replace"))
    if m:
        titel = re.sub(r"\s+", " ", m.group(1)).strip()
    return titel, tekst.strip()


def opdelen(tekst: str, doel: int = CHUNK_CHARS, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Alinea-bewust opdelen: eerst op lege regels, dan op zinnen, met overlap."""
    tekst = re.sub(r"\r\n?", "\n", tekst).strip()
    if not tekst:
        return []
    brokken: list[str] = []
    huidig = ""
    for alinea in re.split(r"\n\s*\n", tekst):
        alinea = alinea.strip()
        if not alinea:
            continue
        if len(huidig) + len(alinea) + 2 <= doel:
            huidig = f"{huidig}\n\n{alinea}".strip()
            continue
        if huidig:
            brokken.append(huidig)
        if len(alinea) <= doel:
            huidig = alinea
            continue
        # te lange alinea: op zinnen knippen
        stuk = ""
        for zin in re.split(r"(?<=[.!?;:])\s+", alinea):
            if len(stuk) + len(zin) + 1 <= doel:
                stuk = f"{stuk} {zin}".strip()
            else:
                if stuk:
                    brokken.append(stuk)
                stuk = zin
        huidig = stuk
    if huidig:
        brokken.append(huidig)
    if overlap <= 0 or len(brokken) < 2:
        return brokken
    met_overlap = [brokken[0]]
    for vorige, huidige in zip(brokken, brokken[1:]):
        staart = vorige[-overlap:]
        met_overlap.append((staart + " " + huidige).strip())
    return met_overlap


# --------------------------------------------------------------------------- #
# Werkruimtes
# --------------------------------------------------------------------------- #
def werkruimte_zoek(slug: str) -> sqlite3.Row:
    with db() as con:
        rij = con.execute("SELECT * FROM workspaces WHERE slug=?", (slug,)).fetchone()
    if rij is None:
        raise KeyError(f"werkruimte '{slug}' bestaat niet")
    return rij


def werkruimte_aanmaken(naam: str, slug: str | None = None, systeem_prompt: str = "",
                        chat_model: str = "", embed_model: str = "") -> dict:
    slug = slugify(slug or naam)
    with _db_lock, db() as con:
        bestaand = con.execute("SELECT slug FROM workspaces WHERE slug=?", (slug,)).fetchone()
        if bestaand:
            raise ValueError(f"werkruimte '{slug}' bestaat al")
        con.execute(
            "INSERT INTO workspaces (slug,naam,systeem_prompt,chat_model,embed_model,aangemaakt)"
            " VALUES (?,?,?,?,?,?)",
            (slug, naam, systeem_prompt, chat_model, embed_model, nu()),
        )
    return {"slug": slug, "naam": naam, "aangemaakt": nu()}


def werkruimtes() -> list[dict]:
    with db() as con:
        rijen = con.execute(
            "SELECT w.*, (SELECT COUNT(*) FROM documents d WHERE d.workspace_id=w.id) AS documenten,"
            " (SELECT COUNT(*) FROM chunks c WHERE c.workspace_id=w.id) AS brokken"
            " FROM workspaces w ORDER BY w.naam"
        ).fetchall()
    return [dict(r) for r in rijen]


def document_toevoegen(slug: str, titel: str, tekst: str, bron: str = "") -> dict:
    ws = werkruimte_zoek(slug)
    tekst = (tekst or "").strip()
    if not tekst:
        raise ValueError("geen tekst om toe te voegen")
    brokken = opdelen(tekst)
    if not brokken:
        raise ValueError("na opdelen bleef er niets over")
    embed_model = ws["embed_model"] or EMBED_MODEL
    vectoren: list[list[float] | None]
    embed_fout = ""
    try:
        vectoren = list(embed(brokken, embed_model))
    except EngineFout as e:
        vectoren = [None] * len(brokken)
        embed_fout = str(e)
    with _db_lock, db() as con:
        cur = con.execute(
            "INSERT INTO documents (workspace_id,titel,bron,sha256,tekens,toegevoegd)"
            " VALUES (?,?,?,?,?,?)",
            (ws["id"], titel or "zonder titel", bron,
             hashlib.sha256(tekst.encode()).hexdigest()[:16], len(tekst), nu()),
        )
        doc_id = cur.lastrowid
        for i, (brok, vec) in enumerate(zip(brokken, vectoren)):
            con.execute(
                "INSERT INTO chunks (document_id,workspace_id,volgnr,tekst,vector) VALUES (?,?,?,?,?)",
                (doc_id, ws["id"], i, brok, vec_naar_blob(vec) if vec else None),
            )
    return {
        "document_id": doc_id,
        "titel": titel,
        "brokken": len(brokken),
        "tekens": len(tekst),
        "vectoren": sum(1 for v in vectoren if v),
        "waarschuwing": f"geen vectoren ({embed_fout}); zoeken gebeurt nu op woordoverlap" if embed_fout else "",
    }


def documenten(slug: str) -> list[dict]:
    ws = werkruimte_zoek(slug)
    with db() as con:
        rijen = con.execute(
            "SELECT d.*, (SELECT COUNT(*) FROM chunks c WHERE c.document_id=d.id) AS brokken"
            " FROM documents d WHERE d.workspace_id=? ORDER BY d.id DESC",
            (ws["id"],),
        ).fetchall()
    return [dict(r) for r in rijen]


def document_verwijderen(slug: str, doc_id: int) -> None:
    ws = werkruimte_zoek(slug)
    with _db_lock, db() as con:
        con.execute("DELETE FROM chunks WHERE document_id=? AND workspace_id=?", (doc_id, ws["id"]))
        con.execute("DELETE FROM documents WHERE id=? AND workspace_id=?", (doc_id, ws["id"]))


def zoek(slug: str, vraag: str, top_k: int = TOP_K) -> list[dict]:
    ws = werkruimte_zoek(slug)
    with db() as con:
        rijen = con.execute(
            "SELECT c.id, c.tekst, c.vector, c.volgnr, d.titel, d.bron, d.id AS doc_id"
            " FROM chunks c JOIN documents d ON d.id=c.document_id WHERE c.workspace_id=?",
            (ws["id"],),
        ).fetchall()
    if not rijen:
        return []
    qvec: list[float] | None = None
    try:
        qvec = embed([vraag], ws["embed_model"] or EMBED_MODEL)[0]
    except EngineFout:
        qvec = None
    scores: list[tuple[float, sqlite3.Row]] = []
    for r in rijen:
        if qvec is not None and r["vector"]:
            s = inproduct(qvec, blob_naar_vec(r["vector"]))
        else:
            s = lexicaal_score(vraag, r["tekst"])
        scores.append((s, r))
    scores.sort(key=lambda x: x[0], reverse=True)
    uit = []
    for s, r in scores[: max(1, top_k)]:
        if s <= 0:
            continue
        uit.append(
            {
                "score": round(s, 4),
                "titel": r["titel"],
                "bron": r["bron"],
                "document_id": r["doc_id"],
                "brok": r["volgnr"],
                "tekst": r["tekst"],
            }
        )
    return uit


def vraag(slug: str, vraagtekst: str, top_k: int = TOP_K, chat_id: int | None = None) -> dict:
    ws = werkruimte_zoek(slug)
    bronnen = zoek(slug, vraagtekst, top_k)
    if bronnen:
        blokken = "\n\n".join(
            f"[{i + 1}] {b['titel']}" + (f" ({b['bron']})" if b["bron"] else "") + f"\n{b['tekst']}"
            for i, b in enumerate(bronnen)
        )
        systeem = (
            "Je bent de assistent van Werdens Media LLM. Antwoord in het Nederlands, "
            "bondig en alleen op basis van de bronnen hieronder. Verwijs naar de "
            "bronnen met hun nummer tussen blokhaken, bijvoorbeeld [1]. Weet je het "
            "antwoord niet uit de bronnen, zeg dat dan eerlijk."
        )
        if ws["systeem_prompt"]:
            systeem += "\n\n" + ws["systeem_prompt"]
        gebruiker = f"Bronnen:\n{blokken}\n\nVraag: {vraagtekst}"
    else:
        systeem = ws["systeem_prompt"] or (
            "Je bent de assistent van Werdens Media LLM. Antwoord in het Nederlands."
        )
        gebruiker = vraagtekst
    antwoord = chat(
        [{"role": "system", "content": systeem}, {"role": "user", "content": gebruiker}],
        ws["chat_model"] or CHAT_MODEL,
    )
    if chat_id is None:
        with _db_lock, db() as con:
            cur = con.execute(
                "INSERT INTO chats (workspace_id,titel,aangemaakt) VALUES (?,?,?)",
                (ws["id"], vraagtekst[:80], nu()),
            )
            chat_id = cur.lastrowid
    with _db_lock, db() as con:
        con.execute(
            "INSERT INTO berichten (chat_id,rol,inhoud,bronnen,tijd) VALUES (?,?,?,?,?)",
            (chat_id, "user", vraagtekst, "", nu()),
        )
        con.execute(
            "INSERT INTO berichten (chat_id,rol,inhoud,bronnen,tijd) VALUES (?,?,?,?,?)",
            (chat_id, "assistant", antwoord, json.dumps(bronnen), nu()),
        )
    return {"antwoord": antwoord, "bronnen": bronnen, "chat_id": chat_id}


def chats(slug: str) -> list[dict]:
    ws = werkruimte_zoek(slug)
    with db() as con:
        rijen = con.execute(
            "SELECT * FROM chats WHERE workspace_id=? ORDER BY id DESC", (ws["id"],)
        ).fetchall()
    return [dict(r) for r in rijen]


def berichten(chat_id: int) -> list[dict]:
    with db() as con:
        rijen = con.execute(
            "SELECT * FROM berichten WHERE chat_id=? ORDER BY id", (chat_id,)
        ).fetchall()
    uit = []
    for r in rijen:
        d = dict(r)
        d["bronnen"] = json.loads(d["bronnen"]) if d["bronnen"] else []
        uit.append(d)
    return uit


def sleutel_aanmaken(slug: str, label: str = "") -> str:
    ws = werkruimte_zoek(slug)
    sleutel = "wmlm_" + secrets.token_urlsafe(24)
    with _db_lock, db() as con:
        con.execute(
            "INSERT INTO sleutels (sleutel,workspace_id,label,aangemaakt) VALUES (?,?,?,?)",
            (sleutel, ws["id"], label, nu()),
        )
    return sleutel


def sleutel_werkruimte(sleutel: str) -> sqlite3.Row | None:
    with db() as con:
        rij = con.execute(
            "SELECT w.* FROM sleutels s JOIN workspaces w ON w.id=s.workspace_id WHERE s.sleutel=?",
            (sleutel,),
        ).fetchone()
    return rij


# --------------------------------------------------------------------------- #
# Webinterface
# --------------------------------------------------------------------------- #
PAGINA = """<!doctype html>
<html lang="nl"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Werdens Media LLM</title>
<style>
  /* Huisstijl Werdens Monitor: licht, kaarten op wit, blauw als accent. */
  __HUISSTIJL__
  main{display:grid;grid-template-columns:290px 1fr;gap:0;align-items:start;
       min-height:calc(100vh - 69px)}
  aside{border-right:1px solid var(--line);padding:16px;overflow:auto;align-self:stretch}
  section{padding:18px 22px;overflow:auto}
  .pane{background:var(--pane);border:1px solid var(--line);border-radius:var(--r);
        box-shadow:var(--schaduw);padding:14px;margin-bottom:14px}
  label{display:block;font-size:11.5px;color:var(--muted2);margin:10px 0 5px;
        text-transform:uppercase;letter-spacing:.08em;font-weight:700}
  input,textarea,select,button{font:inherit;color:var(--txt);background:var(--pane);
        border:1px solid var(--line2);border-radius:var(--r-klein);padding:9px 11px;width:100%}
  textarea{min-height:110px;resize:vertical}
  button{cursor:pointer;background:var(--merk);border:0;color:#fff;font-weight:700;margin-top:10px;
         border-radius:var(--r-klein);padding:10px 11px}
  button:hover{filter:brightness(1.03)}
  button.ghost{background:var(--pane);border:1px solid var(--line2);color:var(--txt);font-weight:600}
  .ws{padding:9px 11px;border:1px solid var(--line);border-radius:var(--r-klein);margin-bottom:7px;
      cursor:pointer;background:var(--pane);font-size:14px}
  .ws.actief{border-color:var(--accent);background:var(--accent-soft);
             box-shadow:0 0 0 3px rgba(46,163,242,.12)}
  .ws small{display:block;color:var(--muted2);font-size:11px}
  .msg{max-width:760px;margin:0 0 14px;padding:12px 14px;border-radius:var(--r);
       border:1px solid var(--line);white-space:normal;box-shadow:var(--schaduw)}
  .msg.user{background:var(--accent-soft);border-color:#cfe6fa;margin-left:auto}
  .msg.bot{background:var(--pane)}
  .bronnen{margin-top:10px;border-top:1px dashed var(--line);padding-top:8px;font-size:13px;
           color:var(--muted)}
  .bron{background:var(--pane2);border:1px solid var(--line);border-radius:var(--r-klein);
        padding:8px 10px;margin-top:6px}
  .bron b{color:var(--accent-d)}
  .rij{display:flex;gap:10px}.rij>*{flex:1}
  .status{font-size:12px;color:var(--muted2)}
  .ok{color:var(--ok)}.fout{color:var(--red)}
  /* Antwoorden zijn markdown: laat koppen, lijsten, code en vet zien. */
  .msg p{margin:0 0 9px}
  .msg p:last-child{margin-bottom:0}
  .msg ul,.msg ol{margin:6px 0 9px 20px;padding:0}
  .msg li{margin:3px 0}
  .msg pre{background:var(--pane3);border:1px solid var(--line);border-radius:var(--r-klein);
           padding:10px;margin:8px 0;overflow:auto;font-size:13px;line-height:1.45;white-space:pre-wrap}
  .msg pre code{background:none;border:0;padding:0}
  .fragment{font-size:13px;color:var(--muted);white-space:normal;max-height:150px;overflow:hidden}
  .fragment p{margin:0 0 5px}
  .fragment pre{max-height:80px}
</style></head><body>
<header class="kop">
  <div class="merk">
    <span class="logo"><img src="/logo.png" alt="Werdens Media" onerror="this.style.display='none'"></span>
    <div><div class="naam">Werdens Media LLM</div>
      <div class="sub">Werdens Media · werkruimtes</div></div>
  </div>
  <div class="kop-rechts"><span id="engine" class="status"></span></div>
</header>
<main>
  <aside>
    <div class="pane">
      <label>Werkruimtes</label>
      <div id="lijst"></div>
      <label>Nieuwe werkruimte</label>
      <input id="nieuwenaam" placeholder="Bijvoorbeeld: VoorraadThuis">
      <button onclick="maakWerkruimte()">Aanmaken</button>
    </div>
    <div class="pane">
      <label>Document toevoegen</label>
      <input id="doktitel" placeholder="Titel">
      <textarea id="doktekst" placeholder="Plak tekst, markdown of code…"></textarea>
      <input id="dokurl" placeholder="…of een webadres (https://…)">
      <button onclick="voegTekst()">Tekst toevoegen</button>
      <button class="ghost" onclick="voegUrl()">Webadres ophalen</button>
      <input type="file" id="dokbestand" style="margin-top:8px">
      <button class="ghost" onclick="voegBestand()">Bestand toevoegen</button>
      <div id="dokstatus" class="status"></div>
    </div>
    <div class="pane">
      <label>Documenten</label>
      <div id="docs" class="status">kies een werkruimte</div>
    </div>
  </aside>
  <section>
    <div id="gesprek"></div>
    <div class="pane" style="position:sticky;bottom:0">
      <div class="rij">
        <input id="vraag" placeholder="Stel een vraag over je documenten…"
               onkeydown="if(event.key==='Enter')stelVraag()">
        <button style="flex:0 0 130px" onclick="stelVraag()">Vragen</button>
      </div>
      <div class="status">Het antwoord komt uit jouw documenten; de bronnen staan eronder.</div>
    </div>
  </section>
</main>
<script>
let actief = null;
const $ = id => document.getElementById(id);
const api = async (pad, body) => {
  const r = await fetch(pad, body ? {method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(body)} : {});
  const t = await r.text();
  let d; try { d = JSON.parse(t); } catch(e) { d = {fout:t}; }
  if(!r.ok) throw new Error(d.error || d.fout || ('HTTP '+r.status));
  return d;
};
async function laadWerkruimtes(){
  const d = await api('/api/workspaces');
  $('lijst').innerHTML = d.workspaces.map(w =>
    `<div class="ws ${w.slug===actief?'actief':''}" onclick="kies('${w.slug}')">
       ${w.naam}<small>${w.documenten} documenten · ${w.brokken} brokken</small></div>`).join('')
    || '<div class="status">nog geen werkruimtes</div>';
}
async function maakWerkruimte(){
  const naam = $('nieuwenaam').value.trim(); if(!naam) return;
  await api('/api/workspaces', {naam}); $('nieuwenaam').value=''; await laadWerkruimtes();
}
async function kies(slug){
  actief = slug; $('gesprek').innerHTML=''; await laadWerkruimtes(); await laadDocs();
}
async function laadDocs(){
  if(!actief){ $('docs').textContent='kies een werkruimte'; return; }
  const d = await api('/api/workspaces/'+actief+'/documents');
  $('docs').innerHTML = d.documents.map(x =>
    `<div class="bron"><b>${x.titel}</b><br>${x.brokken} brokken · ${x.tekens} tekens</div>`).join('')
    || 'nog geen documenten';
}
async function voegTekst(){
  if(!actief) return meld('Kies eerst een werkruimte.');
  const tekst = $('doktekst').value.trim(); if(!tekst) return;
  $('dokstatus').textContent = 'bezig met inlezen en vectoren maken…';
  try{
    const d = await api(`/api/workspaces/${actief}/documents`,
      {title:$('doktitel').value||'zonder titel', text:tekst});
    $('dokstatus').innerHTML = `<span class="ok">toegevoegd: ${d.brokken} brokken, ${d.vectoren} vectoren</span>`;
    $('doktekst').value=''; $('doktitel').value=''; await laadWerkruimtes(); await laadDocs();
  }catch(e){ meld(e.message); }
}
async function voegUrl(){
  if(!actief) return meld('Kies eerst een werkruimte.');
  const url = $('dokurl').value.trim(); if(!url) return;
  $('dokstatus').textContent = 'webadres ophalen…';
  try{
    const d = await api(`/api/workspaces/${actief}/documents/bestand`, {url});
    $('dokstatus').innerHTML = `<span class="ok">opgehaald: ${d.titel} (${d.brokken} brokken)</span>`;
    $('dokurl').value=''; await laadWerkruimtes(); await laadDocs();
  }catch(e){ meld(e.message); }
}
async function voegBestand(){
  if(!actief) return meld('Kies eerst een werkruimte.');
  const f = $('dokbestand').files[0]; if(!f) return;
  $('dokstatus').textContent = 'bestand lezen…';
  const tekst = await f.text();
  $('dokstatus').textContent = 'bezig met inlezen en vectoren maken…';
  try{
    const d = await api(`/api/workspaces/${actief}/documents`,
      {title:f.name, text:tekst, source:f.name});
    $('dokstatus').innerHTML = `<span class="ok">toegevoegd: ${d.brokken} brokken</span>`;
    $('dokbestand').value=''; await laadWerkruimtes(); await laadDocs();
  }catch(e){ meld(e.message); }
}
function meld(t){ $('dokstatus').innerHTML = `<span class="fout">${t}</span>`; }
function md(s){
  const NL = String.fromCharCode(10);
  const esc = String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  const delen = esc.split('```');
  let uit = '';
  delen.forEach((deel,i) => {
    if(i % 2 === 1){
      uit += '<pre>' + deel.replace(new RegExp('^[a-zA-Z0-9+#-]*' + NL),'').replace(new RegExp(NL + '$'),'') + '</pre>';
      return;
    }
    let t = deel
      .replace(/^#### (.*)$/gm,'<b>$1</b>')
      .replace(/^### (.*)$/gm,'<b>$1</b>')
      .replace(/^## (.*)$/gm,'<b>$1</b>')
      .replace(/^# (.*)$/gm,'<b>$1</b>')
      .replace(new RegExp('[*][*]([^*' + NL + ']+)[*][*]','g'),'<b>$1</b>')
      .replace(new RegExp('(^|[ (])[*]([^*' + NL + ']+)[*]','g'),'$1<i>$2</i>')
      .replace(new RegExp('`([^`' + NL + ']+)`','g'),'<code>$1</code>');
    const lijst = (soort) => (m, blok) => NL + '<' + soort + '>' + blok.trim().split(NL)
      .map(r => '<li>' + r.replace(soort === 'ul' ? /^[-*] / : /^[0-9]+[.] /,'') + '</li>').join('') + '</' + soort + '>';
    t = t.replace(new RegExp('(?:^|' + NL + ')((?:[-*] .*(?:' + NL + '|$))+)','g'), lijst('ul'));
    t = t.replace(new RegExp('(?:^|' + NL + ')((?:[0-9]+[.] .*(?:' + NL + '|$))+)','g'), lijst('ol'));
    t = t.replace(new RegExp(NL + '{2,}','g'),'</p><p>').replace(new RegExp(NL,'g'),'<br>');
    uit += '<p>' + t + '</p>';
  });
  return uit;
}

function toon(rol, tekst, bronnen){
  const div = document.createElement('div');
  div.className = 'msg ' + rol;
  div.innerHTML = md(tekst);
  if(bronnen && bronnen.length){
    const b = document.createElement('div'); b.className='bronnen';
    b.innerHTML = '<b>Bronnen</b>' + bronnen.map((x,i) =>
      `<div class="bron">[${i+1}] <b>${x.titel}</b>${x.bron?' · '+x.bron:''}
       <div class="status">score ${x.score}</div>
       <div class="fragment">${md(x.tekst.slice(0,300))}${x.tekst.length>300?'…':''}</div></div>`).join('');
    div.appendChild(b);
  }
  $('gesprek').appendChild(div);
  div.scrollIntoView({behavior:'smooth', block:'end'});
}
async function stelVraag(){
  if(!actief) return meld('Kies eerst een werkruimte.');
  const q = $('vraag').value.trim(); if(!q) return;
  $('vraag').value=''; toon('user', q);
  const wacht = document.createElement('div'); wacht.className='msg bot';
  wacht.textContent='aan het zoeken in je documenten…'; $('gesprek').appendChild(wacht);
  try{
    const d = await api(`/api/workspaces/${actief}/query`, {question:q});
    wacht.remove(); toon('bot', d.antwoord, d.bronnen);
  }catch(e){ wacht.textContent = 'Fout: ' + e.message; }
}
(async () => {
  try{
    const h = await api('/health');
    $('engine').innerHTML = h.engine.online
      ? ` · <span class="ok">engine online (${h.engine.versie})</span>`
      : ` · <span class="fout">engine offline</span>`;
  }catch(e){}
  await laadWerkruimtes();
  const d = await api('/api/workspaces');
  if(d.workspaces.length) kies(d.workspaces[0].slug);
})();
</script></body></html>
"""

WIDGET = """/* Werdens Media LLM — embed-widget
   Zet dit op je site:
   <script src="http://<server>:8090/widget.js"
           data-werkruimte="mijn-project" data-kleur="#2ea3f2"></script> */
(function () {
  var s = document.currentScript;
  var ws = (s && s.dataset.werkruimte) || 'standaard';
  var kleur = (s && s.dataset.kleur) || '#2ea3f2';
  var basis = new URL(s.src).origin;
  var knop = document.createElement('button');
  knop.textContent = 'Vraag het de assistent';
  knop.style.cssText = 'position:fixed;right:20px;bottom:20px;z-index:99999;padding:12px 18px;'
    + 'border:0;border-radius:999px;cursor:pointer;font:600 14px system-ui;color:#04121a;'
    + 'background:' + kleur;
  var kader = document.createElement('iframe');
  kader.style.cssText = 'position:fixed;right:20px;bottom:80px;width:380px;height:560px;'
    + 'max-width:92vw;max-height:80vh;border:1px solid #e3eaf4;border-radius:16px;'
    + 'z-index:99999;display:none;box-shadow:0 18px 40px -24px rgba(15,23,42,.5);background:#f4f7fb';
  kader.src = basis + '/embed?werkruimte=' + encodeURIComponent(ws);
  knop.onclick = function () {
    kader.style.display = kader.style.display === 'none' ? 'block' : 'none';
  };
  document.body.appendChild(knop);
  document.body.appendChild(kader);
})();
"""

EMBED_PAGINA = """<!doctype html><html lang="nl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Werdens Media LLM</title><style>
 __HUISSTIJL__
 body{font-size:14px}
 header.kop{padding:10px 12px;gap:9px}
 .logo{width:28px;height:28px;flex:none}
 .logo img{width:100%;height:100%;object-fit:contain;display:block}
 .naam{font-weight:700;font-size:14px;letter-spacing:-.02em}
 #gesprek{padding:12px;height:calc(100% - 118px);overflow:auto}
 .msg{padding:9px 11px;border-radius:var(--r-klein);margin-bottom:9px;white-space:normal;
      background:var(--pane);border:1px solid var(--line);box-shadow:none}
 .msg.user{background:var(--accent-soft);border-color:#cfe6fa}
 .bron{font-size:12px;color:var(--muted);margin-top:6px;border-top:1px dashed var(--line);padding-top:5px}
 .balk{padding:10px 12px;border-top:1px solid var(--line);display:flex;gap:8px;background:var(--pane)}
 .balk input{flex:1;height:36px}
 .balk button{background:var(--merk);border:0;color:#fff;border-radius:var(--r-klein);
   padding:0 14px;height:36px;font-weight:700}
</style></head><body>
<header class="kop"><span class="logo"><img src="/logo.png" alt="Werdens Media" onerror="this.style.display='none'"></span><span class="naam">Werdens Media LLM</span></header>
<div id="gesprek"></div>
<div class="balk"><input id="v" placeholder="Stel je vraag…" onkeydown="if(event.key==='Enter')vraag()">
<button onclick="vraag()">Vraag</button></div>
<script>
var ws = new URLSearchParams(location.search).get('werkruimte') || 'standaard';
var g = document.getElementById('gesprek');
function toon(rol, t, b){
  var d = document.createElement('div'); d.className='msg '+rol; d.textContent=t;
  if(b && b.length){ var s=document.createElement('div'); s.className='bron';
    s.textContent='Bronnen: ' + b.map(function(x,i){return '['+(i+1)+'] '+x.titel;}).join(', ');
    d.appendChild(s); }
  g.appendChild(d); d.scrollIntoView({block:'end'});
}
async function vraag(){
  var q = document.getElementById('v').value.trim(); if(!q) return;
  document.getElementById('v').value=''; toon('user', q);
  var w = document.createElement('div'); w.className='msg'; w.textContent='…'; g.appendChild(w);
  try{
    var r = await fetch('/api/workspaces/'+encodeURIComponent(ws)+'/query',
      {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({question:q})});
    var d = await r.json(); w.remove();
    if(!r.ok) return toon('', 'Fout: ' + (d.error||r.status));
    toon('', d.antwoord, d.bronnen);
  }catch(e){ w.textContent = 'Fout: ' + e.message; }
}
</script></body></html>
"""


# --------------------------------------------------------------------------- #
# HTTP-API
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = f"wmlm-werkruimte/{VERSIE}"

    def log_message(self, format, *args):  # noqa: A002
        sys.stderr.write("[wmlm-werkruimte] %s\n" % (format % args))

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

    def _html(self, tekst: str):
        self._stuur(tekst.encode(), 200, "text/html; charset=utf-8")

    def _fout(self, msg: str, code: int = 400):
        self._json({"error": msg}, code)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return {}

    def _delen(self) -> list[str]:
        return [d for d in self.path.split("?")[0].split("/") if d]

    def _query(self) -> dict:
        if "?" not in self.path:
            return {}
        from urllib.parse import parse_qs

        return {k: v[0] for k, v in parse_qs(self.path.split("?", 1)[1]).items()}

    def do_OPTIONS(self):
        self._stuur(b"", 204)

    def do_GET(self):
        pad = self.path.split("?")[0]
        try:
            if pad == "/logo.png":
                return self._stuur(logo_bytes(), 200, "image/png")
            if pad in ("/", "/index.html"):
                # PAGINA is geen f-string (de JS zit vol accolades), dus de huisstijl
                # komt er via een plaatshouder in.
                return self._html(PAGINA.replace("__HUISSTIJL__", HUISSTIJL))
            if pad == "/health":
                return self._json({"status": "ok", "versie": VERSIE, "engine": engine_online()})
            if pad == "/widget.js":
                return self._stuur(WIDGET.encode(), 200, "application/javascript; charset=utf-8")
            if pad == "/embed":
                return self._html(EMBED_PAGINA.replace("__HUISSTIJL__", HUISSTIJL))
            if pad == "/api/workspaces":
                return self._json({"workspaces": werkruimtes()})
            delen = self._delen()
            if len(delen) == 4 and delen[:2] == ["api", "workspaces"] and delen[3] == "documents":
                return self._json({"documents": documenten(delen[2])})
            if len(delen) == 4 and delen[:2] == ["api", "workspaces"] and delen[3] == "chats":
                return self._json({"chats": chats(delen[2])})
            if len(delen) == 3 and delen[:2] == ["api", "chats"]:
                return self._json({"berichten": berichten(int(delen[2]))})
            if pad == "/v1/models":
                return self._json(
                    {
                        "object": "list",
                        "data": [
                            {
                                "id": f"werkruimte:{w['slug']}",
                                "object": "model",
                                "created": int(time.time()),
                                "owned_by": "wmlm",
                            }
                            for w in werkruimtes()
                        ],
                    }
                )
            return self._fout("niet gevonden", 404)
        except KeyError as e:
            return self._fout(str(e), 404)
        except EngineFout as e:
            return self._fout(str(e), 502)
        except Exception as e:  # noqa: BLE001
            return self._fout(f"{type(e).__name__}: {e}", 500)

    def do_POST(self):
        delen = self._delen()
        try:
            b = self._body()
            if delen == ["api", "workspaces"]:
                if not b.get("naam"):
                    return self._fout("veld 'naam' ontbreekt")
                return self._json(werkruimte_aanmaken(b["naam"], b.get("slug"),
                                                      b.get("systeem_prompt", ""),
                                                      b.get("chat_model", ""),
                                                      b.get("embed_model", "")), 201)
            if len(delen) == 4 and delen[:2] == ["api", "workspaces"] and delen[3] == "documents":
                titel = b.get("title") or b.get("titel") or "zonder titel"
                return self._json(
                    document_toevoegen(delen[2], titel, b.get("text") or b.get("tekst") or "",
                                       b.get("source") or b.get("bron") or ""), 201
                )
            if len(delen) == 5 and delen[3] == "documents" and delen[4] == "bestand":
                url = b.get("url") or ""
                if not url.startswith(("http://", "https://")):
                    return self._fout("geef een volledig webadres (https://…)")
                titel, tekst = haal_url(url)
                return self._json(
                    document_toevoegen(delen[2], b.get("title") or titel or url, tekst, url), 201
                )
            if len(delen) == 4 and delen[:2] == ["api", "workspaces"] and delen[3] == "query":
                q = b.get("question") or b.get("vraag") or ""
                if not q.strip():
                    return self._fout("veld 'question' ontbreekt")
                return self._json(vraag(delen[2], q, int(b.get("top_k") or TOP_K),
                                        b.get("chat_id")))
            if len(delen) == 4 and delen[:2] == ["api", "workspaces"] and delen[3] == "sleutels":
                return self._json({"sleutel": sleutel_aanmaken(delen[2], b.get("label", ""))}, 201)
            if delen == ["v1", "chat", "completions"]:
                return self._openai_chat(b)
            return self._fout("niet gevonden", 404)
        except KeyError as e:
            return self._fout(str(e), 404)
        except ValueError as e:
            return self._fout(str(e), 400)
        except EngineFout as e:
            return self._fout(str(e), 502)
        except Exception as e:  # noqa: BLE001
            return self._fout(f"{type(e).__name__}: {e}", 500)

    def do_DELETE(self):
        delen = self._delen()
        try:
            if len(delen) == 5 and delen[3] == "documents":
                document_verwijderen(delen[2], int(delen[4]))
                return self._json({"status": "verwijderd"})
            return self._fout("niet gevonden", 404)
        except Exception as e:  # noqa: BLE001
            return self._fout(f"{type(e).__name__}: {e}", 500)

    # OpenAI-compatible: model="werkruimte:<slug>" doet RAG met bronvermelding
    def _openai_chat(self, b: dict):
        model = str(b.get("model") or "")
        if not model.startswith("werkruimte:"):
            return self._fout(
                "gebruik model 'werkruimte:<slug>' — bijvoorbeeld werkruimte:mijn-project", 400
            )
        slug = model.split(":", 1)[1]
        berichten_in = b.get("messages") or []
        laatste = next(
            (m.get("content") or "" for m in reversed(berichten_in) if m.get("role") == "user"), ""
        )
        if not laatste.strip():
            return self._fout("geen gebruikersvraag gevonden", 400)
        uit = vraag(slug, laatste, int(b.get("top_k") or TOP_K))
        inhoud = uit["antwoord"]
        if uit["bronnen"]:
            inhoud += "\n\nBronnen: " + "; ".join(
                f"[{i + 1}] {x['titel']}" + (f" ({x['bron']})" if x["bron"] else "")
                for i, x in enumerate(uit["bronnen"])
            )
        return self._json(
            {
                "id": "chatcmpl-" + secrets.token_hex(12),
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": inhoud},
                     "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            }
        )


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def cmd_serve(_a):
    init_db()
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Werdens Media LLM — werkruimtelaag {VERSIE}")
    print(f"  web     : http://{HOST}:{PORT}/")
    print(f"  engine  : {ENGINE}  ({'online' if engine_online()['online'] else 'OFFLINE'})")
    print(f"  chat    : {CHAT_MODEL}")
    print(f"  embed   : {EMBED_MODEL}")
    print(f"  database: {DB}")
    srv.serve_forever()


def cmd_ws(a):
    init_db()
    print(json.dumps(werkruimte_aanmaken(a.naam, a.slug), ensure_ascii=False, indent=2))


def cmd_lijst(_a):
    init_db()
    for w in werkruimtes():
        print(f"{w['slug']:<28} {w['naam']:<28} {w['documenten']:>3} documenten, {w['brokken']:>5} brokken")


def cmd_doc(a):
    init_db()
    if a.bestand:
        pad = Path(a.bestand)
        tekst = bestand_naar_tekst(pad)
        titel = a.titel or pad.name
        bron = str(pad)
    elif a.url:
        titel, tekst = haal_url(a.url)
        titel = a.titel or titel or a.url
        bron = a.url
    else:
        raise SystemExit("geef --bestand of --url mee")
    print(json.dumps(document_toevoegen(a.werkruimte, titel, tekst, bron),
                     ensure_ascii=False, indent=2))


def cmd_vraag(a):
    init_db()
    uit = vraag(a.werkruimte, " ".join(a.vraag), a.top_k)
    print(uit["antwoord"])
    if uit["bronnen"]:
        print("\nBronnen:")
        for i, b in enumerate(uit["bronnen"], 1):
            print(f"  [{i}] {b['titel']} (score {b['score']})")


def main():
    p = argparse.ArgumentParser(prog="wmlm_workspace", description="Werkruimtelaag van de Werdens Media LLM")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("serve", help="server starten").set_defaults(fn=cmd_serve)
    sub.add_parser("lijst", help="werkruimtes tonen").set_defaults(fn=cmd_lijst)
    w = sub.add_parser("workspace-aanmaken", help="nieuwe werkruimte")
    w.add_argument("naam")
    w.add_argument("--slug")
    w.set_defaults(fn=cmd_ws)
    d = sub.add_parser("document-toevoegen", help="document toevoegen")
    d.add_argument("werkruimte")
    d.add_argument("--titel")
    d.add_argument("--bestand")
    d.add_argument("--url")
    d.set_defaults(fn=cmd_doc)
    v = sub.add_parser("vraag", help="vraag stellen aan een werkruimte")
    v.add_argument("werkruimte")
    v.add_argument("vraag", nargs="+")
    v.add_argument("--top-k", type=int, default=TOP_K)
    v.set_defaults(fn=cmd_vraag)
    a = p.parse_args()
    if not getattr(a, "fn", None):
        p.print_help()
        return
    a.fn(a)


if __name__ == "__main__":
    main()
