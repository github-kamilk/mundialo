import json
import unittest

from app.agents import LlmMediaCurator
from app.models import FakeModelGateway, GenerationError
from app.tools.contracts import MatchContext, SearchHit

CTX = MatchContext(
    home_team="Belgia",
    away_team="Egipt",
    date="2026-06-15",
    competition="FIFA World Cup 2026",
    score="1-1",
)

# Realny ksztalt puli Belgii z run_20260616070346: relacja, digest cudzej prasy, duplikat.
RECAP = SearchHit(
    url="https://sporza.be/nl/matches/.../belgie-egypte-lukaku-gelijkspel~1/",
    title="Lukaku vermijdt valse start, Rode Duivels halen opgelucht adem",
    snippet="Belgie speelt 1-1 gelijk tegen Egypte.",
)
DIGEST = SearchHit(
    url="https://sporza.be/nl/2026/06/16/buitenlandse-pers-zag-maar-een-uitblinker~2/",
    title="Buitenlandse pers zag maar een uitblinker bij tamme Rode Duivels",
    snippet="L'Equipe en The Athletic over de bleke Belgen.",
)
RECAP_DUP = SearchHit(
    url="https://sporza.be/nl/matches/.../lukaku-redt-belgie~3/",
    title="Lukaku redt Belgie tegen Egypte na zwakke eerste helft",
    snippet="Invaller Lukaku zorgt voor de gelijkmaker.",
)
POOL = [RECAP, DIGEST, RECAP_DUP]


def _resp(selected):
    return json.dumps({"selected": selected})


