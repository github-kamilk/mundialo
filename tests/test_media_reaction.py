import json
import unittest
from dataclasses import replace

from app.agents import (
    FixtureTranslator,
    LlmMediaEditorial,
    LlmMediaTranslator,
    build_media_package,
    collect_media,
)
from app.agents.editorial import MatchResearcher
from app.agents.media_reaction import _synonym_hint
from app.evaluation.judges import MediaFactChecker, MediaQualityJudge
from app.models import FakeModelGateway, GenerationError
from app.orchestration import EditorInChiefCoordinator
from app.orchestration.coordinator import OPERATOR_OVERRIDE_NOTE
from app.schemas import EvidenceStore, MatchRequest, PackageStatus
from app.tools import ToolGateway

HAPPY_ID = "mexico_rpa_opener_2026"
HAPPY_QUERY = "Meksyk - RPA mecz otwarcia mundialu 2026"

MX_RESP = json.dumps(
    {
        "quotes": [
            {"evidence_id": "e_mx_universal", "translation_pl": "Tri zaczyna mundial z watpliwosciami."},
            {"evidence_id": "e_mx_record", "translation_pl": "Rozczarowanie na otwarcie w Meksyku."},
        ],
        "mood_summary": "Prasa w Meksyku pisze o niedosycie.",
    }
)
ZA_RESP = json.dumps(
    {
        "quotes": [
            {"evidence_id": "e_za_news24", "translation_pl": "Bafana schodzi z podniesionymi glowami."},
            {"evidence_id": "e_za_supersport", "translation_pl": "Remis, ktory smakuje jak manifest."},
        ],
        "mood_summary": "Media w RPA pisza o dumie.",
    }
)


def _raw(match_id: str, country: str):
    gateway = ToolGateway()
    return gateway, gateway.fetch_media_reactions(match_id, country)


def _happy_package():
    gateway = ToolGateway()
    evidence = EvidenceStore()
    facts = MatchResearcher(gateway).fetch_facts(HAPPY_ID, evidence)
    countries = [facts.home_team, facts.away_team]
    raw = collect_media(gateway, HAPPY_ID, countries, evidence)
    panels = [FixtureTranslator().write_panel(country, raw[country]) for country in countries]
    package = build_media_package("mpkg_test", facts, panels, evidence)
    return package, evidence


class MediaGatewayTests(unittest.TestCase):
    def test_returns_only_trusted_quotes(self) -> None:
        _, items = _raw(HAPPY_ID, "Meksyk")
        self.assertEqual(len(items), 2)
        self.assertEqual({item.outlet for item in items}, {"ElUniversalMX", "RecordMX"})
        for item in items:
            self.assertTrue(item.url.startswith("https://"))
            self.assertTrue(item.original_text)

    def test_drops_url_outside_whitelist(self) -> None:
        _, items = _raw("mexico_rpa_offwhitelist_2026", "RPA")
        self.assertEqual(items, [])

    def test_sanitizes_injection_in_original(self) -> None:
        _, items = _raw("mexico_rpa_injection_2026", "Meksyk")
        self.assertEqual(len(items), 1)
        self.assertIn("[usunieto", items[0].original_text)
        self.assertNotIn("Ignore all previous", items[0].original_text)


class FixtureTranslatorTests(unittest.TestCase):
    def test_builds_quotes_from_gold_translation(self) -> None:
        _, raw = _raw(HAPPY_ID, "Meksyk")
        panel = FixtureTranslator().write_panel("Meksyk", raw)
        self.assertEqual(len(panel.quotes), 2)
        self.assertEqual(panel.source_count, 2)
        self.assertIsNone(panel.mood_summary)
        self.assertTrue(all(quote.translation_pl for quote in panel.quotes))


class LlmMediaTranslatorTests(unittest.TestCase):
    def test_valid_response_builds_panel_with_mood(self) -> None:
        _, raw = _raw(HAPPY_ID, "Meksyk")
        translator = LlmMediaTranslator(FakeModelGateway(responses=[MX_RESP]))
        panel = translator.write_panel("Meksyk", raw)
        self.assertEqual(len(panel.quotes), 2)
        self.assertEqual(panel.source_count, 2)
        self.assertIsNotNone(panel.mood_summary)

    def test_system_prompt_requires_mood_with_two_sources(self) -> None:
        # regresja (Hiszpania-RZP): gpt-4o losowo zwracal null w mood_summary mimo 2
        # cytatow, a kod cicho je gubil - prompt ma jasno WYMAGAC mood przy >=2 zrodlach
        translator = LlmMediaTranslator(FakeModelGateway(responses=[]))
        system = translator._system_prompt("Republika Zielonego Przyladka", "0-0")
        self.assertIn("mood_summary", system)
        self.assertIn("WYMAGANE", system)

    def test_system_prompt_demands_factual_fidelity_and_idiom(self) -> None:
        # regresja Francja-Senegal: streszczenie mylilo role ('Olise otworzyl wynik asysta'
        # zamiast 'Mbappe strzelil, Olise asystowal') i bylo kalka ze zrodla ('swietlista
        # inspiracja'). Prompt ma egzekwowac wiernosc rolom + naturalna polszczyzne.
        translator = LlmMediaTranslator(FakeModelGateway(responses=[]))
        system = translator._system_prompt("Francja", "3-1")
        self.assertIn("WIERNOSC FAKTOM", system)
        self.assertIn("asystowal", system)
        self.assertIn("kalka", system)  # anty-kalka: idiomatyczny PL
        self.assertIn("protokol taktyczny", system)  # anty-zargon

    def test_system_prompt_forbids_fabricated_conclusions(self) -> None:
        # regresja Austria-Jordania (run_20260617101300): streszczenie czysto
        # informacyjnego newsu o rankingu doklejalo wymyslona teze ('to sygnal do zmian
        # w skladzie'), ktorej w artykule nie ma. Prompt ma zakazac dorabiania wnioskow.
        system = LlmMediaTranslator(FakeModelGateway(responses=[]))._system_prompt("Jordania", "3-1")
        self.assertIn("rekomendacji", system)
        self.assertIn("suchy news", system)

    def test_single_source_suppresses_mood(self) -> None:
        _, raw = _raw(HAPPY_ID, "Meksyk")
        # jeden dostepny artykul: mood_summary wymaga >=2 zrodel, wiec ma byc stlumione
        single_item = [item for item in raw if item.evidence_id == "e_mx_universal"]
        single = json.dumps(
            {
                "quotes": [{"evidence_id": "e_mx_universal", "translation_pl": "Tri zaczyna z watpliwosciami."}],
                "mood_summary": "Prasa pisze cos zbiorczego.",
            }
        )
        panel = LlmMediaTranslator(FakeModelGateway(responses=[single])).write_panel("Meksyk", single_item)
        self.assertEqual(len(panel.quotes), 1)
        self.assertIsNone(panel.mood_summary)

    def test_rejects_hallucinated_evidence(self) -> None:
        _, raw = _raw(HAPPY_ID, "Meksyk")
        bad = json.dumps({"quotes": [{"evidence_id": "e_nieistnieje", "translation_pl": "x"}], "mood_summary": None})
        translator = LlmMediaTranslator(FakeModelGateway(responses=[bad] * 5))
        with self.assertRaises(GenerationError):
            translator.write_panel("Meksyk", raw)

    def test_rejects_banned_translation(self) -> None:
        _, raw = _raw(HAPPY_ID, "Meksyk")
        bad = json.dumps(
            {
                "quotes": [{"evidence_id": "e_mx_universal", "translation_pl": "To bylo absolutnie niesamowite."}],
                "mood_summary": None,
            }
        )
        translator = LlmMediaTranslator(FakeModelGateway(responses=[bad] * 5))
        with self.assertRaises(GenerationError):
            translator.write_panel("Meksyk", raw)

    def test_recovers_when_retry_swaps_banned_for_allowed_synonym(self) -> None:
        # Regresja Szwecja-Tunezja (run_20260615143513): cytat Isaka 'Otroligt'
        # ('niesamowite') wpadal w zakazane slowo i tlumacz zaliczyl 5 nieudanych prob
        # -> translation_unavailable. Po doprecyzowaniu promptu/feedbacku model ma
        # oddac sens dozwolonym synonimem; petla MUSI sie z tego podniesc na retry.
        _, raw = _raw(HAPPY_ID, "Meksyk")
        banned_first = json.dumps(
            {
                "quotes": [
                    {"evidence_id": "e_mx_universal", "translation_pl": "To bylo niesamowite."},
                    {"evidence_id": "e_mx_record", "translation_pl": "Rozczarowanie na otwarcie."},
                ],
                "mood_summary": None,
            }
        )
        translator = LlmMediaTranslator(FakeModelGateway(responses=[banned_first, MX_RESP]))
        panel = translator.write_panel("Meksyk", raw)
        self.assertEqual(len(panel.quotes), 2)
        # panel zbudowany z czystej, ponowionej odpowiedzi (mood z MX_RESP), bez banowanego rdzenia
        self.assertIsNotNone(panel.mood_summary)
        joined = " ".join(q.translation_pl for q in panel.quotes).lower()
        self.assertNotIn("niesamowit", joined)

    def test_synonym_hint_covers_absolutnie_cognate(self) -> None:
        # Regresja Argentyna-Algieria (run_20260617085257): hiszp. cytat 'una actuacion
        # absolutamente esperanzadora' -> 'absolutnie' wpadal w zakazane slowo, a feedback
        # retry podpowiadal synonimy TYLKO dla 'niesamowite'/'szok'. Bez celowanego
        # zamiennika dla 'absolutnie' tlumacz powtarzal leksem 5x -> translation_unavailable.
        hint = _synonym_hint(["absolutnie"])
        self.assertIn("absolutnie", hint)
        # konkretny dozwolony zamiennik, nie tylko generyczne 'nie uzywaj'
        self.assertTrue(any(word in hint for word in ("kompletnie", "w pelni", "zupelnie")))
        self.assertNotIn("oddaj sens neutralnie", hint)

    def test_recovers_when_retry_swaps_absolutnie_for_allowed_synonym(self) -> None:
        # Ten sam mechanizm co Szwecja-Tunezja, ale dla kognatu 'absolutamente'->'absolutnie':
        # petla MUSI sie podniesc, gdy retry oddaje sens dozwolonym synonimem.
        _, raw = _raw(HAPPY_ID, "Meksyk")
        banned_first = json.dumps(
            {
                "quotes": [
                    {"evidence_id": "e_mx_universal", "translation_pl": "To byl absolutnie obiecujacy poczatek."},
                    {"evidence_id": "e_mx_record", "translation_pl": "Rozczarowanie na otwarcie."},
                ],
                "mood_summary": None,
            }
        )
        translator = LlmMediaTranslator(FakeModelGateway(responses=[banned_first, MX_RESP]))
        panel = translator.write_panel("Meksyk", raw)
        self.assertEqual(len(panel.quotes), 2)
        joined = " ".join(q.translation_pl for q in panel.quotes).lower()
        self.assertNotIn("absolutnie", joined)


