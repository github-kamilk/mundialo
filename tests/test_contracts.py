import unittest

from app.schemas import SourceTier
from app.tools import (
    AcquisitionMode,
    NarrativeResearchTool,
    ProviderCapability,
    ProviderDescriptor,
    SourceRegistry,
    StructuredDataProvider,
    ToolGateway,
)


class DataLayerContractTests(unittest.TestCase):
    def test_gateway_conforms_to_structured_data_provider(self) -> None:
        # Guard przed driftem: fixture-gateway musi spelniac docelowy kontrakt A/B,
        # zeby podmiana na realny provider nie zmieniala workflow.
        self.assertIsInstance(ToolGateway(), StructuredDataProvider)

    def test_gateway_conforms_to_narrative_research_tool(self) -> None:
        self.assertIsInstance(ToolGateway(), NarrativeResearchTool)

    def test_provider_descriptor_capabilities(self) -> None:
        descriptor = ProviderDescriptor(
            provider_id="OfflineMetricFixture",
            tier=SourceTier.B,
            capabilities=frozenset({ProviderCapability.METRICS}),
            acquisition_mode=AcquisitionMode.FIXTURE,
        )

        self.assertTrue(descriptor.can(ProviderCapability.METRICS))
        self.assertFalse(descriptor.can(ProviderCapability.FACTS))


class SourceCatalogTests(unittest.TestCase):
    def test_chosen_providers_registered_as_auth_required(self) -> None:
        registry = SourceRegistry()
        for provider_id in ("OfficialMatchApi", "LicensedStatsApi", "SearchApiNarratives"):
            descriptor = registry.get(provider_id)
            self.assertIsNotNone(descriptor)
            assert descriptor is not None
            self.assertTrue(descriptor.requires_auth)

    def test_capability_based_trust(self) -> None:
        registry = SourceRegistry()

        self.assertTrue(registry.is_trusted_for("OfficialMatchApi", "facts"))
        self.assertFalse(registry.is_trusted_for("OfficialMatchApi", "metrics"))
        self.assertTrue(registry.is_trusted_for("LicensedStatsApi", "metrics"))
        self.assertTrue(registry.is_trusted_for("SearchApiNarratives", "narratives"))
        self.assertFalse(registry.is_trusted_for("SearchApiNarratives", "facts"))

    def test_research_provider_cannot_be_used_for_facts(self) -> None:
        # Twarda granica projektu: agent research (Tier C) nigdy nie jest faktem.
        registry = SourceRegistry()
        fact_providers = registry.providers_with(ProviderCapability.FACTS)

        self.assertNotIn("SearchApiNarratives", fact_providers)
        self.assertIn("OfficialMatchApi", fact_providers)


if __name__ == "__main__":
    unittest.main()
