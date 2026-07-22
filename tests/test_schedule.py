import unittest

from app.models import FakeModelGateway
from app.orchestration import EditorInChiefCoordinator
from app.schemas import MatchRequest, PackageStatus
from app.tools import (
    FakePageFetcher,
    FakeSearchClient,
    LiveFactsProvider,
    ScheduledMatch,
    ToolGateway,
    find_scheduled_match,
    load_schedule,
)
from app.agents import LlmFactsScout

SCHEDULE = [
    ScheduledMatch(
        date="2026-06-11",
        home="Meksyk",
        away="RPA",
        city="Mexico City",
        stadium="Estadio Ciudad de Mexico (Azteca)",
        stage="faza grupowa, mecz otwarcia",
        competition="Mistrzostwa Swiata 2026",
    ),
    ScheduledMatch(
        date="2026-07-01",
        home="Meksyk",
        away="RPA",
        city="Dallas",
        stadium="Dallas Stadium",
        stage="1/8 finalu",
        competition="Mistrzostwa Swiata 2026",
    ),
    ScheduledMatch(
        date="2026-06-12",
        home="Korea Poludniowa",
        away="Czechy",
        city="Guadalajara",
        stadium="Estadio Guadalajara (Akron)",
        competition="Mistrzostwa Swiata 2026",
    ),
]


class ScheduleLookupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.registry = ToolGateway().registry

    def test_finds_match_by_country_aliases_in_query(self) -> None:
        match = find_scheduled_match(
            self.registry, SCHEDULE, "Korea Poludniowa - Czechy mundial", None
        )
        self.assertIsNotNone(match)
        assert match is not None
        self.assertEqual(match.city, "Guadalajara")
        self.assertEqual(match.date, "2026-06-12")

    def test_date_hint_disambiguates_double_meeting(self) -> None:
        match = find_scheduled_match(self.registry, SCHEDULE, "Meksyk RPA", "2026-06-11")
        assert match is not None
        self.assertEqual(match.city, "Mexico City")

    def test_without_date_latest_meeting_wins(self) -> None:
        match = find_scheduled_match(self.registry, SCHEDULE, "Meksyk RPA", None)
        assert match is not None
        self.assertEqual(match.city, "Dallas")

    def test_date_hint_off_by_one_still_matches(self) -> None:
        # regresja Iran-NZ / Arabia-Urugwaj: mecz wieczorny w USA datowany lokalnie o dobe
        # inaczej niz date_hint usera - terminarz musi tolerowac +-1 dzien (inaczej brak venue)
        for hint in ("2026-06-11", "2026-06-13"):
            match = find_scheduled_match(
                self.registry, SCHEDULE, "Korea Poludniowa - Czechy", hint
            )
            self.assertIsNotNone(match, hint)
            assert match is not None
            self.assertEqual(match.date, "2026-06-12")

    def test_date_hint_two_days_off_is_rejected(self) -> None:
        # wieksza roznica = realnie inny mecz, nie granica strefy
        self.assertIsNone(
            find_scheduled_match(
                self.registry, SCHEDULE, "Korea Poludniowa - Czechy", "2026-06-14"
            )
        )

    def test_exact_date_preferred_over_neighbor(self) -> None:
        # gdy istnieje dokladne dopasowanie, +-1 nie moze go nadpisac
        match = find_scheduled_match(self.registry, SCHEDULE, "Meksyk RPA", "2026-06-11")
        assert match is not None
        self.assertEqual(match.date, "2026-06-11")

    def test_unknown_pair_returns_none(self) -> None:
        self.assertIsNone(
            find_scheduled_match(self.registry, SCHEDULE, "Brazylia - Argentyna", None)
        )

    def test_real_schedule_file_loads(self) -> None:
        matches = load_schedule()
        self.assertGreaterEqual(len(matches), 2)
        for match in matches:
            # placeholder drabinki ('Francja/Hiszpania' - uczestnik jeszcze nieznany)
            # jest dozwolony: lookup po nazwie kraju nigdy go nie trafi, a operator
            # podmienia go na kanoniczna pare po rozstrzygnieciu polfinalow
            if "/" in match.home or "/" in match.away:
                continue
            # kraje musza byc kanoniczne (klucze rejestru) - inaczej lookup nie zadziala
            self.assertIsNotNone(self.registry.country_profile(match.home), match.home)
            self.assertIsNotNone(self.registry.country_profile(match.away), match.away)


class ScheduleEnrichmentTests(unittest.TestCase):
    def test_facts_get_venue_date_and_stage_from_schedule(self) -> None:
        gateway = ToolGateway()
        # live facts nic nie znajduje -> fixture fallback (mexico_rpa_opener_2026)
        facts_research = LiveFactsProvider(
            registry=gateway.registry,
            search_client=FakeSearchClient(default_hits=[]),
            fetcher=FakePageFetcher(pages={}),
            scout=LlmFactsScout(FakeModelGateway(responses=[])),
            budget=gateway.budget,
        )
        coordinator = EditorInChiefCoordinator(
            gateway=gateway, facts_research=facts_research, schedule=SCHEDULE
        )
        run = coordinator.run(
            MatchRequest(
                match_query="Meksyk - RPA mecz otwarcia mundialu 2026",
                date_hint="2026-06-11",
            )
        )
        self.assertEqual(run.status, PackageStatus.NEEDS_HUMAN_REVIEW)  # fixture-fallback cap
        self.assertTrue(any("terminarz:" in note for note in run.notes))
        package = run.media_package
        self.assertIsNotNone(package)
        assert package is not None
        self.assertEqual(package.match.venue, "Estadio Ciudad de Mexico (Azteca), Mexico City")
        self.assertEqual(package.match.date, "2026-06-11")

    def test_auto_date_hint_from_schedule_on_frozen_request(self) -> None:
        # regresja: MatchRequest jest frozen - date_hint z terminarza musi isc
        # przez replace(), nie przez mutacje (FrozenInstanceError na runie live)
        gateway = ToolGateway()
        coordinator = EditorInChiefCoordinator(gateway=gateway, schedule=SCHEDULE)
        run = coordinator.run(
            MatchRequest(match_query="Meksyk - RPA mecz otwarcia mundialu 2026")
        )
        # SCHEDULE ma dwa spotkania tej pary; bez date_hint wygrywa najnowsze
        self.assertEqual(run.request.date_hint, "2026-07-01")


if __name__ == "__main__":
    unittest.main()
