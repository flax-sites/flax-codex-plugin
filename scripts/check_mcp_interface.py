#!/usr/bin/env python3
"""Read-only check of the registered Flax MCP discovery/auth contract."""
import argparse
import json
import urllib.request

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--url", default="https://agents.flaxsites.com/chatgpt/mcp")
args = parser.parse_args()


def rpc(method, params=None, token=None):
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(
        args.url,
        data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}).encode(),
        headers=headers,
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        result = json.load(response)
    assert "error" not in result, result
    return result["result"]


initialized = rpc("initialize", {
    "protocolVersion": "2025-03-26", "capabilities": {},
    "clientInfo": {"name": "flax-interface-check", "version": "1.0"},
})
assert "flax.sites.list" in initialized["instructions"], initialized
tools = rpc("tools/list")["tools"]
by_name = {tool["name"]: tool for tool in tools}
required = {
    "flax.sites.list", "flax.site.get_model", "flax.sites.create",
    "flax.drafts.validate_model_update", "flax.drafts.propose_model_update",
    "flax.drafts.get_change",
}
assert required <= by_name.keys(), "Missing tools: " + str(required - by_name.keys())
assert "flax.drafts.publish_change" not in by_name
for name, tool in by_name.items():
    expected_type = "noauth" if name == "flax.sites.create" else "oauth2"
    assert tool["securitySchemes"][0]["type"] == expected_type, name
    assert tool["_meta"]["securitySchemes"] == tool["securitySchemes"], name
for token in (None, "invalid-interface-check-token"):
    result = rpc("tools/call", {"name": "flax.sites.list", "arguments": {}}, token)
    assert result.get("isError") is True, result
    challenges = result["_meta"]["mcp/www_authenticate"]
    assert challenges and 'resource_metadata="' in challenges[0], challenges
    assert 'error="' in challenges[0] and 'error_description="' in challenges[0], challenges
    assert "structuredContent" not in result, "Unauthenticated call returned customer data"
print(f"PASS: {len(tools)} discoverable tools, explicit auth policies, native linking for missing/invalid tokens; no site changed.")
