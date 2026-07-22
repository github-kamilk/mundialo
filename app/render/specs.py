"""Budowa specyfikacji slajdow z MediaReactionPackage (plain dict z run.json).

Czysta logika bez zaleznosci od Jinja2/Playwright — testowalna offline.
Zasada: tekst zatwierdzony przez sedziow (headline/body slajdow karuzeli)
trafia do speca BEZ transformacji tresci; wzbogacamy go tylko o metadane
prezentacyjne (outlet, tier, domena, flaga, akcent koloru).
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
REGISTRY_PATH = REPO_ROOT / "data" / "sources" / "country_media.json"
PALETTES_PATH = REPO_ROOT / "data" / "render" / "city_palettes.json"

RENDERABLE_STATUSES = {"ready"}
REVIEW_STATUSES = {"needs_human_review"}

# awaryjna paleta, gdyby plik palet zniknal - rownia z "default" w city_palettes.json
_FALLBACK_PALETTE = {
    "accent": "#e9c46a",
    "accent_2": "#5dd6c0",
    "glow_a": "#1c2b4d",
    "glow_b": "#15243f",
    "bg_top": "#0d1426",
    "bg_bottom": "#0a0e1b",
}


def _fold(text: str) -> str:
    import unicodedata

    normalized = unicodedata.normalize("NFKD", text or "")
    return "".join(ch for ch in normalized if not unicodedata.combining(ch)).lower()


def load_palettes(path: Path = PALETTES_PATH) -> dict[str, Any]:
    """Palety miast-gospodarzy. Brak/bledny plik => sama paleta domyslna."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"default": dict(_FALLBACK_PALETTE), "cities": []}
    if not isinstance(data.get("default"), dict):
        data["default"] = dict(_FALLBACK_PALETTE)
    if not isinstance(data.get("cities"), list):
        data["cities"] = []
    return data


def resolve_palette(venue: str, palettes: dict[str, Any] | None = None) -> tuple[str, dict[str, str]]:
    """(miasto, paleta) dla venue z terminarza/faktow; brak dopasowania => ('', default).

    Dopasowanie: alias miasta/stadionu jako podlancuch venue po fold_ascii -
    venue bywa pelne ('Estadio Akron, Guadalajara') albo samo miasto.
    """
    palettes = palettes if palettes is not None else load_palettes()
    folded_venue = _fold(venue)
    if folded_venue:
        for entry in palettes.get("cities", []):
            for alias in entry.get("aliases", []):
                if alias and _fold(alias) in folded_venue:
                    return entry.get("city", ""), {
                        **palettes["default"],
                        **(entry.get("palette") or {}),
                    }
    return "", dict(palettes["default"])


# Akceptujemy kod ISO 3166-1 alpha-2 ("gb") oraz subdywizje ISO 3166-2
# ("gb-sct", "gb-eng") - narody brytyjskie maja wlasne flagi (Saltire, krzyz
# sw. Jerzego), a nie Union Jack. flagcdn serwuje je pod tym samym kodem.
_FLAG_CODE = re.compile(r"[a-z]{2}(-[a-z]{3})?$")


