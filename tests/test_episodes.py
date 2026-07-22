"""Etap 1 pamieci epizodycznej (architektura-pamiec-epizodyczna.md): telemetria
zdarzen operacyjnych + epizod w run.json. Czysta obserwowalnosc - testy pilnuja,
ze zdarzenia sa klasyfikowane wg runbooku (botblock/stale_path/transient) i ze
telemetria NICZEGO nie zmienia w zachowaniu retrievalu ani nie polyka bledow."""

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from app.memory import OutletHealthStore
from app.observability import (
    OutletFetchEvent,
    RunTelemetry,
    SearchEvent,
    classify_fetch_error,
    classify_search_error,
)
from app.observability.logging import RunLogger
from app.orchestration import EditorInChiefCoordinator
from app.schemas import MatchRequest, PackageStatus, SourceTier, to_plain
from app.tools import (
    AcquisitionMode,
    BudgetTracker,
    CountryMediaProfile,
    FakePageFetcher,
    FakeSearchClient,
    MatchContext,
    MediaOutletProfile,
    MediaResearchProvider,
    ProviderCapability,
    ProviderDescriptor,
    ResearchError,
    SearchHit,
    TelemetrySearchClient,
    ToolGateway,
)
from app.tools.research import _MIN_ARTICLE_BODY_FOR_SLIDE, collect_section_hits

URL_MX = "https://www.eluniversal.com.mx/deportes/mundial-mexico-sudafrica"

# pelna relacja (>= prog slajdu) vs cienki flash
ART_FULL = (
    "El Tri decepciona en su debut mundialista frente a Sudafrica. " * 20
)
ART_THIN = "El Tri decepciona en su debut."


class _FragmentScout:
    """Fake scout: zwraca poczatek tekstu jako 'cytat' (bez walidacji verbatim -
    ta jest w LlmMediaScout; provider testujemy na mechanice, nie na guardzie)."""

    def extract(self, context, outlet, language, url, text, max_fragments=1):
        return [text[:40]]


class _BotblockFetcher:
    """Fetcher symulujacy twardy 403 na poziomie artykulu (kicker-style)."""

    def fetch(self, url: str) -> str:
        raise ResearchError(f"fetch nieudany ({url}): 403 Forbidden", status_code=403)


class _StalePathLinksFetcher:
    """Fetcher sekcji odpowiadajacy 404 (zla sciezka po driftcie URL)."""

    def fetch(self, url: str) -> str:
        raise ResearchError(f"fetch nieudany ({url}): 404", status_code=404)

    def fetch_links(self, url: str):
        raise ResearchError(f"fetch sekcji nieudany ({url}): 404 Not Found", status_code=404)


class _Failing432SearchClient:
    def search(self, query, allowed_domains, limit=5):
        raise ResearchError(
            "Tavily search nieudany: Client error '432 unknown' for url 'https://api.tavily.com/search'"
        )


def _media_provider(fetcher, hits, telemetry, health=None):
    gateway = ToolGateway()
    return MediaResearchProvider(
        registry=gateway.registry,
        search_client=FakeSearchClient(default_hits=hits),
        fetcher=fetcher,
        scout=_FragmentScout(),
        budget=gateway.budget,
        telemetry=telemetry,
        health=health,
    )


class FetchClassificationTests(unittest.TestCase):
    """Mapowanie bledow na klasy runbooku: 403=botblock (NIE ruszac), 404=stale_path
    (czlowiek szuka nowego URL), 5xx/timeout=transient (ignorowac pojedyncze)."""

    def test_http_codes_map_to_runbook_classes(self) -> None:
        cases = {
            401: "botblock",
            403: "botblock",
            400: "stale_path",
            404: "stale_path",
            429: "transient",
            503: "transient",
        }
        for status, expected in cases.items():
            with self.subTest(status=status):
                error = ResearchError("fetch nieudany (url): x", status_code=status)
                self.assertEqual(classify_fetch_error(error), expected)

    def test_textual_fallbacks_for_errors_without_status(self) -> None:
        self.assertEqual(
            classify_fetch_error(ResearchError("pusta tresc po ekstrakcji: url")), "empty"
        )
        self.assertEqual(
            classify_fetch_error(ResearchError("fetch nieudany (url): ReadTimeout")),
            "transient",
        )
        self.assertEqual(classify_fetch_error(ResearchError("cos innego")), "error")

    def test_search_432_is_distinct_actionable_class(self) -> None:
        error = ResearchError("Tavily search nieudany: Client error '432 unknown'")
        self.assertEqual(classify_search_error(error), "432")


