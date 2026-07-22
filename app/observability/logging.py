from __future__ import annotations

import json
from pathlib import Path

from app.schemas import WorkflowRun, to_plain


class RunLogger:
    def __init__(self, root: Path | None = None) -> None:
        self.root = root or Path.cwd() / "runs"

    def save(self, run: WorkflowRun) -> Path:
        run_dir = self.root / run.run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        path = run_dir / "run.json"
        path.write_text(json.dumps(to_plain(run), ensure_ascii=False, indent=2), encoding="utf-8")
        return path

