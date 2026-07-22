import unittest

from app.evaluation.judges import QualityJudge, find_banned_phrases
from app.memory import DEFAULT_VOICE_PROFILE, MemoryStore
from app.memory.voice import banned_hits
from app.orchestration import EditorInChiefCoordinator
from app.schemas import MatchRequest


def _psg_package():
    run = EditorInChiefCoordinator().run(
        MatchRequest(match_query="PSG - Arsenal, final Ligi Mistrzow 2026", post_type="data_story")
    )
    assert run.package is not None
    return run, run.package


class VoiceProfileTests(unittest.TestCase):
    def test_memory_exposes_voice_and_banned_phrases(self) -> None:
        memory = MemoryStore()
        self.assertEqual(memory.voice, DEFAULT_VOICE_PROFILE)
        self.assertIn("niesamowit", memory.banned_phrases())

    def test_few_shot_filtered_by_context(self) -> None:
        hooks = DEFAULT_VOICE_PROFILE.few_shot("hook")
        self.assertTrue(hooks)
        self.assertTrue(all(pair.context == "hook" for pair in hooks))


class AntiSlopLinterTests(unittest.TestCase):
    def test_default_psg_package_passes_no_banned_phrases(self) -> None:
        _, package = _psg_package()
        report = QualityJudge().validate(package)
        check = next(c for c in report.checks if c.name == "no_banned_phrases")
        self.assertEqual(check.result, "pass")

    def test_linter_blocks_when_banned_word_present(self) -> None:
        # "kontrola" wystepuje w copy PSG; uzywamy go jako custom banned slowa,
        # zeby udowodnic, ze linter realnie blokuje (chroni przyszle copy-LLM).
        _, package = _psg_package()
        report = QualityJudge(banned_phrases=["kontrola"]).validate(package)
        check = next(c for c in report.checks if c.name == "no_banned_phrases")
        self.assertEqual(check.result, "fail")
        self.assertIn("no_banned_phrases", report.blocking_issues)

    def test_find_banned_phrases_helper(self) -> None:
        _, package = _psg_package()
        hits = find_banned_phrases(package, ["kontrola", "xyznotpresent"])
        self.assertIn("kontrola", hits)
        self.assertNotIn("xyznotpresent", hits)


class BannedHitsTests(unittest.TestCase):
    def test_stems_match_inflected_forms(self) -> None:
        banned = DEFAULT_VOICE_PROFILE.banned_phrases
        self.assertIn("niesamowit", banned_hits("To bylo niesamowite widowisko", banned))
        self.assertIn("magiczn", banned_hits("Magiczny wieczor w Meksyku", banned))
        self.assertIn("niesamowit", banned_hits("Niesamowita końcówka meczu", banned))

    def test_no_match_inside_word(self) -> None:
        banned = DEFAULT_VOICE_PROFILE.banned_phrases
        self.assertEqual(banned_hits("Podanie do Lewandowowi otworzylo akcje", banned), [])

    def test_exact_word_still_matches(self) -> None:
        self.assertIn("wow", banned_hits("Wow, co za mecz", ["wow"]))
        self.assertIn("bez watpienia", banned_hits("Bez watpienia najlepszy", ["bez watpienia"]))


if __name__ == "__main__":
    unittest.main()
