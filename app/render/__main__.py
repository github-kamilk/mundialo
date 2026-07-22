"""CLI warstwy graficznej.

Przyklady:
    python -m app.render                          # najnowszy run z media_package i statusem ready
    python -m app.render --run-dir runs/run_x     # konkretny run
    python -m app.render --allow-review           # renderuj tez needs_human_review (do podgladu)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from app.render.specs import (
    RENDERABLE_STATUSES,
    REVIEW_STATUSES,
    build_caption_text,
    build_slide_specs,
    build_x_post_text,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.render",
        description="Renderuje karuzele PNG (1080x1350) z media_package zapisanego runu.",
    )
    parser.add_argument("--run-dir", default=None, help="Katalog runu (runs/run_...). Domyslnie najnowszy nadajacy sie do renderu.")
    parser.add_argument("--runs-dir", default="runs", help="Katalog z runami (do autodetekcji).")
    parser.add_argument("--out", default=None, help="Katalog wyjsciowy PNG (domyslnie <run-dir>/slides).")
    parser.add_argument("--scale", type=int, default=2, help="Mnoznik rozdzielczosci (2 => 2160x2700).")
    parser.add_argument(
        "--allow-review",
        action="store_true",
        help="Pozwol renderowac runy needs_human_review (podglad przed recznym review).",
    )
    return parser


def _load_run(run_dir: Path) -> dict | None:
    run_file = run_dir / "run.json"
    if not run_file.exists():
        return None
    try:
        return json.loads(run_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _renderable(media_package: dict | None, allow_review: bool) -> bool:
    if not media_package:
        return False
    status = media_package.get("status", "")
    return status in RENDERABLE_STATUSES or (allow_review and status in REVIEW_STATUSES)


def _find_latest_run(runs_dir: Path, allow_review: bool) -> Path | None:
    candidates = sorted((d for d in runs_dir.iterdir() if d.is_dir()), reverse=True)
    for run_dir in candidates:
        run = _load_run(run_dir)
        if run and _renderable(run.get("media_package"), allow_review):
            return run_dir
    return None


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.run_dir:
        run_dir = Path(args.run_dir)
    else:
        runs_dir = Path(args.runs_dir)
        if not runs_dir.is_dir():
            print(f"Brak katalogu runow: {runs_dir}", file=sys.stderr)
            return 2
        found = _find_latest_run(runs_dir, args.allow_review)
        if not found:
            print("Nie znaleziono runu z media_package nadajacego sie do renderu.", file=sys.stderr)
            return 2
        run_dir = found

    run = _load_run(run_dir)
    if not run:
        print(f"Brak poprawnego run.json w {run_dir}", file=sys.stderr)
        return 2

    media_package = run.get("media_package")
    if not media_package:
        print(f"Run {run_dir.name} nie ma media_package (to run data_story?).", file=sys.stderr)
        return 2

    status = media_package.get("status", "")
    if not _renderable(media_package, args.allow_review):
        print(
            f"Run {run_dir.name} ma status '{status}' — renderuje tylko 'ready' "
            "(uzyj --allow-review dla needs_human_review).",
            file=sys.stderr,
        )
        return 2

    specs = build_slide_specs(media_package)
    if not specs:
        print("Karuzela jest pusta — nic do renderu.", file=sys.stderr)
        return 2

    out_dir = Path(args.out) if args.out else run_dir / "slides"

    from app.render.renderer import render_slides

    paths = render_slides(specs, out_dir, scale=args.scale)
    caption_path = out_dir / "caption.txt"
    caption_path.write_text(build_caption_text(media_package), encoding="utf-8")
    x_post_path = out_dir / "x_post.txt"
    x_post_path.write_text(build_x_post_text(media_package), encoding="utf-8")
    print(f"Run: {run_dir.name} (status: {status})")
    for path in paths:
        print(f"  {path}")
    print(f"  {caption_path}")
    print(f"  {x_post_path}")
    print(f"Zapisano {len(paths)} slajdow + caption.txt + x_post.txt do {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
