#!/usr/bin/env python3
"""Preflight fixture'a meczowego: sprawdza, czy plik przejdzie pipeline, ZANIM odpalisz run.

Uzywa tych samych realnych walidatorow co produkcja (SourceRegistry: provider/tier/whitelist
domen, sanityzacja, filtr MEDIA_REACTION), wiec nie duplikuje logiki i sie z nia nie rozjedzie.

Uzycie:
    python scripts/validate_fixture.py <match_id> [--llm] [--match "fraza do --match"]

<match_id> to nazwa pliku w data/fixtures/matches/ bez .json (np. mexico_rpa_opener_2026).
--llm    : poluzowuje wymog gold 'translation_pl' (model przetlumaczy oryginaly).
--match  : dodatkowo testuje, czy ta fraza rozwiaze sie do tego meczu (resolver).

Exit code 0 = gotowe (moga byc ostrzezenia), 1 = blokery / nie gotowe.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.tools import ToolGateway, ToolGatewayError  # noqa: E402

OK = "OK"
WARN = "WARN"
FAIL = "FAIL"

_SYMBOL = {OK: "[ OK ]", WARN: "[WARN]", FAIL: "[FAIL]"}


class Report:
    def __init__(self) -> None:
        self.rows: list[tuple[str, str, str]] = []

    def add(self, level: str, name: str, detail: str = "") -> None:
        self.rows.append((level, name, detail))

    @property
    def failed(self) -> bool:
        return any(level == FAIL for level, _, _ in self.rows)

    @property
    def warned(self) -> bool:
        return any(level == WARN for level, _, _ in self.rows)

    def print(self) -> None:
        for level, name, detail in self.rows:
            line = f"{_SYMBOL[level]} {name}"
            if detail:
                line += f" -> {detail}"
            print(line)


def _best_alias(raw: dict) -> str | None:
    """Najbardziej specyficzny alias (najwiecej tokenow) = najmniejsze ryzyko kolizji."""
    aliases = [a for a in raw.get("aliases", []) if isinstance(a, str) and a.strip()]
    if not aliases:
        return raw.get("match_id")
    return max(aliases, key=lambda alias: len(alias.split()))


def _last_dropped(gateway: ToolGateway) -> list[str]:
    if not gateway.calls:
        return []
    try:
        observation = json.loads(gateway.calls[-1].observation)
    except (ValueError, AttributeError):
        return []
    dropped = observation.get("dropped", [])
    return dropped if isinstance(dropped, list) else []


def validate(match_id: str, *, llm: bool, match_query: str | None) -> Report:
    report = Report()
    gateway = ToolGateway()

    try:
        raw = gateway.fixtures.load(match_id)
    except ToolGatewayError as error:
        report.add(FAIL, "fixture_wczytany", str(error))
        return report
    report.add(OK, "fixture_wczytany", f"{match_id}.json (poprawny JSON)")

    if raw.get("match_id") != match_id:
        report.add(
            WARN,
            "match_id_zgodny_z_nazwa_pliku",
            f"plik={match_id} vs match_id={raw.get('match_id')!r}; resolver uzywa pola match_id",
        )

    required = ["competition", "stage", "date", "venue"]
    missing = [key for key in required if not raw.get(key)]
    if missing:
        report.add(FAIL, "wymagane_pola_meczu", f"brakuje: {missing}")
    else:
        report.add(OK, "wymagane_pola_meczu")

    teams = raw.get("teams") or {}
    home, away = teams.get("home"), teams.get("away")
    if not home or not away:
        report.add(FAIL, "oba_kraje", f"teams.home={home!r}, teams.away={away!r}")
    elif home == away:
        report.add(FAIL, "oba_kraje", f"home == away ({home!r})")
    else:
        report.add(OK, "oba_kraje", f"{home} vs {away}")

    score = raw.get("score") or {}
    if not score.get("full_time"):
        report.add(FAIL, "wynik_full_time", "brak score.full_time (np. '1-1')")
    else:
        report.add(OK, "wynik_full_time", score["full_time"])

    evidence_ids = {item.get("id") for item in raw.get("evidence", []) if isinstance(item, dict)}
    if not evidence_ids:
        report.add(FAIL, "evidence_obecne", "brak wpisow evidence[] (potrzebne fakty Tier A)")
    else:
        report.add(OK, "evidence_obecne", f"{len(evidence_ids)} dowodow")

    referenced: list[tuple[str, str]] = []
    for goal in raw.get("goals", []):
        if isinstance(goal, dict) and "evidence_id" in goal:
            referenced.append(("goal", goal["evidence_id"]))
    for event in raw.get("key_events", []):
        if isinstance(event, dict) and "evidence_id" in event:
            referenced.append(("key_event", event["evidence_id"]))
    for fsid in raw.get("fact_source_ids", []):
        referenced.append(("fact_source_id", fsid))
    dangling = sorted({f"{kind}:{eid}" for kind, eid in referenced if eid not in evidence_ids})
    if dangling:
        report.add(FAIL, "powiazania_evidence_id", f"wskazuja na nieistniejace id: {dangling}")
    else:
        report.add(OK, "powiazania_evidence_id", f"{len(referenced)} referencji spojnych")

    try:
        gateway.fetch_match_facts(match_id)
        report.add(OK, "integralnosc_zrodel_faktow", "provider/tier/domena w whitelist")
    except ToolGatewayError as error:
        report.add(FAIL, "integralnosc_zrodel_faktow", str(error))

    countries = [c for c in (home, away) if c]
    for country in countries:
        items = gateway.fetch_media_reactions(match_id, country)
        dropped = _last_dropped(gateway)
        if not items:
            detail = "0 cytatow z zaufanych outletow"
            if dropped:
                detail += f"; odrzucone: {dropped}"
            else:
                detail += "; sprawdz pole media[].country i czy outlet jest w country_media.json"
            report.add(FAIL, f"media[{country}]", detail)
            continue

        if dropped:
            report.add(WARN, f"media[{country}]_odrzucone", f"{dropped} (poza whitelista / nieznany outlet)")

        translated = [item for item in items if item.translation_pl]
        if llm:
            report.add(OK, f"media[{country}]", f"{len(items)} cytatow (LLM przetlumaczy oryginaly)")
            if len(translated) < len(items):
                report.add(
                    WARN,
                    f"media[{country}]_translation_pl",
                    "brak gold dla czesci cytatow; przy bledzie LLM fallback je pominie",
                )
        else:
            if not translated:
                report.add(
                    FAIL,
                    f"media[{country}]",
                    f"{len(items)} cytatow, ale 0 z 'translation_pl' -> panel bylby pusty "
                    "(dodaj tlumaczenia albo odpal z --llm)",
                )
            elif len(translated) < len(items):
                report.add(
                    WARN,
                    f"media[{country}]_translation_pl",
                    f"tylko {len(translated)}/{len(items)} ma 'translation_pl' (reszta bedzie pominieta)",
                )
            else:
                report.add(OK, f"media[{country}]", f"{len(items)} cytatow z tlumaczeniem PL")

    query = match_query or _best_alias(raw)
    if query:
        resolution = gateway.resolve_match(query)
        status = resolution.get("status")
        resolved_id = resolution.get("match_id")
        if status == "resolved" and resolved_id == match_id:
            report.add(OK, "resolver", f"'{query}' -> {match_id}")
        elif status == "resolved":
            report.add(
                WARN,
                "resolver",
                f"'{query}' rozwiazuje sie do innego meczu: {resolved_id}; "
                "alias jest niejednoznaczny - dodaj bardziej wyrozniajaca fraze do aliases",
            )
        else:
            report.add(
                WARN,
                "resolver",
                f"'{query}' -> {status}; dopracuj aliases (resolver wymaga min. 2 wspolnych tokenow)",
            )

    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="validate_fixture",
        description="Preflight fixture'a meczowego przed odpaleniem pipeline.",
    )
    parser.add_argument("match_id", help="Nazwa pliku w data/fixtures/matches/ bez .json.")
    parser.add_argument("--llm", action="store_true", help="Poluzuj wymog gold translation_pl.")
    parser.add_argument("--match", default=None, help="Fraza do przetestowania resolvera.")
    args = parser.parse_args(argv)

    print(f"== Preflight: {args.match_id} (tryb: {'LLM' if args.llm else 'deterministyczny'}) ==")
    report = validate(args.match_id, llm=args.llm, match_query=args.match)
    report.print()
    print("-" * 60)

    if report.failed:
        print("WERDYKT: NIE GOTOWE - sa blokery (FAIL). Popraw fixture i odpal ponownie.")
        return 1

    suffix = " --llm" if args.llm else ""
    example_query = args.match
    if not example_query:
        try:
            example_query = _best_alias(ToolGateway().fixtures.load(args.match_id)) or args.match_id
        except ToolGatewayError:
            example_query = args.match_id
    if report.warned:
        print("WERDYKT: GOTOWE z OSTRZEZENIAMI (WARN) - przejrzyj uwagi powyzej.")
    else:
        print("WERDYKT: GOTOWE.")
    print(f'Run:  python -m app --match "{example_query}" --pretty --save-run{suffix}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