class AssemblerTests(unittest.TestCase):
    def test_carousel_follows_required_structure(self) -> None:
        package, _ = _happy_package()
        roles = [slide.role for slide in package.carousel.slides]
        self.assertEqual(roles[0], "title")
        self.assertEqual(roles[-1], "sources")
        self.assertEqual(roles.count("media_country"), 4)
        self.assertEqual(len(package.carousel.slides), 6)

    def test_quote_evidence_is_marked_used(self) -> None:
        package, evidence = _happy_package()
        self.assertTrue(package.quote_evidence_ids())
        for evidence_id in package.quote_evidence_ids():
            item = evidence.get(evidence_id)
            self.assertIsNotNone(item)
            assert item is not None
            self.assertTrue(item.used_in_output)


class MediaJudgeTests(unittest.TestCase):
    def test_happy_package_passes_both_judges(self) -> None:
        package, evidence = _happy_package()
        self.assertEqual(MediaFactChecker().validate(package, evidence).status, "pass")
        self.assertEqual(MediaQualityJudge().validate(package).status, "pass")

    def test_missing_original_blocks_fact_check(self) -> None:
        package, _ = _happy_package()
        report = MediaFactChecker().validate(package, EvidenceStore())
        self.assertIn("original_retained_in_evidence", report.blocking_issues)

    def test_mood_without_two_sources_blocks(self) -> None:
        package, _ = _happy_package()
        bad_panel = replace(
            package.panels[0],
            mood_summary="Prasa pisze cos zbiorczego.",
            source_count=1,
            quotes=package.panels[0].quotes[:1],
        )
        broken = replace(package, panels=[bad_panel, package.panels[1]])
        report = MediaQualityJudge().validate(broken)
        self.assertIn("mood_requires_two_sources", report.blocking_issues)

    def test_banned_phrase_in_translation_blocks(self) -> None:
        package, _ = _happy_package()
        bad_quote = replace(package.panels[0].quotes[0], translation_pl="To bylo absolutnie niesamowite.")
        bad_panel = replace(package.panels[0], quotes=[bad_quote, *package.panels[0].quotes[1:]])
        broken = replace(package, panels=[bad_panel, package.panels[1]])
        report = MediaQualityJudge().validate(broken)
        self.assertIn("no_banned_phrases", report.blocking_issues)


