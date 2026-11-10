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
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# --------------------------------------------------------------------------- #
# Configuratie (env-vars met dezelfde geest als OLLAMA_*)
# --------------------------------------------------------------------------- #
HOME = Path(os.environ.get("WLLM_HOME", Path(__file__).resolve().parent))
MODELS_DIR = Path(os.environ.get("WLLM_MODELS", HOME / "models"))
BLOBS_DIR = Path(os.environ.get("WLLM_BLOBS", HOME / "blobs"))
MANIFESTS_DIR = Path(os.environ.get("WLLM_MANIFESTS", HOME / "manifests"))
ENGINE = Path(
    os.environ.get(
        "WLLM_ENGINE",
        HOME / "engine" / "llama-b12262" / "llama-server",
    )
)
HOST = os.environ.get("WLLM_HOST", "0.0.0.0")
PORT = int(os.environ.get("WLLM_PORT", "11434"))
THREADS = int(os.environ.get("WLLM_THREADS", str(os.cpu_count() or 4)))
# Hermes Agent weigert modellen onder de 64.000 tokens context, dus de default
# moet daar op of boven zitten. 64k f16 KV-cache is ~2,15 GB; met q8_0 is dat
# ~1,07 GB en dat past naast Hermes op 8 GB.
CTX = int(os.environ.get("WLLM_CTX", "65536"))
KV_TYPE = os.environ.get("WLLM_KV_TYPE", "q8_0")
KEEP_ALIVE = int(os.environ.get("WLLM_KEEP_ALIVE", "300"))  # seconden
ENGINE_PORT_BASE = int(os.environ.get("WLLM_ENGINE_PORT", "18500"))
VERSION = "0.1.0"

for d in (MODELS_DIR, BLOBS_DIR, MANIFESTS_DIR):
    d.mkdir(parents=True, exist_ok=True)


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


def engine_stream(eng: Engine, payload: dict):
    """Genereer SSE-chunks van llama-server als dicts."""
    eng.busy += 1
    try:
        req = urllib.request.Request(
            eng.base + "/v1/chat/completions",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=3600) as resp:
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
        eng.busy -= 1
        eng.last_used = time.time()


def engine_once(eng: Engine, payload: dict) -> dict:
    p = dict(payload)
    p["stream"] = False
    eng.busy += 1
    try:
        req = urllib.request.Request(
            eng.base + "/v1/chat/completions",
            data=json.dumps(p).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=3600) as resp:
            return json.load(resp)
    finally:
        eng.busy -= 1
        eng.last_used = time.time()


# --------------------------------------------------------------------------- #
# HTTP-API
# --------------------------------------------------------------------------- #
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

    # -- routing -----------------------------------------------------------
    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/api/version", "/version"):
            return self._json({"version": VERSION})
        if path == "/api/tags":
            return self._json({"models": STORE.list_models()})
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
        if path in ("/", "/health"):
            return self._json({"status": "ok", "version": VERSION, "engine": str(ENGINE)})
        return self._err("not found", 404)

    def do_POST(self):
        path = self.path.split("?")[0]
        try:
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
        payload = {
            "model": eng.model,
            "messages": messages,
            "stream": bool(b.get("stream", True)),
            "max_tokens": b.get("max_tokens") or params.pop("max_tokens", 512),
        }
        payload.update({k: v for k, v in params.items() if not k.startswith("_")})
        return eng, messages, payload

    def _chat(self):
        b = self._body()
        eng, _msgs, payload = self._prepare(b)
        stream = payload["stream"]
        t0 = time.time()
        if not stream:
            r = engine_once(eng, payload)
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
        for chunk in engine_stream(eng, payload):
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
        eng, _msgs, payload = self._prepare(b)
        stream = payload["stream"]
        t0 = time.time()
        if not stream:
            r = engine_once(eng, payload)
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
        for chunk in engine_stream(eng, payload):
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
        payload = dict(b)
        payload["model"] = eng.model
        if payload.get("stream"):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "close")
            self.end_headers()
            for chunk in engine_stream(eng, payload):
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
        r = engine_once(eng, payload)
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
        payload = {
            "model": eng.model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "max_tokens": b.get("max_tokens") or 512,
            "temperature": b.get("temperature", 0.8),
        }
        r = engine_once(eng, payload)
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


def cmd_serve(_a):
    srv = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"wllm {VERSION} — engine: {ENGINE}")
    print(f"  Ollama-API : http://{HOST}:{PORT}/api/...")
    print(f"  OpenAI-API : http://{HOST}:{PORT}/v1/...")
    print(f"  modellen   : {MODELS_DIR}")
    print(f"  threads={THREADS} ctx={CTX} keep_alive={KEEP_ALIVE}s")

    def bye(*_s):
        ENGINES.shutdown()
        srv.shutdown()
        sys.exit(0)

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
    a = p.parse_args()
    if not getattr(a, "fn", None):
        p.print_help()
        return
    a.fn(a)


if __name__ == "__main__":
    main()
