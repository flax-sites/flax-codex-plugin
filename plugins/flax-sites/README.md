# Flax Sites Codex plugin

This plugin gives Codex a local OAuth-capable MCP bridge for Flax websites.
It is intentionally a local client: the Flax MCP server remains hosted by
Flax, while PKCE, browser callback handling, token refresh, and MCP stdio
transport run on the user's machine.

## Install locally

Copy this plugin directory into the user's Codex plugin location, or install
it through the marketplace/package mechanism used by the target Codex build.
The plugin requires Python 3.10+ and has no third-party dependencies.

The bundled `.mcp.json` starts:

```text
python3 ./scripts/flax_mcp.py
```

The MCP manifest sets `cwd` to `./`, which Codex resolves against the installed
plugin directory. The launcher path is relative to that directory. Do not use
`${PLUGIN_ROOT}` in `cwd`: this Codex build treats it as a literal directory name.

## Use

Ask Codex to connect to an exact site URL. Codex calls `flax_connect`, which:

1. Fetches that origin's `/.well-known/mcp.json` without following redirects.
2. Follows the advertised site-scoped protected-resource and OAuth metadata.
3. Registers a short-lived public OAuth client and generates S256 PKCE.
4. Opens a one-time browser consent flow on `127.0.0.1`.
5. Stores the resulting token locally under that site origin and proxies the site's MCP tools.

The local bridge can retain multiple authorized site origins for the same Codex
client. Calling `flax_connect` for a site that is already authorized selects it
without opening another browser flow. `flax_connection_status` lists all saved
sites, and `flax_disconnect` removes only the active site unless a `siteUrl` is
provided.

For a new site, Codex calls only `flax.sites.create`. Its embedded app handles
template selection, signup, business details and initial deployment together,
then returns the published URL. This is the only anonymous onboarding tool.
The separate signup, template browser and browser-launch shortcuts are removed.
Site-management tools require an existing authorized site connection.

The local `flax_audit_public_site` tool performs bounded read-only checks of
HTML metadata, `robots.txt`, and `sitemap.xml` on that exact connected origin.
It accepts relative paths only and avoids general web-reader or shell-parser
fallbacks during technical reviews.

No credentials, authorization codes, or tokens should be pasted into chat.
The bridge never calls Flax publishing tools; owners publish approved drafts in
Flax.

## Distribution

This package can be distributed from Flax's website, a repository release, or
a package registry for local Codex installation. Hosting the package does not
make it available inside `chatgpt.com`; ChatGPT requires a separately enabled
remote MCP app/connector.

## Token storage

On macOS the bridge attempts the login keychain with a bounded timeout and
otherwise uses `~/.config/flax-sites/tokens.json` with mode `0600`. A stalled
keychain operation therefore falls back automatically instead of blocking the
MCP session. The fallback file can be removed with the user's normal account
tools or by calling `flax_disconnect`. Local disconnect does not revoke the
site grant; revoke access in Flax when that is required.
