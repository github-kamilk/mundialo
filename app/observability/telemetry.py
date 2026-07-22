"""Telemetria runu: maszynowy zapis zdarzen operacyjnych toru research (epizody, etap 1).

Realizacja architektura-pamiec-epizodyczna.md: zdarzenia emitowane w miejscach
dzisiejszych diag.append() skladaja sie w epizod runu zapisywany do run.json.
Etap 1 to czysta obserwowalnosc - zero wplywu na zachowanie retrievalu; etap 2
doda magazyn zdrowia zrodel konsumujacy te zdarzenia miedzy runami.

Zasada anti-injection: do telemetrii ida WYLACZNIE URL-e, statusy i liczby -
nigdy tresc artykulow. Pamiec epizodyczna nie moze byc kanalem, ktorym tekst
z internetu wraca do systemu.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

# Klasy wyniku fetchu - mapowanie 1:1 na runbook utrzymania zrodel (drift sekcji):
# botblock = NIE ruszac (search/raw_content nadrabia); stale_path = czlowiek musi
# znalezc nowy URL; transient = ignorowac pojedyncze; empty = 200 bez tresci po
# ekstrakcji (kandydat na JS-wall); thin = tresc za cienka na slajd (flash/alerte).
FETCH_OK = "ok"
FETCH_THIN = "thin"
FETCH_BOTBLOCK = "botblock"
FETCH_STALE_PATH = "stale_path"
FETCH_TRANSIENT = "transient"
FETCH_EMPTY = "empty"
FETCH_ERROR = "error"

SECTION_LINKS = "links"
SECTION_NO_LINKS = "no_links"


def classify_fetch_error(error: Exception) -> str:
    """Mapuje blad fetchu na klase zdarzenia po kodzie HTTP (ResearchError.status_code).

    Fallback tekstowy tylko dla bledow bez kodu (timeout/connect nie maja statusu,
    'pusta tresc' to wlasny blad ekstrakcji fetchera).
    """
    status = getattr(error, "status_code", None)
    if status in (401, 403, 451):
        return FETCH_BOTBLOCK
    if status in (400, 404, 410):
        return FETCH_STALE_PATH
    if status == 429 or (status is not None and status >= 500):
        return FETCH_TRANSIENT
    text = str(error).lower()
    if "pusta tresc" in text:
        return FETCH_EMPTY
    if "timeout" in text or "timed out" in text or "connect" in text or "network" in text:
        return FETCH_TRANSIENT
    return FETCH_ERROR


def classify_search_error(error: Exception) -> str:
    """Klasa bledu search: '432' (kredyty Tavily) to osobny, akcjonowalny sygnal."""
    text = str(error)
    if "432" in text:
        return "432"
    lowered = text.lower()
    if "timeout" in lowered or "timed out" in lowered or "connect" in lowered:
        return FETCH_TRANSIENT
    return FETCH_ERROR


@dataclass(frozen=True)
class OutletFetchEvent:
    """Wynik proby pobrania JEDNEGO artykulu outletu (tor medialny)."""

    provider_id: str
    country: str
    url: str
    outcome: str  # FETCH_* powyzej
    body_len: int = 0
    # czy indeks search dostarczyl raw_content (fallback tresci mimo padlego fetchu) -
    # botblock z had_raw_content=True to problem KOLEJNOSCI, nie utraty tresci
    had_raw_content: bool = False


@dataclass(frozen=True)
class SectionProbeEvent:
    """Wynik sondy strony sekcji outletu (zrodlo najswiezszych linkow).

    `article_links` liczy linki w domenie wygladajace na artykul NIEZALEZNIE od
    dopasowania do meczu - to wlasciwy detektor JS-walla (sekcja zyje, ale nic
    o tym meczu = normalne; sekcja bez ZADNYCH linkow artykulow = sciana).
    """

    provider_id: str
    country: str
    section_url: str
    outcome: str  # SECTION_LINKS | SECTION_NO_LINKS | FETCH_BOTBLOCK | FETCH_STALE_PATH | ...
    links_found: int = 0  # linki pasujace do meczu (weszly do puli)
    article_links: int = 0  # linki artykulowe w domenie (przed filtrem meczu)


@dataclass(frozen=True)
class SearchEvent:
    """Wynik jednego zapytania search (przez dekorator TelemetrySearchClient)."""

    query: str
    hits: int = 0
    error: str | None = None  # "432" | "transient" | "error" | None


@dataclass
class RunTelemetry:
    """Kolektor zdarzen jednego runu (append-only; koordynator resetuje per run).

    Wspoldzielony przez search-klienta, provider medialny i koordynatora - jak
    gateway.budget. Brak kolektora (None w providerach) = zachowanie sprzed epizodow.
    """

    outlet_events: list[OutletFetchEvent] = field(default_factory=list)
    section_events: list[SectionProbeEvent] = field(default_factory=list)
    search_events: list[SearchEvent] = field(default_factory=list)

    def emit(self, event: OutletFetchEvent | SectionProbeEvent | SearchEvent) -> None:
        if isinstance(event, OutletFetchEvent):
            self.outlet_events.append(event)
        elif isinstance(event, SectionProbeEvent):
            self.section_events.append(event)
        elif isinstance(event, SearchEvent):
            self.search_events.append(event)
        else:  # pragma: no cover - blad programisty, nie danych
            raise TypeError(f"nieznany typ zdarzenia telemetrii: {type(event).__name__}")

    def reset(self) -> None:
        self.outlet_events.clear()
        self.section_events.clear()
        self.search_events.clear()

    def as_dict(self) -> dict[str, list[dict[str, Any]]]:
        return {
            "outlet_events": [asdict(event) for event in self.outlet_events],
            "section_events": [asdict(event) for event in self.section_events],
            "search_events": [asdict(event) for event in self.search_events],
        }