class OutletFetchEventTests(unittest.TestCase):
    def test_botblocked_fetch_emits_botblock_with_raw_content_flag(self) -> None:
        # kicker-scenariusz: fetch 403, ale raw_content z indeksu ratuje tresc -
        # event mowi 'problem KOLEJNOSCI (fetch martwy), nie utraty tresci'
        telemetry = RunTelemetry()
        hit = SearchHit(url=URL_MX, title="Mexico", snippet="...", raw_content=ART_FULL)
        provider = _media_provider(_BotblockFetcher(), [hit], telemetry)
        items = provider.research(MatchContext("Meksyk", "RPA", date="2026-06-11"), "Meksyk")

        self.assertTrue(items, "raw_content mial uratowac ekstrakcje mimo 403")
        events = telemetry.outlet_events
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].outcome, "botblock")
        self.assertEqual(events[0].provider_id, "ElUniversalMX")
        self.assertEqual(events[0].country, "Meksyk")
        self.assertTrue(events[0].had_raw_content)

    def test_full_article_emits_ok_and_thin_flash_emits_thin(self) -> None:
        for article, expected in ((ART_FULL, "ok"), (ART_THIN, "thin")):
            with self.subTest(expected=expected):
                telemetry = RunTelemetry()
                hit = SearchHit(url=URL_MX, title="Mexico", snippet="...")
                provider = _media_provider(
                    FakePageFetcher(pages={URL_MX: article}), [hit], telemetry
                )
                provider.research(MatchContext("Meksyk", "RPA", date="2026-06-11"), "Meksyk")
                events = telemetry.outlet_events
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0].outcome, expected)
                if expected == "ok":
                    self.assertGreaterEqual(events[0].body_len, _MIN_ARTICLE_BODY_FOR_SLIDE)

    def test_no_telemetry_means_no_behavior_change(self) -> None:
        # ColdStartParity dla etapu 1: provider bez telemetrii dziala jak dotad
        hit = SearchHit(url=URL_MX, title="Mexico", snippet="...")
        provider = _media_provider(FakePageFetcher(pages={URL_MX: ART_FULL}), [hit], None)
        items = provider.research(MatchContext("Meksyk", "RPA", date="2026-06-11"), "Meksyk")
        self.assertTrue(items)


class SectionProbeEventTests(unittest.TestCase):
    def test_live_section_emits_links_with_article_count(self) -> None:
        gateway = ToolGateway()
        egypt = gateway.registry.country_profile("Egipt")
        belgium = gateway.registry.country_profile("Belgia")
        recap = "https://www.filgoal.com/articles/531040/مصر-بلجيكا-كأس-العالم-حسام-حسن"
        fetcher = FakePageFetcher(
            pages={},
            links={
                "https://www.filgoal.com/": [(recap, "مصر تتعادل مع بلجيكا في كأس العالم")]
            },
        )
        telemetry = RunTelemetry()
        collect_section_hits(
            fetcher, gateway.budget, egypt, belgium, [], "media[Egipt]", telemetry=telemetry
        )
        filgoal = [
            e for e in telemetry.section_events if e.section_url == "https://www.filgoal.com/"
        ]
        self.assertEqual(len(filgoal), 1)
        self.assertEqual(filgoal[0].outcome, "links")
        self.assertEqual(filgoal[0].links_found, 1)
        self.assertGreaterEqual(filgoal[0].article_links, 1)

    def test_navigation_only_section_emits_no_links(self) -> None:
        # JS-wall (Curacao-scenariusz): 200 OK, zero linkow artykulowych
        gateway = ToolGateway()
        egypt = gateway.registry.country_profile("Egipt")
        belgium = gateway.registry.country_profile("Belgia")
        fetcher = FakePageFetcher(pages={}, links={"https://www.filgoal.com/": []})
        telemetry = RunTelemetry()
        collect_section_hits(
            fetcher, gateway.budget, egypt, belgium, [], "media[Egipt]", telemetry=telemetry
        )
        filgoal = [
            e for e in telemetry.section_events if e.section_url == "https://www.filgoal.com/"
        ]
        self.assertEqual(len(filgoal), 1)
        self.assertEqual(filgoal[0].outcome, "no_links")
        self.assertEqual(filgoal[0].article_links, 0)

    def test_404_section_emits_stale_path(self) -> None:
        gateway = ToolGateway()
        egypt = gateway.registry.country_profile("Egipt")
        belgium = gateway.registry.country_profile("Belgia")
        telemetry = RunTelemetry()
        collect_section_hits(
            _StalePathLinksFetcher(),
            gateway.budget,
            egypt,
            belgium,
            [],
            "media[Egipt]",
            telemetry=telemetry,
        )
        self.assertTrue(telemetry.section_events)
        self.assertTrue(all(e.outcome == "stale_path" for e in telemetry.section_events))


