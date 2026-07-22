"""Render specow slajdow do PNG: Jinja2 -> HTML -> headless Chromium (Playwright).

Assety (fonty, flagi) sa wstrzykiwane jako data URI — HTML jest samowystarczalny,
bez zaleznosci od file:// i sieci w czasie renderu.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

RENDER_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = RENDER_DIR / "templates"
ASSETS_DIR = RENDER_DIR / "assets"
FONTS_DIR = ASSETS_DIR / "fonts"
FLAGS_DIR = ASSETS_DIR / "flags"

VIEWPORT = {"width": 1080, "height": 1350}

# rodzina, waga, plik (subset latin-ext+latin sciagamy skryptem scripts/fetch_render_assets.py)
_FONT_FACES = [
    ("Archivo", 800, "archivo-800"),
    ("Inter", 400, "inter-400"),
    ("Inter", 600, "inter-600"),
]


def _data_uri(path: Path, mime: str) -> str:
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def build_fonts_css(fonts_dir: Path = FONTS_DIR) -> str:
    """@font-face z data URI dla kazdego dostepnego pliku fontu.

    Brak plikow nie jest bledem — szablony maja fallback na fonty systemowe.
    """
    rules: list[str] = []
    for family, weight, stem in _FONT_FACES:
        for suffix in ("latin-ext", "latin"):
            path = fonts_dir / f"{stem}-{suffix}.woff2"
            if not path.exists():
                continue
            rules.append(
                "@font-face { font-family: '%s'; font-weight: %d; font-style: normal; "
                "src: url('%s') format('woff2'); }"
                % (family, weight, _data_uri(path, "font/woff2"))
            )
    return "\n".join(rules)


def _flag_filter_factory(flags_dir: Path):
    cache: dict[str, str] = {}

    def flag(iso2: str) -> str:
        iso2 = (iso2 or "").strip().lower()
        if not iso2:
            return ""
        if iso2 not in cache:
            path = flags_dir / f"{iso2}.png"
            cache[iso2] = _data_uri(path, "image/png") if path.exists() else ""
        return cache[iso2]

    return flag


def build_environment(
    templates_dir: Path = TEMPLATES_DIR, flags_dir: Path = FLAGS_DIR
) -> Environment:
    env = Environment(
        loader=FileSystemLoader(str(templates_dir)),
        autoescape=select_autoescape(["html"]),
    )
    env.filters["flag"] = _flag_filter_factory(flags_dir)
    return env


def render_html(spec: dict[str, Any], env: Environment, fonts_css: str) -> str:
    template = env.get_template(f"{spec['role']}.html")
    return template.render(spec=spec, fonts_css=fonts_css)


def render_slides(
    specs: list[dict[str, Any]],
    out_dir: Path,
    scale: int = 2,
    timeout_ms: int = 15000,
) -> list[Path]:
    """Renderuje wszystkie specy do out_dir/slide_NN.png. Zwraca sciezki."""
    from playwright.sync_api import sync_playwright  # import lokalny: testy specow nie potrzebuja playwrighta

    env = build_environment()
    fonts_css = build_fonts_css()
    out_dir.mkdir(parents=True, exist_ok=True)

    paths: list[Path] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport=VIEWPORT, device_scale_factor=scale)
        try:
            for spec in specs:
                html = render_html(spec, env, fonts_css)
                page.set_content(html, wait_until="load")
                page.wait_for_selector('html[data-ready="1"]', timeout=timeout_ms)
                path = out_dir / f"slide_{int(spec['slide_number']):02d}.png"
                page.screenshot(path=str(path))
                paths.append(path)
        finally:
            browser.close()
    return paths
