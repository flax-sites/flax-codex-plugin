import tempfile
import unittest
from pathlib import Path

from flax_mcp import TokenStore, metadata_candidates, normalize_origin, pkce_pair


class FlaxMcpTests(unittest.TestCase):
    def test_normalize_origin_discards_path_and_query(self):
        self.assertEqual(
            normalize_origin("https://example.com/some/page?x=1"),
            "https://example.com",
        )

    def test_normalize_origin_rejects_credentials(self):
        with self.assertRaises(Exception):
            normalize_origin("https://user:pass@example.com")

    def test_pkce_challenge_is_url_safe(self):
        verifier, challenge = pkce_pair()
        self.assertGreaterEqual(len(verifier), 43)
        self.assertNotIn("=", challenge)

    def test_site_scoped_metadata_has_supported_paths(self):
        candidates = metadata_candidates("https://agents.example/site-mcp/site-1/oauth")
        self.assertIn(
            "https://agents.example/.well-known/oauth-authorization-server/site-mcp/site-1/oauth",
            candidates,
        )
        self.assertIn(
            "https://agents.example/site-mcp/site-1/oauth/.well-known/oauth-authorization-server",
            candidates,
        )

    def test_file_store_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tokens.json"
            store = TokenStore(path)
            value = {"access_token": "test", "mcp_url": "https://agents.example/mcp"}
            store.save(value)
            self.assertEqual(store.load(), value)
            store.clear()
            self.assertIsNone(store.load())


if __name__ == "__main__":
    unittest.main()
