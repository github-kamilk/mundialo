from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.evaluation.judges import media_texts, package_texts
from app.evaluation.leaderboard import Leaderboard
from app.schemas import MatchRequest, ValidationReport, WorkflowRun, to_plain


@dataclass(frozen=True)
class ScenarioResult:
    scenario_id: str
    passed: bool
    expected_status: str
    actual_status: str
    failures: list[str] = field(default_factory=list)
    score: int = 0


def _checks_index(*reports: ValidationReport) -> dict[str, str]:
    index: dict[str, str] = {}
    for report in reports:
        for check in report.checks:
            index[check.name] = check.result
    return index


def _serialized_copy(run: WorkflowRun) -> str:
    if run.package is not None:
        return " ".join(package_texts(run.package))
    if run.media_package is not None:
        return " ".join(media_texts(run.media_package))
    return ""


def evaluate_scenario(scenario: dict[str, Any], run: WorkflowRun) -> ScenarioResult:
    failures: list[str] = []

    expected_status = scenario["expected_status"]
    if run.status.value != expected_status:
        failures.append(f"status: oczekiwano {expected_status}, jest {run.status.value}")

    checks = _checks_index(run.fact_check, run.quality_report)
    for name in scenario.get("must_have_checks", []):
        if checks.get(name) != "pass":
            failures.append(f"check '{name}' powinien byc pass, jest {checks.get(name, 'brak')}")
    for name in scenario.get("must_fail_checks", []):
        if checks.get(name) != "fail":
            failures.append(f"check '{name}' powinien byc fail, jest {checks.get(name, 'brak')}")

    blocking = set(run.fact_check.blocking_issues) | set(run.quality_report.blocking_issues)
    for issue in scenario.get("expected_blocking", []):
        if issue not in blocking:
            failures.append(f"oczekiwano blocking_issue '{issue}', brak w {sorted(blocking)}")

    copy_text = _serialized_copy(run).lower()
    for term in scenario.get("forbidden_terms_in_copy", []):
        if term.lower() in copy_text:
            failures.append(f"zakazany termin '{term}' pojawil sie w copy")

    score = Leaderboard().score_run(scenario["id"], run).score
    return ScenarioResult(
        scenario_id=scenario["id"],
        passed=not failures,
        expected_status=expected_status,
        actual_status=run.status.value,
        failures=failures,
        score=score,
    )


def run_scenarios(root: Path | None = None) -> dict[str, Any]:
    # Import leniwy: harness ewaluacji zalezy od orchestration w runtime,
    # nie w czasie importu (rozbija cykl evaluation <-> orchestration).
    from app.orchestration import EditorInChiefCoordinator

    scenario_root = root or Path(__file__).resolve().parent / "scenarios"
    coordinator = EditorInChiefCoordinator()
    results: list[ScenarioResult] = []
    for path in sorted(scenario_root.glob("*.json")):
        scenario = json.loads(path.read_text(encoding="utf-8"))
        request = MatchRequest(
            match_query=scenario["match_query"],
            date_hint=scenario.get("date_hint"),
            competition_hint=scenario.get("competition_hint"),
            post_type=scenario.get("post_type", "data_story"),
        )
        run = coordinator.run(request)
        results.append(evaluate_scenario(scenario, run))

    passed = sum(1 for result in results if result.passed)
    total = len(results)
    return {
        "summary": {
            "total": total,
            "passed": passed,
            "failed": total - passed,
            "pass_rate": round(passed / total, 3) if total else 0.0,
        },
        "results": [to_plain(result) for result in results],
    }


def main() -> int:
    report = run_scenarios()
    print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0 if report["summary"]["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
