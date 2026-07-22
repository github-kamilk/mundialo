import copy
import json
import unittest

from app.agents.llm_copywriter import build_copy_draft
from app.memory import DEFAULT_VOICE_PROFILE
from app.models import FakeModelGateway
from app.orchestration import EditorInChiefCoordinator
from app.schemas import MatchRequest, PackageStatus

ALLOWED = {"e_possession", "e_shots", "e_match_result", "e_penalties"}
BANNED = DEFAULT_VOICE_PROFILE.banned_phrases

VALID_DRAFT = {
    "hook": "Wynik mowi remis. Dane mowia: jednostronny final.",
    "voiceover": [
        {"time_range": "0-3s", "text": "Wynik mowi remis. Dane mowia: jednostronny final.", "claim_ids": []},
        {"time_range": "3-10s", "text": "PSG i Arsenal skonczyli 1-1, o trofeum zdecydowaly karne.", "claim_ids": ["e_match_result", "e_penalties"]},
        {"time_range": "10-25s", "text": "Najwazniejsza liczba: 61% pilki dla PSG.", "claim_ids": ["e_possession"]},
        {"time_range": "25-45s", "text": "Wiecej pilki i strzalow, a final wisial na jedenastkach.", "claim_ids": ["e_shots"]},
        {"time_range": "45-60s", "text": "To kontrola czy przewaga bez nokautu?", "claim_ids": []},
    ],
    "on_screen_text": ["1-1, karne 4-3", "61% posiadania PSG"],
    "cta": "To byla kontrola PSG, czy Arsenal sprowadzil final do karnych?",
    "slides": [
        {"slide_number": 1, "role": "hook", "headline": "PSG mialo pilke. Arsenal mial final na granicy.", "body": "Posiadanie kontra karne.", "claim_ids": [], "visual_brief": "Duzy tytul."},
        {"slide_number": 2, "role": "context", "headline": "1-1 po 120 minutach", "body": "PSG wygralo dopiero karne 4-3.", "claim_ids": ["e_match_result", "e_penalties"], "visual_brief": "Wynik."},
        {"slide_number": 3, "role": "number", "headline": "Liczba meczu: 61%", "body": "Tyle pilki mialo PSG.", "claim_ids": ["e_possession"], "visual_brief": "Progress bar."},
        {"slide_number": 4, "role": "chart", "headline": "Przewaga byla, nokautu nie", "body": "17 strzalow PSG do 8 Arsenalu.", "claim_ids": ["e_shots"], "visual_brief": "Mini tabela."},
        {"slide_number": 5, "role": "interpretation", "headline": "Przewaga to nie kontrola", "body": "Final dotrwal do karnych mimo inicjatywy PSG.", "claim_ids": ["e_match_result"], "visual_brief": "Timeline."},
        {"slide_number": 6, "role": "cta", "headline": "Wynik oddaje przebieg?", "body": "Kontrola PSG czy plan Arsenalu?", "claim_ids": [], "visual_brief": "Pytanie."},
    ],
    "stories": [
        {"frame_number": 1, "kind": "poll", "text": "Czy PSG kontrolowalo final?", "claim_ids": []},
    ],
    "caption": {
        "text": "PSG wygralo final, ale historia wyniku nie wystarcza. 61% pilki, wiecej strzalow i dopiero karne.",
        "hashtags": ["#pilkawliczbach", "#championsleague"],
        "source_note": "Zrodla: OfflineMetricFixture, OfflineVerifiedFixture.",
        "claim_ids": ["e_possession", "e_shots"],
    },
}


def _run(responses: list[str]):
    coordinator = EditorInChiefCoordinator(model_gateway=FakeModelGateway(responses=responses))
    return coordinator.run(
        MatchRequest(match_query="PSG - Arsenal, final Ligi Mistrzow 2026", post_type="data_story")
    )


class BuildCopyDraftTests(unittest.TestCase):
    def test_valid_draft_builds(self) -> None:
        draft = build_copy_draft(copy.deepcopy(VALID_DRAFT), ALLOWED, BANNED)
        self.assertEqual(len(draft.slides), 6)

    def test_rejects_hallucinated_evidence(self) -> None:
        bad = copy.deepcopy(VALID_DRAFT)
        bad["caption"]["claim_ids"] = ["e_does_not_exist"]
        with self.assertRaises(ValueError) as ctx:
            build_copy_draft(bad, ALLOWED, BANNED)
        self.assertIn("halucynacja", str(ctx.exception))

    def test_rejects_banned_phrase(self) -> None:
        bad = copy.deepcopy(VALID_DRAFT)
        bad["hook"] = "Ten final byl absolutnie niesamowity!"
        bad["voiceover"][0]["text"] = bad["hook"]
        with self.assertRaises(ValueError) as ctx:
            build_copy_draft(bad, ALLOWED, BANNED)
        self.assertIn("anti-slop", str(ctx.exception))

    def test_rejects_wrong_slide_count(self) -> None:
        bad = copy.deepcopy(VALID_DRAFT)
        bad["slides"] = bad["slides"][:3]
        with self.assertRaises(ValueError):
            build_copy_draft(bad, ALLOWED, BANNED)


class LlmCopywriterIntegrationTests(unittest.TestCase):
    def test_valid_llm_output_produces_ready_package(self) -> None:
        run = _run([json.dumps(VALID_DRAFT)])

        self.assertEqual(run.status, PackageStatus.READY)
        self.assertIsNotNone(run.package)
        self.assertIn("copy: llm", run.notes)

    def test_invalid_then_valid_triggers_repair_retry(self) -> None:
        run = _run(["to nie jest json", json.dumps(VALID_DRAFT)])

        self.assertEqual(run.status, PackageStatus.READY)
        self.assertIn("copy: llm", run.notes)

    def test_model_failure_falls_back_to_deterministic(self) -> None:
        run = _run(["garbage", "garbage", "garbage"])

        self.assertEqual(run.status, PackageStatus.READY)
        self.assertIsNotNone(run.package)
        self.assertTrue(any("fallback" in note for note in run.notes))

    def test_no_model_gateway_uses_deterministic(self) -> None:
        run = EditorInChiefCoordinator().run(
            MatchRequest(match_query="PSG - Arsenal, final Ligi Mistrzow 2026", post_type="data_story")
        )
        self.assertIn("copy: deterministyczny", run.notes)


if __name__ == "__main__":
    unittest.main()
