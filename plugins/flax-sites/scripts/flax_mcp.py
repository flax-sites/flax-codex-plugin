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
import socket
import subprocess
import sys
import threading
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path
from typing import Any


PLUGIN_VERSION = "0.1.0"
TOKEN_SERVICE = "flax-sites-codex"
TOKEN_ACCOUNT = "default"
TOKEN_FILE = Path.home() / ".config" / "flax-sites" / "tokens.json"
CALLBACK_TIMEOUT_SECONDS = 60
KEYCHAIN_TIMEOUT_SECONDS = 5
MCP_REQUEST_TIMEOUT_SECONDS = 60
TOKEN_STORAGE_VERSION = 2
MAX_AUDIT_PATHS = 12
MAX_PUBLIC_RESPONSE_BYTES = 1024 * 1024
MAX_SITEMAP_URLS = 500
BLOCKED_REMOTE_TOOLS = {"flax_publish_change", "flax.drafts.publish_change"}
PUBLIC_MCP_URL = "https://agents.flaxsites.com/mcp"
PUBLIC_TOOL_NAMES = {"flax.sites.create"}
REMOVED_ONBOARDING_TOOLS = {"flax.account.start_signup", "flax.sites.browse_templates", "flax_start_site_creation"}
PUBLIC_RESOURCE_URIS = {"ui://flax/site-creation"}

# Keep the stable local proxy names that Codex snapshots before OAuth, but use
# the namespaced identifiers exposed by the site MCP server on the wire.
REMOTE_TOOL_NAMES = {
    "flax_list_sites": "flax.sites.list",
    "flax_get_site_model": "flax.site.get_model",
    "flax_get_site_insights": "flax.analytics.get_insights",
    "flax_get_search_performance": "flax.analytics.get_search_performance",
    "flax_upsert_article": "flax.content.upsert_article",
    "flax_validate_model_update": "flax.drafts.validate_model_update",
    "flax_propose_model_update": "flax.drafts.propose_model_update",
    "flax_begin_image_upload": "flax.media.begin_upload",
    "flax_get_change": "flax.drafts.get_change",
}


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


def merge_query_params(url: str, params: dict[str, str]) -> str:
    parsed = urllib.parse.urlsplit(url)
    merged = dict(urllib.parse.parse_qsl(parsed.query, keep_blank_values=True))
    merged.update(params)
    return urllib.parse.urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            urllib.parse.urlencode(merged),
            parsed.fragment,
        )
    )


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
    except (TimeoutError, socket.timeout) as error:
        raise BridgeError(
            f"Could not reach {url}: request timed out after {timeout:g}s"
        ) from error
    except urllib.error.URLError as error:
        raise BridgeError(f"Could not reach {url}: {error.reason}") from error
    except OSError as error:
        reason = str(error).strip() or "the connection was closed"
        raise BridgeError(f"Could not reach {url}: {reason}") from error


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


def public_text_request(
    url: str, timeout: float = 20
) -> tuple[int, dict[str, str], str]:
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "text/html,application/xhtml+xml,application/xml,text/xml,text/plain",
            "User-Agent": "Flax-Codex-Site-Audit/1.0",
        },
        method="GET",
    )
    opener = urllib.request.build_opener(NoRedirectHandler())

    def read_response(response: Any) -> tuple[int, dict[str, str], str]:
        raw = response.read(MAX_PUBLIC_RESPONSE_BYTES + 1)
        if len(raw) > MAX_PUBLIC_RESPONSE_BYTES:
            raise BridgeError(f"Public page response exceeded {MAX_PUBLIC_RESPONSE_BYTES} bytes")
        headers = dict(response.headers.items())
        return response.status, headers, raw.decode("utf-8", errors="replace")

    try:
        with opener.open(request, timeout=timeout) as response:
            return read_response(response)
    except urllib.error.HTTPError as error:
        return read_response(error)
    except urllib.error.URLError as error:
        raise BridgeError(f"Could not reach {url}: {error.reason}") from error


class PageMetadataParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.h1_values: list[str] = []
        self.description: str | None = None
        self.canonical: str | None = None
        self.robots: str | None = None
        self.json_ld_values: list[str] = []
        self._in_title = False
        self._h1_parts: list[str] | None = None
        self._json_ld_parts: list[str] | None = None

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        attributes = {name.lower(): value or "" for name, value in attrs}
        lowered = tag.lower()
        if lowered == "title":
            self._in_title = True
        elif lowered == "h1":
            self._h1_parts = []
        elif lowered == "meta":
            name = attributes.get("name", "").lower()
            if name == "description" and self.description is None:
                self.description = attributes.get("content") or None
            elif name == "robots" and self.robots is None:
                self.robots = attributes.get("content") or None
        elif lowered == "link":
            rel = attributes.get("rel", "").lower().split()
            if "canonical" in rel and self.canonical is None:
                self.canonical = attributes.get("href") or None
        elif (
            lowered == "script"
            and attributes.get("type", "").lower() == "application/ld+json"
        ):
            self._json_ld_parts = []

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.lower()
        if lowered == "title":
            self._in_title = False
        elif lowered == "h1" and self._h1_parts is not None:
            value = " ".join("".join(self._h1_parts).split())
            if value:
                self.h1_values.append(value)
            self._h1_parts = None
        elif lowered == "script" and self._json_ld_parts is not None:
            value = "".join(self._json_ld_parts).strip()
            if value:
                self.json_ld_values.append(value)
            self._json_ld_parts = None

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_parts.append(data)
        if self._h1_parts is not None:
            self._h1_parts.append(data)
        if self._json_ld_parts is not None:
            self._json_ld_parts.append(data)


def page_metadata(html: str) -> dict[str, Any]:
    parser = PageMetadataParser()
    parser.feed(html)
    schema_types: set[str] = set()

    def collect_schema_types(value: Any) -> None:
        if isinstance(value, dict):
            type_value = value.get("@type")
            if isinstance(type_value, str):
                schema_types.add(type_value)
            elif isinstance(type_value, list):
                schema_types.update(item for item in type_value if isinstance(item, str))
            for child in value.values():
                collect_schema_types(child)
        elif isinstance(value, list):
            for child in value:
                collect_schema_types(child)

    for raw_json_ld in parser.json_ld_values:
        try:
            collect_schema_types(json.loads(raw_json_ld))
        except json.JSONDecodeError:
            continue

    title = " ".join("".join(parser.title_parts).split()) or None
    return {
        "title": title,
        "description": parser.description,
        "canonical": parser.canonical,
        "robots": parser.robots,
        "h1Count": len(parser.h1_values),
        "h1": parser.h1_values,
        "schemaTypes": sorted(schema_types),
    }


def sitemap_summary(xml_text: str) -> dict[str, Any]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as error:
        return {"urlCount": 0, "urls": [], "parseError": str(error)}
    urls = [
        (element.text or "").strip()
        for element in root.iter()
        if element.tag.rsplit("}", 1)[-1] == "loc" and (element.text or "").strip()
    ]
    return {
        "urlCount": len(urls),
        "urls": urls[:MAX_SITEMAP_URLS],
        "truncated": len(urls) > MAX_SITEMAP_URLS,
    }


def normalize_audit_path(path: str) -> str:
    if not isinstance(path, str) or not path.strip():
        raise BridgeError("Every audit path must be a non-empty relative site path")
    value = path.strip()
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme
        or parsed.netloc
        or not parsed.path.startswith("/")
        or value.startswith("//")
        or parsed.fragment
    ):
        raise BridgeError("Audit paths must be relative paths on the connected site")
    return urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))


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


def as_tool_result(value: Any) -> dict[str, Any]:
    """Return a valid MCP CallToolResult for local and proxied tool values."""
    if isinstance(value, dict) and isinstance(value.get("content"), list):
        return value
    return {
        "content": [
            {
                "type": "text",
                "text": json.dumps(value, separators=(",", ":"), ensure_ascii=False),
            }
        ]
    }