def load_iso2_map(registry_path: Path = REGISTRY_PATH) -> dict[str, str]:
    """Mapa kraj -> kod flagi z kurowanego rejestru. Brak pliku => pusta mapa."""
    try:
        data = json.loads(Path(registry_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    result: dict[str, str] = {}
    for entry in data.get("countries", []):
        country = entry.get("country")
        iso2 = (entry.get("iso2") or "").strip().lower()
        if country and _FLAG_CODE.fullmatch(iso2):
            result[country] = iso2
    return result


def load_display_map(registry_path: Path = REGISTRY_PATH) -> dict[str, str]:
    """Mapa kraj (kanonicznie, ASCII) -> nazwa do pokazania (pelne diakrytyki).

    Nazwa kanoniczna jest kluczem technicznym w calym pipeline; na grafice
    pokazujemy 'Korea Południowa', nie 'Korea Poludniowa'.
    """
    try:
        data = json.loads(Path(registry_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    result: dict[str, str] = {}
    for entry in data.get("countries", []):
        country = entry.get("country")
        display = (entry.get("display_name") or "").strip()
        if country:
            result[country] = display or country
    return result


def load_outlet_names(registry_path: Path = REGISTRY_PATH) -> dict[str, str]:
    """Mapa provider_id -> ludzka nazwa redakcji ('News24ZA' -> 'News24').

    provider_id to klucz techniczny pipeline'u; na grafice pokazujemy nazwe
    outletu z katalogu. Brak pliku => pusta mapa (fallback: provider_id).
    """
    try:
        data = json.loads(Path(registry_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    result: dict[str, str] = {}
    for entry in data.get("countries", []):
        for outlet in entry.get("outlets", []):
            provider_id = outlet.get("provider_id")
            name = (outlet.get("name") or "").strip()
            if provider_id:
                result[provider_id] = name or provider_id
    return result


def domain_of(url: str) -> str:
    match = re.match(r"https?://(?:www\.)?([^/:?#]+)", url or "")
    return match.group(1) if match else ""


def format_date_pl(iso_date: str) -> str:
    match = re.match(r"(\d{4})-(\d{2})-(\d{2})", iso_date or "")
    if not match:
        return iso_date or ""
    year, month, day = match.groups()
    return f"{day}.{month}.{year}"


def _display_score(full_time: str | None) -> str:
    if not full_time:
        return ""
    return full_time.replace("-", "–")  # 2-0 -> 2–0


def _title_question(headline: str, home: str, away: str) -> str:
    """Odklej prefiks z wynikiem od hooka/pytania slajdu tytulowego.

    Wynik i druzyny sa osobno w bloku score, wiec prefiks "Home N-M Away" w tekscie
    to dublowanie. Dwa formaty asemblera:
    - nowy (rama redakcyjna): "Home N-M Away. Hook z atrybucja" - tniemy po kropce
      za pelnym wzorcem druzyna+wynik+druzyna (atrybucja z ':' w hooku ZOSTAJE);
    - stary szablon: "Home N-M Away: jak odebraly to media?" - tniemy po ':'.
    Gdy prefiks nie wyglada na wynik, headline zostaje w calosci (zero zgadywania).
    """
    if home and away:
        pattern = (
            rf"^\s*{re.escape(home)}\s+\d{{1,2}}\s*[-:–]\s*\d{{1,2}}\s+"
            rf"{re.escape(away)}\s*\.\s*(?P<hook>.+)$"
        )
        match = re.match(pattern, headline, flags=re.IGNORECASE)
        if match:
            return _capitalize_safe(match.group("hook").strip())
    if ":" in headline:
        prefix, rest = headline.split(":", 1)
        has_digits = bool(re.search(r"\d", prefix))
        mentions_team = (home and home.lower() in prefix.lower()) or (
            away and away.lower() in prefix.lower()
        )
        if has_digits and mentions_team and rest.strip():
            return _capitalize_safe(rest.strip())
    return headline


def _capitalize_safe(text: str) -> str:
    """Wielka litera na starcie, ale bez psucia marek typu 'iSport' -> 'ISport'.

    Gdy drugi znak jest wielka litera, pierwszy zostaje (camelCase marki)."""
    if len(text) >= 2 and text[1].isupper():
        return text
    return text[0].upper() + text[1:] if text else text


def _quotes_by_evidence(media_package: dict[str, Any]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for panel in media_package.get("panels", []):
        for quote in panel.get("quotes", []):
            evidence_id = quote.get("evidence_id")
            if evidence_id:
                index[evidence_id] = quote
    return index


def _quote_for_slide(
    slide: dict[str, Any], by_evidence: dict[str, dict[str, Any]]
) -> dict[str, Any] | None:
    for claim_id in slide.get("claim_ids", []):
        if claim_id in by_evidence:
            return by_evidence[claim_id]
    headline = slide.get("headline", "")
    for quote in by_evidence.values():
        outlet = quote.get("outlet", "")
        if outlet and outlet in headline:
            return quote
    return None


def build_slide_specs(
    media_package: dict[str, Any],
    iso2_map: dict[str, str] | None = None,
    display_map: dict[str, str] | None = None,
    outlet_names: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Zamienia media_package (dict z run.json) na liste specow slajdow."""
    iso2_map = iso2_map if iso2_map is not None else load_iso2_map()
    display_map = display_map if display_map is not None else load_display_map()
    outlet_names = outlet_names if outlet_names is not None else load_outlet_names()

    def display(country: str) -> str:
        return display_map.get(country, country)

    def outlet_display(provider_id: str) -> str:
        return outlet_names.get(provider_id, provider_id)

    match = media_package.get("match", {})
    home = match.get("home_team", "")
    away = match.get("away_team", "")
    slides = media_package.get("carousel", {}).get("slides", [])
    by_evidence = _quotes_by_evidence(media_package)
    total = len(slides)
    # jedna paleta na cala karuzele, z miasta-gospodarza (venue z terminarza/faktow)
    host_city, palette = resolve_palette(match.get("venue", ""))

    specs: list[dict[str, Any]] = []
    for slide in slides:
        role = slide.get("role", "")
        base = {
            "role": role,
            "slide_number": slide.get("slide_number", len(specs) + 1),
            "total": total,
            "headline": slide.get("headline", ""),
            "body": slide.get("body", ""),
            "palette": palette,
            "host_city": host_city,
        }
        if role == "title":
            base.update(
                {
                    "home_team": display(home),
                    "away_team": display(away),
                    "home_iso2": iso2_map.get(home, ""),
                    "away_iso2": iso2_map.get(away, ""),
                    "score": _display_score((match.get("score") or {}).get("full_time")),
                    "date": format_date_pl(match.get("date", "")),
                    "question": _title_question(base["headline"], home, away),
                    # byline (sama nazwa dziennika) gotowy z editorialu (juz zhumanizowany);
                    # osobny element pod tytulem, nie wciskany w naglowek
                    "attribution": (slide.get("attribution") or "").strip(),
                }
            )
        elif role == "media_country":
            quote = _quote_for_slide(slide, by_evidence)
            country = (quote or {}).get("country", "")
            base.update(
                {
                    "country": display(country),
                    "iso2": iso2_map.get(country, ""),
                    "outlet": outlet_display((quote or {}).get("outlet", "")),
                    "tier": (quote or {}).get("tier", ""),
                    "domain": domain_of((quote or {}).get("url", "")),
                    "accent": "home" if country == home else "away",
                }
            )
        elif role == "sources":
            items = []
            for panel in media_package.get("panels", []):
                country = panel.get("country", "")
                for quote in panel.get("quotes", []):
                    items.append(
                        {
                            "outlet": outlet_display(quote.get("outlet", "")),
                            "domain": domain_of(quote.get("url", "")),
                            "country": display(country),
                            "iso2": iso2_map.get(country, ""),
                            "tier": quote.get("tier", ""),
                        }
                    )
            base["items"] = items
            base["date"] = format_date_pl(match.get("date", ""))
        specs.append(base)
    return specs


def build_caption_text(
    media_package: dict[str, Any],
    outlet_names: dict[str, str] | None = None,
    display_map: dict[str, str] | None = None,
) -> str:
    """Gotowy opis posta na IG: opis redakcyjny -> linki do zrodel -> hashtagi.

    Sklada wylacznie tresci zatwierdzone przez sedziow (caption z pakietu) plus
    pelne URL-e zrodel, ktorych na slajdach celowo nie ma (na slajdzie zrodlowym
    tylko outlety i domeny - linki ida wlasnie tutaj, do opisu).
    """
    outlet_names = outlet_names if outlet_names is not None else load_outlet_names()
    display_map = display_map if display_map is not None else load_display_map()
    caption = media_package.get("caption") or {}

    lines: list[str] = []
    text = (caption.get("text") or "").strip()
    if text:
        lines.append(text)

    sources: list[str] = []
    seen: set[str] = set()
    for panel in media_package.get("panels", []):
        country = panel.get("country", "")
        country_display = display_map.get(country, country)
        for quote in panel.get("quotes", []):
            url = (quote.get("url") or "").strip()
            if not url or url in seen:
                continue
            seen.add(url)
            outlet = quote.get("outlet", "")
            sources.append(
                f"- {outlet_names.get(outlet, outlet)} ({country_display}): {url}"
            )
    if sources:
        lines.append("")
        lines.append("Źródła:")
        lines.extend(sources)

    hashtags = [tag for tag in (caption.get("hashtags") or []) if tag]
    if hashtags:
        lines.append("")
        lines.append(" ".join(hashtags))
    return "\n".join(lines) + "\n"


# koniec zdania tylko przed wielka litera/cudzyslowem - kropka po liczebniku
# porzadkowym ('w 78. minucie') nie konczy zdania
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?…])\s+(?=[A-ZĄĆĘŁŃÓŚŹŻ\"„'(])")


def build_x_post_text(
    media_package: dict[str, Any],
    outlet_names: dict[str, str] | None = None,
    display_map: dict[str, str] | None = None,
) -> str:
    """Gotowy dlugi post na X: tekstowy odpowiednik karuzeli IG, 1:1 informacyjnie.

    X dopuszcza dlugie posty, wiec zadnego skracania: naglowek tytulowy +
    podtytul, PELNE streszczenia kazdego cytowanego artykulu (tresc slajdow),
    CTA z opisu, zrodla z linkami i komplet hashtagow. Tylko tresci
    zatwierdzone przez sedziow; techniczne provider_id podmieniane na ludzkie
    nazwy redakcji.
    """
    outlet_names = outlet_names if outlet_names is not None else load_outlet_names()
    display_map = display_map if display_map is not None else load_display_map()
    caption = media_package.get("caption") or {}
    title = media_package.get("title_slide") or {}

    def humanize(text: str) -> str:
        # streszczenia atrybuuja technicznym provider_id ('TSNCA relacjonuje...')
        for provider_id, name in outlet_names.items():
            if name and provider_id != name:
                text = text.replace(provider_id, name)
        return text

    blocks: list[str] = []
    headline = humanize((title.get("headline") or "").strip())
    if headline:
        blocks.append(headline)
    title_body = (title.get("body") or "").strip()
    if title_body:
        blocks.append(title_body)

    sources: list[str] = []
    seen: set[str] = set()
    for panel in media_package.get("panels", []):
        country = panel.get("country", "")
        country_display = display_map.get(country, country)
        for quote in panel.get("quotes", []):
            outlet = quote.get("outlet", "")
            outlet_display = outlet_names.get(outlet, outlet)
            body = humanize(quote.get("summary_pl") or quote.get("translation_pl") or "")
            if not body:
                continue
            # streszczenia same atrybuuja redakcje w pierwszym zdaniu - wtedy
            # naglowek bloku nie dubluje nazwy; goly cytat (fixture) dostaje pelna
            if outlet_display and outlet_display.lower() in body.lower():
                blocks.append(f"🗞️ {country_display}:\n{body}")
            else:
                blocks.append(f"🗞️ {outlet_display} ({country_display}):\n{body}")
            url = (quote.get("url") or "").strip()
            if url and url not in seen:
                seen.add(url)
                sources.append(f"- {outlet_display} ({country_display}): {url}")

    caption_text = (caption.get("text") or "").strip()
    if caption_text.endswith("?"):
        # CTA-pytanie z opisu; reszta captionu to kontrast, ktory streszczenia
        # juz niosa w pelnej wersji
        sentences = _SENTENCE_SPLIT_RE.split(caption_text)
        if sentences:
            blocks.append(sentences[-1].strip())

    if sources:
        blocks.append("Źródła:\n" + "\n".join(sources))

    hashtags = [tag for tag in (caption.get("hashtags") or []) if tag]
    if hashtags:
        blocks.append(" ".join(hashtags))

    return "\n\n".join(blocks) + "\n"