class ScoreConsistencyTests(unittest.TestCase):
    """Cross-check wyniku z faktow ze wzmiankami w mediach (bezpiecznik na stale fakty)."""

    def test_score_mismatch_with_media_blocks(self) -> None:
        package, evidence = _happy_package()  # fakty fixture: 1-1
        panel = package.panels[0]
        quote = replace(
            panel.quotes[0],
            original_text="Bafana Bafana had a day to forget going down 2-0 at the Azteca.",
        )
        panel2 = replace(panel, quotes=[quote, *panel.quotes[1:]])
        package2 = replace(package, panels=[panel2, *package.panels[1:]])
        report = MediaFactChecker().validate(package2, evidence)
        self.assertIn("score_consistent_with_media", report.blocking_issues)

    def test_matching_score_mention_passes(self) -> None:
        package, evidence = _happy_package()  # fakty fixture: 1-1
        panel = package.panels[0]
        quote = replace(panel.quotes[0], original_text="A 1-1 draw that felt like a loss.")
        panel2 = replace(panel, quotes=[quote, *panel.quotes[1:]])
        package2 = replace(package, panels=[panel2, *package.panels[1:]])
        report = MediaFactChecker().validate(package2, evidence)
        self.assertNotIn("score_consistent_with_media", report.blocking_issues)

    def test_no_score_mentions_is_silent(self) -> None:
        package, evidence = _happy_package()
        report = MediaFactChecker().validate(package, evidence)
        self.assertNotIn("score_consistent_with_media", report.blocking_issues)

    @staticmethod
    def _with_facts_and_summary(package, full_time, summary):
        score = replace(package.match.score, full_time=full_time)
        match = replace(package.match, score=score)
        panel = package.panels[0]
        quote = replace(panel.quotes[0], summary_pl=summary)
        panel2 = replace(panel, quotes=[quote, *panel.quotes[1:]])
        return replace(package, match=match, panels=[panel2, *package.panels[1:]])

    def test_halftime_score_in_summary_does_not_block(self) -> None:
        # Regresja Francja-Senegal 3-1 (run_20260617084327): streszczenie wymienia
        # tylko wynik DO PRZERWY (0-0), a 3-1 opisuje slownie. To nie konflikt.
        package, evidence = _happy_package()
        summary = (
            "Pierwsza połowa była wyrównana i zakończyła się wynikiem 0-0, ale to "
            "Francja przejęła inicjatywę w drugiej połowie. Mbappé, autor dwóch "
            "kluczowych bramek, był nie do zatrzymania."
        )
        package2 = self._with_facts_and_summary(package, "3-1", summary)
        report = MediaFactChecker().validate(package2, evidence)
        self.assertNotIn("score_consistent_with_media", report.blocking_issues)

    def test_french_halftime_marker_does_not_block(self) -> None:
        package, evidence = _happy_package()
        panel = package.panels[0]
        quote = replace(
            panel.quotes[0],
            original_text="Score nul et vierge à la mi-temps, 0-0 entre les deux équipes.",
        )
        score = replace(package.match.score, full_time="3-1")
        match = replace(package.match, score=score)
        package2 = replace(
            package,
            match=match,
            panels=[replace(panel, quotes=[quote, *panel.quotes[1:]]), *package.panels[1:]],
        )
        report = MediaFactChecker().validate(package2, evidence)
        self.assertNotIn("score_consistent_with_media", report.blocking_issues)

    def test_shootout_score_in_spanish_quote_does_not_block(self) -> None:
        # Regresja Niemcy-Paragwaj 1-1 (run_20260630100914): recap ABC mowi o
        # "4-3 en la definición por penales" - to wynik karnych, nie regulaminowy.
        # Nie moze blokowac vs koncowe 1-1 (--score operatora).
        package, evidence = _happy_package()
        panel = package.panels[0]
        quote = replace(
            panel.quotes[0],
            original_text=(
                "La selección paraguaya escribió una de las páginas más gloriosas de "
                "su historia al derrotar a Alemania por 4-3 en la definición por penales."
            ),
        )
        panel2 = replace(panel, quotes=[quote, *panel.quotes[1:]])
        package2 = replace(package, panels=[panel2, *package.panels[1:]])  # fakty fixture: 1-1
        report = MediaFactChecker().validate(package2, evidence)
        self.assertNotIn("score_consistent_with_media", report.blocking_issues)

    def test_shootout_score_in_polish_translation_does_not_block(self) -> None:
        # Ten sam mecz, ale 4-3 siedzi w NASZYM tlumaczeniu PL ("w serii rzutow karnych").
        package, evidence = _happy_package()
        summary = (
            "Paragwaj napisał jedną z najchlubniejszych kart w historii, pokonując "
            "Niemcy 4-3 w serii rzutów karnych po remisie w regulaminowym czasie."
        )
        package2 = self._with_facts_and_summary(package, "1-1", summary)
        report = MediaFactChecker().validate(package2, evidence)
        self.assertNotIn("score_consistent_with_media", report.blocking_issues)

    def test_german_shootout_marker_does_not_block(self) -> None:
        # Niemiecki recap: "4:3 nach Elfmeterschießen" (ß->ss przy foldowaniu).
        package, evidence = _happy_package()
        panel = package.panels[0]
        quote = replace(
            panel.quotes[0],
            original_text="Paraguay gewinnt 4:3 nach Elfmeterschießen und Deutschland ist raus.",
        )
        package2 = replace(
            package, panels=[replace(panel, quotes=[quote, *panel.quotes[1:]]), *package.panels[1:]]
        )  # fakty fixture: 1-1
        report = MediaFactChecker().validate(package2, evidence)
        self.assertNotIn("score_consistent_with_media", report.blocking_issues)

    def test_dutch_and_scandinavian_shootout_markers_do_not_block(self) -> None:
        # NL/NO/SV: wynik serii karnych (4-3) przy koncowym 1-1 z faktow fixture.
        shootout_quotes = (
            ("nl", "Oranje wint na strafschoppen met 4-3 en gaat door naar de volgende ronde."),
            ("no", "Landslaget vant 4-3 i straffesparkkonkurransen etter en dramatisk kveld."),
            ("sv", "Landslaget vann med 4-3 på straffar efter förlängningen."),
        )
        for lang, text in shootout_quotes:
            with self.subTest(lang=lang):
                package, evidence = _happy_package()
                panel = package.panels[0]
                quote = replace(panel.quotes[0], original_text=text)
                package2 = replace(
                    package,
                    panels=[replace(panel, quotes=[quote, *panel.quotes[1:]]), *package.panels[1:]],
                )  # fakty fixture: 1-1
                report = MediaFactChecker().validate(package2, evidence)
                self.assertNotIn("score_consistent_with_media", report.blocking_issues)

    def test_regulation_score_before_extra_time_does_not_block(self) -> None:
        # Faza pucharowa: mecz rozstrzygniety W DOGRYWCE. Recap wymienia wynik
        # PO 90 MINUTACH (0-0/1-0), rozny od koncowego 1-1 z faktow fixture -
        # to wynik czastkowy, nie konflikt (ta sama klasa co wynik do przerwy).
        et_quotes = (
            ("pl", "Po 90 minutach było 0-0, dopiero dogrywka przyniosła rozstrzygnięcie."),
            ("en", "It was 0-0 after 90 minutes and the tie needed extra time."),
            ("fr", "Le score était de 0-0 à l'issue du temps réglementaire, avant la prolongation."),
            ("es", "El marcador era 0-0 en el tiempo reglamentario y el pase se decidió en la prórroga."),
            ("pt", "Estava 0-0 no tempo regulamentar e a decisão foi para o prolongamento."),
            ("de", "Nach 90 Minuten stand es 0:0, erst die Verlängerung brachte die Entscheidung."),
            ("nl", "Na de reguliere speeltijd stond het 0-0 en dwong de verlenging de beslissing af."),
            ("no", "Det sto 0-0 etter ordinær tid, og ekstraomgangene måtte avgjøre."),
            ("sv", "Det stod 0-0 efter ordinarie tid och matchen gick till förlängning."),
        )
        for lang, text in et_quotes:
            with self.subTest(lang=lang):
                package, evidence = _happy_package()
                panel = package.panels[0]
                quote = replace(panel.quotes[0], original_text=text)
                package2 = replace(
                    package,
                    panels=[replace(panel, quotes=[quote, *panel.quotes[1:]]), *package.panels[1:]],
                )  # fakty fixture: 1-1
                report = MediaFactChecker().validate(package2, evidence)
                self.assertNotIn("score_consistent_with_media", report.blocking_issues)

    def test_extra_time_plus_shootout_combo_does_not_block(self) -> None:
        # Pelny pucharowy scenariusz w JEDNYM cytacie: wynik po 90 minutach (0-0),
        # remis po dogrywce (1-1 = koncowy) i seria karnych (4-3). Zaden z tych
        # wynikow nie moze blokowac vs koncowe 1-1 z faktow fixture.
        package, evidence = _happy_package()
        panel = package.panels[0]
        quote = replace(
            panel.quotes[0],
            original_text=(
                "Po 90 minutach było 0-0, po dogrywce wciąż 1-1, a w serii rzutów "
                "karnych gospodarze wygrali 4-3 i zameldowali się w ćwierćfinale."
            ),
        )
        package2 = replace(
            package, panels=[replace(panel, quotes=[quote, *panel.quotes[1:]]), *package.panels[1:]]
        )  # fakty fixture: 1-1
        report = MediaFactChecker().validate(package2, evidence)
        self.assertNotIn("score_consistent_with_media", report.blocking_issues)

    def test_conflicting_final_next_to_extra_time_clause_still_blocks(self) -> None:
        # Bezpiecznik: marker dogrywki w JEDNYM zdaniu nie maskuje PRAWDZIWEGO
        # konfliktu w INNYM ("ostatecznie 3-1" vs fakty 1-1 musi blokowac).
        package, evidence = _happy_package()
        summary = (
            "Po 90 minutach było 0-0 i mecz poszedł do dogrywki. Ostatecznie padło "
            "3-1 dla gospodarzy, choć goście długo się bronili. Trener chwalił zmianę."
        )
        package2 = self._with_facts_and_summary(package, "1-1", summary)
        report = MediaFactChecker().validate(package2, evidence)
        self.assertIn("score_consistent_with_media", report.blocking_issues)

    def test_in_play_penalty_with_conflicting_final_still_blocks(self) -> None:
        # Bezpiecznik: GOL z karnego W GRZE ("de penal", l.poj.) to nie seria - wynik
        # koncowy 2-0 w tym samym zdaniu wciaz musi blokowac vs fakty 1-1.
        package, evidence = _happy_package()
        panel = package.panels[0]
        quote = replace(
            panel.quotes[0],
            original_text="México abrió de penal y se impuso 2-0 sin sobresaltos.",
        )
        panel2 = replace(panel, quotes=[quote, *panel.quotes[1:]])
        package2 = replace(package, panels=[panel2, *package.panels[1:]])  # fakty fixture: 1-1
        report = MediaFactChecker().validate(package2, evidence)
        self.assertIn("score_consistent_with_media", report.blocking_issues)

    def test_final_score_conflict_in_separate_clause_still_blocks(self) -> None:
        # Klauzulowanie nie moze maskowac PRAWDZIWEGO konfliktu wyniku koncowego:
        # 0-0 do przerwy jest pomijane, ale 2-1 (inna klauzula) wciaz blokuje vs 3-1.
        package, evidence = _happy_package()
        summary = (
            "Do przerwy było 0-0. Ostatecznie padło 2-1 dla gospodarzy, choć goście "
            "mieli swoje sytuacje. Sędzia doliczył pięć minut."
        )
        package2 = self._with_facts_and_summary(package, "3-1", summary)
        report = MediaFactChecker().validate(package2, evidence)
        self.assertIn("score_consistent_with_media", report.blocking_issues)