class TelemetrySearchClientTests(unittest.TestCase):
    def test_432_emits_event_and_reraises(self) -> None:
        telemetry = RunTelemetry()
        client = TelemetrySearchClient(inner=_Failing432SearchClient(), telemetry=telemetry)
        with self.assertRaises(ResearchError):
            client.search("Meksyk RPA Mundial 2026", ("eluniversal.com.mx",))
        self.assertEqual(len(telemetry.search_events), 1)
        self.assertEqual(telemetry.search_events[0].error, "432")
        self.assertEqual(telemetry.search_events[0].hits, 0)

    def test_success_emits_hit_count(self) -> None:
        telemetry = RunTelemetry()
        hits = [SearchHit(url=URL_MX, title="t", snippet="s")]
        client = TelemetrySearchClient(
            inner=FakeSearchClient(default_hits=hits), telemetry=telemetry
        )
        result = client.search("q", ("eluniversal.com.mx",))
        self.assertEqual(result, hits)
        self.assertEqual(telemetry.search_events[0].hits, 1)
        self.assertIsNone(telemetry.search_events[0].error)


class EpisodeInRunTests(unittest.TestCase):
    def test_completed_run_carries_episode_and_serializes(self) -> None:
        run = EditorInChiefCoordinator().run(
            MatchRequest(match_query="PSG - Arsenal, final Ligi Mistrzow 2026", post_type="data_story")
        )
        self.assertIsNotNone(run.episode)
        self.assertEqual(run.episode["run_id"], run.run_id)
        self.assertEqual(run.episode["status"], run.status.value)
        self.assertEqual(run.episode["outlet_events"], [])
        # caly run z epizodem musi byc JSON-owalny (zapis do run.json)
        json.dumps(to_plain(run), ensure_ascii=False)

    def test_halted_run_carries_episode_with_blocking(self) -> None:
        # halty to najciekawsze epizody (one_country_media_missing itd.)
        run = EditorInChiefCoordinator().run(MatchRequest(match_query="Atlantis FC - Moon United"))
        self.assertEqual(run.status, PackageStatus.INSUFFICIENT_EVIDENCE)
        self.assertIsNotNone(run.episode)
        self.assertEqual(run.episode["status"], "insufficient_evidence")
        self.assertTrue(run.episode["blocking"])

    def test_telemetry_resets_between_runs(self) -> None:
        # zdarzenia z poprzedniego runu nie moga przeciekac do nastepnego epizodu
        coordinator = EditorInChiefCoordinator()
        coordinator.run(
            MatchRequest(match_query="PSG - Arsenal, final Ligi Mistrzow 2026", post_type="data_story")
        )
        coordinator.telemetry.emit(
            OutletFetchEvent(provider_id="X", country="Y", url="https://x", outcome="ok")
        )
        run2 = coordinator.run(
            MatchRequest(match_query="PSG - Arsenal, final Ligi Mistrzow 2026", post_type="data_story")
        )
        self.assertEqual(run2.episode["outlet_events"], [])


# --- Etap 2: magazyn zdrowia zrodel (OutletHealthStore) --------------------------

HAPPY_MEDIA_QUERY = "Meksyk - RPA mecz otwarcia mundialu 2026"


def _episode_dict(
    at: str = "2026-07-10T12:00:00+00:00",
    run_id: str = "run_x",
    outlet_events=(),
    section_events=(),
    search_events=(),
) -> dict:
    return {
        "run_id": run_id,
        "at": at,
        "match_query": "q",
        "status": "ready",
        "blocking": [],
        "outlet_events": list(outlet_events),
        "section_events": list(section_events),
        "search_events": list(search_events),
    }


def _outlet_event(
    url: str = "https://www.kicker.de/spielbericht-1",
    outcome: str = "botblock",
    provider: str = "KickerDE",
    country: str = "Niemcy",
) -> dict:
    return {
        "provider_id": provider,
        "country": country,
        "url": url,
        "outcome": outcome,
        "body_len": 0,
        "had_raw_content": True,
    }


