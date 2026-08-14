import json
import subprocess
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

from flax_mcp import (
    BridgeError,
    FlaxBridge,
    McpStdioServer,
    TokenStore,
    merge_query_params,
    metadata_candidates,
    normalize_audit_path,
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
        self.assertIn("flax_audit_public_site", tool_names)
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

    def test_keychain_write_passes_json_as_the_password_and_verifies_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tokens.json"
            store = TokenStore(path)
            value = {
                "version": 2,
                "access_token": "test",
                "site_url": "https://site.example",
            }
            add_result = subprocess.CompletedProcess(
                ["security"], 0, "", ""
            )
            invalid_find_result = subprocess.CompletedProcess(
                ["security"], 0, "\n", ""
            )
            with mock.patch.object(store, "_keychain_available", return_value=True):
                with mock.patch(
                    "flax_mcp.subprocess.run",
                    side_effect=[add_result, invalid_find_result],
                ) as run:
                    store.save(value)

            add_call = run.call_args_list[0]
            self.assertEqual(add_call.args[0][-2:], ["-w", json.dumps(value, separators=(",", ":"))])
            self.assertNotIn("input", add_call.kwargs)
            self.assertEqual(store.load(), value)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_proxy_tools_are_declared_before_authentication(self):
        class Bridge:
            def connected(self):
                return False

        server = McpStdioServer()
        server.bridge = Bridge()
        names = [tool["name"] for tool in server.tools()]
        self.assertIn("flax_connect", names)
        self.assertIn("flax_audit_public_site", names)
        self.assertIn("flax_get_site_model", names)
        self.assertIn("flax_get_model_hash", names)

    def test_audit_paths_must_stay_on_connected_origin(self):
        self.assertEqual(normalize_audit_path("/services?area=salford"), "/services?area=salford")
        for path in ["https://evil.example/", "//evil.example/", "services", "/#part"]:
            with self.subTest(path=path):
                with self.assertRaises(BridgeError):
                    normalize_audit_path(path)

    def test_public_site_audit_extracts_structured_technical_data(self):
        class Store:
            def load(self):
                return {
                    "access_token": "test",
                    "site_url": "https://site.example",
                }

        html = """<!doctype html><html><head>
        <title>  Example Site </title>
        <meta content="Example description" name="description">
        <meta content="index,follow" name="robots">
        <link href="https://site.example/services" rel="canonical">
        <script type="application/ld+json">{"@graph":[{"@type":"LocalBusiness"},{"@type":["Service","Thing"]}]}</script>
        </head><body><h1>Artificial <span>Grass</span></h1></body></html>"""
        responses = {
            "https://site.example/services": (
                200,
                {"Content-Type": "text/html; charset=utf-8"},
                html,
            ),
            "https://site.example/robots.txt": (
                200,
                {"Content-Type": "text/plain"},
                "User-agent: *\nSitemap: https://site.example/sitemap.xml\n",
            ),
            "https://site.example/sitemap.xml": (
                200,
                {"Content-Type": "application/xml"},
                "<urlset xmlns=\"http://www.sitemaps.org/schemas/sitemap/0.9\"><url><loc>https://site.example/</loc></url><url><loc>https://site.example/services</loc></url></urlset>",
            ),
        }
        bridge = FlaxBridge(Store())
        with mock.patch(
            "flax_mcp.public_text_request", side_effect=lambda url: responses[url]
        ):
            result = bridge.audit_public_site(["/services"])

        page = result["pages"][0]
        self.assertEqual(result["siteUrl"], "https://site.example")
        self.assertEqual(page["title"], "Example Site")
        self.assertEqual(page["description"], "Example description")
        self.assertEqual(page["canonical"], "https://site.example/services")
        self.assertEqual(page["robots"], "index,follow")
        self.assertEqual(page["h1"], ["Artificial Grass"])
        self.assertEqual(page["schemaTypes"], ["LocalBusiness", "Service", "Thing"])
        self.assertIn("User-agent: *", result["indexing"]["robots"]["text"])
        self.assertEqual(result["indexing"]["sitemap"]["urlCount"], 2)

    def test_public_site_audit_requires_an_authenticated_site(self):
        class Store:
            def load(self):
                return None

        with self.assertRaises(BridgeError):
            FlaxBridge(Store()).audit_public_site(["/"])

    def test_public_site_audit_tool_returns_call_tool_result(self):
        class Bridge:
            def audit_public_site(self, paths, include_indexing_files, site_url):
                self.call = (paths, include_indexing_files, site_url)
                return {"siteUrl": "https://site.example", "pages": []}

        server = McpStdioServer()
        server.bridge = Bridge()
        messages = []
        server.send = messages.append
        server.handle(
            {
                "jsonrpc": "2.0",
                "id": 8,
                "method": "tools/call",
                "params": {
                    "name": "flax_audit_public_site",
                    "arguments": {
                        "paths": ["/services"],
                        "includeIndexingFiles": False,
                    },
                },
            }
        )
        payload = json.loads(messages[0]["result"]["content"][0]["text"])
        self.assertEqual(payload["siteUrl"], "https://site.example")
        self.assertEqual(server.bridge.call, (["/services"], False, None))

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

    def test_connect_fails_fast_when_browser_cannot_open(self):
        discovery = {
            "mcp": {
                "url": "https://agents.example/site-mcp/site-1/mcp",
                "protectedResourceMetadataUrl": "https://agents.example/site-mcp/site-1/protected",
            }
        }
        protected = {
            "authorization_servers": ["https://agents.example/site-mcp/site-1/oauth"],
            "scopes_supported": ["openid"],
        }
        authorization_metadata = {
            "registration_endpoint": "https://agents.example/site-mcp/site-1/oauth/register",
            "authorization_endpoint": "https://flaxsites.com/oauth/site-context",
            "token_endpoint": "https://agents.example/oauth/token",
            "scopes_supported": ["openid"],
        }

        class Server:
            def shutdown(self):
                return None

        with tempfile.TemporaryDirectory() as directory:
            store = TokenStore(Path(directory) / "tokens.json")
            with mock.patch(
                "flax_mcp.json_request",
                side_effect=[
                    (200, {}, discovery),
                    (200, {}, protected),
                    (200, {}, authorization_metadata),
                    (201, {}, {"client_id": "client-1"}),
                ],
            ):
                with mock.patch(
                    "flax_mcp.start_callback_server",
                    return_value=(Server(), "http://127.0.0.1:1234/oauth/callback"),
                ):
                    with mock.patch("flax_mcp.webbrowser.open", return_value=False):
                        with self.assertRaisesRegex(
                            BridgeError, "browser use is unavailable"
                        ):
                            FlaxBridge(store).connect("https://site.example")

    def test_connection_persistence_is_verified_before_success(self):
        class Store:
            def load(self):
                return None

            def save(self, _value):
                return None

        token = {
            "access_token": "test",
            "mcp_url": "https://agents.example/site-mcp/site-1/mcp",
            "site_url": "https://site.example",
        }
        with self.assertRaisesRegex(BridgeError, "could not be persisted"):
            FlaxBridge(Store())._save_connections(
                {"https://site.example": token}, "https://site.example"
            )

    def test_registration_failure_reports_http_status_and_provider_detail(self):
        discovery = {
            "mcp": {
                "url": "https://agents.example/site-mcp/site-1/mcp",
                "protectedResourceMetadataUrl": "https://agents.example/site-mcp/site-1/protected",
            }
        }
        protected = {
            "authorization_servers": ["https://agents.example/site-mcp/site-1/oauth"],
            "scopes_supported": ["openid"],
        }
        authorization_metadata = {
            "registration_endpoint": "https://agents.example/site-mcp/site-1/oauth/register",
            "scopes_supported": ["openid"],
        }

        class Server:
            def shutdown(self):
                return None

        with tempfile.TemporaryDirectory() as directory:
            store = TokenStore(Path(directory) / "tokens.json")
            with mock.patch(
                "flax_mcp.json_request",
                side_effect=[
                    (200, {}, discovery),
                    (200, {}, protected),
                    (200, {}, authorization_metadata),
                    (
                        400,
                        {},
                        {
                            "error": "invalid_client_metadata",
                            "error_description": "redirect URI is not allowed",
                        },
                    ),
                ],
            ):
                with mock.patch(
                    "flax_mcp.start_callback_server",
                    return_value=(Server(), "http://127.0.0.1:1234/oauth/callback"),
                ):
                    with self.assertRaisesRegex(
                        BridgeError,
                        r"registration failed \(HTTP 400\): redirect URI is not allowed",
                    ):
                        FlaxBridge(store).connect("https://site.example")

    def test_multiple_site_connections_are_retained_and_selectable(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TokenStore(Path(directory) / "tokens.json")
            store.save(
                {
                    "version": 2,
                    "active_site_url": "https://first.example",
                    "connections": {
                        "https://first.example": {
                            "access_token": "first",
                            "expires_at": 4102444800,
                            "mcp_url": "https://agents.example/site-mcp/first/mcp",
                            "site_url": "https://first.example",
                        },
                        "https://second.example": {
                            "access_token": "second",
                            "expires_at": 4102444800,
                            "mcp_url": "https://agents.example/site-mcp/second/mcp",
                            "site_url": "https://second.example",
                        },
                    },
                }
            )

            bridge = FlaxBridge(store)
            result = bridge.connect("https://second.example/page")

            self.assertIn("already connected", result["message"])
            status = bridge.connection_status()
            self.assertEqual(status["siteUrl"], "https://second.example")
            self.assertEqual(
                status["connectedSites"],
                ["https://first.example", "https://second.example"],
            )
            disconnected = bridge.disconnect("https://first.example")
            self.assertEqual(disconnected["siteUrl"], "https://first.example")
            self.assertEqual(disconnected["connectedSites"], ["https://second.example"])

    def test_legacy_single_connection_is_migrated_when_selected(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TokenStore(Path(directory) / "tokens.json")
            store.save(
                {
                    "access_token": "legacy",
                    "expires_at": 4102444800,
                    "mcp_url": "https://agents.example/site-mcp/legacy/mcp",
                    "site_url": "https://legacy.example",
                }
            )

            FlaxBridge(store).connect("https://legacy.example")
            migrated = store.load()
            self.assertEqual(migrated["active_site_url"], "https://legacy.example")
            self.assertIn("https://legacy.example", migrated["connections"])

    def test_rpc_uses_the_selected_site_connection(self):
        with tempfile.TemporaryDirectory() as directory:
            store = TokenStore(Path(directory) / "tokens.json")
            store.save(
                {
                    "version": 2,
                    "active_site_url": "https://second.example",
                    "connections": {
                        "https://first.example": {
                            "access_token": "first",
                            "expires_at": 4102444800,
                            "mcp_url": "https://agents.example/site-mcp/first/mcp",
                            "site_url": "https://first.example",
                        },
                        "https://second.example": {
                            "access_token": "second",
                            "expires_at": 4102444800,
                            "mcp_url": "https://agents.example/site-mcp/second/mcp",
                            "site_url": "https://second.example",
                        },
                    },
                }
            )
            bridge = FlaxBridge(store)
            with mock.patch(
                "flax_mcp.json_request",
                return_value=(200, {}, {"jsonrpc": "2.0", "result": {"tools": []}}),
            ) as request:
                bridge.rpc("tools/list")

            self.assertEqual(
                request.call_args.args[0],
                "https://agents.example/site-mcp/second/mcp",
            )
            self.assertEqual(
                request.call_args.kwargs["headers"]["Authorization"],
                "Bearer second",
            )

    def test_local_tool_call_returns_mcp_call_tool_result(self):
        class Store:
            def load(self):
                return {"access_token": "test", "site_url": "https://example.com"}

        class Bridge:
            store = Store()

            def connection_status(self):
                return {
                    "connected": True,
                    "siteUrl": "https://example.com",
                    "connectedSites": ["https://example.com"],
                }

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
            {
                "connected": True,
                "siteUrl": "https://example.com",
                "connectedSites": ["https://example.com"],
            },
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
