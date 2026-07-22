"""Jednorazowe assety brandowe na premiere profilu @mundaily_.

Generuje do katalogu brand/:
- intro_slide_01.png, intro_slide_02.png (1080x1350) - post powitalny,
- avatar.png (1080x1080) - awatar profilu (IG przytnie do kola),
- intro_caption.txt - opis posta powitalnego,
- bio.txt - biogram profilu.

Uzywa tych samych fontow i palety co warstwa render (spojnosc identyfikacji).
Uruchomienie: python scripts/brand_assets.py
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.agents.media_reaction import BASE_HASHTAGS  # noqa: E402
from app.render.renderer import build_fonts_css  # noqa: E402

OUT_DIR = Path(__file__).resolve().parents[1] / "brand"

# paleta domyslna warstwy render (base.html)
_CSS_VARS = """
  --ink: #f2f5fb;
  --muted: #8e99b3;
  --line: rgba(255, 255, 255, 0.08);
  --gold: #e9c46a;
  --teal: #5dd6c0;
  --glow-a: #1c2b4d;
  --glow-b: #15243f;
  --bg-top: #0d1426;
  --bg-bottom: #0a0e1b;
  --card: rgba(255, 255, 255, 0.035);
"""


def _page(fonts_css: str, width: int, height: int, style: str, content: str) -> str:
    return f"""<!doctype html>
<html lang="pl"><head><meta charset="utf-8"><style>
{fonts_css}
:root {{ {_CSS_VARS} }}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
html, body {{ width: {width}px; height: {height}px; overflow: hidden; }}
body {{
  font-family: "Inter", "Segoe UI", system-ui, sans-serif;
  color: var(--ink);
  background:
    radial-gradient(1400px 900px at 85% -15%, var(--glow-a) 0%, transparent 62%),
    radial-gradient(1000px 700px at -10% 110%, var(--glow-b) 0%, transparent 55%),
    linear-gradient(165deg, var(--bg-top) 0%, var(--bg-bottom) 100%);
  position: relative;
}}
.pitch-circle {{
  position: absolute; border: 2px solid rgba(255,255,255,0.05); border-radius: 50%;
  left: 50%; transform: translateX(-50%); pointer-events: none;
}}
.display {{ font-family: "Archivo", "Inter", sans-serif; font-weight: 800; letter-spacing: -0.01em; }}
.eyebrow {{
  font-size: 26px; font-weight: 600; letter-spacing: 0.32em;
  text-transform: uppercase; color: var(--gold);
}}
.footer {{
  display: flex; justify-content: space-between; align-items: center;
  padding-top: 40px; border-top: 1px solid var(--line);
  font-size: 27px; color: var(--muted);
}}
.footer .brand {{ font-weight: 600; color: var(--ink); letter-spacing: 0.04em; }}
.footer .brand span {{ color: var(--gold); }}
{style}
</style></head><body>{content}
<script>
  function done() {{ document.documentElement.dataset.ready = "1"; }}
  if (document.fonts && document.fonts.ready) {{ document.fonts.ready.then(done); }} else {{ done(); }}
</script>
</body></html>"""


def intro_slide_1(fonts_css: str) -> str:
    style = """
.slide { position: relative; height: 100%; display: flex; flex-direction: column;
         justify-content: space-between; padding: 84px 88px 64px; }
.headline { font-size: 92px; line-height: 1.1; text-align: center; }
.headline .accent { color: var(--gold); }
.sub { font-size: 36px; line-height: 1.55; color: var(--muted); text-align: center;
       margin-top: 44px; }
.sub strong { color: var(--ink); font-weight: 600; }
.flags-row { display: flex; justify-content: center; gap: 26px; font-size: 64px;
             margin-top: 10px; letter-spacing: 0.1em; }
.swipe { color: var(--gold); font-weight: 600; }
"""
    content = """
<div class="pitch-circle" style="width:1500px;height:1500px;top:-1130px;"></div>
<div class="slide">
  <div style="text-align:center;"><span class="eyebrow">Nowy profil &bull; Mundial 2026</span></div>
  <div>
    <h1 class="headline display">Ten sam mecz.<br><span class="accent">Dwa kraje, dwie wersje.</span></h1>
    <p class="sub">Po ka&#380;dym meczu mundialu czytamy <strong>pras&#281; obu reprezentacji
    w oryginale</strong> i pokazujemy jak r&oacute;&#380;nie mo&#380;na
    opisa&#263; ten sam wynik.</p>
    <p class="sub">Treści generowane są <strong>automatycznie</strong>. Dlatego możesz się ich spodziewać chwilę po ostatnim gwizdku sędziego.</p>
    <div class="flags-row">&#129302; &#127757; &#9917; &#128240;</div>
  </div>
  <div class="footer">
    <div class="brand"><span>@</span>mundaily_</div>
    <div class="swipe">przesu&nacute; &rarr;</div>
  </div>
</div>"""
    return _page(fonts_css, 1080, 1350, style, content)


def intro_slide_2(fonts_css: str) -> str:
    style = """
.slide { position: relative; height: 100%; display: flex; flex-direction: column;
         padding: 84px 88px 64px; }
.head { font-size: 72px; margin: 36px 0 50px; }
.head .accent { color: var(--gold); }
.steps { display: flex; flex-direction: column; gap: 34px; flex: 1; }
.step { display: flex; gap: 30px; align-items: flex-start; padding: 34px 38px;
        border: 1.5px solid var(--line); border-radius: 26px; background: var(--card); }
.step .no { font-family: "Archivo", "Inter", sans-serif; font-weight: 800;
            font-size: 52px; color: var(--gold); line-height: 1; min-width: 64px; }