class MediaCoordinatorTests(unittest.TestCase):
    def test_happy_deterministic_path(self) -> None:
        run = EditorInChiefCoordinator().run(MatchRequest(match_query=HAPPY_QUERY))
        self.assertEqual(run.status, PackageStatus.READY)
        self.assertIsNotNone(run.media_package)
        self.assertIsNone(run.package)
        self.assertIn("media: deterministyczny (fixture)", run.notes)
        assert run.media_package is not None
        roles = [slide.role for slide in run.media_package.carousel.slides]
        self.assertEqual(roles, ["title", "media_country", "media_country", "media_country", "media_country", "sources"])

    def test_happy_llm_path_sets_mood(self) -> None:
        coordinator = EditorInChiefCoordinator(model_gateway=FakeModelGateway(responses=[MX_RESP, ZA_RESP]))
        run = coordinator.run(MatchRequest(match_query=HAPPY_QUERY))
        self.assertEqual(run.status, PackageStatus.READY)
        self.assertIn("media: llm", run.notes)
        assert run.media_package is not None
        self.assertTrue(all(panel.mood_summary for panel in run.media_package.panels))

    def test_llm_failure_falls_back_to_fixture(self) -> None:
        coordinator = EditorInChiefCoordinator(model_gateway=FakeModelGateway(responses=["x", "x", "x"]))
        run = coordinator.run(MatchRequest(match_query=HAPPY_QUERY))
        self.assertEqual(run.status, PackageStatus.READY)
        self.assertTrue(any("fallback" in note for note in run.notes))

    def test_one_country_missing_needs_review(self) -> None:
        run = EditorInChiefCoordinator().run(
            MatchRequest(match_query="Meksyk - RPA brak relacji jednego kraju")
        )
        self.assertEqual(run.status, PackageStatus.NEEDS_HUMAN_REVIEW)
        self.assertIn("one_country_media_missing", run.fact_check.blocking_issues)

    def test_no_sources_is_insufficient(self) -> None:
        run = EditorInChiefCoordinator().run(
            MatchRequest(match_query="Meksyk - RPA bez glosow mediow")
        )
        self.assertEqual(run.status, PackageStatus.INSUFFICIENT_EVIDENCE)
        self.assertIn("media_unavailable", run.fact_check.blocking_issues)

    def test_offwhitelist_degrades(self) -> None:
        run = EditorInChiefCoordinator().run(
            MatchRequest(match_query="Meksyk - RPA zrodlo spoza whitelisty")
        )
        self.assertEqual(run.status, PackageStatus.NEEDS_HUMAN_REVIEW)
        self.assertIn("one_country_media_missing", run.fact_check.blocking_issues)


class OperatorScoreOverrideTests(unittest.TestCase):
    """--score: operator wstrzykuje recznie zweryfikowany wynik, gdy zrodla sa
    nieosiagalne (Portugalia-DR Konga 1-1: FIFA JS-wall, prasa pisze 'empate' bez cyfr).
    Wynik to evidence o niskim zaufaniu i ZAWSZE wymusza needs_human_review."""

    def test_override_forces_review_even_when_path_would_be_ready(self) -> None:
        # bez --score ten sam mecz konczy sie READY (gold 1-1); z --score wynik jest
        # niepotwierdzony zewnetrznie -> needs_human_review mimo identycznego wyniku
        run = EditorInChiefCoordinator().run(
            MatchRequest(match_query=HAPPY_QUERY, score_override="1-1")
        )
        self.assertEqual(run.status, PackageStatus.NEEDS_HUMAN_REVIEW)
        self.assertIn(OPERATOR_OVERRIDE_NOTE, run.notes)
        assert run.media_package is not None
        self.assertEqual(run.media_package.match.score.full_time, "1-1")

    def test_acquire_facts_builds_from_operator_score(self) -> None:
        coordinator = EditorInChiefCoordinator()
        evidence = EvidenceStore()
        notes: list[str] = []
        facts, halt = coordinator._acquire_facts(
            MatchRequest(match_query="Meksyk - RPA", score_override="2-1"),
            evidence,
            notes=notes,
        )
        self.assertIsNone(halt)
        assert facts is not None
        self.assertEqual(facts.score.full_time, "2-1")
        self.assertEqual([facts.home_team, facts.away_team], ["Meksyk", "RPA"])
        self.assertIn(OPERATOR_OVERRIDE_NOTE, notes)
        overrides = [item for item in evidence.ledger() if item.provider == "OperatorOverride"]
        self.assertEqual(len(overrides), 1)
        self.assertEqual(overrides[0].value, "2-1")
        self.assertEqual(overrides[0].confidence, "low")

    def test_malformed_score_override_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            MatchRequest(match_query="Meksyk - RPA", score_override="remis").validate()