def public_rpc(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    """Access only anonymous onboarding; never send a site's authorization."""
    params = params or {}
    allowed = method in {"tools/list", "resources/list"}
    allowed |= method == "tools/call" and params.get("name") in PUBLIC_TOOL_NAMES
    allowed |= method == "resources/read" and params.get("uri") in PUBLIC_RESOURCE_URIS
    if not allowed:
        raise BridgeError("This request requires a site-scoped connection")
    for attempt in range(2):
        try:
            status, _, body = json_request(
                PUBLIC_MCP_URL, "POST",
                {"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                timeout=15, follow_redirects=False,
            )
            break
        except BridgeError as error:
            # A TLS handshake failure occurs before the HTTP request is sent.
            if attempt or "EOF occurred in violation of protocol" not in str(error):
                raise
    if not 200 <= status < 300 or not isinstance(body, dict):
        raise BridgeError("Flax anonymous onboarding is unavailable")
    if isinstance(body.get("error"), dict):
        raise BridgeError(str(body["error"].get("message", "Onboarding request failed")))
    return body


def remote_tool_name(name: str) -> str:
    return REMOTE_TOOL_NAMES.get(name, name)


def project_model_hash_result(value: Any) -> Any:
    """Project the public model-read response onto the legacy hash proxy."""
    if not isinstance(value, dict):
        return value
    result = value.get("result")
    if not isinstance(result, dict) or result.get("isError"):
        return value
    structured = result.get("structuredContent")
    digest = structured.get("hash") if isinstance(structured, dict) else None
    if not isinstance(digest, str) or not digest:
        return value
    return {
        **value,
        "result": {
            "content": [
                {
                    "type": "text",
                    "text": json.dumps(
                        {"hash": digest}, separators=(",", ":")
                    ),
                }
            ],
            "structuredContent": {"hash": digest},
        },
    }


class TokenStore:
    """Prefer OS keychains and use a mode-600 file as a portable fallback."""

    def __init__(self, path: Path = TOKEN_FILE) -> None:
        self.path = path

    def _keychain_available(self) -> bool:
        if self.path != TOKEN_FILE or sys.platform != "darwin":
            return False
        try:
            return subprocess.run(
                ["which", "security"],
                capture_output=True,
                text=True,
                timeout=KEYCHAIN_TIMEOUT_SECONDS,
            ).returncode == 0
        except subprocess.TimeoutExpired:
            return False

    def _keychain_value(self) -> dict[str, Any] | None:
        if not self._keychain_available():
            return None
        try:
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
                timeout=KEYCHAIN_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            return None
        if result.returncode != 0 or not result.stdout.strip():
            return None
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError:
            return None
        return value if isinstance(value, dict) else None

    def load(self) -> dict[str, Any] | None:
        # A file is only written when Keychain is unavailable or its round-trip
        # verification fails. Prefer it so a stale/invalid Keychain item cannot
        # mask a successfully persisted fallback connection.
        if not self.path.exists():
            return self._keychain_value()
        try:
            file_value = json.loads(self.path.read_text())
        except (OSError, json.JSONDecodeError):
            file_value = None
        if isinstance(file_value, dict):
            return file_value
        return self._keychain_value()

    def save(self, value: dict[str, Any]) -> None:
        encoded = json.dumps(value, separators=(",", ":"))
        if self._keychain_available():
            try:
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
                        encoded,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=KEYCHAIN_TIMEOUT_SECONDS,
                )
            except subprocess.TimeoutExpired:
                result = None
            if result and result.returncode == 0 and self._keychain_value() == value:
                try:
                    self.path.unlink()
                except FileNotFoundError:
                    pass
                return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: str | None = None
        try:
            file_descriptor, temporary_path = tempfile.mkstemp(
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                dir=self.path.parent,
                text=True,
            )
            with os.fdopen(file_descriptor, "w", encoding="utf-8") as temporary:
                temporary.write(json.dumps(value, indent=2) + "\n")
                temporary.flush()
                os.fsync(temporary.fileno())
            os.chmod(temporary_path, 0o600)
            os.replace(temporary_path, self.path)
        except OSError:
            if temporary_path:
                try:
                    os.unlink(temporary_path)
                except FileNotFoundError:
                    pass
            raise

    def clear(self) -> None:
        if self._keychain_available():
            try:
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
                    timeout=KEYCHAIN_TIMEOUT_SECONDS,
                )
            except subprocess.TimeoutExpired:
                pass
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

    def _connections(self) -> tuple[dict[str, dict[str, Any]], str | None]:
        """Load the site-keyed store, migrating the former single-token shape."""
        stored = self.store.load() or {}
        candidates: list[tuple[Any, Any]] = []
        if isinstance(stored, dict) and isinstance(stored.get("connections"), dict):
            candidates = list(stored["connections"].items())
        elif isinstance(stored, dict) and stored.get("site_url"):
            candidates = [(stored.get("site_url"), stored)]

        connections: dict[str, dict[str, Any]] = {}
        for key, candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            site_value = candidate.get("site_url") or key
            if not isinstance(site_value, str):
                continue
            try:
                origin = normalize_origin(site_value)
            except BridgeError:
                continue
            token = dict(candidate)
            token["site_url"] = origin
            connections[origin] = token

        active_value = stored.get("active_site_url") if isinstance(stored, dict) else None
        if not isinstance(active_value, str) and isinstance(stored, dict):
            active_value = stored.get("site_url")
        active_site: str | None = None
        if isinstance(active_value, str):
            try:
                normalized_active = normalize_origin(active_value)
            except BridgeError:
                normalized_active = None
            if normalized_active in connections:
                active_site = normalized_active
        if active_site is None and connections:
            active_site = next(iter(connections))
        return connections, active_site

    def _save_connections(
        self,
        connections: dict[str, dict[str, Any]],
        active_site_url: str | None,
    ) -> None:
        active = active_site_url if active_site_url in connections else None
        try:
            self.store.save(
                {
                    "version": TOKEN_STORAGE_VERSION,
                    "active_site_url": active,
                    "connections": connections,
                }
            )
        except OSError as error:
            raise BridgeError(
                "Flax connection could not be persisted locally; check local credential storage and try again"
            ) from error
        if active is None:
            return
        persisted_connections, persisted_active = self._connections()
        expected = connections.get(active) or {}
        persisted = persisted_connections.get(active) or {}
        if (
            persisted_active != active
            or persisted.get("access_token") != expected.get("access_token")
            or persisted.get("mcp_url") != expected.get("mcp_url")
        ):
            raise BridgeError(
                "Flax connection could not be persisted locally; check local credential storage and try again"
            )

    def _active_token(self) -> dict[str, Any] | None:
        connections, active_site = self._connections()
        return connections.get(active_site) if active_site else None

    def _token_for_site(self, site_url: str | None = None) -> dict[str, Any]:
        connections, active_site = self._connections()
        target = normalize_origin(site_url) if site_url else active_site
        token = connections.get(target) if target else None
        if not token or not token.get("access_token") or not token.get("site_url"):
            raise BridgeError("Connect Flax first with flax_connect")
        return token

    def connection_status(self) -> dict[str, Any]:
        connections, active_site = self._connections()
        sites = sorted(
            site_url
            for site_url, token in connections.items()
            if token.get("mcp_url")
            and (token.get("access_token") or token.get("refresh_token"))
        )
        return {
            "connected": bool(active_site and active_site in sites),
            "siteUrl": active_site if active_site in sites else None,
            "connectedSites": sites,
        }

    def connected(self) -> bool:
        token = self._active_token()
        return bool(
            token
            and token.get("mcp_url")
            and (token.get("access_token") or token.get("refresh_token"))
        )

    def connect(self, site_url: str) -> dict[str, Any]:
        origin = normalize_origin(site_url)
        connections, _ = self._connections()
        existing = connections.get(origin) or {}
        same_site = existing.get("site_url") == origin
        has_current_access = bool(existing.get("access_token")) and existing.get(
            "expires_at", 0
        ) >= time.time() + 30
        can_refresh = bool(existing.get("refresh_token"))
        if same_site and existing.get("mcp_url") and (has_current_access or can_refresh):
            self._save_connections(connections, origin)
            self.session_id = None
            return {
                "siteUrl": origin,
                "message": "Flax is already connected for this site.",
            }
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
                detail = ""
                if isinstance(client, dict):
                    raw_detail = (
                        client.get("error_description")
                        or client.get("error")
                        or client.get("message")
                    )
                    if isinstance(raw_detail, str):
                        detail = " ".join(raw_detail.split())[:240]
                suffix = f": {detail}" if detail else ""
                raise BridgeError(
                    f"Flax OAuth client registration failed (HTTP {status}){suffix}"
                )
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
            authorization_url = merge_query_params(
                authorization_metadata["authorization_endpoint"], params
            )
            eprint("Opening Flax authorization in your browser…")
            try:
                browser_opened = webbrowser.open(authorization_url)
            except webbrowser.Error:
                browser_opened = False
            if not browser_opened:
                raise BridgeError(
                    "Flax OAuth requires browser access, but browser use is unavailable. Enable browser access and retry flax_connect."
                )
            if not CallbackHandler.event.wait(CALLBACK_TIMEOUT_SECONDS):
                raise BridgeError(
                    "Timed out waiting for the Flax OAuth callback. Browser access may be unavailable; retry flax_connect when it is enabled."
                )
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
            connections[origin] = token
            self._save_connections(connections, origin)
            self.session_id = None
            return {"siteUrl": origin, "message": "Flax is connected for this site."}
        finally:
            server.shutdown()

    def disconnect(self, site_url: str | None = None) -> dict[str, Any]:
        connections, active_site = self._connections()
        target = normalize_origin(site_url) if site_url else active_site
        if not target or target not in connections:
            raise BridgeError("No local Flax connection exists for that site")
        del connections[target]
        next_active = next(iter(sorted(connections)), None)
        if connections:
            self._save_connections(connections, next_active)
        else:
            self.store.clear()
        self.session_id = None
        return {
            "siteUrl": target,
            "connectedSites": sorted(connections),
            "message": "The local Flax connection was removed. Revoke it in Flax if needed.",
        }

    def audit_public_site(
        self,
        paths: Any = None,
        include_indexing_files: Any = True,
        site_url: str | None = None,
    ) -> dict[str, Any]:
        token = self._token_for_site(site_url)
        if paths is None:
            paths = ["/"]
        if not isinstance(paths, list) or not paths or len(paths) > MAX_AUDIT_PATHS:
            raise BridgeError(f"Provide between 1 and {MAX_AUDIT_PATHS} audit paths")
        if not isinstance(include_indexing_files, bool):
            raise BridgeError("includeIndexingFiles must be a boolean")

        origin = normalize_origin(token["site_url"])
        normalized_paths = [normalize_audit_path(path) for path in paths]

        def fetch(path: str) -> tuple[dict[str, Any], str]:
            status, headers, body = public_text_request(f"{origin}{path}")
            lowered_headers = {name.lower(): value for name, value in headers.items()}
            result = {
                "path": path,
                "status": status,
                "contentType": lowered_headers.get("content-type"),
                "contentCharacters": len(body),
            }
            if lowered_headers.get("location"):
                result["location"] = lowered_headers["location"]
            return result, body

        pages = []
        for path in normalized_paths:
            summary, body = fetch(path)
            content_type = str(summary.get("contentType") or "").lower()
            if "html" in content_type or (summary["status"] == 200 and body.lstrip().startswith("<")):
                summary.update(page_metadata(body))
            pages.append(summary)

        result: dict[str, Any] = {"siteUrl": origin, "pages": pages}
        if include_indexing_files:
            robots, robots_body = fetch("/robots.txt")
            robots["text"] = robots_body[:100_000]
            robots["truncated"] = len(robots_body) > 100_000
            sitemap, sitemap_body = fetch("/sitemap.xml")
            sitemap.update(sitemap_summary(sitemap_body))
            result["indexing"] = {"robots": robots, "sitemap": sitemap}
        return result

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
        connections, active_site = self._connections()
        origin = normalize_origin(merged["site_url"])
        connections[origin] = merged
        self._save_connections(connections, active_site or origin)
        return merged

    def rpc(self, method: str, params: dict[str, Any] | None = None) -> Any:
        token = self._active_token()
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
            timeout=MCP_REQUEST_TIMEOUT_SECONDS,
        )
        if status == 401:
            token = self.refresh(token)
            headers["Authorization"] = f"Bearer {token['access_token']}"
            status, response_headers, body = json_request(
                token["mcp_url"], method="POST",
                body={"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}},
                headers=headers,
                timeout=MCP_REQUEST_TIMEOUT_SECONDS,
            )
        if status < 200 or status >= 300:
            raise BridgeError(f"Flax MCP request failed with HTTP {status}")
        if not isinstance(body, dict):
            raise BridgeError("Flax MCP returned an unexpected response type")
        if response_headers.get("Mcp-Session-Id"):
            self.session_id = response_headers["Mcp-Session-Id"]
        return body


LOCAL_TOOLS = [
    {
        "name": "flax_connect",
        "description": "Connect Codex to an existing Flax site using browser OAuth, or select a site that is already authorized. This tool requires the site's exact URL and must not be used when the user wants to create a new site. Never request credentials or tokens in chat.",
        "inputSchema": {"type": "object", "properties": {"siteUrl": {"type": "string", "description": "Exact site origin, for example https://example.com"}}, "required": ["siteUrl"]},
    },
    {
        "name": "flax_connection_status",
        "description": "Show the active local Flax OAuth site and all sites authorized in this Codex client.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "flax_disconnect",
        "description": "Remove one local Flax OAuth connection. With no siteUrl, remove the active site. This does not revoke access in Flax.",
        "inputSchema": {"type": "object", "properties": {"siteUrl": {"type": "string", "description": "Optional exact site origin; defaults to the active site."}}, "additionalProperties": False},
    },
    {
        "name": "flax_audit_public_site",
        "description": "Audit public HTML metadata, status, robots.txt, and sitemap.xml only on the exact connected Flax site. Use this instead of a general web reader or shell commands for technical site checks.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "paths": {
                    "type": "array",
                    "items": {"type": "string", "pattern": "^/"},
                    "minItems": 1,
                    "maxItems": MAX_AUDIT_PATHS,
                    "default": ["/"],
                },
                "includeIndexingFiles": {"type": "boolean", "default": True},
                "siteUrl": {"type": "string", "description": "Optional authorized site origin; defaults to the active site."},
            },
            "additionalProperties": False,
        },
        "annotations": {
            "readOnlyHint": True,
            "openWorldHint": True,
            "destructiveHint": False,
        },
    },
]

