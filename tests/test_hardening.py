import unittest

from app.evaluation import run_scenarios
from app.orchestration import EditorInChiefCoordinator
from app.schemas import (
    EvidenceItem,
    MatchRequest,
    PackageStatus,
    SourceTier,
)
from app.tools import SourcePolicyError, SourceRegistry


class SourceRegistryIntegrityTests(unittest.TestCase):
    def _item(self, provider: str, tier: SourceTier) -> EvidenceItem:
        return EvidenceItem(
            id="x",
            claim="c",
            value="v",
            source_url="fixture://x",
            source_tier=tier,
            provider=provider,
            retrieved_at="2026-06-10T00:00:00+02:00",
        )

    def test_unknown_provider_rejected(self) -> None:
        with self.assertRaises(SourcePolicyError):
            SourceRegistry().validate_evidence(self._item("RandomBlog", SourceTier.A))

    def test_tier_mismatch_rejected(self) -> None:
        # narrative provider podszywa sie pod Tier A (faktyczny) -> poisoning guard
        with self.assertRaises(SourcePolicyError):
            SourceRegistry().validate_evidence(
                self._item("OfflineNarrativeFixture", SourceTier.A)
            )

    def test_consistent_evidence_accepted(self) -> None:
        SourceRegistry().validate_evidence(
            self._item("OfflineVerifiedFixture", SourceTier.A)
        )


class GracefulDegradationTests(unittest.TestCase):
    def test_missing_metrics_does_not_crash(self) -> None:
        run = EditorInChiefCoordinator().run(
            MatchRequest(match_query="Lowland United - Coastal City", post_type="data_story")
        )

        self.assertEqual(run.status, PackageStatus.NEEDS_HUMAN_REVIEW)
        self.assertIsNone(run.package)
        self.assertIn("metrics_unavailable", run.fact_check.blocking_issues)

    def test_conflicting_sources_blocks_ready(self) -> None:
        run = EditorInChiefCoordinator().run(
            MatchRequest(match_query="PSG - Arsenal konflikt zrodel", post_type="data_story")
        )

        self.assertEqual(run.status, PackageStatus.NEEDS_HUMAN_REVIEW)
        conflict_check = next(
            check for check in run.fact_check.checks if check.name == "no_source_conflicts"
        )
        self.assertEqual(conflict_check.result, "fail")


class PerRunIsolationTests(unittest.TestCase):
    def test_reused_coordinator_does_not_accumulate_tool_calls(self) -> None:
        coordinator = EditorInChiefCoordinator()
        first = coordinator.run(
            MatchRequest(match_query="PSG - Arsenal, final Ligi Mistrzow 2026", post_type="data_story")
        )
        second = coordinator.run(
            MatchRequest(match_query="PSG - Arsenal, final Ligi Mistrzow 2026", post_type="data_story")
        )

        self.assertEqual(len(first.tool_calls), len(second.tool_calls))


class EvalHarnessTests(unittest.TestCase):
    def test_all_scenarios_pass_assertions(self) -> None:
        report = run_scenarios()

        self.assertEqual(report["summary"]["failed"], 0)
        self.assertGreaterEqual(report["summary"]["total"], 5)


if __name__ == "__main__":
    unittest.main()