def _section_event(
    section: str = "https://www.vg.no/spesial/",
    outcome: str = "no_links",
    provider: str = "VGNO",
    country: str = "Norwegia",
    article_links: int = 0,
) -> dict:
    return {
        "provider_id": provider,
        "country": country,
        "section_url": section,
        "outcome": outcome,
        "links_found": 0,
        "article_links": article_links,
    }


def _seed_streak(store, outcome: str = "botblock", days=("2026-07-08", "2026-07-09", "2026-07-10"), **kwargs):
    """Seria zdarzen w ROZNYCH dniach (dedup tnie tylko ten sam dzien - re-rolle)."""
    for day in days:
        store.apply(
            _episode_dict(
                at=f"{day}T12:00:00+00:00",
                run_id=f"run_{day}",
                outlet_events=[_outlet_event(outcome=outcome, **kwargs)],
            )
        )


class _RecordingHealth:
    """Stub magazynu do testow okablowania koordynatora."""

    def __init__(self, advisories=None, fail=False):
        self.applied: list[dict] = []
        self._advisories = advisories or []
        self._fail = fail

    def advisories(self, countries=None):
        if self._fail:
            raise RuntimeError("magazyn padl")
        return list(self._advisories)

    def apply(self, episode):
        if self._fail:
            raise RuntimeError("magazyn padl")
        self.applied.append(episode)


class OutletHealthApplyTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / ".outlet_health.json"
        self.store = OutletHealthStore(path=self.path)

    def test_apply_appends_and_persists(self) -> None:
        self.store.apply(_episode_dict(outlet_events=[_outlet_event()]))
        data = json.loads(self.path.read_text(encoding="utf-8"))
        events = data["outlets"]["KickerDE"]["events"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["outcome"], "botblock")
        self.assertEqual(events[0]["day"], "2026-07-10")
        self.assertEqual(data["outlets"]["KickerDE"]["country"], "Niemcy")

    def test_rerolls_same_day_are_deduped(self) -> None:
        # re-roll tego samego meczu: ta sama porazka tego samego artykulu tego
        # samego dnia liczy sie RAZ (zdrowie to stan, nie czestotliwosc)
        for run_id in ("run_a", "run_b", "run_c"):
            self.store.apply(_episode_dict(run_id=run_id, outlet_events=[_outlet_event()]))
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(len(data["outlets"]["KickerDE"]["events"]), 1)

    def test_window_trims_oldest_events(self) -> None:
        store = OutletHealthStore(path=self.path, max_events=5)
        for index in range(8):
            store.apply(
                _episode_dict(
                    at=f"2026-07-{index + 1:02d}T12:00:00+00:00",
                    outlet_events=[_outlet_event()],
                )
            )
        data = json.loads(self.path.read_text(encoding="utf-8"))
        events = data["outlets"]["KickerDE"]["events"]
        self.assertEqual(len(events), 5)
        self.assertEqual(events[0]["day"], "2026-07-04")  # najstarsze wypadly

    def test_empty_episode_does_not_create_file(self) -> None:
        # runy fixture/testowe (zero zdarzen) nie churnuja pliku
        self.store.apply(_episode_dict())
        self.assertFalse(self.path.exists())

    def test_432_timestamp_stored(self) -> None:
        self.store.apply(
            _episode_dict(search_events=[{"query": "q", "hits": 0, "error": "432"}])
        )
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(data["search"]["last_432_at"], "2026-07-10T12:00:00+00:00")


class AdvisoryThresholdTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / ".outlet_health.json"
        self.store = OutletHealthStore(path=self.path)

    def test_below_threshold_no_advisory(self) -> None:
        _seed_streak(self.store, days=("2026-07-09", "2026-07-10"))
        self.assertEqual(self.store.advisories(["Niemcy"]), [])

    def test_botblock_streak_yields_advisory(self) -> None:
        _seed_streak(self.store)
        notes = self.store.advisories(["Niemcy"])
        self.assertEqual(len(notes), 1)
        self.assertIn("outlet_health[Niemcy]: KickerDE botblock 3x od 2026-07-08", notes[0])
        self.assertIn("raw_content", notes[0])

    def test_transient_does_not_break_streak(self) -> None:
        # czkawkowy timeout miedzy dwoma 403 nie zeruje obrazu bot-blocka
        _seed_streak(self.store, days=("2026-07-07", "2026-07-08"))
        self.store.apply(
            _episode_dict(
                at="2026-07-09T12:00:00+00:00",
                outlet_events=[_outlet_event(outcome="transient")],
            )
        )
        _seed_streak(self.store, days=("2026-07-10",))
        notes = self.store.advisories(["Niemcy"])
        self.assertEqual(len(notes), 1)
        self.assertIn("botblock 3x", notes[0])

    def test_ok_breaks_streak(self) -> None:
        # outlet ozdrowial: swiezy udany fetch kasuje advisory
        _seed_streak(self.store)
        self.store.apply(
            _episode_dict(
                at="2026-07-11T12:00:00+00:00",
                outlet_events=[_outlet_event(outcome="ok")],
            )
        )
        self.assertEqual(self.store.advisories(["Niemcy"]), [])

    def test_section_no_links_flags_jswall(self) -> None:
        for day in ("2026-07-08", "2026-07-09", "2026-07-10"):
            self.store.apply(
                _episode_dict(
                    at=f"{day}T12:00:00+00:00", section_events=[_section_event()]
                )
            )
        notes = self.store.advisories(["Norwegia"])
        self.assertEqual(len(notes), 1)
        self.assertIn("sekcja https://www.vg.no/spesial/ 3x zero linkow", notes[0])
        self.assertIn("JS-wall", notes[0])

    def test_section_stale_path_suggests_registry_update(self) -> None:
        for day in ("2026-07-08", "2026-07-09", "2026-07-10"):
            self.store.apply(
                _episode_dict(
                    at=f"{day}T12:00:00+00:00",
                    section_events=[_section_event(outcome="stale_path")],
                )
            )
        notes = self.store.advisories(["Norwegia"])
        self.assertEqual(len(notes), 1)
        self.assertIn("country_media.json", notes[0])

    def test_432_advisory_fresh_vs_stale(self) -> None:
        self.store.apply(
            _episode_dict(search_events=[{"query": "q", "hits": 0, "error": "432"}])
        )
        fresh = OutletHealthStore(
            path=self.path,
            now=lambda: datetime(2026, 7, 11, 12, 0, tzinfo=timezone.utc),
        )
        self.assertTrue(any("Tavily 432" in note for note in fresh.advisories()))
        stale = OutletHealthStore(
            path=self.path,
            now=lambda: datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc),
        )
        self.assertFalse(any("Tavily 432" in note for note in stale.advisories()))

    def test_country_filter(self) -> None:
        _seed_streak(self.store)  # Niemcy
        self.assertEqual(self.store.advisories(["Meksyk"]), [])
        self.assertTrue(self.store.advisories(["Niemcy"]))
        self.assertTrue(self.store.advisories())  # bez filtra: wszystko

    def test_report_contains_streak_and_advisory(self) -> None:
        _seed_streak(self.store)
        report = self.store.report()
        self.assertIn("KickerDE", report)
        self.assertIn("botblock x3", report)
        self.assertIn("outlet_health[Niemcy]", report)


class HealthNeverBlocksTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.path = Path(self._tmp.name) / ".outlet_health.json"

    def test_missing_file_is_neutral_cold_start(self) -> None:
        store = OutletHealthStore(path=self.path)
        self.assertEqual(store.advisories(["Niemcy"]), [])

    def test_corrupted_file_yields_reset_note_and_apply_recovers(self) -> None:
        self.path.write_text("{to nie jest json", encoding="utf-8")
        store = OutletHealthStore(path=self.path)
        notes = store.advisories()
        self.assertTrue(any("magazyn nieczytelny" in note for note in notes))
        # apply nadpisuje smieci swiezym stanem (reset) i nie rzuca
        store.apply(_episode_dict(outlet_events=[_outlet_event()]))
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertIn("KickerDE", data["outlets"])


class HealthIsolationTests(unittest.TestCase):
    def test_apply_never_touches_country_media_json(self) -> None:
        # zasada 2.2 architektury: magazyn zdrowia NIE modyfikuje Mapy Wiedzy
        registry_path = (
            Path(__file__).resolve().parents[1] / "data" / "sources" / "country_media.json"
        )
        before = registry_path.read_bytes()
        with tempfile.TemporaryDirectory() as tmp:
            store = OutletHealthStore(path=Path(tmp) / ".outlet_health.json")
            _seed_streak(store)
            store.advisories()
        self.assertEqual(registry_path.read_bytes(), before)


