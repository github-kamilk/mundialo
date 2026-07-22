import unittest

from app.evaluation import FactChecker
from app.orchestration import EditorInChiefCoordinator
from app.schemas import EvidenceStore, MatchRequest


class FactCheckTests(unittest.TestCase):
    def test_missing_evidence_blocks_package(self) -> None:
        run = EditorInChiefCoordinator().run(
            MatchRequest(match_query="PSG - Arsenal, final Ligi Mistrzow 2026", post_type="data_story")
        )
        assert run.package is not None

        report = FactChecker().validate(run.package, EvidenceStore())

        self.assertEqual(report.status, "needs_human_review")
        self.assertIn("missing_evidence", report.blocking_issues)


if __name__ == "__main__":
    unittest.main()

