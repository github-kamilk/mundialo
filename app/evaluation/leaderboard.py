from __future__ import annotations

from dataclasses import dataclass

from app.schemas import WorkflowRun


@dataclass(frozen=True)
class LeaderboardRow:
    scenario_id: str
    status: str
    fact_check: str
    quality: str
    score: int


class Leaderboard:
    def score_run(self, scenario_id: str, run: WorkflowRun) -> LeaderboardRow:
        score = 0
        if run.fact_check.status == "pass":
            score += 40
        if run.quality_report.status == "pass":
            score += 40
        if run.package and run.package.editorial_angle.score.total >= 7:
            score += 20
        return LeaderboardRow(
            scenario_id=scenario_id,
            status=run.status.value,
            fact_check=run.fact_check.status,
            quality=run.quality_report.status,
            score=score,
        )

