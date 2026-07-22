from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from app.schemas import EvidenceItem, SourceTier
from app.tools.contracts import (
    AcquisitionMode,
    ProviderCapability,
    ProviderDescriptor,
)
from app.tools.control import domain_allowed

DEFAULT_COUNTRY_MEDIA_PATH = (
    Path(__file__).resolve().parents[2] / "data" / "sources" / "country_media.json"
)


class SourcePolicyError(RuntimeError):
    pass


_PURPOSE_TO_CAPABILITY: dict[str, ProviderCapability] = {
    "facts": ProviderCapability.FACTS,
    "metrics": ProviderCapability.METRICS,
    "narratives": ProviderCapability.NARRATIVES,
    "resolve": ProviderCapability.RESOLVE,
    "media_reaction": ProviderCapability.MEDIA_REACTION,
}

_VALID_TIERS = frozenset({"A", "B", "C"})
_VALID_CONFIDENCE = frozenset({"high", "medium"})


@dataclass(frozen=True)
class MediaOutletProfile:
    """Pelny profil outletu: deskryptor whitelisty + metadane dla agenta research."""

    descriptor: ProviderDescriptor
    name: str
    sections: tuple[str, ...]
    confidence: str
    verified_at: str | None


@dataclass(frozen=True)
class CountryMediaProfile:
    """Research brief per kraj: gdzie zajrzec i co przeszukac."""

    country: str
    language: str
    iso2: str
    confederation: str
    role: str
    team_names: tuple[str, ...]
    query_templates: tuple[str, ...]
    outlets: tuple[MediaOutletProfile, ...]
    editorial_note: str | None = None
    english_name: str | None = None
    # Lokalny termin "mistrzostwa swiata" + rok (WK 2026 / WM 2026 / Mundial 2026 /
    # Coupe du monde 2026...). Zasila bezprzeciwnikowe zapytanie '{team} {world_cup}':
    # lokalna prasa nazywa przeciwnika po swojemu (Duitsland/Alemania), wiec anglo
    # 'Germany' gubi ranking - lokalny termin MS + swiezosc (time_range) lapie recap.
    world_cup: str | None = None
    # Egzonimy TEGO kraju w jezykach CUDZEJ prasy: mapa 'kod jezyka lidera' -> nazwa
    # ('es' -> 'Suiza'). opp_variants w zapytaniach lokalnej prasy niosly tylko WLASNE
    # przydomki przeciwnika (Schweiz/Suisse/Nati) - prasa argentynska zadnego z nich
    # nie uzywa, wiec zapytania z przeciwnikiem nie dosiegaly cronik (Argentyna-Szwajcaria
    # run_20260713060703). Krotka krotka par zamiast dict: profil jest frozen+hashowalny.
    exonyms: tuple[tuple[str, str], ...] = ()

    def aliases(self) -> tuple[str, ...]:
        """Wszystkie nazwy, pod ktorymi kraj moze wystapic w zapytaniu lub zrodle."""
        seen: dict[str, str] = {}
        for name in (self.country, self.english_name, *self.team_names):
            if name and name not in seen:
                seen[name] = name
        return tuple(seen)

    def exonym_for(self, language: str) -> str | None:
        """Nazwa tego kraju w prasie piszacej w `language`; None gdy brak wpisu."""
        for lang, name in self.exonyms:
            if lang == language:
                return name
        return None


def _media_outlet_descriptor(
    provider_id: str,
    tier: SourceTier,
    domains: tuple[str, ...],
    country: str,
    language: str,
) -> ProviderDescriptor:
    return ProviderDescriptor(
        provider_id=provider_id,
        tier=tier,
        capabilities=frozenset({ProviderCapability.MEDIA_REACTION}),
        acquisition_mode=AcquisitionMode.RESEARCH,
        domains=domains,
        cost_class="metered",
        requires_auth=True,
        country=country,
        language=language,
    )


