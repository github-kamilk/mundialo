"""Roadmapa postow: terminarz vs wygenerowane runy.

Uzycie:
    python scripts/roadmap.py

Dla kazdego meczu z terminarza pokazuje najlepszy istniejacy run (po krajach
z media_package/requestu) i czy sa wyrenderowane slajdy.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from app.tools import ToolGateway, load_schedule  # noqa: E402

RUNS_DIR = Path(__file__).resolve().parents[1] / "runs"

# im wyzej, tym lepiej - pokazujemy najlepszy osiagniety stan dla meczu
_STATUS_RANK = {"": 0, "insufficient_evidence": 1, "needs_human_review": 2, "ready": 3}


def _run_countries(run: dict, registry) -> frozenset[str]:
    package = run.get("media_package") or {}
    match = package.get("match") or {}
    home, away = match.get("home_team"), match.get("away_team")
    if home and away:
        return frozenset({home, away})
    query = (run.get("request") or {}).get("match_query", "")
    profiles = registry.countries_in_text(query)
    return frozenset(profile.country for profile in profiles[:2])


def main() -> int:
    schedule = load_schedule()
    if not schedule:
        print("Brak terminarza (data/schedule/world_cup_2026.json) albo plik pusty.")
        return 1
    registry = ToolGateway().registry

    runs: list[tuple[Path, dict]] = []
    if RUNS_DIR.exists():
        for run_dir in sorted(RUNS_DIR.iterdir()):
            run_file = run_dir / "run.json"
            if not run_file.exists():
                continue
            try:
                runs.append((run_dir, json.loads(run_file.read_text(encoding="utf-8"))))
            except (OSError, json.JSONDecodeError):
                continue

    print(f"{'data':<12} {'mecz':<34} {'miasto':<16} {'status':<22} render")
    print("-" * 96)
    for match in sorted(schedule, key=lambda m: m.date):
        wanted = frozenset({match.home, match.away})
        best_status, best_dir = "", None
        for run_dir, run in runs:
            if _run_countries(run, registry) != wanted:
                continue
            status = str(run.get("status", ""))
            if _STATUS_RANK.get(status, 0) >= _STATUS_RANK.get(best_status, 0):
                best_status, best_dir = status, run_dir
        rendered = "tak" if best_dir and any((best_dir / "slides").glob("slide_*.png")) else "-"
        status_label = best_status or "BRAK RUNU"
        print(
            f"{match.date:<12} {match.home + ' - ' + match.away:<34} "
            f"{match.city:<16} {status_label:<22} {rendered}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
