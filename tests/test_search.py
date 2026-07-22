import os
import unittest
from unittest.mock import MagicMock, patch

from app.tools.contracts import SearchHit
from app.tools.control import ResearchError, TtlCache
from app.tools.search import (
    CachingSearchClient,
    FakeSearchClient,
    TavilySearchClient,
    _RetryableSearchError,
)


class TavilyRetryTest(unittest.TestCase):
    """Retry na transient bledy sieci: jeden timeout NIE moze haltowac runu
    jako live_facts_unavailable, gdy dane sa dostepne (regresja z 2026-06-14)."""

    def _client(self, **kw) -> tuple[TavilySearchClient, list[float]]:
        sleeps: list[float] = []
        client = TavilySearchClient(api_key="k", sleep=sleeps.append, **kw)
        return client, sleeps

    def test_retries_transient_then_succeeds(self) -> None:
        client, sleeps = self._client(max_attempts=3, backoff_base=0.1)
        data = {"results": [{"url": "https://news24.com/x", "title": "t", "content": "c"}]}
        client._search_once = MagicMock(
            side_effect=[_RetryableSearchError("timeout"), _RetryableSearchError("timeout"), data]
        )
        hits = client.search("q", ("news24.com",), limit=5)
        self.assertEqual(client._search_once.call_count, 3)
        self.assertEqual([h.url for h in hits], ["https://news24.com/x"])
        self.assertEqual(len(sleeps), 2)  # backoff tylko MIEDZY probami

    def test_exhausts_retries_raises_research_error(self) -> None:
        client, _ = self._client(max_attempts=3)
        client._search_once = MagicMock(side_effect=_RetryableSearchError("read operation timed out"))
        with self.assertRaises(ResearchError) as ctx:
            client.search("q", ("news24.com",))
        self.assertEqual(client._search_once.call_count, 3)
        self.assertIn("po 3 probach", str(ctx.exception))

    def test_permanent_error_not_retried(self) -> None:
        # 4xx / zly klucz / zly JSON przychodza juz jako ResearchError - retry ich nie naprawi
        client, sleeps = self._client(max_attempts=3)
        client._search_once = MagicMock(side_effect=ResearchError("Tavily search nieudany: 401"))
        with self.assertRaises(ResearchError):
            client.search("q", ("news24.com",))
        self.assertEqual(client._search_once.call_count, 1)
        self.assertEqual(sleeps, [])

    def test_missing_key_fails_fast_without_network(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            client = TavilySearchClient(api_key=None)
            client._search_once = MagicMock()
            with self.assertRaises(ResearchError):
                client.search("q", ("news24.com",))
            client._search_once.assert_not_called()


HIT = SearchHit(
    url="https://news24.com/recap",
    title="Recap",
    snippet="s",
    published_at="2026-06-30",
    raw_content="pelny tekst artykulu",
)


class CachingSearchClientTests(unittest.TestCase):
    """Cache na torze SEARCH: re-roll tego samego meczu buduje TE SAME zapytania,
    a kazde palilo budzet Tavily (HTTP 432 topil kolejne kraje haltem
    one_country_media_missing). Puste wyniki NIE sa cache'owane - re-roll po
    zwloce indeksacji musi dostac swieza probe."""

    def _client(self, inner: FakeSearchClient) -> CachingSearchClient:
        return CachingSearchClient(inner=inner, cache=TtlCache(), ttl_seconds=60)

    def test_second_identical_query_served_from_cache(self) -> None:
        inner = FakeSearchClient(default_hits=[HIT])
        client = self._client(inner)
        first = client.search("q", ("news24.com",), limit=5)
        second = client.search("q", ("news24.com",), limit=5)
        self.assertEqual(len(inner.calls), 1)
        self.assertEqual(first, second)
        # rekonstrukcja z cache zachowuje wszystkie pola (w tym raw_content)
        self.assertEqual(second[0].raw_content, HIT.raw_content)
        self.assertEqual(second[0].published_at, HIT.published_at)

    def test_empty_results_are_not_cached(self) -> None:
        inner = FakeSearchClient(default_hits=[])
        client = self._client(inner)
        client.search("q", ("news24.com",))
        client.search("q", ("news24.com",))
        self.assertEqual(len(inner.calls), 2)

    def test_different_query_or_domains_miss_cache(self) -> None:
        inner = FakeSearchClient(default_hits=[HIT])
        client = self._client(inner)
        client.search("q1", ("news24.com",))
        client.search("q2", ("news24.com",))
        client.search("q1", ("news24.com", "other.com"))
        self.assertEqual(len(inner.calls), 3)

    def test_inner_config_differentiates_key(self) -> None:
        # run z --time-range day nie moze czytac wynikow okna week
        cache = TtlCache()
        inner_week = FakeSearchClient(default_hits=[HIT])
        inner_week.time_range = "week"
        inner_day = FakeSearchClient(default_hits=[HIT])
        inner_day.time_range = "day"
        CachingSearchClient(inner=inner_week, cache=cache).search("q", ("news24.com",))
        CachingSearchClient(inner=inner_day, cache=cache).search("q", ("news24.com",))
        self.assertEqual(len(inner_week.calls), 1)
        self.assertEqual(len(inner_day.calls), 1)

    def test_search_error_propagates_without_caching(self) -> None:
        inner = MagicMock()
        inner.search.side_effect = ResearchError("Tavily search nieudany: 432")
        client = CachingSearchClient(inner=inner, cache=TtlCache())
        with self.assertRaises(ResearchError):
            client.search("q", ("news24.com",))
        inner.search.side_effect = None
        inner.search.return_value = [HIT]
        self.assertEqual(client.search("q", ("news24.com",)), [HIT])


if __name__ == "__main__":
    unittest.main()