def _infrastructure_catalog() -> dict[str, ProviderDescriptor]:
    return {
        "OfflineVerifiedFixture": ProviderDescriptor(
            provider_id="OfflineVerifiedFixture",
            tier=SourceTier.A,
            capabilities=frozenset(
                {ProviderCapability.RESOLVE, ProviderCapability.FACTS}
            ),
            acquisition_mode=AcquisitionMode.FIXTURE,
        ),
        "OfflineMetricFixture": ProviderDescriptor(
            provider_id="OfflineMetricFixture",
            tier=SourceTier.B,
            capabilities=frozenset({ProviderCapability.METRICS}),
            acquisition_mode=AcquisitionMode.FIXTURE,
        ),
        "OfflineNarrativeFixture": ProviderDescriptor(
            provider_id="OfflineNarrativeFixture",
            tier=SourceTier.C,
            capabilities=frozenset({ProviderCapability.NARRATIVES}),
            acquisition_mode=AcquisitionMode.FIXTURE,
        ),
        "OfficialMatchApi": ProviderDescriptor(
            provider_id="OfficialMatchApi",
            tier=SourceTier.A,
            capabilities=frozenset(
                {ProviderCapability.RESOLVE, ProviderCapability.FACTS}
            ),
            acquisition_mode=AcquisitionMode.API,
            domains=("fifa.com", "uefa.com"),
            cost_class="licensed",
            requires_auth=True,
        ),
        "LicensedStatsApi": ProviderDescriptor(
            provider_id="LicensedStatsApi",
            tier=SourceTier.B,
            capabilities=frozenset({ProviderCapability.METRICS}),
            acquisition_mode=AcquisitionMode.API,
            cost_class="licensed",
            requires_auth=True,
        ),
        "SearchApiNarratives": ProviderDescriptor(
            provider_id="SearchApiNarratives",
            tier=SourceTier.C,
            capabilities=frozenset({ProviderCapability.NARRATIVES}),
            acquisition_mode=AcquisitionMode.RESEARCH,
            cost_class="metered",
            requires_auth=True,
        ),
    }


def _require_str(entry: dict, key: str, *, context: str) -> str:
    value = entry.get(key)
    if not isinstance(value, str) or not value.strip():
        raise SourcePolicyError(f"{context}: pole '{key}' musi byc niepustym stringiem")
    return value.strip()


def _require_str_list(entry: dict, key: str, *, context: str) -> list[str]:
    raw = entry.get(key)
    if not isinstance(raw, list) or not raw:
        raise SourcePolicyError(f"{context}: pole '{key}' musi byc niepusta lista")
    values: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            raise SourcePolicyError(f"{context}: wpis w '{key}' musi byc niepustym stringiem")
        values.append(item.strip())
    return values