class SummaryValidationTests(unittest.TestCase):
    """Slajd = streszczenie artykulu: walidacja >=5 zdan, domkniecia i liczb."""

    ARTICLE = (
        "El Tri decepciona en su debut. La defensa cometio errores graves. "
        "El medio campo perdio el balon 18 veces. La aficion espera mas del equipo. "
        "El proximo rival sera mas dificil."
    )

    def _item(self):
        from app.schemas import SourceTier
        from app.tools import RawMediaItem

        return RawMediaItem(
            evidence_id="e_sum_test",
            outlet="ElUniversalMX",
            country="Meksyk",
            language="es",
            url="https://www.eluniversal.com.mx/deportes/test",
            original_text="El Tri decepciona en su debut",
            tier=SourceTier.A,
            retrieved_at="2026-06-11T00:00:00Z",
            article_text=self.ARTICLE,
        )

    def _build(self, summary, final_score=None):
        translator = LlmMediaTranslator(FakeModelGateway(responses=[]))
        item = self._item()
        data = {
            "quotes": [
                {
                    "evidence_id": item.evidence_id,
                    "translation_pl": "Tri rozczarowuje w debiucie.",
                    "summary_pl": summary,
                }
            ],
            "mood_summary": None,
        }
        return translator._build_panel("Meksyk", data, {item.evidence_id: item}, final_score)

    def test_valid_summary_lands_on_slide(self) -> None:
        summary = (
            "El Universal surowo ocenia debiut Meksyku. Redakcja wytyka obronie powazne bledy. "
            "Gazeta wylicza, ze srodek pola stracil pilke 18 razy. Jak pisze El Universal, "
            "'El Tri rozczarowuje w debiucie'. Dziennik dodaje, ze kolejny rywal bedzie trudniejszy."
        )
        panel = self._build(summary)
        self.assertEqual(panel.quotes[0].summary_pl, summary)

    def test_missing_summary_is_rejected_when_article_present(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            self._build(None)
        self.assertIn("summary_pl", str(ctx.exception))

    def test_too_few_sentences_rejected(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            self._build("Krotko. Za krotko. Naprawde za krotko.")
        self.assertIn("zdan", str(ctx.exception))

    def test_dangling_thought_rejected(self) -> None:
        summary = (
            "El Universal surowo ocenia debiut Meksyku. Redakcja wytyka obronie bledy. "
            "Gazeta pisze o stracie pilki. Kibice oczekuja wiecej. A kolejny rywal to"
        )
        with self.assertRaises(ValueError):
            self._build(summary)

    def test_invented_number_rejected(self) -> None:
        summary = (
            "El Universal surowo ocenia debiut Meksyku. Redakcja wytyka obronie bledy. "
            "Gazeta wylicza 37 strat pilki w srodku pola. Kibice oczekuja wiecej. "
            "Kolejny rywal bedzie trudniejszy."
        )
        with self.assertRaises(ValueError) as ctx:
            self._build(summary)
        self.assertIn("37", str(ctx.exception))


class HashtagTests(unittest.TestCase):
    """Hashtagi: deterministyczna baza + tagi meczu (kraje, english_name, przydomki)."""

    def test_slugifies_names(self) -> None:
        from app.agents.media_reaction import hashtag_slug

        self.assertEqual(hashtag_slug("El Tri"), "eltri")
        self.assertEqual(hashtag_slug("Bafana Bafana"), "bafanabafana")
        self.assertEqual(hashtag_slug("Korea Południowa"), "koreapoludniowa")

    def test_builds_base_plus_match_tags(self) -> None:
        from app.agents.media_reaction import MAX_HASHTAGS, build_hashtags

        gateway = ToolGateway()
        facts = MatchResearcher(gateway).fetch_facts(HAPPY_ID, EvidenceStore())
        tags = build_hashtags(
            facts,
            {"Meksyk": ["Mexico", "El Tri"], "RPA": ["South Africa", "Bafana Bafana"]},
        )
        for expected in ("#mundial2026", "#fifaworldcup", "#meksyk", "#rpa",
                         "#mexico", "#eltri", "#southafrica", "#bafanabafana"):
            self.assertIn(expected, tags)
        self.assertEqual(len(tags), len(set(tags)))  # bez duplikatow
        self.assertLessEqual(len(tags), MAX_HASHTAGS)

    def test_unsluggable_names_are_dropped(self) -> None:
        from app.agents.media_reaction import build_hashtags

        gateway = ToolGateway()
        facts = MatchResearcher(gateway).fetch_facts(HAPPY_ID, EvidenceStore())
        tags = build_hashtags(facts, {"Meksyk": ["대한민국"]})  # hangul -> pusty slug
        self.assertNotIn("#", [tag.strip() for tag in tags])

    def test_coordinator_package_carries_rich_hashtags(self) -> None:
        run = EditorInChiefCoordinator().run(MatchRequest(match_query=HAPPY_QUERY))
        assert run.media_package is not None
        tags = run.media_package.caption.hashtags
        self.assertIn("#mundial2026", tags)
        self.assertIn("#meksyk", tags)
        self.assertIn("#eltri", tags)  # przydomek z rejestru
        self.assertGreaterEqual(len(tags), 12)


class ScoreInSummaryTests(unittest.TestCase):
    """Koncowy wynik mieszka na slajdzie tytulowym - streszczenia go nie powtarzaja."""

    ARTICLE = (
        "El Tri gano 2-0 en su debut. El equipo ya ganaba 1-0 al descanso. "
        "La defensa cometio errores graves. El medio campo perdio el balon 18 veces. "
        "La aficion espera mas del equipo."
    )

    def _build(self, summary, final_score):
        from app.schemas import SourceTier
        from app.tools import RawMediaItem

        item = RawMediaItem(
            evidence_id="e_score_test",
            outlet="ElUniversalMX",
            country="Meksyk",
            language="es",
            url="https://www.eluniversal.com.mx/deportes/test-score",
            original_text="El Tri gano en su debut",
            tier=SourceTier.A,
            retrieved_at="2026-06-11T00:00:00Z",
            article_text=self.ARTICLE,
        )
        translator = LlmMediaTranslator(FakeModelGateway(responses=[]))
        data = {
            "quotes": [
                {
                    "evidence_id": item.evidence_id,
                    "translation_pl": "Tri wygrywa w debiucie.",
                    "summary_pl": summary,
                }
            ],
            "mood_summary": None,
        }
        return translator._build_panel("Meksyk", data, {item.evidence_id: item}, final_score)

    def test_final_score_in_summary_rejected(self) -> None:
        summary = (
            "El Universal chwali debiut Meksyku zakonczony wygrana 2-0. Redakcja widzi "
            "jednak bledy w obronie. Gazeta wylicza 18 strat w srodku pola. Kibice "
            "oczekuja wiecej od zespolu. Nastepny mecz pokaze wiecej."
        )
        with self.assertRaises(ValueError) as ctx:
            self._build(summary, final_score="2-0")
        self.assertIn("wynik", str(ctx.exception))

    def test_reversed_orientation_rejected(self) -> None:
        summary = (
            "El Universal opisuje porazke rywala 0-2 w debiucie. Redakcja widzi bledy "
            "w obronie. Gazeta wylicza 18 strat w srodku pola. Kibice oczekuja wiecej. "
            "Nastepny mecz pokaze wiecej."
        )
        with self.assertRaises(ValueError):
            self._build(summary, final_score="2-0")

    def test_partial_score_is_allowed(self) -> None:
        summary = (
            "El Universal chwali kontrole Meksyku od pierwszych minut. Redakcja "
            "przypomina prowadzenie 1-0 juz do przerwy. Gazeta wylicza 18 strat rywala "
            "w srodku pola. Kibice oczekuja wiecej od zespolu. Nastepny mecz pokaze wiecej."
        )
        panel = self._build(summary, final_score="2-0")
        self.assertEqual(panel.quotes[0].summary_pl, summary)

    def test_no_final_score_skips_check(self) -> None:
        summary = (
            "El Universal chwali debiut Meksyku zakonczony wygrana 2-0. Redakcja widzi "
            "jednak bledy w obronie. Gazeta wylicza 18 strat w srodku pola. Kibice "
            "oczekuja wiecej od zespolu. Nastepny mecz pokaze wiecej."
        )
        panel = self._build(summary, final_score=None)
        self.assertEqual(panel.quotes[0].summary_pl, summary)


class FinalEqualsHalftimeScoreTests(unittest.TestCase):
    """Regresja: wynik koncowy LICZBOWO ROWNY czastkowemu (final 1-0 vs '1-0 do przerwy').

    Oba strazniki "nie powtarzaj wyniku koncowego" - per-artykul (mentions_final_score
    w torze medialnym) i blokujacy (_summaries_repeating_score w QualityJudge) - musza
    pomijac zdania o wyniku DO PRZERWY. Inaczej legalna wzmianka czastkowa jest blednie
    traktowana jak powtorka, gdy wynik koncowy == czastkowy (1-0 do przerwy przy 1-0).
    test_partial_score_is_allowed nie pokrywal tego: tam czastkowy 1-0 != koncowy 2-0
    z samej WARTOSCI. Bare 'wygrali 1-0' (bez markera przerwy) wciaz ma byc lapane.
    """

    # final 1-0; w kazdym streszczeniu '1-0' wystepuje WYLACZNIE w kontekscie do przerwy
    HALFTIME_SUMMARIES = (
        ("pl", "Gospodarze prowadzili 1-0 do przerwy, a po zmianie stron dowieźli wynik."),
        ("fr", "Les Bleus menaient 1-0 à la mi-temps avant de tenir le score au retour."),
        ("es", "El equipo ganaba 1-0 al descanso y luego controló el resto del encuentro."),
        ("pt", "A seleção vencia 1-0 ao intervalo e depois geriu a vantagem com calma."),
        ("de", "Die Mannschaft führte zur Halbzeit 1:0 und brachte den Vorsprung ins Ziel."),
        ("nl", "Oranje ging met 1-0 de rust in en hield daarna eenvoudig stand."),
        ("no", "Landslaget ledet 1-0 etter første omgang og kontrollerte resten av kampen."),
        ("sv", "Landslaget ledde med 1-0 i halvtid och höll undan efter vilan."),
    )
    BARE_SUMMARY = "Gospodarze pewnie wygrali 1-0 i zameldowali się w kolejnej rundzie."

    def test_mentions_final_score_skips_halftime_equal_to_final(self) -> None:
        from app.agents.media_reaction import mentions_final_score

        for lang, summary in self.HALFTIME_SUMMARIES:
            with self.subTest(lang=lang):
                self.assertFalse(mentions_final_score(summary, "1-0"))

    def test_mentions_final_score_flags_bare_repetition(self) -> None:
        from app.agents.media_reaction import mentions_final_score

        self.assertTrue(mentions_final_score(self.BARE_SUMMARY, "1-0"))

    @staticmethod
    def _repeating(summary, final_score):
        """(_summaries_repeating_score(package), evidence_id zmodyfikowanego cytatu).

        Pozostale cytaty fixture nie maja summary_pl, wiec do wyniku liczy sie tylko
        przerobiony cytat - asercja na jego evidence_id jest deterministyczna.
        """
        from app.evaluation.judges import _summaries_repeating_score

        package, _ = _happy_package()
        score = replace(package.match.score, full_time=final_score)
        match = replace(package.match, score=score)
        panel = package.panels[0]
        quote = replace(panel.quotes[0], summary_pl=summary)
        panel2 = replace(panel, quotes=[quote, *panel.quotes[1:]])
        package2 = replace(package, match=match, panels=[panel2, *package.panels[1:]])
        return _summaries_repeating_score(package2), quote.evidence_id

    def test_summaries_repeating_score_skips_halftime_equal_to_final(self) -> None:
        for lang, summary in self.HALFTIME_SUMMARIES:
            with self.subTest(lang=lang):
                repeated, evidence_id = self._repeating(summary, "1-0")
                self.assertNotIn(evidence_id, repeated)

    def test_summaries_repeating_score_flags_bare_repetition(self) -> None:
        repeated, evidence_id = self._repeating(self.BARE_SUMMARY, "1-0")
        self.assertIn(evidence_id, repeated)


class SummarySalvageTests(unittest.TestCase):
    """Regresja Meksyk-Korea Pld (run_20260619090214 / _091228): JEDEN trudny artykul
    (felieton El Universal 'la maldicion...' o zwyciestwie 1-0) wyczerpywal 5 prob -
    raz bo streszczenie powtarzalo wynik 1-0, raz bo wplatalo rok '1986' spoza
    zapisanego tekstu - i `translation_unavailable` zabijal CALY post razem z poprawnym
    panelem Korei. Po wyczerpaniu prob panel ma sie RATOWAC: streszczenie lamiace
    kontrakt schodzi do samego cytatu, a poprawne artykuly zostaja nietkniete.
    """

    from app.schemas import SourceTier
    from app.tools import RawMediaItem

    ARTICLE_BAD = (
        "Mexico termino con la maldicion del Mundial en casa tras su triunfo. "
        "El equipo no ganaba fuera del Azteca desde hace decadas. La cronica recuerda "
        "los fracasos pasados. La aficion celebra el cambio de historia. Ahora va por "
        "otro logro ante Chequia."
    )
    ARTICLE_GOOD = (
        "Rangel volvio a ser elegido por Javier Aguirre y respondio con una atajada "
        "decisiva. El portero salvo a Mexico en el ultimo minuto. La defensa sufrio "
        "ante los delanteros rivales. El tecnico destaco la concentracion del grupo. "
        "La prensa pide mantener el bloque para el siguiente duelo."
    )

    def _items(self, *evidence_ids):
        texts = {"e_curse": self.ARTICLE_BAD, "e_save": self.ARTICLE_GOOD}
        return [
            self.RawMediaItem(
                evidence_id=eid,
                outlet="ElUniversalMX",
                country="Meksyk",
                language="es",
                url=f"https://www.eluniversal.com.mx/deportes/{eid}",
                original_text="El Tri vencio a Corea del Sur",
                tier=self.SourceTier.A,
                retrieved_at="2026-06-19T00:00:00Z",
                article_text=texts[eid],
            )
            for eid in evidence_ids
        ]

    def _resp(self, entries, mood=None):
        return json.dumps({"quotes": entries, "mood_summary": mood})

    def test_sole_article_repeating_score_degrades_to_quote_only(self) -> None:
        # run_20260619090214: jedyny artykul Meksyku uparcie powtarza koncowy wynik 1-0.
        # Zamiast translation_unavailable -> slajd z samym, zwalidowanym cytatem.
        bad_summary = (
            "El Universal pisze, ze Meksyk zakonczyl klatwe domowego mundialu wygrana 1-0. "
            "Redakcja przypomina lata bez wygranej poza Azteca. Gazeta widzi przelom w "
            "historii kadry. Kibice swietuja zmiane losu. Druzyna celuje w kolejny sukces."
        )
        resp = self._resp(
            [{"evidence_id": "e_curse", "translation_pl": "Skonczyli z klatwa.", "summary_pl": bad_summary}]
        )
        translator = LlmMediaTranslator(FakeModelGateway(responses=[resp] * 5))
        panel = translator.write_panel("Meksyk", self._items("e_curse"), final_score="1-0")
        self.assertEqual(len(panel.quotes), 1)
        self.assertIsNone(panel.quotes[0].summary_pl)  # zeszlo do samego cytatu
        self.assertEqual(panel.quotes[0].translation_pl, "Skonczyli z klatwa.")

    def test_one_bad_article_does_not_drop_the_good_one(self) -> None:
        # run_20260619091228: ten sam felieton wplata '1986' (spoza zapisanego tekstu),
        # ale obok jest poprawny artykul o atajadzie Rangela. Salvage zachowuje oba
        # cytaty; tylko felieton traci streszczenie.
        bad_summary = (
            "El Universal wraca do klatwy z 1986 roku i chwali jej zlamanie. Redakcja "
            "przypomina dawne rozczarowania. Gazeta widzi przelom w historii kadry. "
            "Kibice swietuja zmiane losu. Druzyna celuje w kolejny sukces."
        )
        good_summary = (
            "El Universal stawia teze, ze to Rangel uratowal Meksyk decydujaca interwencja. "
            "Bramkarz wybronil rywali w koncowce. Obrona meczyla sie z napastnikami. "
            "Trener chwalil koncentracje zespolu. Prasa apeluje o utrzymanie skladu."
        )
        resp = self._resp(
            [
                {"evidence_id": "e_curse", "translation_pl": "Wracaja do klatwy.", "summary_pl": bad_summary},
                {"evidence_id": "e_save", "translation_pl": "Rangel uratowal Meksyk.", "summary_pl": good_summary},
            ]
        )
        translator = LlmMediaTranslator(FakeModelGateway(responses=[resp] * 5))
        panel = translator.write_panel("Meksyk", self._items("e_curse", "e_save"), final_score="1-0")
        self.assertEqual(len(panel.quotes), 2)
        by_id = {q.evidence_id: q for q in panel.quotes}
        self.assertIsNone(by_id["e_curse"].summary_pl)  # felieton z '1986' -> sam cytat
        self.assertEqual(by_id["e_save"].summary_pl, good_summary)  # dobry artykul nietkniety

    def test_unsalvageable_panel_still_raises(self) -> None:
        # Gdy nie ma czego ratowac (sama halucynacja evidence), petla nadal pada -
        # salvage nie maskuje twardych bledow integralnosci.
        resp = self._resp([{"evidence_id": "e_nieistnieje", "translation_pl": "x", "summary_pl": None}])
        translator = LlmMediaTranslator(FakeModelGateway(responses=[resp] * 5))
        with self.assertRaises(GenerationError):
            translator.write_panel("Meksyk", self._items("e_curse"), final_score="1-0")


class StageFractionVocabularyTests(unittest.TestCase):
    """Regresja Egipt-Australia (run_20260704103913): angielski recap pisze 'round of
    16'/'last 16', a jedyne poprawne polskie streszczenie awansu mowi '1/8 finalu'.
    Straznik liczb traktowal ulamek fazy jako fabrykowana cyfre ('8' spoza artykulu),
    model nie mial czym zastapic jedynego poprawnego terminu, po wyczerpaniu prob
    salvage cial slajd do golego cytatu bez streszczenia. Nazwa fazy pucharowej to
    SLOWNICTWO, nie statystyka - ulamki 1/2..1/32 sa wylaczone z anty-fabrykacji;
    realnie zmyslone liczby wciaz maja byc lapane.
    """

    ARTICLE = (
        "Egypt reached the World Cup round of 16 for the first time after beating "
        "Australia on penalties. The Pharaohs held their rivals to a draw after extra "
        "time in Dallas before goalkeeper heroics settled the shootout. KingFut rates "
        "the night among the biggest in the national team's history."
    )

    def _build(self, summary):
        from app.schemas import SourceTier
        from app.tools import RawMediaItem

        item = RawMediaItem(
            evidence_id="e_last16",
            outlet="KingFutEG",
            country="Egipt",
            language="ar",
            url=(
                "https://www.kingfut.com/2026/07/03/"
                "egypt-beat-australia-on-penalties-to-reach-world-cup-last-16/"
            ),
            original_text="Egypt reached the World Cup round of 16 for the first time",
            tier=SourceTier.B,
            retrieved_at="2026-07-04T00:00:00Z",
            article_text=self.ARTICLE,
        )
        translator = LlmMediaTranslator(FakeModelGateway(responses=[]))
        data = {
            "quotes": [
                {
                    "evidence_id": item.evidence_id,
                    "translation_pl": "Egipt po raz pierwszy awansowal do 1/8 finalu.",
                    "summary_pl": summary,
                }
            ],
            "mood_summary": None,
        }
        return translator._build_panel("Egipt", data, {item.evidence_id: item}, "1-1")

    def test_stage_fraction_not_flagged_as_fabricated_number(self) -> None:
        summary = (
            "KingFut ocenia awans jako historyczna noc egipskiej pilki. Egipt po raz "
            "pierwszy zameldowal sie w 1/8 finalu mistrzostw swiata. Redakcja podkresla "
            "role bramkarza w serii rzutow karnych. Zdaniem portalu zespol pokazal "
            "charakter w Dallas. Awans otwiera droge do dalszej walki."
        )
        panel = self._build(summary)
        self.assertEqual(panel.quotes[0].summary_pl, summary)

    def test_fabricated_number_still_rejected(self) -> None:
        summary = (
            "KingFut ocenia awans jako historyczna noc egipskiej pilki. Egipt po raz "
            "pierwszy zameldowal sie w 1/8 finalu mistrzostw swiata. Redakcja wylicza 14 "
            "obronionych strzalow bramkarza. Zdaniem portalu zespol pokazal charakter. "
            "Awans otwiera droge do dalszej walki."
        )
        with self.assertRaises(ValueError) as ctx:
            self._build(summary)
        self.assertIn("anty-fabrykacja", str(ctx.exception))


class ArticleTitleInPromptTests(unittest.TestCase):
    """Tytul artykulu = teza redakcji w pigulce - idzie do prompta i puli liczb."""

    def _item(self, title):
        from app.schemas import SourceTier
        from app.tools import RawMediaItem

        return RawMediaItem(
            evidence_id="e_title_test",
            outlet="ElUniversalMX",
            country="Meksyk",
            language="es",
            url="https://www.eluniversal.com.mx/deportes/test-title",
            original_text="El Tri abre el Mundial",
            tier=SourceTier.A,
            retrieved_at="2026-06-11T00:00:00Z",
            article_text=(
                "El Tri abre el Mundial. La defensa cometio errores. El equipo gano. "
                "La aficion celebro. El proximo rival espera."
            ),
            title=title,
        )

    def test_title_lands_in_user_prompt(self) -> None:
        translator = LlmMediaTranslator(FakeModelGateway(responses=[]))
        prompt = translator._user_prompt("Meksyk", [self._item("Una gris victoria del Tri")])
        self.assertIn("TYTUL ARTYKULU: Una gris victoria del Tri", prompt)

    def test_no_title_no_marker(self) -> None:
        translator = LlmMediaTranslator(FakeModelGateway(responses=[]))
        prompt = translator._user_prompt("Meksyk", [self._item(None)])
        self.assertNotIn("TYTUL ARTYKULU", prompt)

    def test_digits_from_title_are_allowed_in_summary(self) -> None:
        item = self._item("90 minut cierpienia")  # '90' tylko w tytule
        translator = LlmMediaTranslator(FakeModelGateway(responses=[]))
        summary = (
            "El Universal pisze o 90 minutach cierpienia mimo wygranej. Redakcja "
            "wytyka obronie bledy. Gazeta docenia jednak zwyciestwo. Kibice "
            "swietowali na trybunach. Kolejny rywal bedzie trudniejszy."
        )
        data = {
            "quotes": [
                {
                    "evidence_id": item.evidence_id,
                    "translation_pl": "Tri otwiera mundial.",
                    "summary_pl": summary,
                }
            ],
            "mood_summary": None,
        }
        panel = translator._build_panel("Meksyk", data, {item.evidence_id: item})
        self.assertEqual(panel.quotes[0].summary_pl, summary)


class ScoreOnTitleSlideJudgeTests(unittest.TestCase):
    """Sedzia: streszczenie powtarzajace koncowy wynik blokuje publikacje."""

    def test_summary_repeating_score_blocks(self) -> None:
        package, _ = _happy_package()  # fakty fixture: 1-1
        quote = replace(
            package.panels[0].quotes[0],
            summary_pl=(
                "El Universal opisuje remis 1-1 jako rozczarowanie. Redakcja widzi "
                "bledy w obronie. Gazeta chwali bramkarza. Kibice oczekuja wiecej. "
                "Nastepny mecz pokaze wiecej."
            ),
        )
        panel = replace(package.panels[0], quotes=[quote, *package.panels[0].quotes[1:]])
        broken = replace(package, panels=[panel, *package.panels[1:]])
        report = MediaQualityJudge().validate(broken)
        self.assertIn("score_only_on_title_slide", report.blocking_issues)

    def test_score_in_quote_translation_is_allowed(self) -> None:
        # cytat doslowny to slowa outletu - wynik w tlumaczeniu cytatu nie blokuje
        package, _ = _happy_package()
        quote = replace(
            package.panels[0].quotes[0],
            translation_pl="Remis 1-1, ktory smakuje jak porazka.",
        )
        panel = replace(package.panels[0], quotes=[quote, *package.panels[0].quotes[1:]])
        broken = replace(package, panels=[panel, *package.panels[1:]])
        report = MediaQualityJudge().validate(broken)
        self.assertNotIn("score_only_on_title_slide", report.blocking_issues)


VALID_EDITORIAL = {
    # hook to CZYSTA teza (bez nazwy redakcji); atrybucje 'wg ...' dokleja kod z based_on[0]
    "hook": "Bafana schodzi z podniesionymi glowami",
    "title_body": "Prasa w Meksyku pisze o niedosycie, w RPA o dumie.",
    "caption": (
        "El Universal widzi watpliwosci Tri, a News24 dume Bafany. "
        "Jeden mecz, dwie zupelnie rozne historie."
    ),
    "cta": "Niedosyt Meksyku czy duma RPA - kto ma racje?",
    # PIERWSZY = zrodlo HOOKA (hook to teza News24/RPA) -> z niego byline 'wg ...'
    "based_on": ["e_za_news24", "e_mx_universal"],
}


def _happy_inputs():
    gateway = ToolGateway()
    evidence = EvidenceStore()
    facts = MatchResearcher(gateway).fetch_facts(HAPPY_ID, evidence)
    countries = [facts.home_team, facts.away_team]
    raw = collect_media(gateway, HAPPY_ID, countries, evidence)
    panels = [FixtureTranslator().write_panel(country, raw[country]) for country in countries]
    return facts, panels, evidence


class MediaEditorialTests(unittest.TestCase):
    """Krok redakcyjny: hook + kontrast z paneli, z guardrailami kuracji."""

    def _write(self, responses):
        facts, panels, _ = _happy_inputs()
        editorial = LlmMediaEditorial(FakeModelGateway(responses=responses))
        return editorial.write(facts, panels)

    def test_valid_frame_is_accepted(self) -> None:
        frame = self._write([json.dumps(VALID_EDITORIAL)])
        self.assertEqual(frame.hook, VALID_EDITORIAL["hook"])
        self.assertEqual(frame.based_on, VALID_EDITORIAL["based_on"])

    def test_rejects_evidence_outside_panels(self) -> None:
        bad = dict(VALID_EDITORIAL, based_on=["e_nieistnieje"])
        with self.assertRaises(GenerationError):
            self._write([json.dumps(bad)] * 3)

    def test_rejects_cta_without_question_mark(self) -> None:
        bad = dict(VALID_EDITORIAL, cta="Kto ma racje, oceniajcie sami.")
        with self.assertRaises(GenerationError):
            self._write([json.dumps(bad)] * 3)

    def test_rejects_final_score_in_hook(self) -> None:
        bad = dict(VALID_EDITORIAL, hook="Prasa zgodnie: remis 1-1 bez blasku")
        with self.assertRaises(GenerationError):
            self._write([json.dumps(bad)] * 3)

    def test_rejects_invented_number(self) -> None:
        bad = dict(VALID_EDITORIAL, caption="News24 wylicza 47 powodow do dumy. Reszta kraju czyta.")
        with self.assertRaises(GenerationError) as ctx:
            self._write([json.dumps(bad)] * 3)
        self.assertIn("47", str(ctx.exception))

    def test_rejects_banned_phrase(self) -> None:
        bad = dict(VALID_EDITORIAL, hook="News24: 'absolutnie wyjatkowy wieczor Bafany'")
        with self.assertRaises(GenerationError):
            self._write([json.dumps(bad)] * 3)

    def test_attribution_is_derived_from_hook_source_outlet(self) -> None:
        # byline liczony deterministycznie z based_on[0] (zrodlo hooka), zhumanizowany;
        # SAMA nazwa dziennika (bez 'wg') - niezalezny od wolnego tekstu LLM
        facts, panels, _ = _happy_inputs()
        frame = LlmMediaEditorial(
            FakeModelGateway(responses=[json.dumps(VALID_EDITORIAL)]),
            outlet_names={"News24ZA": "News24"},
        ).write(facts, panels)
        self.assertEqual(frame.attribution, "News24")

    def test_hook_prompt_forbids_outlet_name_in_thesis(self) -> None:
        # regresja: nazwa redakcji wpadala w naglowek (raz nawias, raz myslnik) -
        # teraz hook to czysta teza, atrybucje dokleja kod
        prompt = LlmMediaEditorial(FakeModelGateway(responses=[]))._system_prompt()
        self.assertIn("BEZ nazwy redakcji", prompt)

    def test_prompt_uses_display_names_and_output_is_humanized(self) -> None:
        facts, panels, _ = _happy_inputs()
        gateway = FakeModelGateway(responses=[json.dumps(VALID_EDITORIAL)])
        editorial = LlmMediaEditorial(
            gateway, outlet_names={"News24ZA": "News24", "ElUniversalMX": "El Universal"}
        )
        frame = editorial.write(facts, panels)
        # prompt: ludzkie nazwy zamiast technicznych provider_id
        self.assertIn("El Universal:", gateway.calls[0]["user"])
        self.assertNotIn("ElUniversalMX", gateway.calls[0]["user"])
        # output: gdyby model mimo to uzyl provider_id, podmieniamy deterministycznie
        gateway2 = FakeModelGateway(
            responses=[json.dumps(dict(VALID_EDITORIAL, hook="News24ZA: 'koszmar Bafany'"))]
        )
        frame2 = LlmMediaEditorial(
            gateway2, outlet_names={"News24ZA": "News24"}
        ).write(facts, panels)
        self.assertEqual(frame.hook, VALID_EDITORIAL["hook"])
        self.assertEqual(frame2.hook, "News24: 'koszmar Bafany'")
        self.assertNotIn("News24ZA", frame2.hook)


class EditorialAssemblerTests(unittest.TestCase):
    """Asembler z rama redakcyjna: hook w tytule, teza + CTA w caption."""

    def _package(self):
        facts, panels, evidence = _happy_inputs()
        editorial = LlmMediaEditorial(
            FakeModelGateway(responses=[json.dumps(VALID_EDITORIAL)])
        ).write(facts, panels)
        return build_media_package("mpkg_test", facts, panels, evidence, editorial=editorial), evidence, facts

    def test_title_slide_carries_hook_and_claims(self) -> None:
        package, _, facts = self._package()
        title = package.carousel.slides[0]
        self.assertEqual(
            title.headline,
            f"{facts.home_team} {facts.score.full_time} {facts.away_team}. "
            f"{VALID_EDITORIAL['hook']}",
        )
        self.assertEqual(title.body, VALID_EDITORIAL["title_body"])
        for evidence_id in VALID_EDITORIAL["based_on"]:
            self.assertIn(evidence_id, title.claim_ids)

    def test_title_slide_carries_attribution_byline(self) -> None:
        package, _, _ = self._package()
        title = package.carousel.slides[0]
        # based_on[0] = e_za_news24 -> News24ZA; bez outlet_names display = provider_id
        self.assertEqual(title.attribution, "News24ZA")
        # nazwa redakcji NIE jest wciskana w naglowek (czysta teza po wyniku)
        self.assertNotIn("News24", title.headline)

    def test_caption_carries_thesis_and_cta(self) -> None:
        package, _, _ = self._package()
        self.assertEqual(
            package.caption.text,
            f"{VALID_EDITORIAL['caption']} {VALID_EDITORIAL['cta']}",
        )

    def test_editorial_package_passes_judges(self) -> None:
        package, evidence, _ = self._package()
        self.assertEqual(MediaFactChecker().validate(package, evidence).status, "pass")
        self.assertEqual(MediaQualityJudge().validate(package).status, "pass")

    def test_without_editorial_template_is_unchanged(self) -> None:
        package, _ = _happy_package()
        self.assertIn("jak odebrały to media?", package.carousel.slides[0].headline)
        self.assertIn("zebraliśmy głosy prasy", package.caption.text)


class EditorialCoordinatorTests(unittest.TestCase):
    def test_llm_editorial_frames_title_and_caption(self) -> None:
        responses = [MX_RESP, ZA_RESP, json.dumps(VALID_EDITORIAL)]
        coordinator = EditorInChiefCoordinator(model_gateway=FakeModelGateway(responses=responses))
        run = coordinator.run(MatchRequest(match_query=HAPPY_QUERY))
        self.assertEqual(run.status, PackageStatus.READY)
        self.assertIn("editorial: llm (hook + kontrast z paneli prasy)", run.notes)
        assert run.media_package is not None
        title = run.media_package.carousel.slides[0]
        # nowy format: redakcja w osobnym bylinie ('wg News24'), nie wciskana w naglowek
        self.assertIn("News24", title.attribution)
        self.assertNotIn("News24", title.headline)
        self.assertIn("kto ma racje?", run.media_package.caption.text)

    def test_editorial_failure_falls_back_to_template(self) -> None:
        # po panelach braknie odpowiedzi dla kroku redakcyjnego -> szablon, run dalej ready
        coordinator = EditorInChiefCoordinator(model_gateway=FakeModelGateway(responses=[MX_RESP, ZA_RESP]))
        run = coordinator.run(MatchRequest(match_query=HAPPY_QUERY))
        self.assertEqual(run.status, PackageStatus.READY)
        self.assertTrue(any(note.startswith("editorial: fallback") for note in run.notes))
        assert run.media_package is not None
        self.assertIn("jak odebrały to media?", run.media_package.carousel.slides[0].headline)


if __name__ == "__main__":
    unittest.main()
