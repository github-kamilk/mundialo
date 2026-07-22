"""Adaptery SearchClient: Fake (offline, testy) + Tavily (realny, lazy import).

Narzedzie wyszukiwania jest scoped do whitelisty domen na DWA sposoby: prosimy API
o include_domains, a niezaleznie filtrujemy wynik przez `domain_allowed` (nie ufamy,
ze provider uszanuje filtr).
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from app.observability.telemetry import RunTelemetry, SearchEvent, classify_search_error
from app.tools.contracts import Cache, SearchHit
from app.tools.control import ResearchError, cache_key, domain_allowed

TAVILY_ENDPOINT = "https://api.tavily.com/search"


@dataclass
class TelemetrySearchClient:
    """Dekorator SearchClient: SearchEvent per zapytanie (epizody, etap 1).

    Owijamy NAJBARDZIEJ zewnetrznie (nad cachem): zdarzenie opisuje 'co run
    widzial', nie 'co poszlo do API' - 432 (kredyty Tavily) i zapytania bez
    hitow to sygnaly zdrowia niezaleznie od tego, czy odpowiedz przyszla
    z dysku. Blad propaguje bez zmian - telemetria niczego nie polyka.
    """

    inner: object  # SearchClient (Protocol); object, bo dataclass nie lubi Protocol
    telemetry: RunTelemetry

    def search(
        self, query: str, allowed_domains: tuple[str, ...], limit: int = 5
    ) -> list[SearchHit]:
        try:
            hits = self.inner.search(query, allowed_domains, limit=limit)
        except Exception as error:
            self.telemetry.emit(
                SearchEvent(query=query, hits=0, error=classify_search_error(error))
            )
            raise
        self.telemetry.emit(SearchEvent(query=query, hits=len(hits)))
        return hits


@dataclass
class CachingSearchClient:
    """Dekorator SearchClient: identyczne zapytanie w oknie TTL nie placi API.

    Powod: re-rolle tego samego meczu (wariancja LLM w kuratorze/scoucie/bramce)
    budowaly TE SAME zapytania i palily budzet Tavily od zera w kazdym procesie -
    wyczerpane kredyty (HTTP 432) topily kolejne kraje haltem one_country_media_missing.
    Z DiskTtlCache re-roll placi tylko za zapytania, ktorych jeszcze nie widzial.

    PUSTYCH wynikow celowo NIE cache'ujemy: 0 hitow tuz po meczu to zwykle zwloka
    indeksacji, a caly sens re-rollu za 30-60 min to swieza proba - zamrozenie
    pustki w cache by go wykastrowalo. Bledy (np. 432) propaguja bez zapisu.

    Klucz rozroznia konfiguracje wyszukiwania (time_range/search_depth wewnetrznego
    klienta), zeby np. run z --time-range day nie czytal wynikow okna week.
    """

    inner: object  # SearchClient (Protocol); object, bo dataclass nie lubi Protocol
    cache: Cache
    ttl_seconds: int = 3600

    def search(
        self, query: str, allowed_domains: tuple[str, ...], limit: int = 5
    ) -> list[SearchHit]:
        key = cache_key(
            "search",
            {
                "query": query,
                "domains": sorted(allowed_domains),
                "limit": limit,
                "time_range": getattr(self.inner, "time_range", None),
                "search_depth": getattr(self.inner, "search_depth", None),
            },
        )
        cached = self.cache.get(key)
        if cached is not None:
            return [SearchHit(**hit) for hit in cached]
        hits = self.inner.search(query, allowed_domains, limit=limit)
        if hits:
            self.cache.set(
                key,
                [
                    {
                        "url": hit.url,
                        "title": hit.title,
                        "snippet": hit.snippet,
                        "published_at": hit.published_at,
                        "raw_content": hit.raw_content,
                    }
                    for hit in hits
                ],
                self.ttl_seconds,
            )
        return hits


class _RetryableSearchError(Exception):
    """Wewnetrzny marker: transient blad sieci (timeout/connect/5xx/429) - warto ponowic."""


@dataclass
class FakeSearchClient:
    """Deterministyczny SearchClient do testow. Zwraca zaskryptowane wyniki.

    `hits_by_query` mapuje doslowne zapytanie na liste wynikow; `default_hits` to
    fallback. Whitelist jest egzekwowana tak samo jak w realnym adapterze.
    """

    hits_by_query: dict[str, list[SearchHit]] = field(default_factory=dict)
    default_hits: list[SearchHit] = field(default_factory=list)
    calls: list[dict] = field(default_factory=list)

    def search(
        self, query: str, allowed_domains: tuple[str, ...], limit: int = 5
    ) -> list[SearchHit]:
        self.calls.append(
            {"query": query, "allowed_domains": list(allowed_domains), "limit": limit}
        )
        hits = self.hits_by_query.get(query, self.default_hits)
        filtered = [hit for hit in hits if domain_allowed(hit.url, allowed_domains)]
        return filtered[:limit]


@dataclass
class TavilySearchClient:
    """Realny adapter Tavily (REST przez httpx). Lazy import - brak twardej zaleznosci.

    `time_range` ('day'|'week'|'month'|'year') odcina archiwalia - bez tego search
    potrafi zwrocic relacje z meczu tych samych druzyn sprzed lat.
    `search_depth` 'advanced' znaczaco poprawia trafnosc (mniej stron tagow,
    podstron klubowych itp.) kosztem 2x drozszego zapytania.
    """

    api_key: str | None = None
    timeout: float = 15.0
    time_range: str | None = None
    search_depth: str = "basic"
    # flaky siec NIE moze zabijac calego runu: jeden timeout/connect-error to halt
    # `live_facts_unavailable`, mimo ze dane sa dostepne. Ponawiamy transient bledy.
    max_attempts: int = 3
    backoff_base: float = 0.5
    sleep: Callable[[float], None] = time.sleep

    def search(
        self, query: str, allowed_domains: tuple[str, ...], limit: int = 5
    ) -> list[SearchHit]:
        key = self.api_key or os.environ.get("TAVILY_API_KEY")
        if not key:
            raise ResearchError("brak TAVILY_API_KEY")

        payload = {
            "api_key": key,
            "query": query,
            "max_results": limit,
            "search_depth": self.search_depth,
            # pelny tekst strony z crawlera Tavily: ratuje nas przy 403/timeout/JS-stronach
            "include_raw_content": True,
        }
        if self.time_range:
            payload["time_range"] = self.time_range
        if allowed_domains:
            payload["include_domains"] = list(allowed_domains)

        data = self._search_with_retry(payload)

        hits: list[SearchHit] = []
        for item in data.get("results", []):
            url = item.get("url", "")
            if not url or not domain_allowed(url, allowed_domains):
                continue
            raw_content = item.get("raw_content")
            hits.append(
                SearchHit(
                    url=url,
                    title=item.get("title", ""),
                    snippet=item.get("content", ""),
                    published_at=item.get("published_date"),
                    raw_content=raw_content if isinstance(raw_content, str) and raw_content.strip() else None,
                )
            )
        return hits[:limit]

    def _search_with_retry(self, payload: dict) -> dict:
        """Petla ponawiania: transient bledy (_RetryableSearchError) probujemy do
        max_attempts z rosnacym backoffem; permanentne (ResearchError) leca od razu."""
        last: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                return self._search_once(payload)
            except _RetryableSearchError as error:
                last = error
                if attempt < self.max_attempts:
                    self.sleep(self.backoff_base * attempt)
        raise ResearchError(
            f"Tavily search nieudany po {self.max_attempts} probach: {last}"
        ) from last

    def _search_once(self, payload: dict) -> dict:
        """Pojedyncze zapytanie. Transient bledy sieci -> _RetryableSearchError,
        reszta (brak httpx, 4xx, zly JSON) -> ResearchError (nie ma sensu ponawiac)."""
        try:
            import httpx
        except ImportError as error:  # pragma: no cover - zalezy od opcjonalnej instalacji
            raise ResearchError(
                "pakiet 'httpx' nie jest zainstalowany (pip install '.[research]')"
            ) from error

        try:
            response = httpx.post(TAVILY_ENDPOINT, json=payload, timeout=self.timeout)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as error:
            code = error.response.status_code
            # 5xx/429 sa przejsciowe; 4xx (zly klucz, zle zapytanie) nie naprawia retry
            if code >= 500 or code == 429:
                raise _RetryableSearchError(error) from error
            raise ResearchError(f"Tavily search nieudany: {error}") from error
        except httpx.TransportError as error:  # timeout, connect, read, network
            raise _RetryableSearchError(error) from error
        except Exception as error:  # noqa: BLE001 - np. zly JSON: degradacja zamiast crashu
            raise ResearchError(f"Tavily search nieudany: {error}") from error
