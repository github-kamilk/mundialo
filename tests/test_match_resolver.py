import unittest

from app.schemas import MatchRequest
from app.tools import ToolGateway


class MatchResolverTests(unittest.TestCase):
    def test_resolves_psg_arsenal_alias(self) -> None:
        gateway = ToolGateway()
        result = gateway.resolve_match("PSG - Arsenal, final Ligi Mistrzow 2026")

        self.assertEqual(result["status"], "resolved")
        self.assertEqual(result["match_id"], "psg_arsenal_ucl_final_2026")

    def test_unknown_match_is_insufficient_evidence(self) -> None:
        gateway = ToolGateway()
        result = gateway.resolve_match("Atlantis FC - Moon United")

        self.assertEqual(result["status"], "insufficient_evidence")

    def test_request_validation_blocks_bad_post_type(self) -> None:
        request = MatchRequest(match_query="PSG - Arsenal", post_type="chaos")

        with self.assertRaises(ValueError):
            request.validate()


if __name__ == "__main__":
    unittest.main()

