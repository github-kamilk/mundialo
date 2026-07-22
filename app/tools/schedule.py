"""Terminarz meczow: dostarczone z gory metadane (druzyny, data, miasto, stadion).

Zrodlo prawdy dla TOZSAMOSCI meczu - wynik nadal pochodzi z live researchu.
Eliminuje ekstrakcje lokalizacji z tekstu (paleta grafik bierze miasto stad),
automatycznie ustawia date_hint (straznik 'inny mecz', filtr swiezosci) i sluzy
jako roadmapa postow (scripts/roadmap.py).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from app.tools.registry import SourceRegistry

DEFAULT_SCHEDULE_PATH = (
    Path(__file__).resolve().parents[2] / "data" / "schedule" / "world_cup_2026.json"
)


@dataclass(frozen=True)
class ScheduledMatch:
    date: str
    home: str
    away: str
    city: str
    stadium: str
    stage: str | None = None
    competition: str | None = None

    @property
    def venue(self) -> str:
        """Venue do faktow/palety: stadion + miasto (aliasy palet lapia oba)."""
        parts = [part for part in (self.stadium, self.city) if part]
        return ", ".join(parts)


def load_schedule(path: Path | None = None) -> list[ScheduledMatch]:
    """Wczytuje terminarz; brak/bledny plik => pusta lista (system dziala jak dotad)."""
    schedule_path = path or DEFAULT_SCHEDULE_PATH
    try:
        data = json.loads(Path(schedule_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    competition = data.get("competition")
    matches: list[ScheduledMatch] = []
    for entry in data.get("matches", []):
        if not isinstance(entry, dict):
            continue
        date = str(entry.get("date") or "").strip()
        home = str(entry.get("home") or "").strip()
        away = str(entry.get("away") or "").strip()
        if not (date and home and away):
            continue
        matches.append(
            ScheduledMatch(
                date=date,
                home=home,
                away=away,
                city=str(entry.get("city") or "").strip(),
                stadium=str(entry.get("stadium") or "").strip(),
                stage=(str(entry.get("stage")).strip() or None) if entry.get("stage") else None,
                competition=competition,
            )
        )
    return matches


def find_scheduled_match(
    registry: SourceRegistry,
    schedule: list[ScheduledMatch],
    query: str,
    date_hint: str | None = None,
) -> ScheduledMatch | None:
    """Mecz z terminarza pasujacy do zapytania usera (kraje po aliasach + data).

    Te same druzyny moga zagrac na turnieju dwa razy (grupa + faza pucharowa):
    przy wielu kandydatach bez date_hint wygrywa NAJNOWSZY (najswiezszy mecz to
    domyslny temat posta); z date_hint - dokladne dopasowanie daty.
    """
    profiles = registry.countries_in_text(query)
    if len(profiles) < 2:
        return None
    wanted = frozenset(profile.country for profile in profiles[:2])

    candidates = [match for match in schedule if frozenset({match.home, match.away}) == wanted]
    if date_hint:
        exact = [match for match in candidates if match.date == date_hint]
        if exact:
            candidates = exact
        else:
            # Granica strefy: mecze wieczorne w Amerykach FIFA/terminarz datuje LOKALNIE o dobe
            # inaczej niz date_hint usera (Iran-NZ, Arabia-Urugwaj: terminarz 06-16 vs run 06-15)
            # -> twardy '==' gubil venue ('nieznany stadion'). Tolerujemy +-1 dzien, jak
            # draft_mismatch w torze faktow; wieksza roznica = realnie inny mecz.
            candidates = [
                match for match in candidates if _date_gap_days(match.date, date_hint) <= 1
            ]
    if not candidates:
        return None
    return max(candidates, key=lambda match: match.date)


def _date_gap_days(a: str, b: str) -> int:
    """Bezwzgledna roznica dni miedzy datami ISO; duza liczba gdy ktorejs nie da sie sparsowac."""
    from datetime import date

    try:
        return abs((date.fromisoformat(a[:10]) - date.fromisoformat(b[:10])).days)
    except ValueError:
        return 10**6