class CuratorSelectionTests(unittest.TestCase):
    def test_picks_own_voice_and_skips_digest_and_duplicate(self) -> None:
        # model wybiera relacje (idx 0), pomija digest (1) i duplikat (2)
        curator = LlmMediaCurator(FakeModelGateway(responses=[_resp([{"index": 0, "reason": "wlasna relacja"}])]))
        notes: list[str] = []
        chosen = curator.select(CTX, "Belgia", POOL, notes=notes)
        self.assertEqual([h.url for h in chosen], [RECAP.url])
        self.assertTrue(any("kurator[Belgia]" in n for n in notes))

    def test_preserves_model_order_and_dedupes_indices(self) -> None:
        curator = LlmMediaCurator(
            FakeModelGateway(responses=[_resp([{"index": 2}, {"index": 0}, {"index": 2}])])
        )
        chosen = curator.select(CTX, "Belgia", POOL)
        self.assertEqual([h.url for h in chosen], [RECAP_DUP.url, RECAP.url])

    def test_index_out_of_pool_is_rejected_then_retried(self) -> None:
        # halucynacja indeksu -> ValueError -> feedback -> druga proba poprawna
        gw = FakeModelGateway(responses=[_resp([{"index": 9}]), _resp([{"index": 0}])])
        chosen = LlmMediaCurator(gw).select(CTX, "Belgia", POOL)
        self.assertEqual([h.url for h in chosen], [RECAP.url])

    def test_empty_selection_is_valid_after_retry(self) -> None:
        # nic sensownego w puli -> pusta lista (wolajacy zrobi fallback). Pustke nad
        # NIEPUSTA pula potwierdzamy DRUGIM wywolaniem (retry-on-empty): dwie pustki = ufamy.
        gw = FakeModelGateway(responses=[_resp([]), _resp([])])
        chosen = LlmMediaCurator(gw).select(CTX, "Belgia", POOL)
        self.assertEqual(chosen, [])
        self.assertEqual(len(gw.calls), 2)

    def test_empty_then_nonempty_retry_recovers(self) -> None:
        # regresja Paragwaj (run_20260630215940): kurator zwrocil [] nad 8 dobrymi
        # kandydatami (whiff lekkiego modelu) -> fallback heurystyczny wzial spoleczny
        # tekst jako goly cytat. Pusta selekcja nad niepusta pula jest ponawiana RAZ;
        # gdy druga proba wybiera realna reakcje, kurator ja zwraca (zamiast schodzic do heur.)
        gw = FakeModelGateway(responses=[_resp([]), _resp([{"index": 0, "reason": "relacja"}])])
        chosen = LlmMediaCurator(gw).select(CTX, "Belgia", POOL)
        self.assertEqual([h.url for h in chosen], [RECAP.url])
        self.assertEqual(len(gw.calls), 2)

    def test_respects_max_select(self) -> None:
        gw = FakeModelGateway(responses=[_resp([{"index": 0}, {"index": 1}, {"index": 2}])])
        chosen = LlmMediaCurator(gw).select(CTX, "Belgia", POOL, max_select=2)
        self.assertEqual(len(chosen), 2)

    def test_empty_pool_short_circuits_without_model_call(self) -> None:
        gw = FakeModelGateway(responses=[])  # brak odpowiedzi: gdyby zawolal, rzucilby
        self.assertEqual(LlmMediaCurator(gw).select(CTX, "Belgia", []), [])
        self.assertEqual(gw.calls, [])

    def test_malformed_payload_eventually_raises_generation_error(self) -> None:
        gw = FakeModelGateway(responses=[json.dumps({"selected": "nie-lista"})] * 5)
        with self.assertRaises(GenerationError):
            LlmMediaCurator(gw).select(CTX, "Belgia", POOL)

    def test_system_prompt_rejects_offtopic_administrative_news(self) -> None:
        # regresja Austria-Jordania (run_20260617101300): kurator wybral artykul o
        # SPADKU W RANKINGU FIFA jako "konsekwencje meczu" - to news administracyjny,
        # ktory tylko wspomina mecz, nie reakcja prasy. Prompt ma go jawnie odrzucac.
        prompt = LlmMediaCurator(FakeModelGateway(responses=[]))._system_prompt("Jordania")
        self.assertIn("rankingu", prompt)
        self.assertIn("administracyjne", prompt)
        # 'konsekwencje' zawezone do SPORTOWYCH, nie kazdy news po meczu
        self.assertIn("SPORTOWE", prompt)

    def test_system_prompt_rejects_dramatic_prematch_analysis(self) -> None:
        # regresja Ekwador-Niemcy (run_20260626060236): kurator wybral PRZEDMECZOWA
        # analize PrimiciasEC (...-126130) opisujaca wczesniejsze mecze i ZAPOWIADAJACA
        # Niemcy, zwiedziony dramatycznym leadem ('sen blaknie... koszmar'), ktory brzmi
        # jak pomeczowa rozpacz. Outlet bez daty w URL -> filtry daty bezradne. Prompt
        # ma jawnie odrzucac zapowiedz z dramatycznym leadem opartą na wczesniejszych meczach.
        prompt = LlmMediaCurator(FakeModelGateway(responses=[]))._system_prompt("Ekwador")
        self.assertIn("WCZESNIEJSZYCH", prompt)
        self.assertIn("ramy czasowej", prompt)
        self.assertIn("lead", prompt)

    def test_system_prompt_prefers_own_report_over_opponent_voice(self) -> None:
        # regresja Niemcy-Paragwaj (run_20260630214254): kurator wybral do panelu Niemiec
        # kawalek Bilda z cytatem kapitana PRZECIWNIKA (Gómez: 'Deutschland wusste, dass
        # sie bluten müssen') zamiast wlasnej niemieckiej RELACJI (sportschau Spielbericht
        # byl w puli). Prompt ma PRIORYTETYZOWAC wlasna relacje/ocene wystepu SWOJEJ druzyny
        # nad tekstem, ktorego bohaterem jest PRZECIWNIK (cytat kapitana/trenera rywala).
        prompt = LlmMediaCurator(FakeModelGateway(responses=[]))._system_prompt("Niemcy")
        self.assertIn("PRIORYTET", prompt)
        self.assertIn("WYSTEPU SWOJEJ druzyny", prompt)
        self.assertIn("PRZECIWNIK", prompt)
        self.assertIn("RYWALA", prompt)

    def test_curator_can_order_own_recap_before_opponent_voice(self) -> None:
        # gdy model zwraca relacje przed cytatem rywala, _build zachowuje ta kolejnosc
        own_recap = SearchHit(
            url="https://www.sportschau.de/fussball/fifa-wm-2026/spielbericht-deutschland-paraguay-100.html",
            title="Deutschland erlebt gegen Paraguay naechstes WM-Debakel",
            snippet="Spielbericht: Aus im Elfmeterschiessen.",
        )
        opponent_voice = SearchHit(
            url="https://sportbild.bild.de/.../paraguay-kapitaen-gomez-deutschland-wusste-...",
            title="Paraguay-Kapitaen Gomez: Deutschland wusste, dass sie bluten muessen",
            snippet="Der Kapitaen der Albirroja ueber das deutsche Aus.",
        )
        pool = [opponent_voice, own_recap]
        ctx = MatchContext(home_team="Niemcy", away_team="Paragwaj", date="2026-06-29",
                           competition="FIFA World Cup 2026", score="1-1")
        gw = FakeModelGateway(responses=[_resp([{"index": 1, "reason": "wlasna relacja"}])])
        chosen = LlmMediaCurator(gw).select(ctx, "Niemcy", pool)
        self.assertEqual([h.url for h in chosen], [own_recap.url])


if __name__ == "__main__":
    unittest.main()
