#!/usr/bin/env python3
"""Zet de rekenkracht van Werdens Media LLM klaar op Windows.

Haalt de Windows-versie van llama.cpp op en zet die in engine/, en haalt de
modelbestanden op naar models/. Daarna kan start-werdens-media-llm.ps1 de drie
lagen starten.

Er is niets te installeren: alleen de standaardbibliotheek van Python 3 wordt
gebruikt. Al aanwezige bestanden worden overgeslagen, dus opnieuw draaien mag
altijd en gaat snel.

Voorbeelden:
    python setup.py                     # cpu-versie + beide modellen
    python setup.py --variant vulkan    # voor een videokaart die Vulkan praat
    python setup.py --variant cuda12    # voor een NVIDIA-kaart
    python setup.py --zonder-modellen   # alleen llama.cpp ophalen
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

RELEASES = "https://api.github.com/repos/ggml-org/llama.cpp/releases"

# Naam van het pakket per soort rekenkracht.
VARIANTEN = {
    "cpu": "win-cpu-x64",
    "vulkan": "win-vulkan-x64",
    "cuda12": "win-cuda-12.4-x64",
    "cuda13": "win-cuda-13.5-x64",
}

# De twee modellen die de lagen nodig hebben: een taalmodel om mee te praten en
# een embedding-model voor de werkruimte (documenten doorzoeken).
MODELLEN = [
    (
        "Llama-3.2-3B-Instruct-Q4_K_M.gguf",
        "https://huggingface.co/bartowski/Llama-3.2-3B-Instruct-GGUF/resolve/main/"
        "Llama-3.2-3B-Instruct-Q4_K_M.gguf",
        "taalmodel",
    ),
    (
        "nomic-embed-text-v1.5.Q8_0.gguf",
        "https://huggingface.co/nomic-ai/nomic-embed-text-v1.5-GGUF/resolve/main/"
        "nomic-embed-text-v1.5.Q8_0.gguf",
        "embedding-model (werkruimte)",
    ),
]


def meld(tekst: str = "") -> None:
    print(tekst, flush=True)


# Op Windows valt de uitvoer terug op de ANSI-codepagina zodra hij naar een
# bestand gaat; zo blijven de tekens leesbaar.
for _stroom in (sys.stdout, sys.stderr):
    _zet = getattr(_stroom, "reconfigure", None)
    if _zet is None:
        continue
    try:
        _zet(encoding="utf-8", errors="replace")
    except (ValueError, OSError):
        pass


def kop(tekst: str) -> None:
    meld()
    meld(f"== {tekst} ==")


def _open(url: str, timeout: int = 60):
    verzoek = urllib.request.Request(url, headers={"User-Agent": "werdens-media-llm-setup"})
    return urllib.request.urlopen(verzoek, timeout=timeout)


def haal_json(url: str) -> Any:
    with _open(url) as antwoord:
        return json.loads(antwoord.read().decode("utf-8", "replace"))


def zoek_pakket(variant: str, tag: str | None) -> tuple[str, str]:
    """Zoek het nieuwste llama.cpp-pakket voor deze variant.

    De nieuwste 'release' van llama.cpp is niet altijd de goede: er staan ook
    lege tussenversies tussen. Daarom lopen we de lijst langs en pakken we de
    eerste die echt een Windows-pakket bij zich heeft.
    """
    gezocht = f"-bin-{VARIANTEN[variant]}.zip"
    if tag:
        gegevens = haal_json(f"{RELEASES}/tags/{tag}")
        for pakket in gegevens.get("assets", []):
            if pakket["name"].endswith(gezocht):
                return tag, pakket["browser_download_url"]
        raise SystemExit(f"  ! release {tag} heeft geen pakket {gezocht}")

    releases = haal_json(f"{RELEASES}?per_page=15")
    if not isinstance(releases, list):
        raise SystemExit("  ! kon de releaselijst van llama.cpp niet lezen")
    for release in releases:
        for pakket in release.get("assets", []):
            if pakket["name"].endswith(gezocht):
                return release["tag_name"], pakket["browser_download_url"]
    raise SystemExit(f"  ! geen Windows-pakket {gezocht} gevonden in de laatste 15 releases")


def haal_bestand(url: str, doel: Path) -> None:
    """Download naar doel, met tussenstand. Half werk komt in .deel te staan,
    zodat een afgebroken download nooit als 'klaar' doorgaat."""
    deel = doel.with_suffix(doel.suffix + ".deel")
    doel.parent.mkdir(parents=True, exist_ok=True)
    begin = time.time()
    with _open(url) as bron, open(deel, "wb") as uit:
        lengte = int(bron.headers.get("Content-Length") or 0)
        gedaan = 0
        vorige_melding = 0.0
        while True:
            blok = bron.read(1024 * 512)
            if not blok:
                break
            uit.write(blok)
            gedaan += len(blok)
            nu = time.time()
            if nu - vorige_melding > 1.0:
                vorige_melding = nu
                mb = gedaan / 1e6
                if lengte:
                    print(
                        f"\r    {mb:7.1f} / {lengte / 1e6:.1f} MB "
                        f"({gedaan * 100 // lengte:3d}%)",
                        end="",
                        flush=True,
                    )
                else:
                    print(f"\r    {mb:7.1f} MB", end="", flush=True)
    if lengte and deel.stat().st_size != lengte:
        deel.unlink(missing_ok=True)
        raise SystemExit(f"  ! {doel.name} is onvolledig opgehaald; probeer het opnieuw")
    deel.replace(doel)
    seconden = max(time.time() - begin, 0.1)
    meld(f"\r    klaar: {doel.name} ({doel.stat().st_size / 1e6:.1f} MB in {seconden:.0f} s)")


def pak_uit(zip_pad: Path, doel: Path) -> None:
    doel.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_pad) as pakket:
        pakket.extractall(doel)


def main() -> int:
    ontleder = argparse.ArgumentParser(
        description="Zet llama.cpp en de modellen van Werdens Media LLM klaar (Windows)."
    )
    ontleder.add_argument(
        "--variant",
        choices=sorted(VARIANTEN),
        default="cpu",
        help="soort rekenkracht: cpu (standaard), vulkan, cuda12 of cuda13",
    )
    ontleder.add_argument("--home", default=None, help="map van de installatie (standaard: deze map)")
    ontleder.add_argument("--tag", default=None, help="llama.cpp-versie vastzetten, bijvoorbeeld b12318")
    ontleder.add_argument("--zonder-modellen", action="store_true", help="sla de modelbestanden over")
    ontleder.add_argument("--opnieuw", action="store_true", help="llama.cpp opnieuw ophalen ook als het er al staat")
    keuze = ontleder.parse_args()

    hier = Path(__file__).resolve().parent
    home = Path(keuze.home).resolve() if keuze.home else hier.parent.parent
    engine_map = home / "engine"
    model_map = home / "models"

    meld(f"Werdens Media LLM — klaarzetten op {os.name}")
    meld(f"  installatiemap : {home}")
    if os.name != "nt":
        meld("  let op         : dit is geen Windows; de Windows-pakketten worden toch opgehaald")

    # ---- llama.cpp ---------------------------------------------------------
    kop("llama.cpp (de rekenkracht)")
    bestaand = sorted(engine_map.glob("llama-*bin-win-*/llama-server.exe"))
    if bestaand and not keuze.opnieuw:
        meld(f"  al aanwezig: {bestaand[-1].relative_to(home)}")
    else:
        tag, url = zoek_pakket(keuze.variant, keuze.tag)
        doel_map = engine_map / f"llama-{tag}-bin-{VARIANTEN[keuze.variant]}"
        meld(f"  versie {tag} ({keuze.variant}) ophalen…")
        tijdelijk = engine_map / "llama.cpp.zip"
        haal_bestand(url, tijdelijk)
        meld(f"  uitpakken naar {doel_map.relative_to(home)}…")
        pak_uit(tijdelijk, doel_map)
        tijdelijk.unlink(missing_ok=True)
        bestaand = sorted(engine_map.glob("llama-*bin-win-*/llama-server.exe"))

    if not bestaand:
        meld("  ! llama-server.exe niet gevonden na uitpakken")
        return 1
    meld(f"  klaar: {bestaand[-1].relative_to(home)}")

    # ---- modellen ----------------------------------------------------------
    if keuze.zonder_modellen:
        kop("modellen")
        meld("  overgeslagen (--zonder-modellen)")
    else:
        kop("modellen")
        for naam, url, soort in MODELLEN:
            doel = model_map / naam
            if doel.exists() and doel.stat().st_size > 1024:
                meld(f"  al aanwezig: {naam} ({doel.stat().st_size / 1e6:.0f} MB)")
                continue
            meld(f"  {soort}: {naam} ophalen…")
            haal_bestand(url, doel)

    kop("klaar")
    meld("  Starten:  start-werdens-media-llm.bat  (of start-werdens-media-llm.ps1)")
    meld("  Chatten:  http://127.0.0.1:11434/chat")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        meld("\n  afgebroken")
        sys.exit(130)
    except urllib.error.URLError as fout:
        meld(f"\n  ! geen verbinding: {fout}")
        sys.exit(1)
