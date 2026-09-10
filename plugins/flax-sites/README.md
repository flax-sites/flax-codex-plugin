# Flax Sites Codex plugin

This plugin gives Codex access to Flax's hosted OAuth-capable MCP server for
Flax websites. The MCP transport is HTTPS, so the package works when installed
from the Git marketplace as well as from a local checkout.

## Install locally

Copy this plugin directory into the user's Codex plugin location, or install
it through the marketplace/package mechanism used by the target Codex build.
The hosted MCP server is declared in `.mcp.json` at
`https://agents.flaxsites.com/chatgpt/mcp` and uses OAuth. The repository still
contains the Python bridge and its tests as development artefacts, but Codex
must not launch that script as a shell fallback.

## Use

Ask Codex to work on an exact site URL. Codex calls `flax_list_sites`, matches
the URL to the authorized site returned by Flax, and passes that site's opaque
`siteId` to the read and draft tools. OAuth is handled by the hosted MCP
connection; credentials and tokens never need to be pasted into chat.

For a new site, Codex calls only `flax.sites.create`. Its embedded app handles
template selection, signup, business details and initial deployment together,
then returns the published URL. This is the only anonymous onboarding tool.
The separate signup, template browser and browser-launch shortcuts are removed.
Site-management tools require an existing authorized site connection.

The `flax_audit_public_site` tool performs bounded read-only checks of
HTML metadata, `robots.txt`, and `sitemap.xml` on that exact connected origin.
It accepts relative paths only and avoids general web-reader or shell-parser
fallbacks during technical reviews.

No credentials, authorization codes, or tokens should be pasted into chat.
The bridge never calls Flax publishing tools; owners publish approved drafts in
Flax.

## Distribution

This package can be distributed from Flax's website, a repository release, or
a package registry. The `.app.json` mapping is the ChatGPT app registration;
the hosted `.mcp.json` entry is the Codex connection to the same production
MCP service.

## Token storage

OAuth credentials are handled by Codex's MCP connection. To revoke access,
disconnect the Flax connection in Codex or revoke the grant in Flax.
