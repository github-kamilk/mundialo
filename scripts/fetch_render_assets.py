"""Jednorazowe pobranie assetow warstwy graficznej (potem render dziala offline).

- Fonty: Archivo 800 + Inter 400/600 (subsety latin + latin-ext, polskie znaki)
  z Google Fonts -> app/render/assets/fonts/*.woff2
- Flagi: PNG w160 z flagcdn.com dla kazdego iso2 z data/sources/country_media.json
  -> app/render/assets/flags/{iso2}.png

Uzycie: python scripts/fetch_render_assets.py
"""

from __future__ import annotations

import json
import re
import sys
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
ASSETS = REPO_ROOT / "app" / "render" / "assets"
FONTS_DIR = ASSETS / "fonts"
FLAGS_DIR = ASSETS / "flags"
REGISTRY = REPO_ROOT / "data" / "sources" / "country_media.json"

# UA przegladarki wspierajacej woff2 — bez tego Google Fonts zwraca ttf
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)

FONT_CSS_URL = (
    "https://fonts.googleapis.com/css2"
    "?family=Archivo:wght@800&family=Inter:wght@400;600&display=swap"
)

STEMS = {("Archivo", "800"): "archivo-800", ("Inter", "400"): "inter-400", ("Inter", "600"): "inter-600"}
SUBSETS = {"latin", "latin-ext"}


def http_get(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def fetch_fonts() -> int:
    FONTS_DIR.mkdir(parents=True, exist_ok=True)
    css = http_get(FONT_CSS_URL).decode("utf-8")
    # bloki: /* latin-ext */ @font-face { font-family: 'Inter'; ... font-weight: 400; src: url(...woff2) ... }
    pattern = re.compile(
        r"/\*\s*([a-z-]+)\s*\*/\s*@font-face\s*\{[^}]*?"
        r"font-family:\s*'([^']+)'[^}]*?"
        r"font-weight:\s*(\d+)[^}]*?"
        r"src:\s*url\((https://[^)]+\.woff2)\)",
        re.S,
    )
    saved = 0
    for subset, family, weight, url in pattern.findall(css):
        stem = STEMS.get((family, weight))
        if not stem or subset not in SUBSETS:
            continue
        target = FONTS_DIR / f"{stem}-{subset}.woff2"
        target.write_bytes(http_get(url))
        print(f"font: {target.name}")
        saved += 1
    return saved


def fetch_flags() -> int:
    FLAGS_DIR.mkdir(parents=True, exist_ok=True)
    registry = json.loads(REGISTRY.read_text(encoding="utf-8"))
    iso2_codes = sorted(
        {
            (entry.get("iso2") or "").strip().lower()
            for entry in registry.get("countries", [])
            if entry.get("iso2")
        }
    )
    saved = 0
    for iso2 in iso2_codes:
        target = FLAGS_DIR / f"{iso2}.png"
        if target.exists():
            continue
        try:
            target.write_bytes(http_get(f"https://flagcdn.com/w160/{iso2}.png"))
            saved += 1
        except OSError as error:
            print(f"flaga {iso2}: BLAD ({error})", file=sys.stderr)
    print(f"flagi: {saved} nowych, {len(iso2_codes)} w rejestrze")
    return saved


def main() -> int:
    fonts = fetch_fonts()
    fetch_flags()
    if fonts < len(STEMS):
        print("UWAGA: nie wszystkie fonty pobrane — render uzyje fallbacku systemowego.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