# Codex snapshots MCP tool names when a task starts. Declare stable proxy tools
# before OAuth so the same task can use them immediately after flax_connect.
# Authenticated tools/list responses replace these fallback definitions below.
PROXIED_REMOTE_TOOLS = [
    {
        "name": "flax_list_sites",
        "description": "List the Flax site granted to this site-scoped connection.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "flax_get_site_model",
        "description": "Read the live SiteDataModel, schema URL, and model hash for this site.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "flax_get_model_hash",
        "description": "Read the current live SiteDataModel hash for this site.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "flax_get_site_insights",
        "description": "Read aggregate site insights when analytics access was granted.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "minimum": 1, "maximum": 90, "default": 28}
            },
        },
    },
    {
        "name": "flax_get_search_performance",
        "description": "Read bounded Google Search Console performance when analytics access was granted.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "days": {"type": "integer", "minimum": 1, "maximum": 90, "default": 28},
                "dimension": {"type": "string", "enum": ["query", "page"], "default": "query"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
            },
        },
    },
    {
        "name": "flax_upsert_article",
        "description": "Create or update an article and create a previewable Flax draft.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "sectionId": {"type": "string"},
                "articleId": {"type": "string"},
                "article": {"$ref": "https://flaxsites.com/schemas/flax/v1/article.schema.json"},
            },
            "required": ["sectionId", "article"],
        },
    },
    {
        "name": "flax_validate_model_update",
        "description": "Validate a model update against the current live model without creating a draft.",
        "inputSchema": {"$ref": "https://flaxsites.com/schemas/flax/v1/model-update-request.schema.json"},
    },
    {
        "name": "flax_propose_model_update",
        "description": "Validate a model update and create a previewable owner-approved Flax draft.",
        "inputSchema": {"$ref": "https://flaxsites.com/schemas/flax/v1/model-update-request.schema.json"},
    },
    {
        "name": "flax_upload_image",
        "description": "Upload a public HTTPS image URL to this site's image CDN.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "contentType": {"type": "string", "enum": ["image/jpeg", "image/png", "image/webp", "image/svg+xml"]},
                "sourceUrl": {"type": "string", "format": "uri"},
            },
            "required": ["name", "contentType", "sourceUrl"],
            "additionalProperties": False,
        },
    },
    {
        "name": "flax_begin_image_upload",
        "description": "Authorize one direct WebP upload for an image supplied by the agent.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string"},
                "totalBytes": {"type": "integer", "minimum": 1},
                "sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
            },
            "required": ["name", "totalBytes", "sha256"],
            "additionalProperties": False,
        },
    },
    {
        "name": "flax_get_change",
        "description": "Read an agent-created change and its preview state for this site.",
        "inputSchema": {
            "type": "object",
            "properties": {"changeId": {"type": "string"}},
            "required": ["changeId"],
        },
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
        tools_by_name = {
            tool["name"]: tool for tool in (
                [*LOCAL_TOOLS, *PROXIED_REMOTE_TOOLS] if self.bridge.connected()
                else [tool for tool in LOCAL_TOOLS if tool["name"] == "flax_connect"]
            )
        }
        public_tools = json.loads(Path(__file__).with_name("onboarding-tools.json").read_text())
        for tool in public_tools:
            if tool.get("name") in PUBLIC_TOOL_NAMES:
                tools_by_name[tool["name"]] = tool
        if self.bridge.connected():
            try:
                remote = self.bridge.rpc("tools/list")
                remote_result = remote.get("result")
                if not isinstance(remote_result, dict):
                    remote_error = remote.get("error")
                    if isinstance(remote_error, dict) and remote_error.get("message"):
                        raise BridgeError(str(remote_error["message"]))
                    raise BridgeError("Flax MCP tools/list returned an invalid response")
                remote_tools = remote_result.get("tools")
                if not isinstance(remote_tools, list):
                    raise BridgeError("Flax MCP tools/list returned no tool list")
                for tool in remote_tools:
                    if (
                        isinstance(tool, dict)
                        and isinstance(tool.get("name"), str)
                        and tool["name"] not in BLOCKED_REMOTE_TOOLS | REMOVED_ONBOARDING_TOOLS
                    ):
                        tools_by_name[tool["name"]] = tool
            except (BridgeError, TimeoutError, OSError) as error:
                eprint(str(error))
        return list(tools_by_name.values())

    def forward_remote_request(
        self,
        request_id: Any,
        method: str,
        params: dict[str, Any] | None = None,
    ) -> None:
        try:
            if (method == "resources/read" and (params or {}).get("uri") in PUBLIC_RESOURCE_URIS) or (method == "resources/list" and not self.bridge.connected()):
                remote = public_rpc(method, params)
            else:
                remote = self.bridge.rpc(method, params or {})
            if isinstance(remote, dict) and isinstance(remote.get("error"), dict):
                self.send({"jsonrpc": "2.0", "id": request_id, "error": remote["error"]})
                return
            result = remote.get("result", remote) if isinstance(remote, dict) else remote
            self.send({"jsonrpc": "2.0", "id": request_id, "result": result})
        except (BridgeError, TimeoutError, OSError) as error:
            self.send(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": -32000, "message": str(error)},
                }
            )

    def handle(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        request_id = message.get("id")
        if method == "notifications/initialized":
            return
        if method == "initialize":
            self.initialized = True
            self.send({"jsonrpc": "2.0", "id": request_id, "result": {"protocolVersion": "2025-06-18", "capabilities": {"tools": {"listChanged": True}, "resources": {}, "prompts": {}, "extensions": {"io.modelcontextprotocol/ui": {"mimeTypes": ["text/html;profile=mcp-app"]}}}, "serverInfo": {"name": "flax-sites", "version": PLUGIN_VERSION}}})
            return
        if method == "tools/list":
            self.send({"jsonrpc": "2.0", "id": request_id, "result": {"tools": self.tools()}})
            return
        if method == "resources/list":
            if not self.bridge.connected():
                resources = json.loads(Path(__file__).with_name("onboarding-resources.json").read_text())
                self.send({"jsonrpc": "2.0", "id": request_id, "result": {"resources": resources}})
            else:
                self.forward_remote_request(request_id, method, message.get("params"))
            return
        if method == "resources/read":
            self.forward_remote_request(request_id, method, message.get("params"))
            return
        if method == "resources/templates/list":
            self.send({"jsonrpc": "2.0", "id": request_id, "result": {"resourceTemplates": []}})
            return
        if method == "prompts/list":
            self.send({"jsonrpc": "2.0", "id": request_id, "result": {"prompts": []}})
            return
        if method == "ping":
            self.send({"jsonrpc": "2.0", "id": request_id, "result": {}})
            return
        if method == "tools/call":
            params = message.get("params") or {}
            name = params.get("name")
            arguments = params.get("arguments") or {}
            try:
                if name in REMOVED_ONBOARDING_TOOLS:
                    raise BridgeError("Use flax.sites.create for the combined new-site flow")
                elif name in PUBLIC_TOOL_NAMES:
                    result = public_rpc("tools/call", {"name": name, "arguments": arguments})
                elif name == "flax_connect":
                    result = self.bridge.connect(arguments.get("siteUrl", ""))
                elif name == "flax_connection_status":
                    result = self.bridge.connection_status()
                elif name == "flax_disconnect":
                    result = self.bridge.disconnect(arguments.get("siteUrl"))
                elif name == "flax_audit_public_site":
                    result = self.bridge.audit_public_site(
                        arguments.get("paths"),
                        arguments.get("includeIndexingFiles", True),
                        arguments.get("siteUrl"),
                    )
                elif name in BLOCKED_REMOTE_TOOLS:
                    raise BridgeError("Publishing is disabled in the local Flax plugin")
                else:
                    remote_name = remote_tool_name(name)
                    if name == "flax_get_model_hash":
                        remote_name = REMOTE_TOOL_NAMES["flax_get_site_model"]
                        arguments = {**arguments, "view": "compact"}
                    result = self.bridge.rpc(
                        "tools/call", {"name": remote_name, "arguments": arguments}
                    )
                    if name == "flax_get_model_hash":
                        result = project_model_hash_result(result)
                if isinstance(result, dict) and "error" in result:
                    self.send({"jsonrpc": "2.0", "id": request_id, "error": result["error"]})
                else:
                    value = result.get("result", result) if isinstance(result, dict) else result
                    self.send(
                        {
                            "jsonrpc": "2.0",
                            "id": request_id,
                            "result": as_tool_result(value),
                        }
                    )
                if name == "flax_connect":
                    self.send({"jsonrpc": "2.0", "method": "notifications/tools/list_changed", "params": {}})
            except (BridgeError, TimeoutError, OSError) as error:
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