class CoordinatorHealthWiringTests(unittest.TestCase):
    def test_advisories_land_in_media_reaction_notes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = OutletHealthStore(path=Path(tmp) / ".outlet_health.json")
            _seed_streak(
                store,
                provider="ElUniversalMX",
                country="Meksyk",
                url="https://www.eluniversal.com.mx/deportes/x",
            )
            run = EditorInChiefCoordinator(health=store).run(
                MatchRequest(match_query=HAPPY_MEDIA_QUERY)
            )
        self.assertTrue(
            any("outlet_health[Meksyk]: ElUniversalMX botblock 3x" in n for n in run.notes),
            run.notes,
        )

    def test_apply_called_only_on_save_run(self) -> None:
        # realne runy (save_run) domykaja petle; ad-hoc nie zatruwa obrazu zdrowia
        health = _RecordingHealth()
        with tempfile.TemporaryDirectory() as tmp:
            coordinator = EditorInChiefCoordinator(
                logger=RunLogger(Path(tmp)), health=health
            )
            coordinator.run(MatchRequest(match_query=HAPPY_MEDIA_QUERY), save_run=False)
            self.assertEqual(health.applied, [])
            run = coordinator.run(MatchRequest(match_query=HAPPY_MEDIA_QUERY), save_run=True)
            self.assertEqual(len(health.applied), 1)
            self.assertEqual(health.applied[0]["run_id"], run.run_id)

    def test_health_failure_never_breaks_run(self) -> None:
        health = _RecordingHealth(fail=True)
        with tempfile.TemporaryDirectory() as tmp:
            run = EditorInChiefCoordinator(
                logger=RunLogger(Path(tmp)), health=health
            ).run(MatchRequest(match_query=HAPPY_MEDIA_QUERY), save_run=True)
        self.assertIsNotNone(run.media_package)
        self.assertTrue(any("advisories nieudane" in n for n in run.notes))


# --- Etap 3: zdrowie steruje kolejnoscia I/O (Select) ----------------------------

NOW = datetime(2026, 7, 10, 18, 0, tzinfo=timezone.utc)
FRESH_DAYS = ("2026-07-08", "2026-07-09", "2026-07-10")
STALE_DAYS = ("2026-07-01", "2026-07-02", "2026-07-03")  # > RE_PROBE_HOURS przed NOW

SECTIONS = tuple(f"https://test.example/sec{i}" for i in range(1, 6))


def _test_profile(sections=SECTIONS, country="Testland", provider="TestOutlet"):
    descriptor = ProviderDescriptor(
        provider_id=provider,
        tier=SourceTier.B,
        capabilities=frozenset({ProviderCapability.MEDIA_REACTION}),
        acquisition_mode=AcquisitionMode.RESEARCH,
        domains=("test.example",),
        country=country,
        language="en",
    )
    outlet = MediaOutletProfile(
        descriptor=descriptor, name="Test", sections=tuple(sections),
        confidence="high", verified_at=None,
    )
    return CountryMediaProfile(
        country=country, language="en", iso2="TL", confederation="UEFA", role="team",
        team_names=("Testland",), query_templates=("{team} recap",),
        outlets=(outlet,), english_name=None, world_cup="WC 2026",
    )


OPPONENT_PROFILE = CountryMediaProfile(
    country="Oppland", language="en", iso2="OP", confederation="UEFA", role="team",
    team_names=("Oppland",), query_templates=("{team} recap",),
    outlets=(
        MediaOutletProfile(
            descriptor=ProviderDescriptor(
                provider_id="OppOutlet",
                tier=SourceTier.B,
                capabilities=frozenset({ProviderCapability.MEDIA_REACTION}),
                acquisition_mode=AcquisitionMode.RESEARCH,
                domains=("opp.example",),
                country="Oppland",
                language="en",
            ),
            name="Opp", sections=(), confidence="high", verified_at=None,
        ),
    ),
    english_name=None, world_cup=None,
)


def _seed_section_streak(store, section, outcome="no_links", days=FRESH_DAYS):
    for day in days:
        store.apply(
            _episode_dict(
                at=f"{day}T12:00:00+00:00",
                run_id=f"run_{day}_{outcome}",
                section_events=[
                    _section_event(
                        section=section, outcome=outcome, provider="TestOutlet",
                        country="Testland",
                        article_links=5 if outcome == "links" else 0,
                    )
                ],
            )
        )