.step .txt { font-size: 34px; line-height: 1.5; color: var(--ink); }
.step .txt em { font-style: normal; color: var(--teal); font-weight: 600; }
.note { font-size: 29px; color: var(--muted); text-align: center; margin: 40px 0 26px; }
"""
    content = """
<div class="pitch-circle" style="width:1500px;height:1500px;bottom:-1180px;"></div>
<div class="slide">
  <div style="text-align:center;"><span class="eyebrow">Jak to dzia&#322;a</span></div>
  <h1 class="head display" style="text-align:center;">Od gwizdka <span class="accent">do artyku&#322;&oacute;w</span></h1>
  <div class="steps">
    <div class="step"><div class="no">1</div><div class="txt">Mecz si&#281; ko&nacute;czy.
      Czytamy gazety <em>obu kraj&oacute;w</em> w ich j&#281;zykach.</div></div>
    <div class="step"><div class="no">2</div><div class="txt">Wybieramy najmocniejsze tezy
      i cytaty. <em>Zawsze z podaniem &#378;r&oacute;d&#322;a.</em></div></div>
    <div class="step"><div class="no">3</div><div class="txt">Sk&#322;adamy jeden post:
      dwa kraje, dwie perspektywy, <em>po polsku</em>.</div></div>
  </div>
  <p class="note">Na ko&nacute;cu ka&#380;dego posta: slajd ze &#378;r&oacute;d&#322;ami.</p>
  <div class="footer">
    <div class="brand"><span>@</span>mundaily_</div>
    <div>obserwuj &#8594;</div>
  </div>
</div>"""
    return _page(fonts_css, 1080, 1350, style, content)


def avatar(fonts_css: str) -> str:
    # IG tnie awatar do kola - wszystko istotne trzymamy w centrum;
    # "M_" to uklon w strone handle'a @mundaily_
    style = """
.wrap { position: relative; height: 100%; display: flex; align-items: center;
        justify-content: center; }
.mono { font-family: "Archivo", "Inter", sans-serif; font-weight: 800;
        font-size: 560px; color: var(--gold); line-height: 1; letter-spacing: -0.03em; }
.mono .underscore { color: var(--teal); }
"""
    content = """
<div class="pitch-circle" style="width:880px;height:880px;top:100px;"></div>
<div class="wrap"><div class="mono">M<span class="underscore">_</span></div></div>"""
    return _page(fonts_css, 1080, 1080, style, content)


INTRO_CAPTION = """Ten sam mecz, dwa różne światy.

Po każdym meczu mundialu 2026 czytamy prasę obu reprezentacji — w oryginale — i pokazujemy po polsku, jak różnie można opisać ten sam wynik. Najmocniejsze tezy, cytaty z atrybucją i slajd ze źródłami w każdym poście. Mówi prasa, nie my.

Na start: Meksyk–RPA i Korea Południowa–Czechy. Prasę którego kraju chcesz zobaczyć następną?

""" + " ".join(BASE_HASHTAGS) + "\n"

X_INTRO_POST = """Ten sam mecz. Dwa kraje, dwie wersje.

Po każdym meczu mundialu 2026 czytamy prasę obu reprezentacji — w oryginale — i pokazujemy po polsku, jak różnie można opisać ten sam wynik.

Jak to działa:
1. Mecz się kończy. Czytamy gazety obu krajów w ich językach.
2. Wybieramy najmocniejsze tezy i cytaty. Zawsze z podaniem źródła.
3. Składamy jeden post: dwa kraje, dwie perspektywy, po polsku.

Treści powstają automatycznie — możesz się ich spodziewać chwilę po ostatnim gwizdku sędziego. Na końcu każdego posta: źródła. Mówi prasa, nie my.

Na start: Meksyk–RPA, Korea Południowa–Czechy i Kanada–Bośnia. Prasę którego kraju chcesz zobaczyć następną?

Karuzele z grafikami: instagram.com/mundaily_

""" + " ".join(BASE_HASHTAGS) + "\n"

BIO = """⚽ Mundial 2026 oczami zagranicznej prasy
🗞️ Po meczu: cytaty mediów obu krajów, po polsku
📌 Zawsze ze źródłem
"""


def main() -> int:
    from playwright.sync_api import sync_playwright

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    fonts_css = build_fonts_css()
    jobs = [
        ("intro_slide_01.png", intro_slide_1(fonts_css), 1080, 1350),
        ("intro_slide_02.png", intro_slide_2(fonts_css), 1080, 1350),
        ("avatar.png", avatar(fonts_css), 1080, 1080),
    ]
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            for name, html, width, height in jobs:
                page = browser.new_page(
                    viewport={"width": width, "height": height}, device_scale_factor=2
                )
                page.set_content(html, wait_until="load")
                page.wait_for_selector('html[data-ready="1"]', timeout=15000)
                page.screenshot(path=str(OUT_DIR / name))
                page.close()
                print(f"[brand] {OUT_DIR / name}")
        finally:
            browser.close()
    (OUT_DIR / "intro_caption.txt").write_text(INTRO_CAPTION, encoding="utf-8")
    (OUT_DIR / "x_intro_post.txt").write_text(X_INTRO_POST, encoding="utf-8")
    (OUT_DIR / "bio.txt").write_text(BIO, encoding="utf-8")
    print(f"[brand] {OUT_DIR / 'intro_caption.txt'}")
    print(f"[brand] {OUT_DIR / 'x_intro_post.txt'}")
    print(f"[brand] {OUT_DIR / 'bio.txt'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