def load_country_media(
    path: Path | None = None,
) -> tuple[dict[str, ProviderDescriptor], dict[str, CountryMediaProfile]]:
    """Laduje katalog mediow per kraj z JSON do deskryptorow i profili research."""
    catalog_path = path or DEFAULT_COUNTRY_MEDIA_PATH
    if not catalog_path.exists():
        raise SourcePolicyError(f"brak pliku katalogu mediow: {catalog_path}")

    data = json.loads(catalog_path.read_text(encoding="utf-8"))
    countries = data.get("countries")
    if not isinstance(countries, list) or not countries:
        raise SourcePolicyError("country_media.json: pole 'countries' musi byc niepusta lista")

    descriptors: dict[str, ProviderDescriptor] = {}
    profiles: dict[str, CountryMediaProfile] = {}
    seen_countries: set[str] = set()

    for country_entry in countries:
        if not isinstance(country_entry, dict):
            raise SourcePolicyError("country_media.json: kazdy kraj musi byc obiektem")

        country = _require_str(country_entry, "country", context="kraj")
        if country in seen_countries:
            raise SourcePolicyError(f"country_media.json: zduplikowany kraj: {country}")
        seen_countries.add(country)

        language = _require_str(country_entry, "language", context=country)
        iso2 = _require_str(country_entry, "iso2", context=country)
        confederation = _require_str(country_entry, "confederation", context=country)
        role = _require_str(country_entry, "role", context=country)

        hints = country_entry.get("search_hints")
        if not isinstance(hints, dict):
            raise SourcePolicyError(f"{country}: brak obiektu 'search_hints'")
        team_names = tuple(_require_str_list(hints, "team_names", context=country))
        query_templates = tuple(_require_str_list(hints, "query_templates", context=country))
        english_name = hints.get("english_name")
        if english_name is not None and (
            not isinstance(english_name, str) or not english_name.strip()
        ):
            raise SourcePolicyError(f"{country}: english_name musi byc stringiem lub null")
        world_cup = hints.get("world_cup")
        if world_cup is not None and (
            not isinstance(world_cup, str) or not world_cup.strip()
        ):
            raise SourcePolicyError(f"{country}: world_cup musi byc stringiem lub null")
        raw_exonyms = hints.get("exonyms", {})
        if not isinstance(raw_exonyms, dict) or any(
            not isinstance(k, str) or not k.strip() or not isinstance(v, str) or not v.strip()
            for k, v in raw_exonyms.items()
        ):
            raise SourcePolicyError(
                f"{country}: exonyms musi byc mapa jezyk->nazwa (niepuste stringi)"
            )
        exonyms = tuple(sorted((k.strip(), v.strip()) for k, v in raw_exonyms.items()))

        raw_outlets = country_entry.get("outlets")
        if not isinstance(raw_outlets, list) or len(raw_outlets) < 1:
            raise SourcePolicyError(f"{country}: musi miec co najmniej 1 outlet")

        outlet_profiles: list[MediaOutletProfile] = []
        for outlet in raw_outlets:
            if not isinstance(outlet, dict):
                raise SourcePolicyError(f"{country}: kazdy outlet musi byc obiektem")

            provider_id = _require_str(outlet, "provider_id", context=country)
            if provider_id in descriptors:
                raise SourcePolicyError(
                    f"country_media.json: zduplikowany provider_id: {provider_id}"
                )

            tier_raw = _require_str(outlet, "tier", context=provider_id)
            if tier_raw not in _VALID_TIERS:
                raise SourcePolicyError(
                    f"{provider_id}: nieprawidlowy tier '{tier_raw}' (dozwolone: A, B, C)"
                )

            domains = tuple(_require_str_list(outlet, "domains", context=provider_id))
            outlet_language = outlet.get("language", language)
            if not isinstance(outlet_language, str) or not outlet_language.strip():
                raise SourcePolicyError(f"{provider_id}: nieprawidlowe pole 'language'")
            outlet_language = outlet_language.strip()

            confidence = outlet.get("confidence", "high")
            if confidence not in _VALID_CONFIDENCE:
                raise SourcePolicyError(
                    f"{provider_id}: nieprawidlowe confidence '{confidence}'"
                )

            verified_at = outlet.get("verified_at")
            if verified_at is not None and (
                not isinstance(verified_at, str) or not verified_at.strip()
            ):
                raise SourcePolicyError(f"{provider_id}: verified_at musi byc stringiem lub null")

            sections_raw = outlet.get("sections", [])
            if not isinstance(sections_raw, list):
                raise SourcePolicyError(f"{provider_id}: 'sections' musi byc lista")
            sections = tuple(
                section.strip()
                for section in sections_raw
                if isinstance(section, str) and section.strip()
            )

            descriptor = _media_outlet_descriptor(
                provider_id,
                SourceTier(tier_raw),
                domains,
                country,
                outlet_language,
            )
            descriptors[provider_id] = descriptor
            outlet_profiles.append(
                MediaOutletProfile(
                    descriptor=descriptor,
                    name=_require_str(outlet, "name", context=provider_id),
                    sections=sections,
                    confidence=confidence,
                    verified_at=verified_at.strip() if isinstance(verified_at, str) else None,
                )
            )

        editorial_note = country_entry.get("editorial_note")
        if editorial_note is not None and (
            not isinstance(editorial_note, str) or not editorial_note.strip()
        ):
            raise SourcePolicyError(f"{country}: editorial_note musi byc stringiem lub null")

        profiles[country] = CountryMediaProfile(
            country=country,
            language=language,
            iso2=iso2,
            confederation=confederation,
            role=role,
            team_names=team_names,
            query_templates=query_templates,
            outlets=tuple(outlet_profiles),
            editorial_note=editorial_note.strip() if isinstance(editorial_note, str) else None,
            english_name=english_name.strip() if isinstance(english_name, str) else None,
            world_cup=world_cup.strip() if isinstance(world_cup, str) else None,
            exonyms=exonyms,
        )

    return descriptors, profiles


def _default_catalog() -> dict[str, ProviderDescriptor]:
    infra = _infrastructure_catalog()
    media, _ = load_country_media()
    overlap = set(infra) & set(media)
    if overlap:
        raise SourcePolicyError(
            f"country_media.json: provider_id koliduje z infrastruktura: {sorted(overlap)}"
        )
    return {**infra, **media}


