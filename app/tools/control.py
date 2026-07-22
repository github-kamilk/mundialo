"""Warstwa kontrolna bramki narzedzi (patrz: architektura-warstwy-danych.md, sek. 2).

Cache, budzety, whitelista domen i sanityzacja anti-injection. Caly modul jest
deterministyczny i testowalny bez kluczy - to wlasnie ta czesc, ktora
liczy sie bardziej niz sam protokol/zlacze.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse


class BudgetExceededError(RuntimeError):
    pass


class DomainNotAllowedError(RuntimeError):
    pass


class ResearchError(RuntimeError):
    """Blad warstwy research (search/fetch). Provider degraduje sie, nie crashuje runu.

    `status_code` (opcjonalny) niesie kod HTTP zrodla bledu - telemetria epizodow
    klasyfikuje nim zdarzenie (botblock/stale_path/transient) bez kruchego
    parsowania tresci komunikatu.
    """

    def __init__(self, message: str, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


def cache_key(tool: str, args: dict[str, Any], provider: str = "") -> str:
    payload = json.dumps(args, sort_keys=True, ensure_ascii=False, default=str)
    return f"{provider}|{tool}|{payload}"


@dataclass
class TtlCache:
    """Prosty cache z TTL. Spelnia protokol Cache."""

    clock: Callable[[], float] = time.monotonic
    _store: dict[str, tuple[float, Any]] = field(default_factory=dict)

    def get(self, key: str) -> Any | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        expires_at, value = entry
        if self.clock() >= expires_at:
            self._store.pop(key, None)
            return None
        return value

    def set(self, key: str, value: Any, ttl_seconds: int) -> None:
        self._store[key] = (self.clock() + ttl_seconds, value)


@dataclass
class DiskTtlCache:
    """Cache z TTL trwaly MIEDZY procesami (protokol Cache; wartosci JSON-owalne).

    Powod istnienia: operator re-rolluje ten sam mecz kilka-kilkanascie razy
    (wariancja LLM), a kazdy proces CLI zaczynal z pustym TtlCache i palil
    budzet Tavily od zera - wyczerpane kredyty (HTTP 432) topily potem KOLEJNE
    mecze haltem one_country_media_missing (22 runy w 7 dni, szczyt po
    re-rollowych wieczorach). Dysk zamiast pamieci = re-roll placi tylko za to,
    czego jeszcze nie widzial.

    Zegar SCIENNY (time.time), nie monotonic: wpisy musza wygasac spojnie
    miedzy procesami. Uszkodzony/nieczytelny plik = miss (kasujemy i idziemy
    dalej) - cache nigdy nie moze wywalic runu.
    """

    root: Path
    clock: Callable[[], float] = time.time

    def _path(self, key: str) -> Path:
        digest = hashlib.sha1(key.encode("utf-8")).hexdigest()
        return self.root / f"{digest}.json"

    def get(self, key: str) -> Any | None:
        path = self._path(key)
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
            expires_at = float(entry["expires_at"])
            value = entry["value"]
        except FileNotFoundError:
            return None
        except Exception:  # noqa: BLE001 - uszkodzony wpis to miss, nie crash
            path.unlink(missing_ok=True)
            return None
        if self.clock() >= expires_at:
            path.unlink(missing_ok=True)
            return None
        return value

    def set(self, key: str, value: Any, ttl_seconds: int) -> None:
        path = self._path(key)
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(
                {"key": key, "expires_at": self.clock() + ttl_seconds, "value": value},
                ensure_ascii=False,
            )
            # zapis atomowy (tmp + replace): rownolegly run nie zobaczy polowy pliku
            tmp = path.with_suffix(".tmp")
            tmp.write_text(payload, encoding="utf-8")
            tmp.replace(path)
        except Exception:  # noqa: BLE001 - cache jest best-effort, run idzie dalej
            return


@dataclass
class BudgetTracker:
    """Twarda granica na run: liczba wywolan, koszt i czas. Autonomia ma limit."""

    # Limity skalibrowane pod pelny run live: facts (search+fetch) + media 2 krajow
    # (do 6 zapytan/kraj po wariantach nazw) musza siec zmiescic z zapasem.
    max_cost_usd: float = 0.50
    max_wall_seconds: float = 240.0
    max_calls: int = 60
    clock: Callable[[], float] = time.monotonic
    calls: int = 0
    cost_usd: float = 0.0
    _started_at: float | None = None

    def charge(self, cost_usd: float = 0.0) -> None:
        if self._started_at is None:
            self._started_at = self.clock()
        self.calls += 1
        self.cost_usd += cost_usd
        if self.calls > self.max_calls:
            raise BudgetExceededError(f"przekroczono limit wywolan: {self.max_calls}")
        if self.cost_usd > self.max_cost_usd:
            raise BudgetExceededError(f"przekroczono budzet kosztu: {self.max_cost_usd} USD")
        if self.clock() - self._started_at > self.max_wall_seconds:
            raise BudgetExceededError(f"przekroczono limit czasu: {self.max_wall_seconds}s")

    @property
    def remaining_calls(self) -> int:
        return max(0, self.max_calls - self.calls)

    def reset(self) -> None:
        self.calls = 0
        self.cost_usd = 0.0
        self._started_at = None


def domain_allowed(url: str, allowed_domains: tuple[str, ...]) -> bool:
    """Pusta whitelista = brak ograniczenia. Dopuszcza subdomeny zaufanych domen."""
    if not allowed_domains:
        return True
    host = (urlparse(url).hostname or "").lower()
    if not host:
        return False
    return any(host == domain or host.endswith(f".{domain}") for domain in allowed_domains)


def assert_domain_allowed(url: str, allowed_domains: tuple[str, ...]) -> None:
    if not domain_allowed(url, allowed_domains):
        raise DomainNotAllowedError(f"domena spoza whitelisty: {url}")


# Wzorce typowych prob prompt-injection w tresci pobranej z internetu (Tier C).
_INJECTION_PATTERNS: tuple[re.Pattern[str], ...] = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"ignore (all )?(previous|above|prior) (instructions|prompts)",
        r"disregard (all )?(previous|above|prior)",
        r"you are now\b",
        r"\bsystem prompt\b",
        r"\bact as\b",
        r"^(system|assistant|user)\s*:",
        r"<\|.*?\|>",
        r"\[/?INST\]",
    )
)

_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_INJECTION_MARKER = "[usunieto potencjalna instrukcje]"


def sanitize_external_text(text: str, max_len: int = 2000) -> str:
    """Neutralizuje tresc z internetu: traktujemy ja jako DANE, nie instrukcje.

    Structured output jest sciana ogniowa, a to dodatkowa warstwa: linie
    wygladajace na instrukcje sa usuwane, znaki kontrolne czyszczone, dlugosc
    ograniczona.
    """
    cleaned = _CONTROL_CHARS.sub("", text)
    safe_lines: list[str] = []
    for line in cleaned.splitlines():
        if any(pattern.search(line) for pattern in _INJECTION_PATTERNS):
            safe_lines.append(_INJECTION_MARKER)
            continue
        safe_lines.append(line)
    collapsed = re.sub(r"[ \t]+", " ", " ".join(safe_lines)).strip()
    if len(collapsed) > max_len:
        collapsed = collapsed[:max_len].rstrip() + "..."
    return collapsed
