"""Raport konsolidacyjny magazynu zdrowia zrodel (pamiec epizodyczna, etap 2).

Uzycie: python -m app.health [--runs-dir runs] [--country Niemcy]

Operator czyta raport (streaki botblock/JS-wall/404 z ostatnich runow) i RECZNIE
przenosi wnioski do country_media.json - konsolidacja semantyczna zostaje ludzka
(architektura-pamiec-epizodyczna.md, zasada 2.2). Raport niczego nie zmienia.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.memory.episodes import OutletHealthStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.health",
        description="Raport zdrowia zrodel z runs/.outlet_health.json (tylko odczyt).",
    )
    parser.add_argument("--runs-dir", default="runs", help="Katalog runs (jak w python -m app).")
    parser.add_argument(
        "--country", default=None, help="Filtr: pokaz tylko wpisy jednego kraju (np. Niemcy)."
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    store = OutletHealthStore(path=Path(args.runs_dir) / ".outlet_health.json")
    print(store.report(country=args.country))
    return 0


if __name__ == "__main__":
    sys.exit(main())