class SourceRegistry:
    """Katalog zaufanych zrodel: tier, capabilities, domeny, tryb pozyskania.

    Zasada nadrzedna: rejestr decyduje, co jest zaufane
    do jakiego celu, niezaleznie od tego, ktory provider akurat dostarcza dane.
    """

    def __init__(
        self,
        catalog: dict[str, ProviderDescriptor] | None = None,
        country_profiles: dict[str, CountryMediaProfile] | None = None,
        country_media_path: Path | None = None,
    ) -> None:
        if catalog is None:
            media, profiles = load_country_media(country_media_path)
            infra = _infrastructure_catalog()
            overlap = set(infra) & set(media)
            if overlap:
                raise SourcePolicyError(
                    f"country_media.json: provider_id koliduje z infrastruktura: "
                    f"{sorted(overlap)}"
                )
            self._providers = {**infra, **media}
            self._country_profiles = profiles
        else:
            self._providers = catalog
            self._country_profiles = country_profiles or {}

    def get(self, provider: str) -> ProviderDescriptor | None:
        return self._providers.get(provider)

    def is_trusted_for(self, provider: str, purpose: str) -> bool:
        descriptor = self.get(provider)
        capability = _PURPOSE_TO_CAPABILITY.get(purpose)
        if not descriptor or capability is None:
            return False
        return descriptor.can(capability)

    def providers_with(self, capability: ProviderCapability) -> list[str]:
        return sorted(
            provider
            for provider, descriptor in self._providers.items()
            if descriptor.can(capability)
        )

    def providers_for_country(
        self, country: str, capability: ProviderCapability = ProviderCapability.MEDIA_REACTION
    ) -> list[ProviderDescriptor]:
        """Whitelist outletow danego kraju dla danej zdolnosci (np. media_reaction)."""
        return sorted(
            (
                descriptor
                for descriptor in self._providers.values()
                if descriptor.country == country and descriptor.can(capability)
            ),
            key=lambda descriptor: (descriptor.tier.value, descriptor.provider_id),
        )

    def media_countries(self) -> list[str]:
        return sorted(
            {
                descriptor.country
                for descriptor in self._providers.values()
                if descriptor.country and descriptor.can(ProviderCapability.MEDIA_REACTION)
            }
        )

    def outlet_for_url(
        self,
        country: str,
        url: str,
        capability: ProviderCapability = ProviderCapability.MEDIA_REACTION,
    ) -> ProviderDescriptor | None:
        """Mapuje URL na outlet kraju, ktorego whitelist domen pasuje (inaczej None)."""
        for descriptor in self.providers_for_country(country, capability):
            if descriptor.domains and domain_allowed(url, descriptor.domains):
                return descriptor
        return None

    def country_profile(self, country: str) -> CountryMediaProfile | None:
        return self._country_profiles.get(country)

    def outlet_display_names(self) -> dict[str, str]:
        """Mapa provider_id -> ludzka nazwa outletu ('News24ZA' -> 'News24').

        provider_id to identyfikator techniczny (unikalny w rejestrze); na tresc
        widoczna dla odbiorcy (hook, caption) idzie nazwa redakcji z katalogu.
        """
        return {
            outlet.descriptor.provider_id: outlet.name
            for profile in self._country_profiles.values()
            for outlet in profile.outlets
        }

    def _alias_index(self) -> list[tuple[str, CountryMediaProfile]]:
        """Indeks (alias_folded -> profil), aliasy najdluzsze najpierw (lazy, cache)."""
        cached = getattr(self, "_alias_index_cache", None)
        if cached is not None:
            return cached
        from app.memory import fold_ascii

        index: list[tuple[str, CountryMediaProfile]] = []
        for profile in self._country_profiles.values():
            for alias in profile.aliases():
                index.append((fold_ascii(alias), profile))
        index.sort(key=lambda pair: len(pair[0]), reverse=True)
        self._alias_index_cache = index
        return index

    def match_country(self, name: str) -> CountryMediaProfile | None:
        """Mapuje dowolna nazwe druzyny/kraju (PL/EN/lokalna) na profil kraju."""
        from app.memory import fold_ascii

        folded = fold_ascii(name).strip()
        if not folded:
            return None
        for alias, profile in self._alias_index():
            if alias == folded:
                return profile
        return None

    def countries_in_text(self, text: str) -> list[CountryMediaProfile]:
        """Wykrywa kraje wymienione w tekscie (np. zapytaniu usera), w kolejnosci wystapienia."""
        import re

        from app.memory import fold_ascii

        folded = fold_ascii(text)
        found: dict[str, tuple[int, CountryMediaProfile]] = {}
        for alias, profile in self._alias_index():
            match = re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", folded)
            if match is None:
                continue
            current = found.get(profile.country)
            if current is None or match.start() < current[0]:
                found[profile.country] = (match.start(), profile)
        return [profile for _, profile in sorted(found.values(), key=lambda pair: pair[0])]

    def country_profiles(self) -> dict[str, CountryMediaProfile]:
        return dict(self._country_profiles)

    def allowed_providers(self) -> list[str]:
        return sorted(self._providers)

    def validate_evidence(self, item: EvidenceItem) -> None:
        """Egzekwuje integralnosc zrodla: znany provider + zgodny tier.

        To jest techniczny bezpiecznik na regule 'Tier C nigdy nie jest faktem'
        i ochrona przed poisoningiem (item podszywajacy sie pod wyzszy tier).
        """
        descriptor = self.get(item.provider)
        if not descriptor:
            raise SourcePolicyError(
                f"unknown provider not in source registry: {item.provider}"
            )
        if descriptor.tier != item.source_tier:
            raise SourcePolicyError(
                f"tier mismatch for provider {item.provider}: "
                f"declared {item.source_tier.value}, registry says {descriptor.tier.value}"
            )
        if not domain_allowed(item.source_url, descriptor.domains):
            raise SourcePolicyError(
                f"source_url spoza whitelisty providera {item.provider}: {item.source_url}"
            )