def _probe_order(fetcher_links, health, max_sections=4):
    """Uruchamia sondowanie sekcji i zwraca KOLEJNOSC faktycznych prob (fetch_links)."""
    fetcher = FakePageFetcher(pages={}, links=fetcher_links)
    diag: list[str] = []
    collect_section_hits(
        fetcher, BudgetTracker(), _test_profile(), OPPONENT_PROFILE, diag,
        "media[Testland]", max_sections=max_sections, health=health,
    )
    return fetcher.calls, diag


class _BrokenHealth:
    def section_dead(self, section_url):
        raise RuntimeError("magazyn padl")

    def section_last_probe(self, section_url):
        raise RuntimeError("magazyn padl")

    def outlet_fetch_dead(self, provider_id):
        raise RuntimeError("magazyn padl")


class ColdStartParityTests(unittest.TestCase):
    """Bez zdrowia / z pustym magazynem / przy awarii magazynu kolejnosc probowania
    sekcji MUSI byc identyczna z rejestrem - pamiec nie zmienia zachowania, dopoki
    niczego nie wie."""

    def setUp(self) -> None:
        self.links = {s: [] for s in SECTIONS}

    def test_no_health_probes_registry_order(self) -> None:
        calls, _ = _probe_order(self.links, health=None)
        self.assertEqual(calls, list(SECTIONS[:4]))

    def test_empty_store_probes_registry_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            store = OutletHealthStore(path=Path(tmp) / "h.json", now=lambda: NOW)
            calls, diag = _probe_order(self.links, health=store)
        self.assertEqual(calls, list(SECTIONS[:4]))
        self.assertFalse(any("zdemotowanych" in d for d in diag))

    def test_broken_health_probes_registry_order(self) -> None:
        calls, _ = _probe_order(self.links, health=_BrokenHealth())
        self.assertEqual(calls, list(SECTIONS[:4]))


class SectionOrderingTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = OutletHealthStore(path=Path(self._tmp.name) / "h.json", now=lambda: NOW)
        self.links = {s: [] for s in SECTIONS}

    def test_dead_section_demoted_out_of_budget(self) -> None:
        # sec1 z JS-wallowa seria spada za budzet; do budzetu wchodza zdrowe
        _seed_section_streak(self.store, SECTIONS[0], outcome="no_links")
        calls, diag = _probe_order(self.links, health=self.store, max_sections=3)
        self.assertEqual(calls, [SECTIONS[1], SECTIONS[2], SECTIONS[3]])
        self.assertTrue(any("1 sekcji zdemotowanych" in d for d in diag))

    def test_exploration_slot_prefers_never_probed(self) -> None:
        # sec1-3 probowane dzis, sec4 dwa dni temu, sec5 NIGDY: ostatni slot
        # budzetu bierze nieznana sekcje (rotacja poza sztywne pierwsze 4)
        for section in SECTIONS[:3]:
            _seed_section_streak(self.store, section, outcome="links", days=("2026-07-10",))
        _seed_section_streak(self.store, SECTIONS[3], outcome="links", days=("2026-07-08",))
        calls, _ = _probe_order(self.links, health=self.store, max_sections=4)
        self.assertEqual(calls, [SECTIONS[0], SECTIONS[1], SECTIONS[2], SECTIONS[4]])

    def test_exploration_slot_prefers_oldest_probe_when_all_known(self) -> None:
        for section in SECTIONS[:3]:
            _seed_section_streak(self.store, section, outcome="links", days=("2026-07-10",))
        _seed_section_streak(self.store, SECTIONS[3], outcome="links", days=("2026-07-08",))
        _seed_section_streak(self.store, SECTIONS[4], outcome="links", days=("2026-07-09",))
        calls, _ = _probe_order(self.links, health=self.store, max_sections=4)
        self.assertEqual(calls, [SECTIONS[0], SECTIONS[1], SECTIONS[2], SECTIONS[3]])


