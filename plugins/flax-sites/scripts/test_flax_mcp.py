import json
import subprocess
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

from flax_mcp import (
    FlaxBridge,
    McpStdioServer,
    TokenStore,
    merge_query_params,
    metadata_candidates,
    normalize_origin,
    pkce_pair,
)


class FlaxMcpTests(unittest.TestCase):
    def test_stdio_process_supports_codex_startup_and_probe_sequence(self):
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            {"jsonrpc": "2.0", "id": 3, "method": "resources/list", "params": {}},
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "resources/templates/list",
                "params": {},
            },
            {"jsonrpc": "2.0", "id": 5, "method": "prompts/list", "params": {}},
            {"jsonrpc": "2.0", "id": 6, "method": "ping", "params": {}},
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {"name": "flax_connection_status", "arguments": {}},
            },
        ]
        process = subprocess.run(
            [sys.executable, str(Path(__file__).with_name("flax_mcp.py"))],
            input="".join(json.dumps(request) + "\n" for request in requests),
            capture_output=True,
            text=True,
            check=True,
            timeout=15,
        )
        responses = [json.loads(line) for line in process.stdout.splitlines()]
        by_id = {response["id"]: response for response in responses}
        tool_names = [tool["name"] for tool in by_id[2]["result"]["tools"]]
        self.assertIn("flax_connect", tool_names)
        self.assertIn("flax_get_site_model", tool_names)
        self.assertIn("flax_get_model_hash", tool_names)
        self.assertEqual(by_id[3]["result"], {"resources": []})
        self.assertEqual(by_id[4]["result"], {"resourceTemplates": []})
        self.assertEqual(by_id[5]["result"], {"prompts": []})
        self.assertEqual(by_id[6]["result"], {})
        self.assertEqual(by_id[7]["result"]["content"][0]["type"], "text")

    def test_mcp_manifest_runs_relative_launcher_from_plugin_root(self):
        manifest_path = Path(__file__).parents[1] / ".mcp.json"
        manifest = json.loads(manifest_path.read_text())
        server = manifest["mcpServers"]["flax-sites"]
        self.assertEqual(server["cwd"], ".")
        self.assertEqual(server["args"], ["./scripts/flax_mcp.py"])

    def test_normalize_origin_discards_path_and_query(self):
        self.assertEqual(
            normalize_origin("https://example.com/some/page?x=1"),
            "https://example.com",
        )

    def test_normalize_origin_rejects_credentials(self):
        with self.assertRaises(Exception):
            normalize_origin("https://user:pass@example.com")

    def test_merge_query_params_preserves_existing_site_context(self):
        authorization_url = merge_query_params(
            "https://flaxsites.com/oauth/site-context?site_id=site-1",
            {
                "response_type": "code",
                "client_id": "codex",
                "resource": "https://agents.example/site-mcp/site-1/mcp",
            },
        )
        parsed = urllib.parse.urlparse(authorization_url)
        query = urllib.parse.parse_qs(parsed.query)
        self.assertEqual(query["site_id"], ["site-1"])
        self.assertEqual(query["response_type"], ["code"])
        self.assertEqual(query["client_id"], ["codex"])
        self.assertEqual(
            query["resource"], ["https://agents.example/site-mcp/site-1/mcp"]
        )

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

    def test_keychain_timeout_falls_back_to_file_store(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TokenStore(Path(directory) / "tokens.json")
            value = {"access_token": "test", "mcp_url": "https://agents.example/mcp"}
            timeout = subprocess.TimeoutExpired("security", 5)
            with mock.patch.object(store, "_keychain_available", return_value=True):
                with mock.patch("flax_mcp.subprocess.run", side_effect=timeout):
                    store.save(value)
                    self.assertEqual(store.load(), value)
                    store.clear()
            self.assertIsNone(store.load())

    def test_proxy_tools_are_declared_before_authentication(self):
        class Bridge:
            def connected(self):
                return False

        server = McpStdioServer()
        server.bridge = Bridge()
        names = [tool["name"] for tool in server.tools()]
        self.assertIn("flax_connect", names)
        self.assertIn("flax_get_site_model", names)
        self.assertIn("flax_get_model_hash", names)

    def test_resource_and_prompt_probes_return_empty_lists(self):
        server = McpStdioServer()
        messages = []
        server.send = messages.append
        for request_id, method in enumerate(
            ["resources/list", "resources/templates/list", "prompts/list", "ping"],
            start=1,
        ):
            server.handle(
                {"jsonrpc": "2.0", "id": request_id, "method": method, "params": {}}
            )
        self.assertEqual(messages[0]["result"], {"resources": []})
        self.assertEqual(messages[1]["result"], {"resourceTemplates": []})
        self.assertEqual(messages[2]["result"], {"prompts": []})
        self.assertEqual(messages[3]["result"], {})

    def test_connect_reuses_a_valid_same_site_connection(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TokenStore(Path(directory) / "tokens.json")
            store.save(
                {
                    "access_token": "test",
                    "expires_at": 4102444800,
                    "mcp_url": "https://agents.example/site-mcp/site-1/mcp",
                    "site_url": "https://example.com",
                }
            )
            result = FlaxBridge(store).connect("https://example.com/page")
            self.assertEqual(result["siteUrl"], "https://example.com")
            self.assertIn("already connected", result["message"])

    def test_local_tool_call_returns_mcp_call_tool_result(self):
        class Store:
            def load(self):
                return {"access_token": "test", "site_url": "https://example.com"}

        class Bridge:
            store = Store()

        server = McpStdioServer()
        server.bridge = Bridge()
        messages = []
        server.send = messages.append
        server.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "flax_connection_status", "arguments": {}},
            }
        )
        result = messages[0]["result"]
        self.assertEqual(result["content"][0]["type"], "text")
        self.assertEqual(
            json.loads(result["content"][0]["text"]),
            {"connected": True, "siteUrl": "https://example.com"},
        )

    def test_successful_connect_returns_tool_result_before_discovery_notification(self):
        class Bridge:
            def connect(self, site_url):
                return {"siteUrl": site_url, "message": "connected"}

        server = McpStdioServer()
        server.bridge = Bridge()
        messages = []
        server.send = messages.append
        server.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "flax_connect",
                    "arguments": {"siteUrl": "https://example.com"},
                },
            }
        )
        self.assertEqual(messages[0]["result"]["content"][0]["type"], "text")
        self.assertEqual(messages[1]["method"], "notifications/tools/list_changed")

    def test_authenticated_tool_discovery_exposes_remote_tools(self):
        class Bridge:
            def connected(self):
                return True

            def rpc(self, method):
                self.method = method
                return {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {
                        "tools": [
                            {"name": "flax_get_site_model", "inputSchema": {}},
                            {"name": "flax_publish_change", "inputSchema": {}},
                        ]
                    },
                }

        server = McpStdioServer()
        server.bridge = Bridge()
        names = [tool["name"] for tool in server.tools()]
        self.assertIn("flax_get_site_model", names)
        self.assertNotIn("flax_publish_change", names)
        self.assertEqual(server.bridge.method, "tools/list")

    def test_remote_call_tool_result_is_preserved(self):
        class Bridge:
            def rpc(self, method, params):
                self.call = (method, params)
                return {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "result": {"content": [{"type": "text", "text": "ok"}]},
                }

        server = McpStdioServer()
        server.bridge = Bridge()
        messages = []
        server.send = messages.append
        server.handle(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "flax_get_site_model", "arguments": {}},
            }
        )
        self.assertEqual(
            messages[0]["result"],
            {"content": [{"type": "text", "text": "ok"}]},
        )
        self.assertEqual(server.bridge.call[0], "tools/call")


if __name__ == "__main__":
    unittest.main()
