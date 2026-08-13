#!/usr/bin/env python3
"""Local OAuth-capable MCP bridge for the Flax Sites Codex plugin.

The bridge speaks MCP over stdio and proxies calls to the exact site-scoped
Flax MCP endpoint discovered from the user's site origin. OAuth is completed
in the user's browser; tokens never enter the chat transcript.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from pathlib import Path
from typing import Any


PLUGIN_VERSION = "0.1.0"
TOKEN_SERVICE = "flax-sites-codex"
TOKEN_ACCOUNT = "default"
TOKEN_FILE = Path.home() / ".config" / "flax-sites" / "tokens.json"
CALLBACK_TIMEOUT_SECONDS = 300
BLOCKED_REMOTE_TOOLS = {"flax_publish_change"}


class BridgeError(RuntimeError):
    pass


def eprint(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def normalize_origin(site_url: str) -> str:
    parsed = urllib.parse.urlparse(site_url.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise BridgeError("Provide an exact site URL such as https://example.com")
    if parsed.username or parsed.password:
        raise BridgeError("Site URLs must not contain credentials")
    return f"{parsed.scheme}://{parsed.netloc}"


def json_request(
    url: str,
    method: str = "GET",
    body: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 30,
    follow_redirects: bool = True,
) -> tuple[int, dict[str, str], Any]:
    request_headers = {"Accept": "application/json", **(headers or {})}
    payload = None
    if body is not None:
        payload = json.dumps(body).encode("utf-8")
        request_headers.setdefault("Content-Type", "application/json")
    request = urllib.request.Request(
        url, data=payload, headers=request_headers, method=method
    )
    opener = urllib.request.build_opener()
    if not follow_redirects:
        opener = urllib.request.build_opener(NoRedirectHandler())
    try:
        with opener.open(request, timeout=timeout) as response:
            raw = response.read()
            return response.status, dict(response.headers.items()), parse_json(raw)
    except urllib.error.HTTPError as error:
        raw = error.read()
        return error.code, dict(error.headers.items()), parse_json(raw)
    except urllib.error.URLError as error:
        raise BridgeError(f"Could not reach {url}: {error.reason}") from error


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def form_request(
    url: str,
    values: dict[str, str],
    timeout: float = 30,
) -> tuple[int, dict[str, str], Any]:
    request = urllib.request.Request(
        url,
        data=urllib.parse.urlencode(values).encode("utf-8"),
        headers={
            "Accept": "application/json",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, dict(response.headers.items()), parse_json(response.read())
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers.items()), parse_json(error.read())
    except urllib.error.URLError as error:
        raise BridgeError(f"Could not reach {url}: {error.reason}") from error


def parse_json(raw: bytes) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return raw.decode("utf-8", errors="replace")


def metadata_candidates(issuer: str) -> list[str]:
    parsed = urllib.parse.urlparse(issuer.rstrip("/"))
    base = f"{parsed.scheme}://{parsed.netloc}"
    path = parsed.path.rstrip("/")
    candidates = [
        f"{issuer.rstrip('/')}/.well-known/oauth-authorization-server",
        f"{issuer.rstrip('/')}/.well-known/openid-configuration",
        f"{base}/.well-known/oauth-authorization-server{path}",
        f"{base}{path}/.well-known/oauth-authorization-server",
    ]
    return list(dict.fromkeys(candidates))


def pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


class TokenStore:
    """Prefer OS keychains and use a mode-600 file as a portable fallback."""

    def __init__(self, path: Path = TOKEN_FILE) -> None:
        self.path = path

    def _keychain_available(self) -> bool:
        return self.path == TOKEN_FILE and sys.platform == "darwin" and subprocess.run(
            ["which", "security"], capture_output=True, text=True
        ).returncode == 0

    def load(self) -> dict[str, Any] | None:
        if self._keychain_available():
            result = subprocess.run(
                [
                    "security",
                    "find-generic-password",
                    "-s",
                    TOKEN_SERVICE,
                    "-a",
                    TOKEN_ACCOUNT,
                    "-w",
                ],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0 and result.stdout.strip():
                try:
                    return json.loads(result.stdout)
                except json.JSONDecodeError:
                    pass
        if not self.path.exists():
            return None
        try:
            return json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            return None

    def save(self, value: dict[str, Any]) -> None:
        encoded = json.dumps(value, separators=(",", ":"))
        if self._keychain_available():
            result = subprocess.run(
                [
                    "security",
                    "add-generic-password",
                    "-U",
                    "-s",
                    TOKEN_SERVICE,
                    "-a",
                    TOKEN_ACCOUNT,
                    "-w",
                ],
                input=encoded,
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(value, indent=2) + "\n")
        os.chmod(self.path, 0o600)

    def clear(self) -> None:
        if self._keychain_available():
            subprocess.run(
                [
                    "security",
                    "delete-generic-password",
                    "-s",
                    TOKEN_SERVICE,
                    "-a",
                    TOKEN_ACCOUNT,
                ],
                capture_output=True,
            )
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass


class CallbackHandler(http.server.BaseHTTPRequestHandler):
    result: dict[str, str] | None = None
    event = threading.Event()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        CallbackHandler.result = dict(urllib.parse.parse_qsl(parsed.query))
        CallbackHandler.event.set()
        content = b"<h1>Flax connected</h1><p>You can return to Codex.</p>"
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def log_message(self, *_args: Any) -> None:
        return


def start_callback_server() -> tuple[http.server.ThreadingHTTPServer, str]:
    CallbackHandler.result = None
    CallbackHandler.event.clear()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), CallbackHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, f"http://127.0.0.1:{port}/oauth/callback"


class FlaxBridge:
    def __init__(self, store: TokenStore | None = None) -> None:
        self.store = store or TokenStore()
        self.session_id: str | None = None

    def connected(self) -> bool:
        token = self.store.load()
        return bool(token and token.get("access_token") and token.get("mcp_url"))

    def connect(self, site_url: str) -> dict[str, Any]:
        origin = normalize_origin(site_url)
        discovery_url = f"{origin}/.well-known/mcp.json"
        status, _, discovery = json_request(discovery_url, follow_redirects=False)
        if status != 200 or not isinstance(discovery, dict):
            raise BridgeError(f"Site discovery failed at {discovery_url}")
        mcp = discovery.get("mcp") or {}
        mcp_url = mcp.get("url")
        protected_url = mcp.get("protectedResourceMetadataUrl")
        if not isinstance(mcp_url, str) or not isinstance(protected_url, str):
            raise BridgeError("The site did not advertise a complete MCP connection")

        status, _, protected = json_request(protected_url)
        if status != 200 or not isinstance(protected, dict):
            raise BridgeError("Flax protected-resource metadata could not be read")
        issuers = protected.get("authorization_servers") or []
        if not issuers or not isinstance(issuers[0], str):
            raise BridgeError("Flax did not advertise an OAuth authorization server")

        authorization_metadata = None
        for candidate in metadata_candidates(issuers[0]):
            candidate_status, _, candidate_body = json_request(candidate)
            if candidate_status == 200 and isinstance(candidate_body, dict):
                authorization_metadata = candidate_body
                break
        if authorization_metadata is None:
            raise BridgeError("Flax OAuth authorization-server metadata was not found")

        server, redirect_uri = start_callback_server()
        try:
            scope_values = protected.get("scopes_supported") or ["openid", "email", "profile"]
            if "offline_access" in (authorization_metadata.get("scopes_supported") or []):
                scope_values = [*scope_values, "offline_access"]
            scope = " ".join(dict.fromkeys(scope_values))
            registration = {
                "client_name": "Flax Sites Codex plugin",
                "redirect_uris": [redirect_uri],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
                "scope": scope,
            }
            status, _, client = json_request(
                authorization_metadata["registration_endpoint"],
                method="POST",
                body=registration,
            )
            if status < 200 or status >= 300 or not isinstance(client, dict):
                raise BridgeError("Flax OAuth client registration failed")
            client_id = client.get("client_id")
            if not isinstance(client_id, str):
                raise BridgeError("Flax OAuth registration returned no client ID")

            verifier, challenge = pkce_pair()
            state = secrets.token_urlsafe(32)
            params = {
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": redirect_uri,
                "scope": scope,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "resource": mcp_url,
            }
            authorization_url = authorization_metadata["authorization_endpoint"] + "?" + urllib.parse.urlencode(params)
            eprint("Opening Flax authorization in your browser…")
            if not webbrowser.open(authorization_url):
                eprint(f"Open this one-time URL in your browser: {authorization_url}")
            if not CallbackHandler.event.wait(CALLBACK_TIMEOUT_SECONDS):
                raise BridgeError("Timed out waiting for the Flax OAuth callback")
            callback = CallbackHandler.result or {}
            if callback.get("state") != state:
                raise BridgeError("Flax OAuth state verification failed")
            if callback.get("error"):
                raise BridgeError(callback.get("error_description") or callback["error"])
            code = callback.get("code")
            if not code:
                raise BridgeError("Flax OAuth callback did not contain an authorization code")

            status, _, token = form_request(
                authorization_metadata["token_endpoint"],
                {
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": redirect_uri,
                    "client_id": client_id,
                    "code_verifier": verifier,
                },
            )
            if status < 200 or status >= 300 or not isinstance(token, dict) or not token.get("access_token"):
                raise BridgeError("Flax OAuth token exchange failed")
            token.update(
                {
                    "mcp_url": mcp_url,
                    "site_url": origin,
                    "client_id": client_id,
                    "token_endpoint": authorization_metadata["token_endpoint"],
                    "expires_at": time.time() + int(token.get("expires_in", 3600)),
                }
            )
            self.store.save(token)
            self.session_id = None
            return {"siteUrl": origin, "message": "Flax is connected for this site."}
        finally:
            server.shutdown()

    def refresh(self, token: dict[str, Any]) -> dict[str, Any]:
        refresh_token = token.get("refresh_token")
        if not refresh_token:
            raise BridgeError("The Flax connection expired; call flax_connect again")
        status, _, refreshed = form_request(
            token["token_endpoint"],
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": token["client_id"],
            },
        )
        if status < 200 or status >= 300 or not isinstance(refreshed, dict) or not refreshed.get("access_token"):
            raise BridgeError("Flax token refresh failed; call flax_connect again")
        merged = {**token, **refreshed, "expires_at": time.time() + int(refreshed.get("expires_in", 3600))}
        self.store.save(merged)
        return merged

    def rpc(self, method: str, params: dict[str, Any] | None = None) -> Any:
        token = self.store.load()
        if not token or not token.get("access_token"):
            raise BridgeError("Connect Flax first with flax_connect")
        if token.get("expires_at", 0) < time.time() + 30:
            token = self.refresh(token)
        headers = {"Authorization": f"Bearer {token['access_token']}", "MCP-Protocol-Version": "2025-06-18"}
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id
        request_id = secrets.randbelow(2**31)
        status, response_headers, body = json_request(
            token["mcp_url"],
            method="POST",
            body={"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}},
            headers=headers,
        )
        if status == 401:
            token = self.refresh(token)
            headers["Authorization"] = f"Bearer {token['access_token']}"
            status, response_headers, body = json_request(
                token["mcp_url"], method="POST",
                body={"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}},
                headers=headers,
            )
        if status < 200 or status >= 300:
            raise BridgeError(f"Flax MCP request failed with HTTP {status}")
        if response_headers.get("Mcp-Session-Id"):
            self.session_id = response_headers["Mcp-Session-Id"]
        return body


LOCAL_TOOLS = [
    {
        "name": "flax_connect",
        "description": "Connect Codex to exactly one Flax site using a browser OAuth flow. Never request credentials or tokens in chat.",
        "inputSchema": {"type": "object", "properties": {"siteUrl": {"type": "string", "description": "Exact site origin, for example https://example.com"}}, "required": ["siteUrl"]},
    },
    {
        "name": "flax_connection_status",
        "description": "Show whether the local Flax OAuth connection is available and which site it is bound to.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "flax_disconnect",
        "description": "Remove the local Flax OAuth connection. This does not revoke access in Flax.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


class McpStdioServer:
    def __init__(self) -> None:
        self.bridge = FlaxBridge()
        self.initialized = False

    def send(self, message: dict[str, Any]) -> None:
        encoded = json.dumps(message, separators=(",", ":"))
        sys.stdout.write(encoded + "\n")
        sys.stdout.flush()

    def tools(self) -> list[dict[str, Any]]:
        tools = list(LOCAL_TOOLS)
        if self.bridge.connected():
            try:
                remote = self.bridge.rpc("tools/list")
                tools.extend(
                    tool
                    for tool in ((remote.get("result") or {}).get("tools") or [])
                    if tool.get("name") not in BLOCKED_REMOTE_TOOLS
                )
            except BridgeError as error:
                eprint(str(error))
        return tools

    def handle(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        request_id = message.get("id")
        if method == "notifications/initialized":
            return
        if method == "initialize":
            self.initialized = True
            self.send({"jsonrpc": "2.0", "id": request_id, "result": {"protocolVersion": "2025-06-18", "capabilities": {"tools": {"listChanged": True}}, "serverInfo": {"name": "flax-sites", "version": PLUGIN_VERSION}}})
            return
        if method == "tools/list":
            self.send({"jsonrpc": "2.0", "id": request_id, "result": {"tools": self.tools()}})
            return
        if method == "tools/call":
            params = message.get("params") or {}
            name = params.get("name")
            arguments = params.get("arguments") or {}
            try:
                if name == "flax_connect":
                    result = self.bridge.connect(arguments.get("siteUrl", ""))
                elif name == "flax_connection_status":
                    token = self.bridge.store.load() or {}
                    result = {"connected": bool(token.get("access_token")), "siteUrl": token.get("site_url")}
                elif name == "flax_disconnect":
                    self.bridge.store.clear()
                    self.bridge.session_id = None
                    result = {"message": "The local Flax connection was removed. Revoke it in Flax if needed."}
                elif name in BLOCKED_REMOTE_TOOLS:
                    raise BridgeError("Publishing is disabled in the local Flax plugin")
                else:
                    result = self.bridge.rpc("tools/call", {"name": name, "arguments": arguments})
                if isinstance(result, dict) and "error" in result:
                    self.send({"jsonrpc": "2.0", "id": request_id, "error": result["error"]})
                else:
                    self.send({"jsonrpc": "2.0", "id": request_id, "result": result.get("result", result) if isinstance(result, dict) else result})
                if name == "flax_connect":
                    self.send({"jsonrpc": "2.0", "method": "notifications/tools/list_changed", "params": {}})
            except BridgeError as error:
                self.send({"jsonrpc": "2.0", "id": request_id, "result": {"isError": True, "content": [{"type": "text", "text": str(error)}]}})
            return
        if request_id is not None:
            self.send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": f"Unsupported MCP method: {method}"}})

    def run(self) -> None:
        for line in sys.stdin:
            if not line.strip():
                continue
            try:
                self.handle(json.loads(line))
            except (json.JSONDecodeError, TypeError) as error:
                eprint(f"Invalid MCP message: {error}")


if __name__ == "__main__":
    McpStdioServer().run()
