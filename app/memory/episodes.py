"""Pamiec epizodyczna runow: epizod (etap 1) + magazyn zdrowia zrodel (etap 2).

Realizacja architektura-pamiec-epizodyczna.md. Po zakonczeniu runu (takze po
halcie - halty to najciekawsze epizody) koordynator skleja zdarzenia z RunTelemetry
w RunEpisode i zapisuje w run.json (pole 'episode'). Celowo BEZ LLM: zdarzenia
mamy w kodzie w momencie zajscia, wiec 'kompresja' to agregacja pol - streszczanie
wlasnych strukturalnych danych modelem byloby proszeniem sie o dryf.

OutletHealthStore (etap 2) skleja epizody w trwaly stan pamieci epizodycznej:
epizody realnych runow (save_run) aggreguja sie w runs/.outlet_health.json, a
nastepny run dostaje z niego ADVISORIES do notes - gotowa diagnoze zamiast
odkrywania jej od zera. Zdrowie jest warstwa EPIZODYCZNA (maszynowa, wygasajaca),
oddzielona od Mapy Wiedzy (country_media.json - ludzka, wersjonowana): magazyn
NIGDY nie modyfikuje rejestru, whitelisty ani tierow.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from app.observability.telemetry import (
    OutletFetchEvent,
    RunTelemetry,
    SearchEvent,
    SectionProbeEvent,
)


@dataclass(frozen=True)
class RunEpisode:
    """Zapis operacyjny jednego runu: co system probowal i jak wyszlo.

    Metadane runu (status, blocking) sa tu ZDUBLOWANE z reszta run.json celowo -
    epizod ma byc samodzielnym rekordem dla magazynu zdrowia (etap 2), czytanym
    bez parsowania calego runu.
    """

    run_id: str
    at: str
    match_query: str
    status: str
    blocking: list[str]
    outlet_events: list[OutletFetchEvent]
    section_events: list[SectionProbeEvent]
    search_events: list[SearchEvent]

    @classmethod
    def from_telemetry(
        cls,
        run_id: str,
        match_query: str,
        status: str | Enum,
        blocking: list[str],
        telemetry: RunTelemetry,
    ) -> "RunEpisode":
        return cls(
            run_id=run_id,
            at=datetime.now(timezone.utc).isoformat(),
            match_query=match_query,
            status=status.value if isinstance(status, Enum) else str(status),
            blocking=list(blocking),
            outlet_events=list(telemetry.outlet_events),
            section_events=list(telemetry.section_events),
            search_events=list(telemetry.search_events),
        )

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# Okno zdarzen per outlet/sekcja: starsze wypadaja same (wygaszanie okna) -
# sygnal negatywny nie jest wyrokiem dozywotnim.
MAX_EVENTS_PER_KEY = 20
# Advisory dopiero od serii >=3 zgodnych zdarzen: pojedynczy fail to szum.
MIN_STREAK_FOR_ADVISORY = 3
# 432 (kredyty Tavily) jest akcjonowalne, poki swieze; starsze samo traci moc.
SEARCH_432_ADVISORY_HOURS = 48
# Re-probe (etap 3): status 'martwy' wygasa, gdy ostatnia obserwacja jest starsza -
# demote dziala tylko na swiezych obserwacjach, stare same traca moc i nastepny
# run wykonuje normalna probe (nadpisujac obraz zdrowia).
RE_PROBE_HOURS = 72

# Serie porazek, ktore DEMOTUJA obiekt w kolejnosci I/O (etap 3). Celowo bez
# 'error' (nieklasyfikowany - konserwatywnie nie karzemy) i bez 'thin'/'empty'
# (tresc dociera / preferencje tresci zalatwia juz _extract_items).
_SECTION_DEAD_OUTCOMES = frozenset({"no_links", "botblock", "stale_path"})


def _streak(events: list[dict[str, Any]]) -> tuple[str, int, str] | None:
    """Seria identycznych wynikow od NAJNOWSZEGO zdarzenia: (outcome, ile, od-kiedy).

    'transient' (5xx/timeout) nie buduje serii ANI jej nie przerywa - pojedynczy
    czkawkowy timeout miedzy dwoma 403 nie moze zerowac obrazu bot-blocka.
    Wyniki zdrowe (ok/links) nie generuja advisories -> None.
    """
    outcome: str | None = None
    count = 0
    since = ""
    for event in reversed(events):
        current = str(event.get("outcome", ""))
        if current == "transient":
            continue
        if outcome is None:
            outcome = current
        if current != outcome:
            break
        count += 1
        since = str(event.get("day", ""))
    if outcome is None or outcome in ("ok", "links"):
        return None
    return outcome, count, since


def _last_day(events: list[dict[str, Any]], skip_transient: bool = False) -> str | None:
    """Dzien NAJNOWSZEGO zdarzenia (dla swiezosci statusu: z pominieciem transient -
    czkawkowy timeout nie odswieza obrazu bot-blocka)."""
    for event in reversed(events):
        if skip_transient and str(event.get("outcome", "")) == "transient":
            continue
        day = str(event.get("day", ""))
        return day or None
    return None


@dataclass
class OutletHealthStore:
    """Magazyn zdrowia zrodel miedzy runami (runs/.outlet_health.json).

    Zasady (architektura-pamiec-epizodyczna.md, sekcja 2):
    - pamiec jest DORADCZA, nigdy blokujaca: brak pliku = neutralny cold start,
      uszkodzony plik = reset + nota, blad zapisu = best-effort;
    - magazyn nie zna rejestru i nie dotyka country_media.json - konsolidacja
      semantyczna (edycja Mapy Wiedzy) zostaje ludzka, na podstawie report();
    - do magazynu ida wylacznie URL-e, statusy i liczby (anti-injection).

    `now` jest wstrzykiwalne dla testow; None = zegar scienny UTC. Celowo NIE
    class-level lambda (funkcja jako atrybut klasy wiazalaby sie jak metoda).
    """

    path: Path
    max_events: int = MAX_EVENTS_PER_KEY
    now: Callable[[], datetime] | None = None

    def _now(self) -> datetime:
        return self.now() if self.now is not None else datetime.now(timezone.utc)

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {
            "version": 1,
            "updated_at": None,
            "outlets": {},
            "sections": {},
            "search": {"last_432_at": None},
        }

    def _load(self) -> tuple[dict[str, Any], bool]:
        """(dane, corrupt). Brak pliku to zdrowy cold start, nie blad."""
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return self._empty(), False
        except OSError:
            return self._empty(), True
        try:
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("magazyn zdrowia musi byc obiektem JSON")
            base = self._empty()
            for key in ("outlets", "sections", "search"):
                value = data.get(key)
                if isinstance(value, dict):
                    base[key] = value
            base["updated_at"] = data.get("updated_at")
            return base, False
        except Exception:  # noqa: BLE001 - uszkodzony plik = reset, nie crash
            return self._empty(), True

    def _save(self, data: dict[str, Any]) -> None:
        """Zapis atomowy (tmp + replace), best-effort jak DiskTtlCache."""
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            tmp.replace(self.path)
        except OSError:
            return

    def apply(self, episode: dict[str, Any]) -> None:
        """Wchlania epizod runu do magazynu (odpowiednik apply_episode_to_state).

        Best-effort na kazdym poziomie: awaria magazynu nie moze topic runu.
        Pusty epizod (fixture/testy: zero zdarzen) nie churnuje pliku.
        """
        try:
            self._apply(episode)
        except Exception:  # noqa: BLE001 - pamiec doradcza, nigdy blokujaca
            return

    def _apply(self, episode: dict[str, Any]) -> None:
        outlet_events = episode.get("outlet_events") or []
        section_events = episode.get("section_events") or []
        search_events = episode.get("search_events") or []
        saw_432 = any(event.get("error") == "432" for event in search_events)
        if not outlet_events and not section_events and not saw_432:
            return
        data, _corrupt = self._load()
        day = str(episode.get("at", ""))[:10]
        run_id = str(episode.get("run_id", ""))

        for event in outlet_events:
            entry = {
                "day": day,
                "url": str(event.get("url", "")),
                "outcome": str(event.get("outcome", "")),
                "body_len": int(event.get("body_len") or 0),
                "had_raw_content": bool(event.get("had_raw_content")),
                "run_id": run_id,
            }
            bucket = data["outlets"].setdefault(
                str(event.get("provider_id", "?")),
                {"country": str(event.get("country", "")), "events": []},
            )
            # dedup re-rolli: ta sama porazka tego samego artykulu tego samego
            # dnia liczy sie raz (zdrowie to stan, nie czestotliwosc)
            if any(
                e.get("url") == entry["url"]
                and e.get("outcome") == entry["outcome"]
                and e.get("day") == day
                for e in bucket["events"]
            ):
                continue
            bucket["events"] = (bucket["events"] + [entry])[-self.max_events :]

        for event in section_events:
            entry = {
                "day": day,
                "outcome": str(event.get("outcome", "")),
                "links_found": int(event.get("links_found") or 0),
                "article_links": int(event.get("article_links") or 0),
                "run_id": run_id,
            }
            bucket = data["sections"].setdefault(
                str(event.get("section_url", "?")),
                {
                    "provider_id": str(event.get("provider_id", "")),
                    "country": str(event.get("country", "")),
                    "events": [],
                },
            )
            if any(
                e.get("outcome") == entry["outcome"] and e.get("day") == day
                for e in bucket["events"]
            ):
                continue
            bucket["events"] = (bucket["events"] + [entry])[-self.max_events :]

        if saw_432:
            data["search"]["last_432_at"] = episode.get("at")
        data["updated_at"] = self._now().isoformat()
        self._save(data)

    def advisories(self, countries: list[str] | None = None) -> list[str]:
        """Gotowa diagnoza do notes nastepnego runu (operacja Select).

        Tylko tekst doradczy - zero wplywu na retrieval (to etap 3). Kazda linia
        podpowiada akcje z runbooku utrzymania zrodel (botblock = nie ruszac,
        stale_path = czlowiek aktualizuje URL, 432 = sprawdz kredyty).
        """
        try:
            return self._advisories(countries)
        except Exception:  # noqa: BLE001 - pamiec doradcza, nigdy blokujaca
            return []

    def _advisories(self, countries: list[str] | None) -> list[str]:
        data, corrupt = self._load()
        notes: list[str] = []
        if corrupt:
            notes.append(
                "outlet_health: magazyn nieczytelny - zdrowie zrodel buduje sie od nowa"
            )
        wanted = set(countries) if countries else None

        for provider_id, bucket in sorted(data["outlets"].items()):
            country = str(bucket.get("country", ""))
            if wanted is not None and country not in wanted:
                continue
            streak = _streak(bucket.get("events", []))
            if streak is None or streak[1] < MIN_STREAK_FOR_ADVISORY:
                continue
            outcome, count, since = streak
            if outcome == "botblock":
                notes.append(
                    f"outlet_health[{country}]: {provider_id} botblock {count}x od {since} "
                    "- fetch martwy, licz na raw_content/search (whitelisty NIE ruszac)"
                )
            elif outcome == "empty":
                notes.append(
                    f"outlet_health[{country}]: {provider_id} pusta tresc {count}x od {since} "
                    "(JS-wall artykulow?)"
                )
            elif outcome == "thin":
                notes.append(
                    f"outlet_health[{country}]: {provider_id} same cienkie artykuly (stuby) "
                    f"{count}x od {since} - streszczenia moga schodzic do samego cytatu"
                )
            elif outcome == "stale_path":
                notes.append(
                    f"outlet_health[{country}]: {provider_id} 404/400 {count}x od {since} "
                    "- URL-e artykulow nieaktualne?"
                )

        for section_url, bucket in sorted(data["sections"].items()):
            country = str(bucket.get("country", ""))
            if wanted is not None and country not in wanted:
                continue
            streak = _streak(bucket.get("events", []))
            if streak is None or streak[1] < MIN_STREAK_FOR_ADVISORY:
                continue
            outcome, count, since = streak
            if outcome == "no_links":
                notes.append(
                    f"outlet_health[{country}]: sekcja {section_url} {count}x zero linkow "
                    f"artykulow od {since} (JS-wall?)"
                )
            elif outcome == "stale_path":
                notes.append(
                    f"outlet_health[{country}]: sekcja {section_url} {count}x 404/400 od "
                    f"{since} - znajdz nowy URL sekcji i zaktualizuj country_media.json "
                    "(pamietaj o verified_at)"
                )
            elif outcome == "botblock":
                notes.append(
                    f"outlet_health[{country}]: sekcja {section_url} {count}x bot-block od "
                    f"{since} - NIE ruszac (search nadrabia)"
                )

        last_432 = data["search"].get("last_432_at")
        if last_432:
            try:
                seen = datetime.fromisoformat(str(last_432))
                if self._now() - seen <= timedelta(hours=SEARCH_432_ADVISORY_HOURS):
                    notes.append(
                        f"outlet_health: Tavily 432 widziane {last_432} - sprawdz "
                        "kredyty/klucz przed runem"
                    )
            except ValueError:
                pass
        return notes

    # --- zapytania dla warstwy retrieval (etap 3, protokol SourceHealth) ---------
    #
    # Magazyn odpowiada na WASKIE pytania o oplacalnosc I/O; POLITYKA kolejnosci
    # (slot eksploracyjny itd.) mieszka w research.py. Kazde zapytanie jest
    # fail-safe: blad = odpowiedz neutralna (False/None), nigdy wyjatek.

    def _is_fresh(self, events: list[dict[str, Any]]) -> bool:
        """Czy ostatnia realna obserwacja (nie-transient) jest mlodsza niz RE_PROBE_HOURS."""
        day = _last_day(events, skip_transient=True)
        if not day:
            return False
        try:
            last = datetime.fromisoformat(day).replace(tzinfo=timezone.utc)
        except ValueError:
            return False
        return self._now() - last < timedelta(hours=RE_PROBE_HOURS)

    def section_dead(self, section_url: str) -> bool:
        """SWIEZA seria porazek sekcji (no_links/botblock/stale_path >=prog).

        Po RE_PROBE_HOURS bez proby status wygasa i sekcja wraca do puli."""
        try:
            data, _ = self._load()
            bucket = data["sections"].get(section_url)
            if not bucket:
                return False
            events = bucket.get("events", [])
            streak = _streak(events)
            if streak is None or streak[1] < MIN_STREAK_FOR_ADVISORY:
                return False
            if streak[0] not in _SECTION_DEAD_OUTCOMES:
                return False
            return self._is_fresh(events)
        except Exception:  # noqa: BLE001 - zdrowie doradcze: blad = neutralnie
            return False

    def section_last_probe(self, section_url: str) -> str | None:
        """Dzien ostatniej proby sekcji (dowolny wynik) - klucz slotu eksploracji."""
        try:
            data, _ = self._load()
            bucket = data["sections"].get(section_url)
            if not bucket:
                return None
            return _last_day(bucket.get("events", []))
        except Exception:  # noqa: BLE001 - zdrowie doradcze: blad = neutralnie
            return None

    def outlet_fetch_dead(self, provider_id: str) -> bool:
        """SWIEZA seria botblock outletu (fetch artykulow martwy, np. kicker 403).

        Tylko botblock: to jedyna klasa, gdzie pomijanie fetchu cos oszczedza
        (timeouty), a raw_content w pelni go zastepuje."""
        try:
            data, _ = self._load()
            bucket = data["outlets"].get(provider_id)
            if not bucket:
                return False
            events = bucket.get("events", [])
            streak = _streak(events)
            if streak is None or streak[1] < MIN_STREAK_FOR_ADVISORY:
                return False
            if streak[0] != "botblock":
                return False
            return self._is_fresh(events)
        except Exception:  # noqa: BLE001 - zdrowie doradcze: blad = neutralnie
            return False

    def report(self, country: str | None = None) -> str:
        """Raport konsolidacyjny dla operatora (python -m app.health).

        Czlowiek czyta raport i RECZNIE przenosi wnioski do country_media.json -
        konsolidacja semantyczna zostaje ludzka (zasada 2.2 architektury).
        """
        data, corrupt = self._load()
        wanted = [country] if country else None
        lines = [
            f"Magazyn zdrowia zrodel: {self.path}",
            f"Aktualizacja: {data.get('updated_at') or 'brak (zdrowie neutralne)'}",
        ]
        if corrupt:
            lines.append("UWAGA: plik nieczytelny - zdrowie buduje sie od nowa")
        lines.append("")
        shown = 0
        for provider_id, bucket in sorted(data["outlets"].items()):
            bucket_country = str(bucket.get("country", ""))
            if country and bucket_country != country:
                continue
            events = bucket.get("events", [])
            streak = _streak(events)
            tail = f"; seria: {streak[0]} x{streak[1]} (od {streak[2]})" if streak else ""
            lines.append(
                f"[{bucket_country}] {provider_id}: {len(events)} zdarzen w oknie{tail}"
            )
            shown += 1
        for section_url, bucket in sorted(data["sections"].items()):
            bucket_country = str(bucket.get("country", ""))
            if country and bucket_country != country:
                continue
            events = bucket.get("events", [])
            streak = _streak(events)
            tail = f"; seria: {streak[0]} x{streak[1]} (od {streak[2]})" if streak else ""
            lines.append(
                f"[{bucket_country}] sekcja {section_url}: {len(events)} zdarzen w oknie{tail}"
            )
            shown += 1
        if not shown:
            lines.append("(brak zdarzen - magazyn pusty lub kraj bez wpisow)")
        lines.append("")
        lines.append(f"Tavily 432: {data['search'].get('last_432_at') or 'brak'}")
        advisories = self.advisories(wanted)
        if advisories:
            lines.append("")
            lines.append(f"Advisories (prog >={MIN_STREAK_FOR_ADVISORY} zgodnych zdarzen):")
            lines.extend(f"- {note}" for note in advisories)
        return "\n".join(lines)
