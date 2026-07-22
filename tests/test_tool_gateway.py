import unittest

from app.tools import SourceRegistry, ToolGateway


class ToolGatewayTests(unittest.TestCase):
    def test_source_policy_distinguishes_facts_and_narratives(self) -> None:
        registry = SourceRegistry()

        self.assertTrue(registry.is_trusted_for("OfflineVerifiedFixture", "facts"))
        self.assertFalse(registry.is_trusted_for("OfflineNarrativeFixture", "facts"))
        self.assertTrue(registry.is_trusted_for("OfflineNarrativeFixture", "narratives"))

    def test_tool_calls_are_logged(self) -> None:
        gateway = ToolGateway()
        gateway.resolve_match("PSG - Arsenal")

        self.assertEqual(len(gateway.as_dicts()), 1)
        self.assertEqual(gateway.as_dicts()[0]["tool"], "resolve_match")


if __name__ == "__main__":
    unittest.main()

