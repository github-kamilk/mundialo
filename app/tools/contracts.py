"""Kontrakty warstwy danych (patrz: architektura-warstwy-danych.md).

To sa interfejsy docelowe, ktore wypelnia kolejny etap (realne providery + agent
research). Definiujemy je najpierw - zasada "kontrakty
najpierw" - zeby workflow byl niezalezny od konkretnego zrodla danych, a podmiana
providera nie zmieniala reszty systemu.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from app.schemas import (
    EvidenceItem,
    MatchFacts,
    MetricSnapshot,
    PublicNarrative,
    SourceTier,
)


class AcquisitionMode(str, Enum):
    FIXTURE = "fixture"
    API = "api"
    SCRAPE = "scrape"
    RESEARCH = "research"


class ProviderCapability(str, Enum):
    RESOLVE = "resolve"
    FACTS = "facts"
    METRICS = "metrics"
    NARRATIVES = "narratives"
    MEDIA_REACTION = "media_reaction"


@dataclass(frozen=True)
class Provenance:
    """Slad pochodzenia kazdej danej. Stemplowany przez bramke."""

    provider: str
    source_url: str
    tier: SourceTier
    retrieved_at: str
    confidence: str = "high"
    cost_usd: float = 0.0
    latency_ms: int = 0


@dataclass(frozen=True)
class ProviderDescriptor:
    """Metadane providera trzymane w SourceRegistry (oddzielone od zachowania).

    Dla outletow toru medialnego doprecyzowane sa `country` i `language`, dzieki
    czemu rejestr jest jednoczesnie whitelista domen per kraj.
    """

    provider_id: str
    tier: SourceTier
    capabilities: frozenset[ProviderCapability]
    acquisition_mode: AcquisitionMode
    domains: tuple[str, ...] = ()
    cost_class: str = "free"
    requires_auth: bool = False
    country: str | None = None
    language: str | None = None

    def can(self, capability: ProviderCapability) -> bool:
        return capability in self.capabilities


@dataclass(frozen=True)
class ToolBudget:
    """Twarde granice na run - autonomia ma limit, nie tylko instrukcje."""

    max_cost_usd: float = 0.50
    max_wall_seconds: float = 90.0
    max_calls: int = 30


@dataclass
class ToolPolicy:
    whitelisted_domains: tuple[str, ...] = ()
    allow_scraping: bool = False
    cache_ttl_seconds: int = 3600
    max_retries: int = 2
    extra: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class Cache(Protocol):
    def get(self, key: str) -> Any | None: ...

    def set(self, key: str, value: Any, ttl_seconds: int) -> None: ...


@runtime_checkable
class StructuredDataProvider(Protocol):
    """Zrodlo faktow/metryk (Tier A/B). Fixture to pierwsza implementacja."""

    def resolve_match(
        self,
        query: str,
        date_hint: str | None = ...,
        competition_hint: str | None = ...,
    ) -> dict[str, Any]: ...

    def fetch_match_facts(self, match_id: str) -> tuple[MatchFacts, list[EvidenceItem]]: ...

    def fetch_team_stats(self, match_id: str) -> MetricSnapshot | None: ...


@runtime_checkable
class NarrativeResearchTool(Protocol):
    """Agent research (Tier C). Output zawsze narrative_only, nigdy fakt."""

    def fetch_public_narratives(self, match_id: str) -> list[PublicNarrative]: ...


@dataclass(frozen=True)
class RawMediaItem:
    """Surowy glos medialny po bramce: original_text jest juz zsanityzowany,
    tier/country/language pochodza z rejestru outletow (zrodlo prawdy), a
    translation_pl to opcjonalny offline-gold z fixture (sciezka bez LLM).

    `article_text` to zsanityzowany tekst CALEGO artykulu (przyciety) - podstawa
    do streszczenia na slajd; gdy None (fixture), slajd dostaje samo tlumaczenie."""

    evidence_id: str
    outlet: str
    country: str
    language: str
    url: str
    original_text: str
    tier: SourceTier
    retrieved_at: str
    translation_pl: str | None = None
    confidence: str = "high"
    article_text: str | None = None
    # tytul artykulu (zsanityzowany) - teza redakcji w pigulce; podstawa dla
    # pierwszego zdania streszczenia (ton naglowka, nie chronologia)
    title: str | None = None


@runtime_checkable
class MediaReactionTool(Protocol):
    """Pozyskiwanie reakcji mediow per kraj. Zwraca tylko glosy z zaufanych
    outletow (whitelist domen + tier z rejestru); reszta jest odrzucana."""

    def fetch_media_reactions(self, match_id: str, country: str) -> list[RawMediaItem]: ...


# --- Warstwa research (live): search + fetch + ekstrakcja przez model ------------
#
# Podzial odpowiedzialnosci: NARZEDZIE robi I/O (search/GET) w obrebie whitelisty
# domen; MODEL (scout) tylko wybiera doslowne fragmenty z juz pobranego, zsanityzowanego
# tekstu. Surowy HTML i siec nigdy nie docieraja do modelu.


@dataclass(frozen=True)
class SearchHit:
    """Kandydat z wyszukiwarki. Traktujemy go jak DANE - nie ufamy tresci ani domenie.

    `raw_content` to pelny tekst strony wyrenderowany przez crawler wyszukiwarki -
    fallback, gdy wlasny fetch pada (403, timeout, strona-aplikacja JS).
    """

    url: str
    title: str
    snippet: str
    published_at: str | None = None
    raw_content: str | None = None


@dataclass(frozen=True)
class MatchContext:
    """Kontekst meczu do budowania zapytan i ramki dla modelu (research medialny).

    `score` (wynik koncowy) pozwala scoutowi odroznic relacje POMECZOWA od
    zapowiedzi - zapowiedz przedmeczowa nie jest reakcja mediow na mecz."""

    home_team: str
    away_team: str
    date: str | None = None
    competition: str | None = None
    stage: str | None = None
    match_id: str | None = None
    score: str | None = None

    def opponent_of(self, country: str) -> str:
        return self.away_team if country == self.home_team else self.home_team


@dataclass(frozen=True)
class GoalDraft:
    team: str
    player: str
    minute: int
    detail: str = "goal"


@dataclass(frozen=True)
class FactsDraft:
    """Szkic faktow wyekstrahowany ze zrodla oficjalnego (Tier A). Provider go waliduje."""

    home_team: str
    away_team: str
    full_time: str
    competition: str
    stage: str
    date: str
    venue: str
    goals: list[GoalDraft] = field(default_factory=list)


@runtime_checkable
class SearchClient(Protocol):
    """Wyszukiwarka scoped do domen whitelisty. Zwraca kandydatow (nie ufamy tresci)."""

    def search(
        self, query: str, allowed_domains: tuple[str, ...], limit: int = ...
    ) -> list[SearchHit]: ...


@runtime_checkable
class PageFetcher(Protocol):
    """Pobiera i ekstrahuje czysty tekst artykulu. Surowy HTML nigdy nie idzie do modelu."""

    def fetch(self, url: str) -> str: ...


@runtime_checkable
class MediaScout(Protocol):
    """Model wybiera <=N doslownych fragmentow z pobranego tekstu (anti-fabrication)."""

    def extract(
        self,
        context: MatchContext,
        outlet: str,
        language: str,
        url: str,
        text: str,
        max_fragments: int = ...,
    ) -> list[str]: ...


@runtime_checkable
class MediaCurator(Protocol):
    """Model wybiera z puli kandydatow NAJLEPSZE, ROZNE reakcje wlasnej prasy kraju.

    Dziala na metadanych (tytul + snippet + URL), bez fetchu - tanio. Zastepuje kruche
    heurystyki jezykowe (opinia/digest/relevance): jeden jezykowo-agnostyczny osad nad cala
    pula zamiast rosnacej listy markerow per-jezyk. Zwraca uporzadkowany podzbior wejscia
    (best-first); pusta lista = nic sensownego, wolajacy robi fallback.
    """

    def select(
        self,
        context: MatchContext,
        country: str,
        candidates: list[SearchHit],
        max_select: int = ...,
        notes: list[str] | None = ...,
    ) -> list[SearchHit]: ...


@runtime_checkable
class FactsScout(Protocol):
    """Model ekstrahuje szkic faktow z tekstu zrodla oficjalnego (z guardrailami)."""

    def extract(self, query: str, text: str) -> FactsDraft: ...


@runtime_checkable
class SourceHealth(Protocol):
    """Zdrowie zrodel z pamieci epizodycznej (etap 3, architektura-pamiec-epizodyczna.md).

    Odpowiada na WASKIE pytania o oplacalnosc I/O (kolejnosc probowania sekcji,
    pomijanie martwego fetchu). NIGDY nie decyduje o zaufaniu (tier), whiteliscie
    ani o tresci widzianej przez model. Implementacja: OutletHealthStore; kazda
    metoda jest fail-safe (blad = odpowiedz neutralna), a status 'martwy' wygasa
    po RE_PROBE_HOURS bez proby (re-probe)."""

    def section_dead(self, section_url: str) -> bool: ...

    def section_last_probe(self, section_url: str) -> str | None: ...

    def outlet_fetch_dead(self, provider_id: str) -> bool: ...


@runtime_checkable
class PostMatchGate(Protocol):
    """Binarna bramka relevancji: czy tekst to reakcja na JUZ ROZEGRANY mecz?

    Waska decyzja (tak/nie) PRZED ekstrakcja cytatu - lapie zapowiedzi/inne mecze, ktore
    omijaja filtry daty (outlet bez daty w URL + `published_at=None`) i zwodza scouta
    dramatycznym leadem. Inaczej niz ekstrakcja (walidator verbatim) ta decyzja nie ma
    twardego walidatora, wiec dostaje DEDYKOWANE, waskie pytanie."""

    def is_post_match_reaction(
        self, context: MatchContext, url: str, text: str
    ) -> bool: ...
