import unittest

from app.orchestration import EditorInChiefCoordinator
from app.schemas import MatchRequest, PackageStatus, to_plain


class WorkflowTests(unittest.TestCase):
    def test_psg_arsenal_generates_ready_package(self) -> None:
        run = EditorInChiefCoordinator().run(
            MatchRequest(match_query="PSG - Arsenal, final Ligi Mistrzow 2026", post_type="data_story")
        )

        self.assertEqual(run.status, PackageStatus.READY)
        self.assertIsNotNone(run.package)
        assert run.package is not None
        self.assertEqual(len(run.package.carousel.slides), 7)
        self.assertGreaterEqual(run.package.editorial_angle.score.total, 7)
        self.assertEqual(run.fact_check.status, "pass")
        self.assertEqual(run.quality_report.status, "pass")

    def test_unknown_match_does_not_generate_package(self) -> None:
        run = EditorInChiefCoordinator().run(
            MatchRequest(match_query="Atlantis FC - Moon United")
        )

        self.assertEqual(run.status, PackageStatus.INSUFFICIENT_EVIDENCE)
        self.assertIsNone(run.package)

    def test_config_notes_land_in_run_notes(self) -> None:
        # zapis konfiguracji modeli w run.json: przy diagnozie wariancji LLM trzeba
        # po fakcie odroznic 'model sie pomylil' od 'operator zapomnial --model'
        note = "modele: jakosciowy=gpt-4o, lekki=gpt-4o-mini"
        for post_type in ("data_story", "media_reaction"):
            with self.subTest(post_type=post_type):
                run = EditorInChiefCoordinator(config_notes=[note]).run(
                    MatchRequest(
                        match_query="PSG - Arsenal, final Ligi Mistrzow 2026",
                        post_type=post_type,
                    )
                )
                self.assertTrue(any(note in n for n in run.notes))

    def test_output_does_not_use_xg_when_fixture_has_no_xg(self) -> None:
        run = EditorInChiefCoordinator().run(
            MatchRequest(match_query="PSG - Arsenal, final Ligi Mistrzow 2026", post_type="data_story")
        )
        plain = to_plain(run.package)
        serialized = str(plain)

        self.assertNotIn("xG", serialized)
        self.assertNotIn("PPDA", serialized)


if __name__ == "__main__":
    unittest.main()

