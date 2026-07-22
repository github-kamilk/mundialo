"""Providery research (live): orkiestracja search -> fetch -> sanitize -> ekstrakcja.

To jest warstwa DETERMINISTYCZNA (I/O + guardraile). Model (scout) jest wstrzykiwany
przez protokol z `contracts`, dzieki czemu `tools` nie zalezy od `agents` (brak cyklu).
Wszystkie wyjscia przechodza przez te same bezpieczniki co fixture: whitelist domen,
sanityzacja anti-injection, tier z rejestru, twardy budzet na run.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.memory import fold_ascii, mentioned_scores
from app.models import GenerationError, ModelError
from app.observability.telemetry import (
    FETCH_OK,
    FETCH_THIN,
    OutletFetchEvent,
    RunTelemetry,
    SECTION_LINKS,
    SECTION_NO_LINKS,
    SectionProbeEvent,
    classify_fetch_error,
)
from app.schemas import EvidenceItem, GoalEvent, MatchFacts, ScoreLine, SourceTier
from app.tools.contracts import (
    Cache,
    FactsScout,
    MatchContext,
    MediaCurator,
    MediaScout,
    PageFetcher,
    PostMatchGate,
    ProviderCapability,
    RawMediaItem,
    SearchClient,
    SearchHit,
    SourceHealth,
)
from app.tools.control import (
    BudgetExceededError,
    BudgetTracker,
    ResearchError,
    cache_key,
    domain_allowed,
    sanitize_external_text,
)
from app.tools.registry import SourceRegistry


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _mint(prefix: str, *parts: str) -> str:
    digest = hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:10]
    return f"{prefix}_{digest}"


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", fold_ascii(value)).strip("_") or "x"


# Heurystyka "w tekscie jest jakis wynik" (1-2, 2:1 itp.) - tani pre-filtr przed LLM.
_SCORE_HINT_RE = re.compile(r"\b\d{1,2}\s*[-:]\s*\d{1,2}\b")


def match_blob(text: str) -> str:
    """Tekst do matchowania nazw/tokenow: fold ASCII + myslniki jako spacje.

    Slugi URL uzywaja myslnikow ('bafana-bafana-in-mexican-loss'), a aliasy
    z rejestru spacji ('bafana bafana') - bez normalizacji wielowyrazowe nazwy
    NIGDY nie matchuja slugow i wlasciwe relacje pomeczowe przegrywaja ranking
    z przypadkowymi hitami.
    """
    return fold_ascii(text).replace("-", " ").replace("_", " ")


def _word_in(token: str, blob: str) -> bool:
    return bool(re.search(rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])", blob))

# Twardy limit dlugosci tekstu do ekstrakcji (raw_content z indeksu bywa ogromny).
_MAX_FACTS_TEXT = 20000

# Ile tekstu artykulu wedruje dalej jako baza streszczenia na slajd.
_MAX_ARTICLE_FOR_SUMMARY = 6000

# Minimalny rozmiar tekstu artykulu, by mial sens jako SLAJD (streszczenie >=5 zdan -
# MIN_SUMMARY_SENTENCES). Breaking-flash/'alerte' (tsa-algerie.com/alerte-...) to sam
# lead (~520 zn.): scout wyciaga z niego cytat, ale streszczenia nie ma z czego zlozyc,
# wiec slajd schodzil do samego cytatu (run_20260623102934, Algieria slajd 1). Pelny
# recap ma kilka tysiecy znakow (ElKhabar tego meczu: ~5960). To PREFERENCJA, nie twardy
# odrzut: gdy kraj ma SAME cienkie zrodla (male nacje, krotkie recapy), i tak ich uzywamy
# (uzupelnienie slotow) - lepiej cytat-stub niz pusty panel.
_MIN_ARTICLE_BODY_FOR_SLIDE = 900

# Ile tekstu widzi scout przy ekstrakcji cytatu. Relacja z meczu trzyma OCENE wyniku
# i przebiegu (decydujace gole, werdykt o grze) zwykle w KONCOWCE - obciecie do 2000 zn.
# (domyslny limit sanitize) zostawialo scoutowi sama sucha rozgrzewke (sklady, pierwsze
# minuty) i czesto wracal z pusta lista. Guard verbatim w scoucie i tak chroni przed
# fabrykacja, wiec mozemy bezpiecznie podac caly artykul.
_MAX_SCOUT_TEXT = 8000

# ID CMS-a z kropka, np. NRK '1.17930270' (stale '1.' + dlugie cyfry): ten sam artykul
# trafia do puli pod slugiem ('...-1.17930270') i samodzielnie ('/sport/1.17930270').
# Wymagamy >=6 cyfr po kropce, zeby nie sklejac wersji/numerow w stylu '1.2'.
_DOTTED_CMS_ID_RE = re.compile(r"(\d+\.\d{6,})")


def canonical_article_key(url: str) -> str:
    """Klucz deduplikacji artykulu: serwisy publikuja ten sam tekst pod wieloma slugami.

    isport.blesk.cz/clanek/.../476520/slug-live i /476520/slug-final to JEDEN artykul
    (wspolne ID). Dlugie ID (>=6 cyfr) jest kluczem SAMODZIELNIE (host+ID) - klix.ba
    trzyma ID na koncu sciezki, wiec rozne slugi przed ID to wciaz ten sam artykul
    (live-blog '...-zmajevi-vode/260610072' i '...-zagrijavanje/260610072' wchodzily
    do puli podwojnie). Krotszy ID numeryczny (4-5 cyfr, ale NIE rok) tez deduplikuje
    sciezke do siebie wlacznie.

    UWAGA - rok w sciezce daty (/2026/06/13/slug) NIE jest ID: traktowanie '2026' jako
    granicy klucza sklejalo WSZYSTKIE artykuly z danego roku w jeden klucz (host/2026),
    przez co cala data-path prasa (lapresse.tn/2026/06/15/..., kapitalis /tunisie/2026/...)
    zapadala sie do jednego wpisu i recapy gubily sie w puli za pierwsza zapowiedzia.
    Lata pomijamy - taki URL keyuje sie pelna sciezka (slug rozroznia artykuly).

    NRK (CMS Polopoly) trzyma ID w formacie '1.17930270' (stale '1.' + cyfry): ten sam
    artykul wystepuje jako '.../sport/landslaget-...-1.17930270' (slug+ID) i samodzielnie
    jako '.../sport/1.17930270'. Kropka psula detekcje '>=6 cyfr' (slug nie jest .isdigit(),
    a samo '1' ma 1 cyfre), wiec OBIE formy szly do puli osobno i zjadaly po slocie. ID z
    kropka jest kluczem niezaleznie od slugu.
    """
    from urllib.parse import urlparse

    parsed = urlparse(url)
    host = (parsed.hostname or "").lower().removeprefix("www.")
    dotted_id = _DOTTED_CMS_ID_RE.search(parsed.path or "")
    if dotted_id:
        return f"{host}/id/{dotted_id.group(1)}"
    segments = [segment for segment in (parsed.path or "").split("/") if segment]

    def _is_year(seg: str) -> bool:
        return len(seg) == 4 and seg[:2] in {"19", "20"}

    def _numeric_core(seg: str) -> str:
        # CMS-y ASP.NET (ahram.org.eg) trzymaja ID z rozszerzeniem pliku:
        # '.../News/570896.aspx' i '.../NewsContent/.../570896/slug' to TEN SAM artykul,
        # ale '.aspx' blokowal wyciagniecie ID i obie formy szly do puli osobno.
        return seg.rsplit(".", 1)[0] if "." in seg else seg

    for index in range(len(segments) - 1, -1, -1):
        core = _numeric_core(segments[index])
        if core.isdigit() and len(core) >= 6:
            return f"{host}/id/{core}"
    for index in range(len(segments) - 1, -1, -1):
        core = _numeric_core(segments[index])
        if core.isdigit() and len(core) >= 4 and not _is_year(core):
            return f"{host}/" + "/".join(segments[:index] + [core])
    return f"{host}/" + "/".join(segments)


_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}")


def _order_section_candidates(
    candidates: list[tuple[Any, str]],
    health: SourceHealth | None,
    max_sections: int,
) -> tuple[list[tuple[Any, str]], int]:
    """Kolejnosc probowania sekcji wg zdrowia (etap 3). Zwraca (kolejnosc, ile_zdemotowano).

    Polityka: sekcje ze SWIEZA seria porazek (JS-wall/botblock/404) ida na koniec
    (demote, nie skip - budzet moze je jeszcze objac), a OSTATNI slot budzetu jest
    EKSPLORACYJNY: dostaje go sekcja najdawniej probowana/nieznana sposrod reszty.
    Bez slotu eksploracji zdrowie byloby samospelniajaca sie smiercia sekcji
    (nigdy nie probujemy -> nigdy nie ozdrowieje).

    Cold-start parity: brak zdrowia / pusty magazyn / blad zapytan => kolejnosc
    IDENTYCZNA z rejestrem (remisy rozstrzyga indeks rejestru).
    """
    if health is None or len(candidates) <= 1:
        return list(candidates), 0
    try:
        dead_flags = [bool(health.section_dead(section)) for _, section in candidates]
        last_probe = [health.section_last_probe(section) for _, section in candidates]
    except Exception:  # noqa: BLE001 - zdrowie doradcze: awaria = kolejnosc jak dotad
        return list(candidates), 0
    indices = list(range(len(candidates)))
    alive = [i for i in indices if not dead_flags[i]]
    dead = [i for i in indices if dead_flags[i]]
    if len(candidates) <= max_sections:
        # wszystko miesci sie w budzecie: martwe tylko NA KONIEC (budget.charge
        # moze wyczerpac sie w polowie - zdrowe maja isc pierwsze)
        order = alive + dead
    else:
        main = alive[: max(0, max_sections - 1)]
        rest = [i for i in indices if i not in main]
        # eksploracja: nieznana (nigdy nie probowana) > najstarsza proba > rejestr
        exploration = min(
            rest, key=lambda i: (last_probe[i] is not None, last_probe[i] or "", i)
        )
        order = main + [exploration] + [i for i in rest if i != exploration]
    return [candidates[i] for i in order], len(dead)


def collect_section_hits(
    fetcher,
    budget: BudgetTracker,
    profile,
    opponent_profile,
    diag: list[str],
    label: str,
    max_sections: int = 3,
    telemetry: RunTelemetry | None = None,
    health: SourceHealth | None = None,
) -> list[SearchHit]:
    """Linki artykulow ze stron sekcji outletow (rejestr `sections`).

    Indeks wyszukiwarki laduje relacje pomeczowe z opoznieniem godzin; strona
    dzialu sportowego listuje je od razu. Bierzemy tylko linki w domenie outletu,
    wygladajace na artykul i pasujace do meczu: wymieniaja przeciwnika ALBO wlasna
    kadre wraz z lokalnym slowem reakcji z query_templates (lokalne slugi uzywaja
    egzonimow typu 'sudafrica', ktorych aliasy przeciwnika nie znaja).

    `health` (etap 3) wplywa WYLACZNIE na kolejnosc probowania w ramach budzetu
    (_order_section_candidates); whitelist i zawartosc puli bez zmian.
    """
    fetch_links = getattr(fetcher, "fetch_links", None)
    if fetch_links is None or opponent_profile is None:
        return []
    opp_tokens = tuple(
        fold_ascii(name) for name in opponent_profile.aliases() if len(name) >= 3
    )
    if not opp_tokens:
        return []
    own_tokens = tuple(fold_ascii(name) for name in profile.aliases() if len(name) >= 3)
    template_words = {
        fold_ascii(word)
        for template in profile.query_templates
        for word in re.findall(
            r"[A-Za-zÀ-ſ]{4,}",
            template.replace("{team}", " ").replace("{opponent}", " "),
        )
    }
    # Lokalny termin MS w skrypcie outletu ('كأس العالم 2026', '월드컵 2026'): slowa
    # szablonow sa lacinskie ([A-Za-z]{4,}), wiec dla outletu arabskiego/koreanskiego
    # own_reaction NIGDY by nie zadzialal i relacja w lokalnym skrypcie wypadala z puli
    # (Egipt/filgoal: 0 linkow). Dorzucamy stokenizowany world_cup, zeby 'wlasna nazwa
    # + كأس العالم' tez sie lapalo.
    if profile.world_cup:
        for word in re.split(r"[\s\-_]+", fold_ascii(profile.world_cup)):
            if len(word) >= 2:
                template_words.add(word)
    # Nazwa wlasna kraju bywa LITERALNIE w query_template ('Deutschland {opponent} WM
    # 2026' zamiast '{team} ...'), wiec wpada do template_words - a wtedy own_reaction =
    # (own_token) AND (ten sam own_token) jest TRYWIALNIE prawdziwe dla KAZDEGO URL-a z
    # nazwa kraju (sportschau: 'deutschland-supercup', 'deutschland-tour', '...-relegation'
    # floodowaly pule i wypychaly relacje meczowa). Slowo reakcji ma byc ROZNE od nazwy
    # wlasnej - odejmujemy aliasy kraju (Niemcy-Paragwaj run_20260630215003).
    template_words -= {fold_ascii(name) for name in profile.aliases()}

    candidates = [
        (outlet, section) for outlet in profile.outlets for section in outlet.sections
    ]
    ordered, demoted = _order_section_candidates(candidates, health, max_sections)
    if demoted:
        diag.append(
            f"{label}: outlet_health: {demoted} sekcji zdemotowanych "
            "(swieza seria porazek; wroca po re-probe)"
        )

    hits: list[SearchHit] = []
    seen: set[str] = set()
    sections_used = 0
    for outlet, section in ordered:
        domains = outlet.descriptor.domains
        if sections_used >= max_sections:
            return hits
        sections_used += 1
        try:
            budget.charge()
            links = fetch_links(section)
        except BudgetExceededError:
            return hits
        except Exception as error:  # noqa: BLE001 - sekcja nie wywala kraju
            diag.append(f"{label}: sekcja {section} nieudana ({error})")
            if telemetry is not None:
                telemetry.emit(
                    SectionProbeEvent(
                        provider_id=outlet.descriptor.provider_id,
                        country=profile.country,
                        section_url=section,
                        outcome=classify_fetch_error(error),
                    )
                )
            continue
        found = 0
        # linki artykulowe w domenie PRZED filtrem meczu: detektor JS-walla
        # (0 = sama nawigacja); 'zero pasujacych' przy zywej sekcji to norma
        article_links = 0
        for href, anchor in links:
            if not domain_allowed(href, domains) or not is_article_url(href):
                continue
            article_links += 1
            key = canonical_article_key(href)
            if key in seen:
                continue
            blob = match_blob(f"{anchor} {href}")
            mentions_opponent = any(token in blob for token in opp_tokens)
            # slowa reakcji po granicach slow: substring lapal 'world'
            # w 'mens-worlds' i sekcje hokejowe wchodzily do puli
            own_reaction = any(token in blob for token in own_tokens) and any(
                _word_in(word, blob) for word in template_words
            )
            if not (mentions_opponent or own_reaction):
                continue
            seen.add(key)
            hits.append(SearchHit(url=href, title=anchor, snippet=""))
            found += 1
        diag.append(f"{label}: sekcja {section} -> {found} linkow pasujacych")
        if telemetry is not None:
            telemetry.emit(
                SectionProbeEvent(
                    provider_id=outlet.descriptor.provider_id,
                    country=profile.country,
                    section_url=section,
                    outcome=SECTION_LINKS if article_links else SECTION_NO_LINKS,
                    links_found=found,
                    article_links=article_links,
                )
            )
    return hits


# Daty zakodowane w URL-ach artykulow (konserwatywnie, tylko jednoznaczne wzorce):
# /2026/06/06/, 20260606 (news24: -20260611-1322), klix: 9-cyfrowe ID 260606010
# (YYMMDD + nr kolejny).
_URL_DATE_SLASHED_RE = re.compile(r"/(20\d{2})[/-](0[1-9]|1[0-2])[/-](0[1-9]|[12]\d|3[01])(?![\d])")
_URL_DATE_COMPACT_RE = re.compile(r"(?<!\d)(20\d{2})(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])(?!\d)")
_URL_DATE_YYMMDD_ID_RE = re.compile(r"/(2[4-9])(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])\d{3}(?:/|$|\?)")


def date_from_url(url: str) -> str | None:
    """Najpozniejsza data zakodowana w URL-u (ISO) albo None, gdy brak wzorca."""
    dates: list[str] = []
    for pattern in (_URL_DATE_SLASHED_RE, _URL_DATE_COMPACT_RE):
        for year, month, day in pattern.findall(url):
            dates.append(f"{year}-{month}-{day}")
    for yy, month, day in _URL_DATE_YYMMDD_ID_RE.findall(url):
        dates.append(f"20{yy}-{month}-{day}")
    return max(dates) if dates else None


def url_date_too_old(url: str, match_date: str | None, margin_days: int = 2) -> bool:
    """True, gdy data z URL-a jest WYRAZNIE starsza niz dzien meczu.

    Margines `margin_days` jest celowy: live-blogi powstaja 1-2 dni przed meczem
    i sa aktualizowane po gwizdku (klix tworzyl relacje 2 dni wczesniej) - twardy
    filtr 'przed dniem meczu' wycialby jedyna dobra relacje. Lapiemy za to
    archiwalia w stylu zapowiedzi sparingu sprzed tygodnia.
    """
    if not match_date or not _ISO_DATE_RE.match(match_date):
        return False
    url_date = date_from_url(url)
    if url_date is None:
        return False
    from datetime import date, timedelta

    try:
        threshold = date.fromisoformat(match_date[:10]) - timedelta(days=margin_days)
        return date.fromisoformat(url_date) < threshold
    except ValueError:
        return False


def url_is_prematch(url: str, match_date: str | None, margin_days: int = 1) -> bool:
    """True, gdy data w URL-u jest WYRAZNIE przed dniem meczu (zapowiedz/preview).

    Sluzy do DEPRIORYTETYZACJI (nie odrzucania) w puli media: relacja pomeczowa ma
    date >= dzien meczu, zapowiedz - wczesniejsza. Bez tego zapowiedz wymieniajaca
    obie druzyny (wysoka 'relevance') bila recap w rankingu i na grafice ladowal
    przedmeczowy optymizm zamiast reakcji na wynik. Margines 1 dnia absorbuje granice
    strefy (mecz wieczorny w Amerykach datowany lokalnie o dzien wczesniej). Brak daty
    w URL = nie traktujemy jako przedmeczowy (nie karzemy artykulow bez daty w slugu).
    """
    if not match_date or not _ISO_DATE_RE.match(match_date):
        return False
    url_date = date_from_url(url)
    if url_date is None:
        return False
    from datetime import date, timedelta

    try:
        threshold = date.fromisoformat(match_date[:10]) - timedelta(days=margin_days)
        return date.fromisoformat(url_date) < threshold
    except ValueError:
        return False


def _published_before(published_at: str | None, match_date: str | None) -> bool:
    """True, gdy hit ma date publikacji JAWNIE wczesniejsza niz dzien meczu.

    Brak ktorejkolwiek daty = nie filtrujemy (rozstrzyga scout). Porownanie
    leksykograficzne na prefiksach ISO YYYY-MM-DD; dzien meczu zostaje (reakcje
    pomeczowe wychodza tego samego wieczora).
    """
    if not published_at or not match_date:
        return False
    if not (_ISO_DATE_RE.match(published_at) and _ISO_DATE_RE.match(match_date)):
        return False
    return published_at[:10] < match_date[:10]


def facts_text_from_hit(fetch, hit: SearchHit, label: str, diag: list[str]) -> str | None:
    """Tekst do ekstrakcji faktow: wlasny fetch -> raw_content z indeksu -> tytul+snippet.

    Strony meczowe duzych portali (fifa/uefa/espn gamecast) to aplikacje JS - wlasny
    fetch zwraca pustke, 403 albo timeout. Crawler wyszukiwarki renderuje strone, wiec
    jego tekst jest pelnoprawnym fallbackiem. Snippet wystarcza na sam wynik (full_time);
    strzelcy i tak przechodza anty-fabrykacje wzgledem tego samego tekstu.
    Kazdy kandydat jest sanityzowany; wygrywa pierwszy z sygnalem wyniku.
    """
    candidates: list[tuple[str, str]] = []
    try:
        candidates.append(("fetch", fetch(hit.url)))
    except BudgetExceededError:
        raise
    except Exception as error:  # noqa: BLE001 - fetch to tylko pierwszy kandydat
        diag.append(f"{label}: fetch {hit.url} nieudany ({error}); probuje tresc z indeksu")
    if hit.raw_content:
        candidates.append(("indeks search (raw_content)", hit.raw_content))
    if hit.title or hit.snippet:
        candidates.append(("indeks search (snippet)", f"{hit.title}. {hit.snippet}"))

    for origin, raw in candidates:
        text = sanitize_external_text(raw[:_MAX_FACTS_TEXT])
        if _SCORE_HINT_RE.search(text):
            if origin != "fetch":
                diag.append(f"{label}: {hit.url} -> uzyto tresci z: {origin}")
            return text
    diag.append(f"{label}: {hit.url} bez wyniku w tresci (fetch i indeks)")
    return None


# Markery "to komentarz/analiza, nie sucha relacja" w slugach URL i tytulach
# (PL/EN/ES/CZ/DE + formaty typowe dla prasy sportowej: first-take, oceny pilkarzy).
_OPINION_HINT_TOKENS = (
    "opinion",
    "opinia",
    "opinie",
    "komentarz",
    "komentar",
    "kommentar",
    "comment",
    "commentary",
    "felieton",
    "analysis",
    "analiza",
    "analyza",
    "analisis",
    # PT/ES: 'cronica'/'cronica' (folded) to standardowa kolumna pomeczowa prasy
    # hiszpanskiej i portugalskiej; 'comentario'/'comentario' i 'analise' to ich
    # formaty opinii. Bez nich crónica Marki ('.../cronica/...') wpadala do puli
    # jako "relacja" tylko z przypadku (wysoka trafnosc), a kolumny lokalne malych
    # krajow (CV/PT) nigdy nie dostawaly preferencji opinii.
    "cronica",
    "comentario",
    "comentarios",
    "analise",
    "column",
    "columna",
    "kolumna",
    "editorial",
    "meinung",
    "nazor",
    "sloupek",
    "takeaways",
    "first take",  # match_blob normalizuje myslniki na spacje
    "verdict",
    "ratings",
    "hodnoceni",
    "calificaciones",
)


# Markery "to PRZEGLAD CUDZEJ prasy / digest reakcji", nie wlasny glos redakcji.
# Taki tekst (np. 'imprensa-internacional-rende-se...') streszcza El Pais/CNN/Euronews
# zamiast wlasnej tezy - na slajdzie "jak odebrala to prasa kraju X" czyta sie jak
# zbieranina niepowiazanych cytatow. To DEPRIORYTET (nie odrzut): bez lepszego
# materialu lokalnego digest wciaz moze trafic do puli jako ostatnia deska ratunku.
_PRESS_ROUNDUP_TOKENS = (
    "imprensa internacional",
    "imprensa mundial",
    "prensa internacional",
    "prensa mundial",
    "revista de imprensa",
    "revista de prensa",
    "press review",
    "paper review",
    "world reacts",
    "world reaction",
    "rassegna stampa",
    "przeglad prasy",
    # "co mowia o NAS za granica": tekst wlasnej redakcji, ale CYTUJACY cuda prase
    # (El Pais UY: '...-que-dicen-en-espana-sobre-uruguay' streszczal Marce/AS, nie
    # wnosil tezy redakcji) - na panelu "jak odebrala mecz prasa Urugwaju" czyta sie
    # jak relacja o HISZPANSKICH mediach. 'que dicen en <kraj>' / 'que dice la prensa'
    # / 'o que diz a imprensa' to standardowe ramy takiego digestu w ES/PT. Deprio,
    # nie odrzut (vestuario: 'que dicen en el vestuario' to wlasny glos - ale demote
    # rusza tylko gdy jest lepszy material, wiec falszywy alarm nie kasuje panelu).
    "que dicen en",
    "que dice la prensa",
    "lo que dice la prensa",
    "o que diz a imprensa",
    # PT: rama "co mowia ZA GRANICA" - 'la fora' (= za granica) w slugu digestu
    # ('o-que-se-diz-la-fora-da-vitoria', 'vitoria-vista-la-fora') + zbiorcze
    # 'todas as reacoes'. Regresja run_20260703094439 (Portugalia-Chorwacja):
    # kurator zracjonalizowal digesty record/abola jako 'wartosciowe relacje'
    # i OBA slajdy Portugalii staly na przegladzie CUDZEJ prasy.
    "la fora",
    "o que se diz",
    "que se dice",
    "todas as reacoes",
    "todas las reacciones",
    "imprensa estrangeira",
    "prensa extranjera",
)


# Markery "to ADMINISTRACYJNY brief NASTEPCZY", nie reakcja prasy na PRZEBIEG meczu.
# Awans w rankingu FIFA ('+3 miejsca'), ruch w tabeli itp. to konsekwencja wyniku, nie
# ocena gry - na panelu "jak odebrala mecz prasa kraju X" filgoal '...-تتقدم-في-التصنيف-
# العالمي-3-مراكز' (= "Egipt awansuje o 3 miejsca w rankingu FIFA") czytal sie jak sucha
# notka statystyczna zamiast glosu o meczu i zjadal slot realnej reakcji (Egipt vs NZ:
# panel domykal brief rankingowy zamiast komentarza o historycznym zwyciestwie). Frazy
# wielowyrazowe celowo - samo 'ranking' lapie 'power ranking'/'player ratings' (opinie).
# DEPRIORYTET (nie odrzut): gdy nie ma nic lepszego, brief wciaz moze domknac panel.
_RANKING_BRIEF_TOKENS = (
    "world ranking",
    "world rankings",
    "fifa ranking",
    "fifa rankings",
    "ranking fifa",
    "ranking mundial",
    "clasificacion mundial fifa",
    "ranking mondial",
    "weltrangliste",
    "التصنيف العالمي",  # world ranking (AR)
    "التصنيف الفيفا",  # FIFA ranking (AR)
)


# Markery "to LISTA MEMOW / reakcji z social media", nie reakcja prasy na PRZEBIEG meczu.
# Zestawienie memow albo "tak zareagowaly sieci" (eluniversal '...-avanza-...-y-se-lleva-los-
# mejores-memes') to material ROZRYWKOWY: nie ocenia gry ani nie wnosi tezy redakcji o meczu,
# a na panelu "jak odebrala mecz prasa kraju X" zjada slot realnej relacji ozdobnikiem z sieci
# (Meksyk 2-0 Ekwador, run_20260701122632: drugi slajd stal na liscie memow zamiast na
# cronice/analizie, a jego streszczenie salvage scinal do golego cytatu). 'meme'/'memes' po
# granicy slowa (relacja meczowa nie ma tego w slugu/tytule) + jawne ramy "reacciones en (las)
# redes". DEPRIORYTET (nie odrzut): gdy w puli nie ma nic lepszego, lista wciaz domyka panel.
_SOCIAL_RECAP_TOKENS = (
    "memes",
    "meme",
    "reacciones en redes",
    "reacciones en las redes",
    "reaccionaron las redes",
)


# Czasowniki "pokonac/wyeliminowac KOGOS". W tytule "<X> <verb> <KRAJ>" wlasny kraj stoi
# jako DOPELNIENIE (ofiara), wiec tekst patrzy na mecz z perspektywy ZWYCIEZCY - spotlight
# na jego bohaterach/gwiazdach. Na panelu reakcji prasy PRZEGRANEGO kraju (zamin.uz:
# 'three heroes who defeated Uzbekistan') czyta sie jak hold dla rywala zamiast refleksji
# nad WLASNYM wystepem, a w kolejnosci do max_quotes_per_country (=2) zjada slot realnej
# reakcji (oceny wlasnych zawodnikow, analiza odpadniecia). KLUCZ to KOLEJNOSC: lapiemy
# tylko '<verb> <KRAJ>' (kraj PO czasowniku); '<KRAJ> eliminated'/'<KRAJ> przegral' (kraj
# PRZED) to wlasny post-mortem i NIE jest ruszany. Czasowniki sa pojedynczymi slowami w
# trybie dokonanym - 'beating' nie zmatchuje 'beat' (wymagamy granicy slowa po obu stronach).
_OPPONENT_TRIBUTE_VERB_TOKENS = (
    "defeated",
    "beat",
    "beaten",
    "stunned",
    "downed",
    "sank",
    "ousted",
    "eliminated",
    "dumped",
    "crushed",
    "knocked out",  # match_blob normalizuje '-' na spacje: 'knocked-out' -> 'knocked out'
    # Tylko jezyki, w ktorych nazwa kraju-DOPELNIENIA NIE jest odmieniana ani poprzedzona
    # przyimkiem/rodzajnikiem - inaczej adjacency '<verb> <alias>' i tak nie matchuje:
    # PT 'venceu Portugal' (bez przyimka), DE 'besiegt/schlug Deutschland'. ES ('vencio A
    # Mexico'), FR ('a battu LE Maroc'), IT ('ha battuto IL Belgio') i PL (odmiana:
    # 'pokonali PolskE') celowo POMINIETE - tam heurystyka milczy (pudlo > falszywy alarm).
    "venceu",
    "derrotou",
    "eliminou",
    "besiegt",
    "schlug",
)


def looks_like_opinion(url: str, title: str) -> bool:
    """Heurystyka: artykul wyglada na komentarz/analize, nie sucha relacje meczowa.

    Felieton z teza (np. News24 'first-take', oceny pilkarzy, analizy) niesie cala
    wartosc formatu reakcji prasy; relacje meczowe glownie dubluja wynik, ktory
    i tak stoi na slajdzie tytulowym. Best-effort: brak markera nie dyskwalifikuje.
    """
    blob = match_blob(f"{url} {title}")
    return any(_word_in(token, blob) for token in _OPINION_HINT_TOKENS)


def looks_like_press_roundup(url: str, title: str) -> bool:
    """Heurystyka: artykul to przeglad CUDZEJ prasy (digest), nie wlasny glos redakcji.

    Taki digest streszcza reakcje zagranicznych mediow zamiast wniesc wlasna teze -
    na panelu reakcji prasy danego kraju daje wrazenie "zbieraniny". Best-effort;
    sluzy do deprioretyzacji w puli, nie do odrzutu.
    """
    blob = match_blob(f"{url} {title}")
    return any(_word_in(token, blob) for token in _PRESS_ROUNDUP_TOKENS)


def looks_like_ranking_brief(url: str, title: str) -> bool:
    """Heurystyka: tekst to brief o RANKINGU FIFA / ruchu w tabeli, nie reakcja na mecz.

    Awans w rankingu ('+3 miejsca') jest NASTEPSTWEM wyniku, nie ocena gry - na panelu
    reakcji prasy daje wrazenie suchej notki statystycznej. Best-effort; deprio, nie odrzut.
    """
    blob = match_blob(f"{url} {title}")
    return any(_word_in(token, blob) for token in _RANKING_BRIEF_TOKENS)


def looks_like_social_recap(url: str, title: str) -> bool:
    """Heurystyka: tekst to lista MEMOW / reakcji z social media, nie reakcja na PRZEBIEG meczu.

    Zestawienie memow ('los mejores memes') albo "tak zareagowaly sieci" to material
    rozrywkowy - nie ocenia gry ani nie wnosi tezy redakcji, a na panelu reakcji prasy zjada
    slot realnej relacji. Best-effort; deprio (przez looks_like_non_reaction), nie odrzut.
    """
    blob = match_blob(f"{url} {title}")
    return any(_word_in(token, blob) for token in _SOCIAL_RECAP_TOKENS)


def looks_like_non_reaction(url: str, title: str) -> bool:
    """Tekst, ktory NIE jest reakcja prasy na PRZEBIEG meczu: digest cudzej prasy, administracyjny
    brief nastepczy (ranking FIFA) albo lista memow/reakcji z sieci. Wspolny deprioretyzator toru
    mediow - demote, NIE odrzut, wiec gdy w puli sa SAME takie hity, panel i tak sie domyka."""
    return (
        looks_like_press_roundup(url, title)
        or looks_like_ranking_brief(url, title)
        or looks_like_social_recap(url, title)
    )


# Sekcje SPORTOWE vs OGOLNOINFORMACYJNE w sciezce URL. Reakcja na mecz mieszka w dziale
# sportu; tekst z dzialu krajowego/spoleczno-obyczajowego ('el dia que todos alentaron...'
# pod /nacionales/) bywa o EMOCJACH wokol meczu, ale nie jest relacja redakcji sportowej -
# jego streszczenie lamie kontrakt slajdu (brak materialu meczowego) i salvage scina je do
# golego cytatu. Deprio (nie odrzut): gdy w puli sa lepsze hity z dzialu sportu, ida przed.
_SPORTS_SECTION_SEGMENTS = frozenset({
    "deportes", "deporte", "sport", "sports", "futbol", "fussball", "soccer", "esportes",
    "esporte", "calcio", "fotball", "fotboll", "voetbal", "futebol", "spor",
})
_NONSPORTS_SECTION_SEGMENTS = frozenset({
    "nacionales", "nacional", "mundo", "world", "economia", "economy", "politica",
    "politics", "sociedad", "society", "cultura", "culture", "espectaculos", "farandula",
    "gente", "lifestyle", "vida", "salud", "health", "tecnologia", "ciencia", "negocios",
})


def looks_like_non_sports_section(url: str) -> bool:
    """True, gdy URL siedzi w dziale OGOLNOINFORMACYJNYM (nacionales/mundo/economia...),
    a NIE w dziale sportu. Patrzy na SEGMENTY sciezki (po '/'), nie na slug - 'nacional'
    w slugu 'seleccion-nacional' nie liczy sie, tylko segment '/nacionales/'. Obecnosc
    segmentu sportowego ('/deportes/', '/fussball/') ma pierwszenstwo -> nie deprio."""
    from urllib.parse import urlsplit

    segments = [seg for seg in fold_ascii(urlsplit(url).path).lower().split("/") if seg]
    if any(seg in _SPORTS_SECTION_SEGMENTS for seg in segments):
        return False
    return any(seg in _NONSPORTS_SECTION_SEGMENTS for seg in segments)


def looks_like_opponent_tribute(
    url: str, title: str, own_aliases: tuple[str, ...]
) -> bool:
    """Heurystyka: tekst patrzy na mecz z perspektywy ZWYCIEZCY (rywala), nie wlasnej druzyny.

    Tytul typu 'bohaterowie, ktorzy POKONALI <KRAJ>' stawia wlasny kraj jako DOPELNIENIE
    czasownika porazki - na panelu reakcji prasy tego kraju czyta sie jak hold dla rywala,
    nie refleksja nad wlasnym wystepem. KONTEKSTOWE (potrzebuje aliasow wlasnego kraju): ten
    sam tytul 'heroes who defeated Uzbekistan' to ZNAKOMITA reakcja na panelu DR Konga
    (wlasni bohaterowie) - demotujemy go TYLKO w panelu przegranego. Wymagamy adjacency
    '<verb> <alias>' z granica slowa: wlasny post-mortem ('<KRAJ> eliminated', '<KRAJ>
    przegral' - kraj PRZED czasownikiem) NIE jest lapany. Best-effort; deprio, nie odrzut.
    """
    blob = match_blob(f"{title} {url}")
    alias_blobs = [a for a in (match_blob(name) for name in own_aliases) if len(a) >= 3]
    if not alias_blobs:
        return False
    return any(
        re.search(rf"(?<![a-z0-9]){re.escape(verb)} {re.escape(alias)}(?![a-z0-9])", blob)
        for verb in _OPPONENT_TRIBUTE_VERB_TOKENS
        for alias in alias_blobs
    )


# Scoreline w slugu/tytule ('...-empata-...-0-0-...', 'Cabo Verde 0-0 Espanha').
# match_blob normalizuje myslniki na spacje, wiec '0-0' -> '0 0' (separator spacja).
_URL_SCORE_RE = re.compile(r"(?<!\d)\d{1,2}\s*[-: ]\s*\d{1,2}(?!\d)")


def url_hints_score(url: str, title: str) -> bool:
    """True, gdy slug/tytul zawiera scoreline (np. '0-0', '2:1') po odsianiu dat.

    W torze FAKTOW celem jest wynik: artykul z wynikiem w slugu (relacja pomeczowa)
    ma pierwszenstwo przed zapowiedzia i przegladem prasy, ktore wyniku nie niosa -
    bez tego sygnalu score-less zapowiedzi/digesty zjadaly top-N i autorytatywna
    relacja (futebol-...-empata-...-0-0) nigdy nie byla ekstrahowana. Daty
    (2026-06-15, 20260615, YYMMDD-ID) sa usuwane, zeby '06-15' nie udawalo wyniku.
    """
    cleaned = url
    for pattern in (_URL_DATE_SLASHED_RE, _URL_DATE_COMPACT_RE, _URL_DATE_YYMMDD_ID_RE):
        cleaned = pattern.sub(" ", cleaned)
    blob = match_blob(f"{title} {cleaned}")
    return bool(_URL_SCORE_RE.search(blob))


# Tokeny slugow/tytulow, ktorymi outlet SAM etykietuje gatunek 'relacja z meczu'.
# match_blob normalizuje myslniki/podkreslenia na spacje, wiec 'match-report' -> 'match report'.
_MATCH_REPORT_TOKENS = (
    "spielbericht",  # DE (kicker: .../<slug-ID>/spielbericht; sportschau: 'spielbericht-...')
    "match report",  # EN ('.../germany-paraguay-match-report')
    "matchreport",  # EN zlepione
    "recap",  # EN
    "cronica",  # ES/PT po fold (crónica/crônica = relacja pomeczowa)
    "wedstrijdverslag",  # NL
    "kampreferat",  # NO/DK
    "matchrapport",  # SV
)


def url_hints_match_report(url: str, title: str = "") -> bool:
    """True, gdy slug/tytul WPROST deklaruje relacje pomeczowa (spielbericht/recap/cronica).

    Bramka temporalna (LlmPostMatchGate) istnieje dla tekstow NIEOZNACZONYCH, ktore
    trzeba osadzic po tresci. Tekst, ktory outlet sam etykietuje jako relacje z meczu,
    nie potrzebuje osadu LLM - a osad bywa gorszy od etykiety: bramka odrzucila jako
    'przedmeczowy' spielbericht sportschau (slug '...,spielbericht-deutschland-
    paraguay-100.html', run_20260630222236) i Niemcy stracily najlepsze zrodlo.
    O 'ten mecz vs inny mecz' dbaja filtry wyzej (sekcje / hit_has_match_context /
    date_from_url) - etykieta gatunku rozstrzyga wylacznie os PRZED/PO.
    """
    blob = match_blob(f"{title} {url}")
    return any(_word_in(token, blob) for token in _MATCH_REPORT_TOKENS)


def _parse_score_pair(score: str | None) -> tuple[int, int] | None:
    match = re.fullmatch(r"\s*(\d{1,2})\s*[-:]\s*(\d{1,2})\s*", score or "")
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def slug_mentions_final_score(score: str | None, url: str, title: str = "") -> bool:
    """True, gdy slug/tytul zawiera DOKLADNIE wynik koncowy meczu (obie orientacje).

    Scoreline ROWNY wynikowi koncowemu w slugu to najtwardszy sygnal relacji
    pomeczowej TEGO meczu. Sama obecnosc pary liczb (url_hints_score) nie wystarcza:
    zapowiedz potrafi niesc wynik POPRZEDNIEGO spotkania w slugu. Daty czyscimy jak
    w url_hints_score ('06-15' to nie wynik).
    """
    final = _parse_score_pair(score)
    if final is None:
        return False
    cleaned = url
    for pattern in (_URL_DATE_SLASHED_RE, _URL_DATE_COMPACT_RE, _URL_DATE_YYMMDD_ID_RE):
        cleaned = pattern.sub(" ", cleaned)
    blob = match_blob(f"{title} {cleaned}")
    wanted = {final, (final[1], final[0])}
    for found in re.finditer(r"(?<!\d)(\d{1,2})\s*[-: ]\s*(\d{1,2})(?!\d)", blob):
        if (int(found.group(1)), int(found.group(2))) in wanted:
            return True
    return False


def hit_has_match_context(profile, opponent_profile, url: str, title: str) -> bool:
    """True, gdy hit z SEARCH niesie realny kontekst tego meczu/turnieju.

    Wymienia przeciwnika ALBO wlasna nazwe WRAZ z sygnalem pilkarskim (slowo
    reakcji z szablonu zapytan, lokalny termin MS, rok '2026' albo scoreline).
    Sama nazwa kraju to ZA MALO: tor SEKCJI ma wlasna bramke (collect_section_hits),
    ale tor SEARCH ufal relevancji Tavily w calosci, wiec fuzzy hit z whitelisty
    wchodzil do puli na sam DEMONIM nazwy kraju - zapytanie 'الأردن ...' wciagnelo
    artykul GOSPODARCZY 'Strong demand for Jordanian dinar in local exchange market'
    (token 'jordan' siedzi w 'jordanian'), kurator go wybral i slajd Jordanii mowil
    o kursie dinara zamiast o meczu (run_20260628080846_7bfd06a3, /article/92765).

    Odrzucamy TYLKO waski przypadek: nazwa wlasnej druzyny pada w blobie jako
    PODCIAG dluzszego slowa (demonim 'Jordan' w 'Jordanian', skladowa zlozenia),
    a nie ma zadnego sygnalu pilki. Fail-open wszedzie indziej (lepiej przepuscic
    niz zgubic recap): brak opponent_profile; nazwa wlasna jako PELNE slowo (ufamy
    jak dotad - alias 'Landslaget' w newsie o kadrze to wciaz pilka); brak jakiejkol-
    wiek nazwy (search potrafi zwrocic trafna relacje bez nazwy w tytule/URL).
    """
    if opponent_profile is None:
        return True
    blob = match_blob(f"{title} {url}")
    opp_tokens = tuple(
        fold_ascii(name) for name in opponent_profile.aliases() if len(name) >= 3
    )
    if any(token in blob for token in opp_tokens):
        return True
    own_tokens = tuple(fold_ascii(name) for name in profile.aliases() if len(name) >= 3)
    # Nazwa wlasna jako PELNE slowo = wiarygodna wzmianka (jak dotad - nie ruszamy).
    if any(_word_in(token, blob) for token in own_tokens):
        return True
    # Zadnej nazwy wlasnej w ogole -> fail-open (zawezamy blast radius do demonimu).
    if not any(token in blob for token in own_tokens):
        return True
    # Tu nazwa wlasna pada TYLKO jako podciag (demonim/zlozenie) - typowy falszywy
    # trop fuzzy-matcha Tavily. Wpuszczamy wylacznie z realnym sygnalem pilki:
    # slowa szablonow (reaction/report/analiza) + lokalny world_cup w skrypcie outletu
    # - po granicy slow, by 'world' nie lapalo sie w 'worlds' itp.
    signal_words = {
        fold_ascii(word)
        for template in profile.query_templates
        for word in re.findall(
            r"[A-Za-zÀ-ſ]{4,}",
            template.replace("{team}", " ").replace("{opponent}", " "),
        )
    }
    if profile.world_cup:
        for word in re.split(r"[\s\-_]+", fold_ascii(profile.world_cup)):
            if len(word) >= 2:
                signal_words.add(word)
    if any(_word_in(word, blob) for word in signal_words):
        return True
    if _word_in("2026", blob):
        return True
    return bool(_URL_SCORE_RE.search(blob))


def prefer_opinion_hits(hits: list[SearchHit]) -> list[SearchHit]:
    """Maks 1 sucha relacja przed opiniami: najlepsza relacja -> opinie -> reszta relacji.

    Z puli biora sie pierwsze hity, ktore przejda ekstrakcje (max_quotes_per_country),
    wiec ta kolejnosc daje redakcyjnie najlepszy mix: jedna relacja (fakty, najwyzsza
    trafnosc) + komentarz (teza). Bez opinii w puli kolejnosc trafnosci zostaje.
    """
    opinions = [hit for hit in hits if looks_like_opinion(hit.url, hit.title)]
    if not opinions:
        return list(hits)
    reports = [hit for hit in hits if hit not in opinions]
    return reports[:1] + opinions + reports[1:]


def is_article_url(url: str) -> bool:
    """Odsiewa nie-artykuly: strone glowna, strony tagow/kategorii/sekcji, goly domain.

    Search potrafi zwrocic 'kicker.de?ref=...' albo 'si.com/soccer/germany' (strona
    tagu) - fetch takiego URL-a to strata budzetu, a tresc i tak nie przejdzie
    ekstrakcji cytatu/wyniku. Heurystyka artykulu: ostatni segment sciezki wyglada
    jak slug (ma myslnik albo cyfre). Strony sekcji/hubow koncza sie generycznym
    slowem ('.../deportes/cronica', '.../lig/turkiye-super-ligi/futbol') - to nie
    artykuly, niezaleznie od liczby segmentow.
    """
    from urllib.parse import urlparse

    parsed = urlparse(url)
    # subdomeny bukmacherskie (sportwetten.bild.de, wetten.*, betting.*) publikuja
    # typy i kursy, nie reakcje pomeczowe - blokujemy po hoscie, bo slug typu
    # '.../deutschland-curacao-prognose-...' ma myslniki i cyfry, wiec przechodzi
    # heurystyke artykulu nizej (sportwetten.bild.de/tipps/... zjadlo slot w puli Niemiec)
    host_labels = set((parsed.hostname or "").lower().split("."))
    betting_hosts = {"sportwetten", "wetten", "betting", "odds", "tipps", "tips"}
    if betting_hosts & host_labels:
        return False
    # subdomeny-scoreboardy ('resultater.nrk.no'): cala subdomena to widzety wynikowe
    # (tabela goli/zdarzen meczu), nie proza - trafilatura zwraca pustke, scout 0 cytatow.
    # Link '.../events/2536578' ma w ostatnim segmencie cyfry, wiec przechodzil heurystyke
    # artykulu i marnowal proby fetch+scout (Norwegia-Francja run_20260627213348: 4 strzaly
    # w events 2536578/2534517/2534520/2534524). Recapy NRK leza pod glownym hostem
    # ('nrk.no/fotballvm2026/...', '/sport/i/<kod>/'), nie pod 'resultater.*'.
    scoreboard_hosts = {"resultater"}
    if scoreboard_hosts & host_labels:
        return False
    # strona PAGINACJI/feedu archiwum ('?page=27', '.../index.html?page=859'): to
    # wycinek listy, nie kanoniczny artykul - slug i renderowana tresc sie rozjezdzaja
    # (Curacao Chronicle indeksowal recap 0-0 pod slugiem o omikronie z '?page=27';
    # cytat byl trafny, ale URL jako ZRODLO jest mylacy i niestabilny). Kanoniczny
    # slug (bez page=) przechodzi normalnie.
    from urllib.parse import parse_qs

    if "page" in parse_qs(parsed.query):
        return False
    path = (parsed.path or "").strip("/")
    if len(path) < 6:
        return False
    segments = [segment for segment in path.split("/") if segment]
    # strony wideo/galerii/zakladow/lifestyle'u nie daja reakcji pomeczowych -
    # szkoda na nie budzetu, a zasmiecaly pule hitow (betting na TSN, piosenka
    # kibicowska w sekcji lifestyle Oslobodjenje)
    media_segments = {
        "video",
        "videos",
        "watch",
        "galeria",
        "gallery",
        "fotos",
        "photos",
        "betting",
        "odds",
        # typy/kursy bukmacherskie jako segment sciezki (bild.de/tipps/...,
        # */wetten/...): zapowiedz z prognoza wyniku, nie reakcja po meczu
        "tipps",
        "tips",
        "wetten",
        "podcast",
        "podcasts",
        "lifestyle",
        # strony-LISTY (wyniki wielu meczow): ekstrakcja faktow z takiej strony
        # wyciaga wynik CUDZEGO meczu (fifa.com scores-fixtures dalo 1-0 przy
        # realnym 1-1); pojedynczy mecz ma strone z 'match', nie z listingiem
        "fixtures",
        "scores-fixtures",
        "results",
        "standings",
        "schedule",
        "tabela",
        "terminarz",
        # tureckie strony-listy (fanatik/trtspor): puan-durumu = tabela,
        # fikstur = terminarz - sortuja sie wysoko ('turkiye' w slugu) i zjadaly
        # top-3 korroboracji, wypychajac realne relacje z meczu
        "puan-durumu",
        "fikstur",
        # scoreboard-widzety zdarzen meczu ('resultater.nrk.no/.../events/2536578'):
        # tabela goli/zmian, nie relacja - trafilatura zwraca pustke. Segment 'events'
        # broni tez gdyby widzet trafil sie pod glownym hostem (poza 'resultater.*').
        "events",
        # strony-ARCHIWA (tag/kategoria/rubryka): listingi, nie artykuly
        # (kapitalis.com/tunisie/tag/karim-rekik, .../category/a-la-une) - sortowaly
        # sie wysoko i zjadaly sloty puli zamiast realnej relacji
        "tag",
        "tags",
        "category",
        "categorie",
        "rubrique",
        # interaktywne 'spesial'-huby norweskiej prasy (vg.no/spesial/2026/fotball-vm,
        # nrk.no/spesial/vm-prognose...): JS-aplikacje (dashboard/longread/prognoza), nie
        # prozatorska relacja - scout nie wyciaga z nich cytatu, a slug z myslnikiem
        # (.../fotball-vm) przechodzil heurystyke i KURATOR marnowal na hub jedyny pick
        # (Norwegia-Francja run_20260627212014: 0 cytatow mimo recapu NRK w puli). Recapy
        # leza pod '/fotballvm2026/' i '/sport/i/<kod>/', nie pod '/spesial/'.
        "spesial",
    }
    lowered = [segment.lower() for segment in segments]
    if media_segments & set(lowered):
        return False
    # strony-LISTY moga miec slowa-klucze WEWNATRZ jednego slugu, nie jako osobny
    # segment: FIFA trzyma hub terminarza/wynikow pod '.../articles/match-schedule-
    # fixtures-results-teams-stadiums'. Taki slug z >=2 slowami listingu to indeks
    # wielu meczow, nie relacja - ekstrakcja faktow wyciaga z niego wynik CUDZEGO
    # meczu (dalo 1-1 przy realnym 0-0; straznik score_consistent_with_media zlapal,
    # ale lepiej nie pobierac listy w ogole). Pojedyncze slowo nie blokuje (np.
    # 'germany-results-in-a-draw' to relacja).
    list_words = {"schedule", "fixtures", "results", "standings", "scores", "table"}
    last_slug_words = set((segments[-1] if segments else "").lower().split("-"))
    if len(list_words & last_slug_words) >= 2:
        return False
    # slug zaczynajacy sie od 'video-'/'galeria-' to tez material wideo
    if any(seg.startswith(("video-", "galeria-", "gallery-")) for seg in lowered):
        return False
    last = segments[-1] if segments else ""
    # ostatni segment to goly rok ('.../weltmeisterschaft/2026', kicker
    # '.../oesterreich/info/weltmeisterschaft/2026') = hub sezonu/turnieju, ktory
    # listuje WSZYSTKIE mecze danej reprezentacji - nie relacja z jednego meczu.
    # Bez tego hub przechodzil heurystyke (rok = cyfra) i zjadal cala pule Austrii.
    if re.fullmatch(r"(19|20)\d{2}", last):
        return False
    # strony przedmeczowe/uzytkowe (gdzie ogladac, sklady, typy) NIE sa reakcja po
    # meczu - lokalna prasa publikuje je tuz przed gwizdkiem i sortowaly sie wysoko
    # (wymieniaja obie druzyny), wypychajac recap. NIE blokujemy 'live'/'liveblog'
    # (live-blog bywa wlasnie recapem po aktualizacji) - tylko jawnie przedmeczowe slugi
    prematch_markers = (
        "live-streaming",
        "live-stream",
        "ou-suivre",
        "ou-regarder",
        "comment-suivre",
        "comment-regarder",
        "where-to-watch",
        "how-to-watch",
        "composition-probable",
        "compositions-probables",
        "avant-match",
        "billetterie",
        "pronostic",
    )
    if any(marker in last for marker in prematch_markers):
        return False
    # digest WIELU meczow ('round-up'/'wrap-up'): jeden artykul streszcza kilka
    # spotkan naraz, wiec ekstrakcja wyniku wyciaga scoreline CUDZEGO meczu. FIFA
    # trzyma dzienne podsumowania pod '.../articles/congo-england-ghana-round-up-
    # review-highlights' (3 reprezentacje w slugu!) - taki slug przechodzil heurystyke
    # (myslniki+slowa), a OfficialMatchApi wyciagnal z niego 0-1 dla Portugalia-DR Konga.
    # Marker 'round-up'/'roundup'/'wrap-up' wystepuje TYLKO w digestach - relacja z
    # JEDNEGO meczu nazywa sie 'england-croatia-highlights-match-report' (bez round-up),
    # wiec single-match recapy zostaja nietkniete.
    digest_markers = ("round-up", "roundup", "wrap-up", "wrapup")
    if any(marker in last for marker in digest_markers):
        return False
    # interaktywne zabawy 'ulóż swoją XI' (ole.com.ar/arma-tu-equipo-argentina,
    # .../arma-equipo-seleccion-argentina-semis-...): widzet-gra, nie reakcja prasy.
    # Jednosegmentowy hub ma >=2 myslniki, wiec przechodzil heurystyke slugu i jako
    # ostatnia deska ratunku wszedl do panelu z cytatem-smieciem
    # (Argentyna-Szwajcaria run_20260713060703). Marker 'arma-*-equipo' wystepuje
    # tylko w tych zabawach - crónica nigdy tak nie zaczyna slugu.
    interactive_markers = ("arma-tu-equipo", "arma-equipo", "arma-el-equipo")
    if any(marker in last for marker in interactive_markers):
        return False
    if len(segments) == 1:
        # jednosegmentowa sciezka: artykul ma dlugi wielowyrazowy slug
        # ('usa-deutschland-testspiel-bericht') albo ID/date; strona sekcji ma
        # jeden myslnik i zero cyfr ('tsn.ca/hockey-canada')
        return last.count("-") >= 2 or any(ch.isdigit() for ch in last)
    # >=2 segmentow: ostatni segment musi wygladac jak slug artykulu (myslnik albo
    # cyfra). Hub/sekcja konczy sie generycznym slowem ('.../futbol', '.../cronica')
    # nawet przy 3+ segmentach - taki URL nie daje reakcji pomeczowej.
    if "-" in last or any(ch.isdigit() for ch in last):
        return True
    # ALBO: ostatni segment to typ pod-widoku ('spielbericht', 'analyse', 'ticker',
    # 'artikel'), a PRZEDOSTATNI to bogaty slug z osadzonym ID - kicker trzyma relacje
    # pod '.../oesterreich-gegen-jordanien-2026-weltmeisterschaft-5179704/spielbericht'.
    # Sam ostatni segment jest tu generyczny, wiec stara heurystyka odrzucala recap;
    # hub/sekcja ma w tym miejscu generyczny przedostatni segment ('deportes',
    # 'nationalmannschaft') bez myslnikow i cyfr, wiec dalej nie przechodzi.
    prev = segments[-2]
    return prev.count("-") >= 2 and any(ch.isdigit() for ch in prev)


def canonical_country(registry: SourceRegistry, name: str) -> str:
    """Nazwa kanoniczna z rejestru (np. 'Germany' -> 'Niemcy'); bez profilu zostaje oryginal."""
    profile = registry.match_country(name)
    return profile.country if profile is not None else name


def expected_countries(registry: SourceRegistry, query: str) -> frozenset[str]:
    """Kraje, ktorych dotyczy zapytanie usera (kanonicznie). Pusty set = nie wykryto dwoch."""
    profiles = registry.countries_in_text(query)
    if len(profiles) < 2:
        return frozenset()
    return frozenset(profile.country for profile in profiles[:2])


def _date_gap_days(a: str, b: str) -> int | None:
    """Bezwzgledna roznica dni miedzy dwiema datami ISO (prefiks YYYY-MM-DD), albo
    None gdy ktorejs nie da sie sparsowac (wtedy wolajacy traktuje to jak niezgodnosc)."""
    if not (_ISO_DATE_RE.match(a) and _ISO_DATE_RE.match(b)):
        return None
    from datetime import date

    try:
        return abs((date.fromisoformat(a[:10]) - date.fromisoformat(b[:10])).days)
    except ValueError:
        return None


def draft_mismatch(
    registry: SourceRegistry,
    draft,
    expected: frozenset[str],
    date_hint: str | None,
) -> str | None:
    """Strażnik 'inny mecz': zwraca opis niezgodnosci albo None gdy draft pasuje.

    Chroni przed ekstrakcja faktow z relacji innego spotkania tych samych (lub innych)
    druzyn - np. archiwalnego meczu mlodziezowego znalezionego przez search.
    """
    if expected:
        got = frozenset(
            {
                canonical_country(registry, draft.home_team),
                canonical_country(registry, draft.away_team),
            }
        )
        if got != expected:
            return f"druzyny {draft.home_team} vs {draft.away_team}, oczekiwano {sorted(expected)}"
    if date_hint and draft.date and draft.date != date_hint:
        # Mecze wieczorne w obu Amerykach (UTC-6..-8) wypadaja nastepnego dnia w
        # UTC/PL: FIFA i prasa datuja je LOKALNIE (Sweden-Tunisia w Monterrey:
        # '2026-06-14'), a terminarz/date_hint bywa o dobe dalej ('2026-06-15').
        # Twardy '!=' odrzucal wtedy autorytatywny raport jako 'inny mecz' i halt
        # konczyl sie match_not_found_live. Tolerujemy +-1 dzien (granica strefy);
        # wieksza roznica = realnie inny mecz (archiwalny sparing sprzed dni).
        gap = _date_gap_days(draft.date, date_hint)
        if gap is None or gap > 1:
            return f"data {draft.date}, oczekiwano {date_hint}"
    return None


def team_name_variants(profile, fallback: str) -> list[str]:
    """Nazwy druzyny do zapytan, najtrafniejsze pierwsze: przydomek -> lokalna -> EN.

    Konwencja danych w team_names: [0]=kanoniczna (polska, dobra dla rejestru, zla
    dla searchu), [1]=lokalna pelna, [2+]=przydomki. Przydomek (USMNT, El Tri,
    DFB-Team) to zargon, ktorym lokalne media NAZYWAJA WLASNA kadre - empirycznie
    najskuteczniejszy klucz wyszukiwania relacji pomeczowych. Bierzemy WSZYSTKIE
    przydomki [2:], nie tylko pierwszy: lokalny egzonim bywa drugim przydomkiem
    (Curacao: 'The Blue Wave' EN + 'Korsou' papiamento) i to wlasnie nim prasa
    tytuluje recap ('Eerste WK-goal Korsou!') - z samym [2] nigdy nie trafia do puli.
    """
    if profile is None:
        return [fallback]
    team_names = list(profile.team_names)
    ordered: list[str] = []
    ordered.extend(team_names[2:])  # przydomki/egzonimy lokalne (moze byc kilka)
    if len(team_names) > 1:
        ordered.append(team_names[1])  # lokalna pelna
    if profile.english_name:
        ordered.append(profile.english_name)
    if not ordered and team_names:
        ordered.append(team_names[0])
    deduped: list[str] = []
    for name in ordered:
        if name not in deduped:
            deduped.append(name)
    return deduped[:3] or [fallback]


def local_media_queries(profile, opponent_profile, opponent_country: str) -> list[str]:
    """Zapytania pod LOKALNA prase: warianty nazwy + recap/report + '{team} {world_cup}'.

    Wspolne dla toru medialnego (reakcje) i toru faktow (korroboracja wyniku): relacja
    pomeczowa lokalnej prasy niesie i cytat, i wynik, wiec ten sam zestaw zapytan dziala
    dla obu. Data ISO jest CELOWO pomijana w tresci zapytania - empirycznie psuje ranking
    wyszukiwarki; swiezosc daje time_range w adapterze search. Przeciwnik po angielsku
    (tak pisza o nim cudze media), a lokalny termin '{team} {world_cup}' zastepuje nazwe
    przeciwnika jako kotwica swiezosci tam, gdzie prasa nazywa rywala po swojemu.
    """
    team_variants = team_name_variants(profile, profile.country)
    opp_variants = (
        team_name_variants(opponent_profile, opponent_country)
        if opponent_profile is not None
        else [opponent_country]
    )
    opp_en = (
        opponent_profile.english_name
        if opponent_profile is not None and opponent_profile.english_name
        else opponent_country
    )
    # Egzonim przeciwnika w JEZYKU prasy lidera ('Suiza' dla prasy es) idzie PRZED
    # wlasne przydomki przeciwnika: Ole/Infobae nigdy nie pisza 'Schweiz'/'Nati',
    # wiec bez egzonimu zapytania z przeciwnikiem nie dosiegaly cronik
    # (Argentyna-Szwajcaria run_20260713060703: 8 zapytan, zero relacji z meczu).
    opp_exonym = (
        opponent_profile.exonym_for(profile.language)
        if opponent_profile is not None
        else None
    )
    if opp_exonym:
        # egzonim wypiera najslabszy przydomek (cap 3 wariantow jak dotad), zeby
        # ogolne queries[:8] nie zjadlo lokalnych templatow ('cronica {team} {opponent}')
        opp_variants = [opp_exonym] + [o for o in opp_variants if o != opp_exonym][:2]
    lead = team_variants[0]
    queries: list[str] = [
        # klasyczne formaty relacji pomeczowych ida PIERWSZE (empirycznie trafiaja
        # w recap/report/takeaways); templaty z danych dorzucaja warianty lokalne
        f"{lead} {opp_en} recap",
        f"{lead} vs {opp_en} match report",
    ]
    # Bezprzeciwnikowe zapytanie lokalne '{team} {world_cup}' (np. 'Korsou WK 2026',
    # 'DFB-Team WM 2026'). Anglocentryczne '{team} {Germany}' nie dosiega recapow
    # lokalnej prasy, ktora przeciwnika nazywa po swojemu (Duitsland/Alemania/Nemecko).
    wc_term = (profile.world_cup or "").strip()
    if wc_term:
        # [2] = czolowy anchor wlasnej druzyny (kontrakt: bezprzeciwnikowa kotwica swiezosci).
        queries.append(f"{lead} {wc_term}")
        # '{team} {world_cup}' SAM nie rozroznia, ktory mecz grupy to recap - druzyna gra
        # 3 spotkania, a 'Coupe du monde 2026' pasuje do wszystkich (flood starych relacji,
        # recap nie wchodzi w top-5). Egzonim przeciwnika w JEZYKU lokalnej prasy
        # ('Pays-Bas'/'Hollande', NIE 'Netherlands') wynosi wlasciwa relacje na #1.
        # opp_variants niesie te egzonimy (ta sama mechanika, co dla wlasnej druzyny).
        for opp in opp_variants:
            opp_query = f"{lead} {opp} {wc_term}"
            if opp_query not in queries:
                queries.append(opp_query)
        # drugi wariant wlasnej nazwy (lokalna pelna) jako dodatkowa kotwica
        for team in team_variants[1:2]:
            local_query = f"{team} {wc_term}"
            if local_query not in queries:
                queries.append(local_query)
    # templaty sa w JEZYKU lokalnej prasy ('cronica {team} {opponent}'), wiec
    # przeciwnik tez po lokalnemu, gdy znamy egzonim ('cronica Albiceleste Suiza')
    opp_for_templates = opp_exonym or opp_en
    for template in profile.query_templates:
        for team in team_variants:
            try:
                built = template.format(team=team, opponent=opp_for_templates)
            except (KeyError, IndexError):
                continue
            if built not in queries:
                queries.append(built)
    return queries[:8]


@dataclass
class MediaResearchProvider:
    """Live media: profil kraju -> zapytania -> search(whitelist) -> fetch -> scout."""

    registry: SourceRegistry
    search_client: SearchClient
    fetcher: PageFetcher
    scout: MediaScout
    budget: BudgetTracker
    cache: Cache | None = None
    # Kurator (LLM): jezykowo-agnostyczny wybor najlepszych ROZNYCH reakcji wlasnej prasy
    # z puli; gdy None lub gdy padnie -> fallback do heurystyk (prefer_opinion_hits).
    curator: MediaCurator | None = None
    # Bramka temporalna (LLM): binarne 'czy to reakcja na JUZ ROZEGRANY mecz?' PRZED
    # ekstrakcja cytatu. Lapie zapowiedzi/inny mecz z outletow bez daty (filtry daty
    # bezradne), ktore zwodza scouta dramatycznym leadem. None -> bramka wylaczona
    # (offline/fixture/testy bez zmian zachowania); awaria bramki -> fail-open.
    recency_gate: PostMatchGate | None = None
    # telemetria epizodow (etap 1, architektura-pamiec-epizodyczna.md): zdarzenia
    # fetch/sekcji do run.json. None = brak zapisu (testy/offline bez zmian);
    # telemetria NIGDY nie wplywa na zachowanie retrievalu.
    telemetry: RunTelemetry | None = None
    # zdrowie zrodel (etap 3): kolejnosc sekcji w budzecie + pomijanie martwego
    # fetchu (botblock-streak z raw_content). None / pusty magazyn / awaria =
    # zachowanie identyczne jak bez pamieci (cold-start parity).
    health: SourceHealth | None = None
    search_limit: int = 5
    # 8, nie 4: swieze sekcje po meczu zalewaja pule zapowiedziami/live-blogami,
    # a wlasciwa relacja/felieton bywa nizej w rankingu; scout i tak odrzuca
    # nietrafione (petla konczy po max_quotes), wiec wieksza pula = odpornosc,
    # nie staly koszt
    max_articles_per_country: int = 8
    max_quotes_per_country: int = 2
    # 4, nie 3: kraje z paywallowym/JS-wall outletem (Portugalia: record.pt trzyma
    # prozaiczne crónica/notas za platnym murem, fetch widzi sama nawigacje ~955 zn.)
    # potrzebuja DRUGIEGO darmowego, fetchowalnego outletu obok glownych dwoch, by
    # panel nie schodzil do suchej statystyki. Pulap = laczny budzet sekcji na kraj.
    max_sections_per_country: int = 4
    cache_ttl_seconds: int = 600

    def research(
        self,
        context: MatchContext,
        country: str,
        notes: list[str] | None = None,
    ) -> list[RawMediaItem]:
        diag = notes if notes is not None else []
        label = f"media[{country}]"
        profile = self.registry.country_profile(country)
        if profile is None or not profile.outlets:
            diag.append(f"{label}: brak profilu/outletow w rejestrze")
            return []
        allowed_domains = tuple(
            sorted({domain for outlet in profile.outlets for domain in outlet.descriptor.domains})
        )
        if not allowed_domains:
            diag.append(f"{label}: outlety bez domen")
            return []

        opponent_profile = self.registry.country_profile(context.opponent_of(country))
        queries = self._build_queries(
            profile, opponent_profile, context.opponent_of(country), context.date
        )
        # sekcje redakcji = najswiezsze artykuly, niezaleznie od lagu indeksu search
        section_hits = self._section_hits(profile, opponent_profile, diag, label)
        hits = self._collect_hits(
            queries,
            allowed_domains,
            profile,
            opponent_profile,
            match_date=context.date,
            extra_hits=section_hits,
            diag=diag,
            label=label,
        )
        diag.append(f"{label}: zapytania {queries} -> {len(hits)} hitow po filtrach")
        picks = self._select_candidates(context, country, hits, diag, label)
        items = self._extract_items(context, country, profile, picks, diag, label)
        if not items and self.curator is not None:
            items = self._recurate_after_dead_picks(
                context, country, profile, hits, picks, diag, label
            )
        diag.append(f"{label}: {len(items)} cytatow")
        return items

    def _recurate_after_dead_picks(
        self,
        context: MatchContext,
        country: str,
        profile,
        pool: list[SearchHit],
        picks: list[SearchHit],
        diag: list[str],
        label: str,
    ) -> list[RawMediaItem]:
        """Ratunek, gdy picki kuratora nie daly ZADNEJ tresci (fetch padl / brak
        raw_content - np. jedyny wybor to outlet, ktory timeoutuje). NIE zsuwamy sie
        na slepo do odrzuconej reszty (to byłyby smieci: zapowiedzi/digesty/inny mecz,
        ktore kurator wlasnie odrzucil) - pytamy KURATORA ponownie o reszte puli.
        Dedup kuratora jest WZGLEDNY: sibling odrzucony jako duplikat martwego picku
        jest teraz pelnoprawnym wyborem. Gdy z reszty nic nie przechodzi progu -> []
        i kraj zostaje pusty (zwloka/retry), bo lepiej ODMOWIC niz wrzucic smiec.
        """
        rest = [hit for hit in pool if hit not in picks]
        if not rest:
            return []
        try:
            backup = self.curator.select(context, country, rest, notes=diag)
        except (GenerationError, ModelError, ValueError) as error:
            diag.append(f"{label}: re-kuracja nieudana ({error})")
            return []
        if not backup:
            diag.append(f"{label}: re-kuracja nic nie wybrala - kraj pusty (zwloka/retry)")
            return []
        diag.append(
            f"{label}: picki kuratora bez tresci; re-kuracja wybrala "
            f"{len(backup)}/{len(rest)} z reszty puli"
        )
        return self._extract_items(context, country, profile, backup, diag, label)

    def _passes_recency_gate(
        self,
        context: MatchContext,
        url: str,
        title: str,
        candidates: list[str],
        diag: list[str],
        label: str,
    ) -> bool:
        """True = artykul wolno ekstrahowac (reakcja pomeczowa albo bramka wylaczona).

        Deterministyczny bypass PRZED osadem LLM: slug etykietowany jako relacja
        pomeczowa (spielbericht/recap/cronica) albo wynik koncowy meczu w slugu nie
        potrzebuja bramki - to twardsze sygnaly niz osad bez walidatora, ktory takie
        teksty potrafil false-rejectowac (sportschau spielbericht, run_20260630222236).

        Bramke odpalamy na NAJPELNIEJSZYM dostepnym tekscie (najlepszy material na osad
        ramy czasowej). Fail-open: bramka None, brak uzytecznego tekstu albo awaria LLM ->
        przepuszczamy (decyduje scout); blokujemy TYLKO gdy bramka PEWNIE mowi 'nie'."""
        if self.recency_gate is None:
            return True
        if url_hints_match_report(url, title):
            diag.append(
                f"{label}: {url} bramka temporalna: pominieta "
                "(slug deklaruje relacje pomeczowa)"
            )
            return True
        if slug_mentions_final_score(context.score, url, title):
            diag.append(
                f"{label}: {url} bramka temporalna: pominieta "
                f"(wynik koncowy {context.score} w slugu)"
            )
            return True
        gate_text = ""
        for candidate in candidates:
            safe_text = sanitize_external_text(candidate, max_len=_MAX_SCOUT_TEXT)
            if safe_text.strip():
                gate_text = safe_text
                break
        if not gate_text:
            return True
        try:
            if self.recency_gate.is_post_match_reaction(context, url, gate_text):
                return True
        except Exception as error:  # noqa: BLE001 - awaria bramki nie moze topic kraju
            diag.append(f"{label}: {url} bramka temporalna nieudana ({error}); przepuszczam")
            return True
        diag.append(f"{label}: {url} bramka temporalna: przedmeczowy/inny mecz - pomijam")
        return False

    def _extract_items(
        self,
        context: MatchContext,
        country: str,
        profile,
        hits: list[SearchHit],
        diag: list[str],
        label: str,
    ) -> list[RawMediaItem]:
        """Fetch -> sanitize -> scout po wybranych hitach; 1 cytat na artykul, cap per kraj.

        Pelne relacje maja pierwszenstwo przed cienkimi 'flash'/'alerte' stubami: stub
        daje cytat, ale bez tresci na streszczenie slajd schodzi do samego cytatu. Cienkie
        odkladamy i uzywamy DOPIERO gdy zabraknie pelniejszych (kraj nie moze zostac pusty).
        """
        items: list[RawMediaItem] = []
        thin: list[RawMediaItem] = []
        gate_rejected: list[tuple[SearchHit, object, list[str]]] = []
        for hit in hits:
            if len(items) >= self.max_quotes_per_country:
                break
            descriptor = self.registry.outlet_for_url(country, hit.url)
            if descriptor is None:
                continue
            # Kandydaci tresci: wlasny fetch + pelny tekst z crawlera search. "Udany"
            # fetch bywa sciana antybotowa ("enable JavaScript...") - krotka, ale
            # niepusta - dlatego probujemy od NAJDLUZSZEGO kandydata, a przy zerze
            # cytatow przechodzimy do nastepnego. Snippetow nie uzywamy: cytat musi
            # byc doslowny, a snippet bywa uciety.
            candidates: list[str] = []
            # etap 3 (raw_content-first): outlet ze SWIEZYM streakiem botblock nie
            # dostaje proby fetchu, o ile indeks przyniosl raw_content (oszczedzamy
            # timeout + budzet; kicker: 15 s na artykul). BEZ raw_content fetch idzie
            # mimo streaka - dostepnosc tresci zawsze wygrywa z optymalizacja. Brak
            # proby = brak zdarzenia, wiec obraz zdrowia starzeje sie i po
            # RE_PROBE_HOURS nastapi normalna proba (re-probe).
            if hit.raw_content and self._fetch_dead(descriptor.provider_id):
                diag.append(
                    f"{label}: {hit.url} fetch pominiety (outlet botblock wg "
                    "zdrowia; ide na raw_content)"
                )
            else:
                try:
                    fetched = self._fetch(hit.url)
                    candidates.append(fetched)
                    self._emit_fetch(country, descriptor, hit, body_len=len(fetched.strip()))
                except BudgetExceededError:
                    continue
                except ResearchError as error:
                    diag.append(f"{label}: {hit.url} fetch nieudany ({error})")
                    self._emit_fetch(country, descriptor, hit, error=error)
            if hit.raw_content:
                candidates.append(hit.raw_content[:_MAX_FACTS_TEXT])
            candidates.sort(key=len, reverse=True)

            # Bramka temporalna PRZED ekstrakcja: czy to reakcja na ROZEGRANY mecz?
            # Lapie zapowiedzi/inny mecz z outletow bez daty, ktore filtry daty
            # przepuszczaja, a scout (tryb 'znajdz emocje') bierze za reakcje.
            # Odrzuty odkladamy: false-reject na jedynym kandydacie nie moze
            # zostawic kraju pustego (patrz _resurrect_gate_rejected).
            if not self._passes_recency_gate(
                context, hit.url, hit.title or "", candidates, diag, label
            ):
                gate_rejected.append((hit, descriptor, candidates))
                continue

            item, body_len = self._scout_item(
                context, country, profile, hit, descriptor, candidates, diag, label
            )
            if item is None:
                continue
            if body_len >= _MIN_ARTICLE_BODY_FOR_SLIDE:
                items.append(item)
            else:
                diag.append(
                    f"{label}: {hit.url} cienki material ({body_len} zn., "
                    "np. flash/alerte) - odlozony, szukam pelniejszego zrodla"
                )
                thin.append(item)
        # uzupelnij brakujace sloty cienkim materialem (lepiej cytat-stub niz pusty kraj)
        for item in thin:
            if len(items) >= self.max_quotes_per_country:
                break
            items.append(item)
        if not items and gate_rejected:
            items.extend(
                self._resurrect_gate_rejected(
                    context, country, profile, gate_rejected, diag, label
                )
            )
        return items

    def _scout_item(
        self,
        context: MatchContext,
        country: str,
        profile,
        hit: SearchHit,
        descriptor,
        candidates: list[str],
        diag: list[str],
        label: str,
    ) -> tuple[RawMediaItem | None, int]:
        """Ekstrakcja cytatu z JEDNEGO artykulu; zwraca (item|None, dlugosc uzytej tresci).

        Dlugosc tresci pozwala wolajacemu odroznic pelna relacje od cienkiego stuba
        (flash/alerte) - patrz _MIN_ARTICLE_BODY_FOR_SLIDE."""
        language = descriptor.language or profile.language
        # 1 cytat na ARTYKUL (slajd = streszczenie artykulu z cytatem); dwa slajdy
        # z tego samego tekstu to redundancja, ktora juz raz poszla na produkcje
        fragments: list[str] = []
        used_text = ""
        for candidate in candidates:
            safe_text = sanitize_external_text(candidate, max_len=_MAX_SCOUT_TEXT)
            if not safe_text.strip():
                continue
            try:
                fragments = self.scout.extract(
                    context,
                    descriptor.provider_id,
                    language,
                    hit.url,
                    safe_text,
                    max_fragments=1,
                )
            except Exception as error:  # noqa: BLE001 - zly tekst nie wywala kraju
                diag.append(f"{label}: {hit.url} ekstrakcja odrzucona ({error})")
                continue
            if fragments:
                used_text = safe_text
                break
        if not fragments:
            diag.append(
                f"{label}: {hit.url} scout: 0 cytatow "
                f"(kandydatow tresci: {len(candidates)})"
            )
            return None, 0
        fragment = fragments[0]
        # tytul z indeksu/anchora sekcji: sanityzowany jak kazda tresc z sieci
        safe_title = sanitize_external_text(hit.title).strip() if hit.title else ""
        item = RawMediaItem(
            evidence_id=_mint("e", descriptor.provider_id, hit.url, fragment),
            outlet=descriptor.provider_id,
            country=country,
            language=language,
            url=hit.url,
            original_text=fragment,
            tier=descriptor.tier,
            retrieved_at=_now_iso(),
            translation_pl=None,
            confidence="high",
            article_text=used_text[:_MAX_ARTICLE_FOR_SUMMARY],
            title=safe_title or None,
        )
        return item, len(used_text.strip())

    def _resurrect_gate_rejected(
        self,
        context: MatchContext,
        country: str,
        profile,
        rejected: list[tuple[SearchHit, object, list[str]]],
        diag: list[str],
        label: str,
    ) -> list[RawMediaItem]:
        """Ostatnia linia obrony przed pustym krajem po bramce temporalnej.

        False-reject bramki na jedynych kandydatach topil caly run haltem
        one_country_media_missing (run_20260630222236: ultimahora 'la-albirroja-
        clasificada-a-los-dieciseisavos' odrzucony jako 'przedmeczowy', Paragwaj
        pusty). Filozofia bramki mowi 'awaria bramki nie moze topic kraju' -
        false-reject to funkcjonalnie ta sama awaria, tylko niewidoczna w wyjatku.

        Przywracamy WYLACZNIE z twarda korroboracja: tekst wymienia wynik koncowy
        meczu (zapowiedz go nie zna, bo mecz jeszcze sie nie odbyl) - deterministyczny
        walidator zamiast drugiej, rownie stochastycznej opinii LLM. mentioned_scores
        pomija wyniki do przerwy i karnych, wiec korroboruje wynik KONCOWY. Bez
        wyniku w kontekscie nie przywracamy nic (status quo: kraj pusty -> halt)."""
        final = _parse_score_pair(context.score)
        if final is None:
            return []
        wanted = {final, (final[1], final[0])}
        for hit, descriptor, candidates in rejected:
            text = ""
            for candidate in candidates:
                safe_text = sanitize_external_text(candidate, max_len=_MAX_SCOUT_TEXT)
                if safe_text.strip():
                    text = safe_text
                    break
            scores = {(int(a), int(b)) for a, b in mentioned_scores(text)} if text else set()
            if not (scores & wanted):
                continue
            item, _ = self._scout_item(
                context, country, profile, hit, descriptor, candidates, diag, label
            )
            if item is not None:
                diag.append(
                    f"{label}: bramka temporalna odrzucila wszystkich kandydatow; "
                    f"przywracam {hit.url} (tekst wymienia wynik koncowy {context.score})"
                )
                return [item]
        return []

    def _select_candidates(
        self,
        context: MatchContext,
        country: str,
        hits: list[SearchHit],
        diag: list[str],
        label: str,
    ) -> list[SearchHit]:
        """Wybor kandydatow do ekstrakcji: kurator LLM (jezykowo-agnostyczny) -> fallback.

        Kurator widzi cala pule (tytul+snippet+URL) i rozumuje semantycznie (wlasny glos vs
        digest cudzej prasy, rozne ujecia, inny mecz/zapowiedz) - zastepuje kruche heurystyki
        jezykowe. Gdy kuratora brak / padnie / nic nie wybierze -> heurystyki jako bezpiecznik.
        """
        if not hits or self.curator is None:
            return self._heuristic_order(hits, diag, label)
        try:
            chosen = self.curator.select(context, country, hits, notes=diag)
        except (GenerationError, ModelError, ValueError) as error:
            diag.append(f"{label}: kurator nieudany ({error}); fallback do heurystyk")
            return self._heuristic_order(hits, diag, label)
        if chosen:
            own_profile = self.registry.country_profile(country)
            own_aliases = own_profile.aliases() if own_profile is not None else (country,)
            chosen = self._demote_roundups(chosen, diag, label, own_aliases)
            diag.append(f"{label}: kurator wybral {len(chosen)}/{len(hits)} kandydatow")
            return self._rescue_own_voice(chosen, hits, diag, label, own_aliases)
        diag.append(f"{label}: kurator nic nie wybral; fallback do heurystyk")
        return self._heuristic_order(hits, diag, label)

    @staticmethod
    def _is_non_own_reaction(hit: SearchHit, own_aliases: tuple[str, ...]) -> bool:
        """Material != wlasny glos redakcji o meczu: digest cudzej prasy, brief
        rankingowy, lista memow, hold dla rywala albo dzial nie-sportowy."""
        return (
            looks_like_non_reaction(hit.url, hit.title)
            or looks_like_opponent_tribute(hit.url, hit.title, own_aliases)
            or looks_like_non_sports_section(hit.url)
        )

    def _rescue_own_voice(
        self,
        chosen: list[SearchHit],
        hits: list[SearchHit],
        diag: list[str],
        label: str,
        own_aliases: tuple[str, ...] = (),
    ) -> list[SearchHit]:
        """Dosypka z PULI, gdy kuracja zostawia na czole mniej realnych reakcji niz
        slotow panelu (max_quotes_per_country).

        Demote w obrebie pickow nie pomaga, gdy kurator wybral SAME digesty - a robi
        to regularnie, bo je racjonalizuje ('o que se diz la fora' opisane jako
        'wartosciowa relacja'): w run_20260703094439 (Portugalia-Chorwacja) OBA slajdy
        Portugalii staly na przegladach CUDZEJ prasy, mimo ze sekcja record.pt dala
        26 linkow wlasnych relacji. Kandydaci spoza wyboru kuratora ida PO realnych
        pickach, a PRZED materialami nie-reakcyjnymi: ekstrakcja (cap max_quotes,
        po kolejnosci) dobiera wtedy wlasny glos zamiast digestu. Gdy pula nie ma
        nic lepszego, digesty jak dotad domykaja panel (dosypka niczego nie usuwa).

        KONTRAKT: odrzuty kuratora sa respektowane, gdy jego wybor sklada sie z
        realnych reakcji - dosypka rusza WYLACZNIE w miejsce slotow zapelnionych
        materialami nie-reakcyjnymi (tam osad kuratora ewidentnie zawiodl). Bez
        tego warunku pool-rejects kuratora (inny mecz, zapowiedz) wracalyby do
        fetch/ekstrakcji w kazdym runie z niepelnym wyborem.
        """
        good = [h for h in chosen if not self._is_non_own_reaction(h, own_aliases)]
        if len(good) == len(chosen):
            # kurator nie wpuscil zadnego digestu - jego odrzutom ufamy w calosci
            return chosen
        missing = self.max_quotes_per_country - len(good)
        if missing <= 0:
            return chosen
        extras = [
            h
            for h in hits
            if h not in chosen and not self._is_non_own_reaction(h, own_aliases)
        ][:missing]
        if not extras:
            return chosen
        demoted = [h for h in chosen if h not in good]
        diag.append(
            f"{label}: kuracja zostawila {len(good)} realnych reakcji na "
            f"{self.max_quotes_per_country} slotow; dokladam {len(extras)} "
            "kandydatow z puli przed materialy nie-reakcyjne"
        )
        return good + extras + demoted

    @staticmethod
    def _demote_roundups(
        chosen: list[SearchHit],
        diag: list[str],
        label: str,
        own_aliases: tuple[str, ...] = (),
    ) -> list[SearchHit]:
        """Po kuracji: materialy != wlasny glos o meczu na KONIEC wyboru, gdy jest w nim glos.

        Kurator (semantyczny) potrafi wpuscic na czolo: (a) digest 'co mowia o nas za
        granica', (b) brief rankingowy, (c) hold dla rywala ('bohaterowie, ktorzy pokonali
        <KRAJ>') - a ekstrakcja bierze picki W KOLEJNOSCI do max_quotes_per_country (=2),
        wiec taki tekst na czole zjadal slot realnej reakcji wlasnej redakcji (Urugwaj 2-2:
        digest Marki/AS zamiast prasy UY; Uzbekistan 1-3 DR Kongo: 'three heroes who
        defeated Uzbekistan' zamiast ocen wlasnych zawodnikow). Demote, NIE odrzut: gdy w
        wyborze sa SAME takie materialy, zostaja (lepszego nie ma) - pula ratuje wtedy
        panel osobno, patrz _rescue_own_voice. Stabilne - kolejnosc wewnatrz grup zachowana.
        Hold dla rywala jest KONTEKSTOWY (own_aliases): demote tylko
        w panelu przegranego, bo dla zwyciezcy to relacja o wlasnych bohaterach.
        """
        demoted = [
            hit
            for hit in chosen
            if MediaResearchProvider._is_non_own_reaction(hit, own_aliases)
        ]
        if not demoted or len(demoted) == len(chosen):
            return chosen
        others = [hit for hit in chosen if hit not in demoted]
        diag.append(
            f"{label}: {len(demoted)} materialow != wlasny glos o meczu (digest cudzej prasy "
            "/ brief rankingowy / hold dla rywala / dzial nie-sportowy) zsunieto na koniec "
            "wyboru kuratora"
        )
        return others + demoted

    @staticmethod
    def _heuristic_order(
        hits: list[SearchHit], diag: list[str], label: str
    ) -> list[SearchHit]:
        """Bezpiecznik bez modelu: deprio przegladow cudzej prasy + opinie przed druga relacja."""
        roundup_count = sum(1 for hit in hits if looks_like_non_reaction(hit.url, hit.title))
        if roundup_count:
            diag.append(
                f"{label}: {roundup_count} materialow nie-reakcji (digest cudzej prasy / "
                "brief rankingowy) zdeprioryteryzowano (!= wlasny glos o meczu)"
            )
        opinion_count = sum(1 for hit in hits if looks_like_opinion(hit.url, hit.title))
        if opinion_count:
            hits = prefer_opinion_hits(hits)
            diag.append(
                f"{label}: {opinion_count}/{len(hits)} hitow wyglada na komentarz/analize "
                "- ida przed druga relacja"
            )
        return hits

    @staticmethod
    def _name_variants(profile, fallback: str) -> list[str]:
        # delegacja do modulowego helpera (jedno zrodlo prawdy, wspoldzielone z torem faktow)
        return team_name_variants(profile, fallback)

    def _build_queries(
        self, profile, opponent_profile, opponent_country: str, date: str | None = None
    ) -> list[str]:
        # date celowo NIEUZYWANE w tresci zapytania: data ISO w query psuje ranking
        # wyszukiwarki (empirycznie); swiezosc zapewnia time_range w adapterze search.
        # Budowa zapytan wspoldzielona z torem faktow (local_media_queries) - jedno
        # zrodlo prawdy dla 'jak pytac lokalna prase o ten mecz'.
        return local_media_queries(profile, opponent_profile, opponent_country)

    def _section_hits(self, profile, opponent_profile, diag: list[str], label: str) -> list[SearchHit]:
        return collect_section_hits(
            self.fetcher,
            self.budget,
            profile,
            opponent_profile,
            diag,
            label,
            max_sections=self.max_sections_per_country,
            telemetry=self.telemetry,
            health=self.health,
        )

    def _fetch_dead(self, provider_id: str) -> bool:
        """Czy zdrowie mowi 'fetch tego outletu jest martwy' (swiezy streak botblock).

        Fail-safe: brak magazynu / blad zapytania = False (probujemy normalnie) -
        pamiec jest doradcza, nigdy blokujaca."""
        if self.health is None:
            return False
        try:
            return bool(self.health.outlet_fetch_dead(provider_id))
        except Exception:  # noqa: BLE001 - zdrowie doradcze: awaria = neutralnie
            return False

    def _emit_fetch(
        self,
        country: str,
        descriptor,
        hit: SearchHit,
        body_len: int = 0,
        error: Exception | None = None,
    ) -> None:
        """Zdarzenie telemetrii dla proby fetchu artykulu (etap 1: sama obserwacja).

        Klasy: blad -> wg kodu HTTP (botblock/stale_path/transient); sukces ponizej
        progu slajdu -> thin (flash/alerte). Fetch z cache liczy sie jak ok - dedup
        po dniu robi magazyn zdrowia (etap 2).
        """
        if self.telemetry is None:
            return
        if error is not None:
            outcome = classify_fetch_error(error)
        else:
            outcome = FETCH_OK if body_len >= _MIN_ARTICLE_BODY_FOR_SLIDE else FETCH_THIN
        self.telemetry.emit(
            OutletFetchEvent(
                provider_id=descriptor.provider_id,
                country=country,
                url=hit.url,
                outcome=outcome,
                body_len=body_len,
                had_raw_content=bool(hit.raw_content),
            )
        )

    def _collect_hits(
        self,
        queries: list[str],
        allowed_domains: tuple[str, ...],
        profile,
        opponent_profile,
        match_date: str | None = None,
        extra_hits: list[SearchHit] | None = None,
        diag: list[str] | None = None,
        label: str = "media",
    ) -> list[SearchHit]:
        """Zbiera pule hitow (sekcje + search) i sortuje po trafnosci.

        Artykul-reakcja wymienia przeciwnika w tytule/URL-u; hity bez zadnej nazwy
        druzyn (np. wystep gwiazdy na otwarciu) spadaja na koniec puli. Hity z data
        publikacji SPRZED dnia meczu to zapowiedzi - odpadaja od razu.
        """
        pool_cap = max(self.max_articles_per_country * 2, 6)
        hits: list[SearchHit] = []
        seen: set[str] = set()
        seen_titles: set[str] = set()
        offtopic = 0

        def _is_duplicate(hit: SearchHit) -> bool:
            """ID kanoniczny tnie ten sam artykul pod roznymi slugami; TYTUL tnie ten sam
            MATERIAL pod roznymi URL-ami bez wspolnego ID. Huby live-bloga ('Alt om
            fotball-VM' w VG) wracaly do 4x pod roznymi '/i/<kod>' - kazdy z innym
            canonical_article_key zjadal osobny slot i wypychal query z prawdziwym recapem
            poza pool_cap, zanim zdazyla sie odpalic. Tytul liczy sie tylko gdy KONKRETNY
            (>=12 zn.), zeby nie sklejac roznych artykulow o pustym/krotkim tytule."""
            key = canonical_article_key(hit.url)
            if key in seen:
                return True
            title_key = " ".join((hit.title or "").split()).casefold()
            if len(title_key) >= 12 and title_key in seen_titles:
                return True
            seen.add(key)
            if len(title_key) >= 12:
                seen_titles.add(title_key)
            return False

        for hit in extra_hits or []:
            if url_date_too_old(hit.url, match_date) or _is_duplicate(hit):
                continue
            hits.append(hit)
        try:
            for query in queries:
                if len(hits) >= pool_cap:
                    break
                self.budget.charge()
                for hit in self.search_client.search(query, allowed_domains, self.search_limit):
                    if (
                        not domain_allowed(hit.url, allowed_domains)
                        or not is_article_url(hit.url)
                        or _published_before(hit.published_at, match_date)
                        or url_date_too_old(hit.url, match_date)
                    ):
                        continue
                    # Tor search ufal relevancji Tavily w calosci - fuzzy hit z whitelisty
                    # wchodzil na sam DEMONIM nazwy kraju (artykul gospodarczy 'Jordanian
                    # dinar' w panelu Jordanii). Tor sekcji ma juz swoja bramke; tu robimy
                    # to samo dla search. Sekcji (extra_hits) NIE dotykamy - przeszly wlasna.
                    if not hit_has_match_context(profile, opponent_profile, hit.url, hit.title):
                        offtopic += 1
                        continue
                    if _is_duplicate(hit):
                        continue
                    hits.append(hit)
                    if len(hits) >= pool_cap:
                        break
        except BudgetExceededError:
            pass
        except ResearchError as error:
            # Search to BACKFILL; sekcje redakcji sa zrodlem pierwszego wyboru
            # (najswiezsze relacje, niezaleznie od lagu indeksu). Awaria
            # wyszukiwarki (limit/4xx API, np. Tavily 432) nie moze kasowac juz
            # zebranych hitow z sekcji - inaczej chwilowy blad search topi caly
            # kraj mimo realnych relacji wlasnej prasy. Degradujemy do tego, co mamy.
            if diag is not None:
                diag.append(
                    f"{label}: search nieudany ({error}); zostaja hity z sekcji ({len(hits)})"
                )

        if offtopic and diag is not None:
            diag.append(
                f"{label}: {offtopic} hitow search bez kontekstu meczu "
                "(sama nazwa kraju / demonim, np. brief gospodarczy) odrzucono"
            )

        tokens = set()
        for source in (profile, opponent_profile):
            if source is None:
                continue
            for name in source.aliases():
                tokens.add(fold_ascii(name))

        def relevance(hit: SearchHit) -> int:
            blob = match_blob(f"{hit.title} {hit.url}")
            return -sum(1 for token in tokens if token and token in blob)

        # Kolejnosc puli: (1) zapowiedzi (data w URL przed meczem) na KONIEC, nawet gdy
        # wymieniaja obie druzyny - relacja pomeczowa ma pierwszenstwo; (2) przeglady
        # CUDZEJ prasy (digest reakcji) ponizej wlasnych relacji/komentarzy - inaczej
        # digest 'imprensa internacional rende-se...' bil realny glos redakcji i panel
        # czytal sie jak zbieranina; (3) na koniec trafnosc (liczba nazw druzyn w URL).
        hits.sort(
            key=lambda hit: (
                url_is_prematch(hit.url, match_date),
                looks_like_non_reaction(hit.url, hit.title),
                # dzial spoleczny/krajowy (/nacionales/) ponizej relacji z dzialu sportu -
                # jego streszczenie salvage'uje do golego cytatu (Paragwaj run_20260630221335)
                looks_like_non_sports_section(hit.url),
                relevance(hit),
            )
        )
        return hits[: self.max_articles_per_country]

    def _fetch(self, url: str) -> str:
        key = cache_key("fetch", {"url": url})
        if self.cache is not None:
            cached = self.cache.get(key)
            if cached is not None:
                return cached
        self.budget.charge()
        text = self.fetcher.fetch(url)
        if self.cache is not None:
            self.cache.set(key, text, self.cache_ttl_seconds)
        return text


@dataclass
class LiveFactsProvider:
    """Live fakty (Tier A): search w domenach oficjalnych -> fetch -> scout faktow.

    Zwraca (MatchFacts, evidence) tylko gdy ekstrakcja przejdzie guardraile; inaczej None.
    Confidence celowo 'medium' (pojedyncze zrodlo live) - sedziowie i czlowiek decyduja dalej.
    """

    registry: SourceRegistry
    search_client: SearchClient
    fetcher: PageFetcher
    scout: FactsScout
    budget: BudgetTracker
    cache: Cache | None = None
    facts_provider_id: str = "OfficialMatchApi"
    search_limit: int = 5
    cache_ttl_seconds: int = 600

    def acquire(
        self,
        query: str,
        date_hint: str | None = None,
        notes: list[str] | None = None,
    ) -> tuple[MatchFacts, list[EvidenceItem]] | None:
        """Szuka faktow w domenach oficjalnych. None = nie znaleziono (z diagnoza w `notes`).

        Bledy infrastruktury (brak klucza, siec, budzet) NIE sa polykane - ida w gore,
        zeby koordynator pokazal realna przyczyne zamiast 'nie znaleziono meczu'.
        """
        diag = notes if notes is not None else []
        descriptor = self.registry.get(self.facts_provider_id)
        if descriptor is None or not descriptor.domains:
            diag.append(f"facts: provider {self.facts_provider_id} bez domen w rejestrze")
            return None
        allowed_domains = descriptor.domains

        hits = self._collect_hits(query, date_hint, allowed_domains, diag)
        if not hits:
            diag.append(f"facts: 0 hitow w domenach {list(allowed_domains)}")
            return None

        expected = expected_countries(self.registry, query)
        for hit in hits:
            text = facts_text_from_hit(self._fetch, hit, "facts", diag)
            if text is None:
                continue
            try:
                draft = self.scout.extract(query, text)
            except BudgetExceededError:
                raise
            except Exception as error:  # noqa: BLE001 - zly artykul: probujemy kolejny hit
                diag.append(f"facts: {hit.url} odrzucony ({error})")
                continue
            mismatch = draft_mismatch(self.registry, draft, expected, date_hint)
            if mismatch:
                diag.append(f"facts: {hit.url} to inny mecz ({mismatch})")
                continue
            return self._build_facts(draft, hit.url, descriptor.tier, date_hint)
        diag.append(f"facts: zaden z {len(hits)} hitow nie przeszedl ekstrakcji")
        return None

    def _collect_hits(
        self,
        query: str,
        date_hint: str | None,
        allowed_domains: tuple[str, ...],
        diag: list[str],
    ) -> list[SearchHit]:
        hits: list[SearchHit] = []
        seen: set[str] = set()
        for candidate in self._build_queries(query, date_hint):
            self.budget.charge()
            found = self.search_client.search(candidate, allowed_domains, self.search_limit)
            fresh = [
                hit
                for hit in found
                if hit.url not in seen
                and domain_allowed(hit.url, allowed_domains)
                and is_article_url(hit.url)
            ]
            seen.update(hit.url for hit in fresh)
            hits.extend(fresh)
            diag.append(f"facts: zapytanie {candidate!r} -> {len(fresh)} hitow")
            if hits:
                break
        return hits

    def _build_queries(self, query: str, date_hint: str | None) -> list[str]:
        """Zapytania pod anglojezyczne zrodla oficjalne, nie surowy (czesto polski) input.

        Wykrywa kraje w zapytaniu po aliasach z rejestru i pyta o '<Home> <Away> result'.
        Surowe zapytanie usera zostaje jako ostatni fallback.
        """
        queries: list[str] = []
        profiles = self.registry.countries_in_text(query)
        if len(profiles) >= 2:
            home = profiles[0].english_name or profiles[0].country
            away = profiles[1].english_name or profiles[1].country
            when = date_hint or ""
            queries.append(f"{home} {away} {when} final score result".strip())
            queries.append(f"{home} vs {away} match report")
        if query not in queries:
            queries.append(query)
        return queries

    def _build_facts(self, draft, source_url: str, tier: SourceTier, date_hint: str | None):
        home_team = canonical_country(self.registry, draft.home_team)
        away_team = canonical_country(self.registry, draft.away_team)
        team_map = {draft.home_team: home_team, draft.away_team: away_team}
        date = draft.date or (date_hint or "")
        match_id = _mint(
            f"live_{_slug(home_team)}_{_slug(away_team)}",
            home_team,
            away_team,
            date,
            draft.full_time,
        )
        retrieved_at = _now_iso()
        result_id = _mint("e_result", match_id)
        evidence: list[EvidenceItem] = [
            EvidenceItem(
                id=result_id,
                claim=f"{home_team} - {away_team}: {draft.full_time}.",
                value=draft.full_time,
                source_url=source_url,
                source_tier=tier,
                provider=self.facts_provider_id,
                retrieved_at=retrieved_at,
                confidence="medium",
            )
        ]
        goals: list[GoalEvent] = []
        for index, goal in enumerate(draft.goals):
            goal_team = team_map.get(goal.team, goal.team)
            goal_id = _mint("e_goal", match_id, str(index), goal.player)
            evidence.append(
                EvidenceItem(
                    id=goal_id,
                    claim=f"{goal.player} ({goal_team}) - gol w {goal.minute}'.",
                    value=f"{goal.player} {goal.minute}'",
                    source_url=source_url,
                    source_tier=tier,
                    provider=self.facts_provider_id,
                    retrieved_at=retrieved_at,
                    confidence="medium",
                )
            )
            goals.append(
                GoalEvent(
                    team=goal_team,
                    player=goal.player,
                    minute=goal.minute,
                    detail=goal.detail,
                    evidence_id=goal_id,
                )
            )

        facts = MatchFacts(
            match_id=match_id,
            competition=draft.competition,
            stage=draft.stage,
            date=date,
            venue=draft.venue,
            home_team=home_team,
            away_team=away_team,
            score=ScoreLine(full_time=draft.full_time),
            goals=goals,
            key_events=[],
            source_ids=[result_id] + [goal.evidence_id for goal in goals],
        )
        return facts, evidence

    def _fetch(self, url: str) -> str:
        key = cache_key("fetch", {"url": url})
        if self.cache is not None:
            cached = self.cache.get(key)
            if cached is not None:
                return cached
        self.budget.charge()
        text = self.fetcher.fetch(url)
        if self.cache is not None:
            self.cache.set(key, text, self.cache_ttl_seconds)
        return text


_Candidate = tuple  # (draft, outlet_descriptor, url, home_kanonicznie, away_kanonicznie)


@dataclass
class CorroboratedMediaFactsProvider:
    """Fakty z mediow krajowych OBU reprezentacji, wynik potwierdzony w >=2 outletach.

    Zrodla 'oficjalne' (fifa/uefa) bywaja bezuzyteczne dla meczow towarzyskich. Tu
    szukamy relacji w zaufanych outletach obu krajow (ta sama whitelista co tor
    medialny) i przyjmujemy wynik TYLKO gdy >=2 rozne outlety zgadzaja sie co do
    full_time (orientacja home/away jest normalizowana przed porownaniem). Te same
    guardraile co wszedzie: whitelist domen, sanityzacja, straznik 'inny mecz'.
    """

    registry: SourceRegistry
    search_client: SearchClient
    fetcher: PageFetcher
    scout: FactsScout
    budget: BudgetTracker
    cache: Cache | None = None
    search_limit: int = 5
    max_articles_per_country: int = 3
    min_corroborating_outlets: int = 2
    max_sections_per_country: int = 2
    cache_ttl_seconds: int = 600

    def acquire(
        self,
        query: str,
        date_hint: str | None = None,
        notes: list[str] | None = None,
    ) -> tuple[MatchFacts, list[EvidenceItem]] | None:
        diag = notes if notes is not None else []
        profiles = self.registry.countries_in_text(query)
        if len(profiles) < 2:
            diag.append("media-facts: nie wykryto dwoch krajow w zapytaniu")
            return None
        first, second = profiles[0], profiles[1]
        expected = frozenset({first.country, second.country})

        candidates: list[_Candidate] = []
        for profile, opponent in ((first, second), (second, first)):
            candidates.extend(
                self._country_candidates(profile, opponent, query, date_hint, expected, diag)
            )
        if not candidates:
            diag.append("media-facts: zadna relacja nie przeszla guardraili")
            return None

        group = self._corroborated_group(candidates, first.country, diag)
        if group is None:
            return None
        return self._build_facts(group, date_hint)

    def _country_candidates(
        self,
        profile,
        opponent,
        query: str,
        date_hint: str | None,
        expected: frozenset[str],
        diag: list[str],
    ) -> list[_Candidate]:
        domains = tuple(
            sorted({domain for outlet in profile.outlets for domain in outlet.descriptor.domains})
        )
        if not domains:
            return []
        team = profile.english_name or profile.country
        opp = opponent.english_name or opponent.country
        # Te same zapytania, co tor medialny (local_media_queries): lokalne warianty
        # nazwy (przydomek/egzonim) + recap/report + '{team} {world_cup}', BEZ daty ISO
        # w tresci. Anglocentryczne '{team} vs {opp} {data} match report' gubilo recapy
        # lokalnej prasy (abola/record/actualite) - korroboracja dawala 0 hitow i wynik
        # nie byl potwierdzany mimo dostepnych relacji (Portugalia-DR Konga 1-1: tylko
        # JS-walled strona FIFA, a lokalna prasa nie wchodzila do puli faktow).
        queries = local_media_queries(profile, opponent, opp)

        label_sections = f"media-facts[{profile.country}]"
        hits: list[SearchHit] = []
        seen: set[str] = set()
        # sekcje redakcji najpierw: swiezy wynik jest tam, zanim zlapie go indeks search
        for hit in collect_section_hits(
            self.fetcher,
            self.budget,
            profile,
            opponent,
            diag,
            label_sections,
            max_sections=self.max_sections_per_country,
        ):
            if hit.url not in seen:
                seen.add(hit.url)
                hits.append(hit)
        for candidate_query in queries:
            if len(hits) >= self.max_articles_per_country * 2:
                break
            self.budget.charge()
            for hit in self.search_client.search(candidate_query, domains, self.search_limit):
                if (
                    hit.url in seen
                    or not domain_allowed(hit.url, domains)
                    or not is_article_url(hit.url)
                ):
                    continue
                seen.add(hit.url)
                hits.append(hit)

        # Kolejnosc puli faktow: (1) scoreline w slugu/tytule na SAM poczatek - to tor
        # faktow, wiec relacja z wynikiem ma pierwszenstwo; bez tego score-less zapowiedz
        # ('estreia-se-hoje') i digest cudzej prasy ('imprensa-internacional-rende-se')
        # zjadaly top-N i autorytatywna relacja '...-empata-...-0-0' nigdy nie byla
        # ekstrahowana -> brak korroboracji -> match_not_found_live. (2) zapowiedzi (data
        # przed meczem) i (3) przeglady cudzej prasy spadaja nizej. (4) na koniec trafnosc
        # (ile nazw druzyn w URL); sort stabilny zachowuje kolejnosc search w obrebie rangi.
        tokens = tuple(
            fold_ascii(name)
            for name in (team, opp, profile.team_names[0], opponent.team_names[0])
        )

        def relevance(hit: SearchHit) -> int:
            blob = match_blob(f"{hit.title} {hit.url}")
            return -sum(1 for token in set(tokens) if token in blob)

        hits.sort(
            key=lambda hit: (
                not url_hints_score(hit.url, hit.title),
                url_is_prematch(hit.url, date_hint),
                looks_like_press_roundup(hit.url, hit.title),
                relevance(hit),
            )
        )

        label = f"media-facts[{profile.country}]"
        score_hinted = sum(1 for hit in hits if url_hints_score(hit.url, hit.title))
        diag.append(
            f"{label}: {len(hits)} hitow po filtrach"
            + (f" ({score_hinted} z wynikiem w slugu -> na poczatek)" if score_hinted else "")
        )
        results: list[_Candidate] = []
        for hit in hits[: self.max_articles_per_country]:
            descriptor = self.registry.outlet_for_url(profile.country, hit.url)
            if descriptor is None:
                continue
            text = facts_text_from_hit(self._fetch, hit, label, diag)
            if text is None:
                continue
            try:
                draft = self.scout.extract(query, text)
            except BudgetExceededError:
                raise
            except Exception as error:  # noqa: BLE001 - zly artykul nie wywala calego kraju
                diag.append(f"{label}: {hit.url} odrzucony ({error})")
                continue
            mismatch = draft_mismatch(self.registry, draft, expected, date_hint)
            if mismatch:
                diag.append(f"media-facts[{profile.country}]: {hit.url} to inny mecz ({mismatch})")
                continue
            results.append(
                (
                    draft,
                    descriptor,
                    hit.url,
                    canonical_country(self.registry, draft.home_team),
                    canonical_country(self.registry, draft.away_team),
                )
            )
        diag.append(f"media-facts[{profile.country}]: {len(results)} relacji z wynikiem")
        return results

    def _corroborated_group(
        self, candidates: list[_Candidate], reference_country: str, diag: list[str]
    ) -> list[_Candidate] | None:
        """Grupuje kandydatow po wyniku w orientacji wzgledem reference_country."""
        groups: dict[str, list[_Candidate]] = {}
        for candidate in candidates:
            draft, _, _, home_n, _ = candidate
            score = draft.full_time
            if home_n != reference_country:
                home_goals, away_goals = score.split("-")
                score = f"{away_goals}-{home_goals}"
            groups.setdefault(score, []).append(candidate)

        best: list[_Candidate] | None = None
        for group in groups.values():
            outlets = {candidate[1].provider_id for candidate in group}
            if len(outlets) < self.min_corroborating_outlets:
                continue
            if best is None or len(outlets) > len({c[1].provider_id for c in best}):
                best = group
        if best is None:
            scores = {score: len(group) for score, group in groups.items()}
            diag.append(
                f"media-facts: brak korroboracji wyniku w >={self.min_corroborating_outlets} "
                f"roznych outletach (kandydaci: {scores})"
            )
            return None
        return best

    def _build_facts(
        self, group: list[_Candidate], date_hint: str | None
    ) -> tuple[MatchFacts, list[EvidenceItem]]:
        primary_draft, primary_outlet, primary_url, home_team, away_team = group[0]
        date = primary_draft.date or (date_hint or "")
        match_id = _mint(
            f"live_{_slug(home_team)}_{_slug(away_team)}",
            home_team,
            away_team,
            date,
            primary_draft.full_time,
        )
        retrieved_at = _now_iso()

        evidence: list[EvidenceItem] = []
        result_ids: list[str] = []
        for draft, descriptor, url, _, _ in group:
            result_id = _mint("e_result", match_id, descriptor.provider_id, url)
            result_ids.append(result_id)
            evidence.append(
                EvidenceItem(
                    id=result_id,
                    claim=f"{home_team} - {away_team}: {primary_draft.full_time}.",
                    value=draft.full_time,
                    source_url=url,
                    source_tier=descriptor.tier,
                    provider=descriptor.provider_id,
                    retrieved_at=retrieved_at,
                    confidence="high",
                )
            )

        team_map = {
            primary_draft.home_team: home_team,
            primary_draft.away_team: away_team,
        }
        goals: list[GoalEvent] = []
        for index, goal in enumerate(primary_draft.goals):
            goal_team = team_map.get(goal.team, goal.team)
            goal_id = _mint("e_goal", match_id, str(index), goal.player)
            evidence.append(
                EvidenceItem(
                    id=goal_id,
                    claim=f"{goal.player} ({goal_team}) - gol w {goal.minute}'.",
                    value=f"{goal.player} {goal.minute}'",
                    source_url=primary_url,
                    source_tier=primary_outlet.tier,
                    provider=primary_outlet.provider_id,
                    retrieved_at=retrieved_at,
                    confidence="medium",
                )
            )
            goals.append(
                GoalEvent(
                    team=goal_team,
                    player=goal.player,
                    minute=goal.minute,
                    detail=goal.detail,
                    evidence_id=goal_id,
                )
            )

        facts = MatchFacts(
            match_id=match_id,
            competition=primary_draft.competition,
            stage=primary_draft.stage,
            date=date,
            venue=primary_draft.venue,
            home_team=home_team,
            away_team=away_team,
            score=ScoreLine(full_time=primary_draft.full_time),
            goals=goals,
            key_events=[],
            source_ids=result_ids + [goal.evidence_id for goal in goals],
        )
        return facts, evidence

    def _fetch(self, url: str) -> str:
        key = cache_key("fetch", {"url": url})
        if self.cache is not None:
            cached = self.cache.get(key)
            if cached is not None:
                return cached
        self.budget.charge()
        text = self.fetcher.fetch(url)
        if self.cache is not None:
            self.cache.set(key, text, self.cache_ttl_seconds)
        return text


@dataclass
class FactsProviderChain:
    """Probuje kolejne zrodla faktow (oficjalne -> media krajowe); pierwszy sukces wygrywa.

    Diagnozy wszystkich prob skladaja sie w jedna liste `notes` - operator widzi
    pelna sciezke decyzji, nie tylko ostatnia porazke.
    """

    providers: tuple = ()

    def acquire(
        self,
        query: str,
        date_hint: str | None = None,
        notes: list[str] | None = None,
    ) -> tuple[MatchFacts, list[EvidenceItem]] | None:
        for provider in self.providers:
            result = provider.acquire(query, date_hint, notes=notes)
            if result is not None:
                return result
        return None