class ReprobeTests(unittest.TestCase):
    """Status 'martwy' wygasa po RE_PROBE_HOURS bez proby: demote dziala tylko na
    swiezych obserwacjach, stare same traca moc."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = OutletHealthStore(path=Path(self._tmp.name) / "h.json", now=lambda: NOW)

    def test_stale_dead_section_returns_to_pool(self) -> None:
        _seed_section_streak(self.store, SECTIONS[0], outcome="no_links", days=STALE_DAYS)
        self.assertFalse(self.store.section_dead(SECTIONS[0]))
        calls, _ = _probe_order({s: [] for s in SECTIONS}, health=self.store, max_sections=4)
        self.assertEqual(calls[0], SECTIONS[0])  # normalna proba, obraz sie nadpisze

    def test_fresh_dead_section_is_dead(self) -> None:
        _seed_section_streak(self.store, SECTIONS[0], outcome="no_links", days=FRESH_DAYS)
        self.assertTrue(self.store.section_dead(SECTIONS[0]))

    def test_stale_botblock_outlet_gets_fetch_again(self) -> None:
        _seed_streak(
            self.store, provider="ElUniversalMX", country="Meksyk",
            url="https://www.eluniversal.com.mx/deportes/a", days=STALE_DAYS,
        )
        self.assertFalse(self.store.outlet_fetch_dead("ElUniversalMX"))
        fetcher = FakePageFetcher(pages={URL_MX: ART_FULL})
        hit = SearchHit(url=URL_MX, title="Mexico", snippet="...", raw_content=ART_FULL)
        provider = _media_provider(fetcher, [hit], RunTelemetry(), health=self.store)
        provider.research(MatchContext("Meksyk", "RPA", date="2026-06-11"), "Meksyk")
        self.assertIn(URL_MX, fetcher.calls)


class OutletFetchSkipTests(unittest.TestCase):
    """raw_content-first: swiezy streak botblock + raw_content z indeksu = zero prob
    fetchu (oszczedzamy timeouty); bez raw_content fetch idzie MIMO streaka."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = OutletHealthStore(path=Path(self._tmp.name) / "h.json", now=lambda: NOW)
        _seed_streak(
            self.store, provider="ElUniversalMX", country="Meksyk",
            url="https://www.eluniversal.com.mx/deportes/a", days=FRESH_DAYS,
        )

    def test_fresh_botblock_skips_fetch_and_uses_raw_content(self) -> None:
        fetcher = FakePageFetcher(pages={})  # fetch rzucilby i zapisal call
        telemetry = RunTelemetry()
        hit = SearchHit(url=URL_MX, title="Mexico", snippet="...", raw_content=ART_FULL)
        provider = _media_provider(fetcher, [hit], telemetry, health=self.store)
        diag: list[str] = []
        items = provider.research(
            MatchContext("Meksyk", "RPA", date="2026-06-11"), "Meksyk", notes=diag
        )
        self.assertTrue(items, "raw_content mial dac cytat bez fetchu")
        # sondy SEKCJI (fetch_links) sa dozwolone; pominiety ma byc fetch ARTYKULU
        self.assertNotIn(URL_MX, fetcher.calls)
        self.assertTrue(any("fetch pominiety" in d for d in diag))
        # brak proby = brak zdarzenia: obraz zdrowia starzeje sie ku re-probe
        self.assertEqual(telemetry.outlet_events, [])

    def test_no_raw_content_fetches_despite_streak(self) -> None:
        # dostepnosc tresci wygrywa z optymalizacja: bez raw_content probujemy fetch
        fetcher = FakePageFetcher(pages={URL_MX: ART_FULL})
        hit = SearchHit(url=URL_MX, title="Mexico", snippet="...")
        provider = _media_provider(fetcher, [hit], RunTelemetry(), health=self.store)
        items = provider.research(MatchContext("Meksyk", "RPA", date="2026-06-11"), "Meksyk")
        self.assertIn(URL_MX, fetcher.calls)
        self.assertTrue(items)

    def test_broken_health_fetches_normally(self) -> None:
        fetcher = FakePageFetcher(pages={URL_MX: ART_FULL})
        hit = SearchHit(url=URL_MX, title="Mexico", snippet="...", raw_content=ART_FULL)
        provider = _media_provider(fetcher, [hit], RunTelemetry(), health=_BrokenHealth())
        provider.research(MatchContext("Meksyk", "RPA", date="2026-06-11"), "Meksyk")
        self.assertIn(URL_MX, fetcher.calls)


class RunTelemetryUnitTests(unittest.TestCase):
    def test_emit_dispatch_and_as_dict(self) -> None:
        telemetry = RunTelemetry()
        telemetry.emit(
            OutletFetchEvent(provider_id="P", country="C", url="https://u", outcome="ok")
        )
        telemetry.emit(SearchEvent(query="q", hits=3))
        payload = telemetry.as_dict()
        self.assertEqual(payload["outlet_events"][0]["provider_id"], "P")
        self.assertEqual(payload["search_events"][0]["hits"], 3)
        self.assertEqual(payload["section_events"], [])
        telemetry.reset()
        self.assertEqual(telemetry.as_dict()["outlet_events"], [])


if __name__ == "__main__":
    unittest.main()
