# Flax for Codex

The Flax Codex plugin connects Codex to a named Flax website through a local,
OAuth-capable MCP bridge.

The plugin keeps the sensitive client-side mechanics local to the user’s
machine:

- exact-origin site discovery;
- browser OAuth with state and S256 PKCE;
- loopback callback handling;
- token refresh and local token storage; and
- MCP JSON-RPC tool proxying.

Flax remains the host of the MCP server. The site owner remains in control:
the plugin can inspect a site and prepare validated drafts, but does not expose
publishing credentials or call `flax_publish_change`.

## Install from GitHub

Add this repository as a Codex marketplace, then install the plugin:

```bash
codex plugin marketplace add https://github.com/flax-sites/flax-codex-plugin
codex plugin add flax-sites@flax
```

Start a new Codex task after installation so the MCP server and skill are
loaded.

## Requirements

- Codex with local plugin support;
- Python 3.10 or newer; and
- a browser for the one-time Flax OAuth consent flow.

The bridge uses only Python’s standard library. It never asks users to paste a
password, authorization code, or access token into chat.

## Use

Ask Codex to connect to the exact Flax site URL. The plugin begins at that
origin’s `/.well-known/mcp.json`, follows its site-scoped OAuth challenge, and
opens the browser consent flow. Once connected, ask Codex to inspect the model,
review opportunities, or prepare a previewable draft.

See the [Flax agent hub](https://flaxsites.com/agents) and [agent connection
documentation](https://flaxsites.com/docs/agents) for the protocol and prompt
recipes.

## Development

The plugin itself lives at `plugins/flax-sites`. Run its standard-library test
suite with:

```bash
python3 plugins/flax-sites/scripts/test_flax_mcp.py
```

## License

MIT
