"""Adaptery PageFetcher: Fake (offline, testy) + Http (realny, lazy import).

Fetcher zwraca CZYSTY TEKST artykulu - ekstrakcja glownej tresci dzieje sie tutaj,
zeby surowy HTML (skrypty, nawigacja, potencjalne injection) nigdy nie trafil dalej.
Sanityzacja anti-injection jest osobna warstwa w providerze (sanitize_external_text).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.tools.control import ResearchError

# Naglowki przegladarkowe: wiele serwisow informacyjnych (np. kicker.de) odpowiada
# 403 na nieznane User-Agenty. Pobieramy publiczne artykuly z whitelisty redakcji,
# z zachowaniem atrybucji (URL zostaje w EvidenceStore i na slajdzie zrodel).
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
DEFAULT_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,de;q=0.8,es;q=0.7,pl;q=0.6",
}
MAX_TEXT_LEN = 20000


def _status_code_of(error: Exception) -> int | None:
    """Kod HTTP z wyjatku httpx (HTTPStatusError ma .response); inne bledy -> None."""
    response = getattr(error, "response", None)
    return getattr(response, "status_code", None)


@dataclass
class FakePageFetcher:
    """Deterministyczny fetcher do testow. Zwraca zaskryptowany tekst po URL."""

    pages: dict[str, str] = field(default_factory=dict)
    links: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    calls: list[str] = field(default_factory=list)

    def fetch(self, url: str) -> str:
        self.calls.append(url)
        if url not in self.pages:
            raise ResearchError(f"FakePageFetcher: brak strony dla {url}")
        return self.pages[url]

    def fetch_links(self, url: str) -> list[tuple[str, str]]:
        self.calls.append(url)
        if url not in self.links:
            raise ResearchError(f"FakePageFetcher: brak linkow dla {url}")
        return self.links[url]


@dataclass
class HttpPageFetcher:
    """Realny fetcher: httpx GET + ekstrakcja tresci (trafilatura). Lazy import."""

    timeout: float = 15.0
    user_agent: str = DEFAULT_USER_AGENT

    def fetch(self, url: str) -> str:
        try:
            import httpx
        except ImportError as error:  # pragma: no cover - opcjonalna instalacja
            raise ResearchError(
                "pakiet 'httpx' nie jest zainstalowany (pip install '.[research]')"
            ) from error

        try:
            response = httpx.get(
                url,
                timeout=self.timeout,
                follow_redirects=True,
                headers={"User-Agent": self.user_agent, **DEFAULT_HEADERS},
            )
            response.raise_for_status()
            html = response.text
        except Exception as error:  # noqa: BLE001 - degradacja zamiast crashu
            # kod HTTP (o ile jest) wedruje w ResearchError - telemetria epizodow
            # klasyfikuje nim zdarzenie (403=botblock, 404=stale_path, 5xx=transient)
            raise ResearchError(
                f"fetch nieudany ({url}): {error}", status_code=_status_code_of(error)
            ) from error

        text = self._extract_text(html)
        if not text.strip():
            raise ResearchError(f"pusta tresc po ekstrakcji: {url}")
        return text[:MAX_TEXT_LEN]

    @staticmethod
    def _extract_text(html: str) -> str:
        try:
            import trafilatura
        except ImportError as error:  # pragma: no cover - opcjonalna instalacja
            raise ResearchError(
                "pakiet 'trafilatura' nie jest zainstalowany (pip install '.[research]')"
            ) from error
        extracted = trafilatura.extract(html, include_comments=False, include_tables=False)
        return extracted or ""

    def fetch_links(self, url: str) -> list[tuple[str, str]]:
        """Linki (url, tekst kotwicy) ze strony sekcji - zrodlo NAJSWIEZSZYCH artykulow.

        Strona dzialu sportowego redakcji listuje relacje natychmiast po publikacji,
        podczas gdy indeks wyszukiwarki laduje je z opoznieniem godzin. Surowy HTML
        nie idzie dalej - tylko pary (href, anchor).
        """
        import re as _re
        from urllib.parse import urljoin

        try:
            import httpx
        except ImportError as error:  # pragma: no cover - opcjonalna instalacja
            raise ResearchError(
                "pakiet 'httpx' nie jest zainstalowany (pip install '.[research]')"
            ) from error

        try:
            response = httpx.get(
                url,
                timeout=self.timeout,
                follow_redirects=True,
                headers={"User-Agent": self.user_agent, **DEFAULT_HEADERS},
            )
            response.raise_for_status()
            html = response.text
        except Exception as error:  # noqa: BLE001 - degradacja zamiast crashu
            raise ResearchError(
                f"fetch sekcji nieudany ({url}): {error}", status_code=_status_code_of(error)
            ) from error

        links: list[tuple[str, str]] = []
        for match in _re.finditer(
            r"<a\s[^>]*href=[\"']([^\"'#]+)[\"'][^>]*>(.*?)</a>", html, _re.IGNORECASE | _re.DOTALL
        ):
            href = urljoin(url, match.group(1).strip())
            anchor = _re.sub(r"<[^>]+>", " ", match.group(2))
            anchor = " ".join(anchor.split())[:200]
            if href.startswith("http"):
                links.append((href, anchor))
        return links[:300]
