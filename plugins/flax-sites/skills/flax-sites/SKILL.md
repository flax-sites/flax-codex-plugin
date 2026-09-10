---
name: flax-sites
description: Connect Codex to one named Flax site through the local OAuth-capable MCP bridge, inspect its model, and prepare safe previewable drafts.
---

# Flax Sites in Codex

Use the `flax-sites` MCP server for Flax website work.

## Connection workflow

1. If the user names an existing site, require its exact website URL and call `flax_connect` with that URL. The bridge opens a one-time browser OAuth flow for that site when needed, stores the resulting token locally under that site origin, and selects an existing authorization without prompting again; never ask the user to paste credentials or tokens.
2. For any new site, call only `flax.sites.create` with the business category if known (for example `templateType: "services"`). It opens one embedded app for template selection, business details, signup, email verification and initial deployment. Stop the turn after opening it and let the user finish in that app. The app sends the published site URL back to this task. Do not call a separate template picker or account signup tool, open an agency signup page, or ask the user to supply the new site's URL. Once the app returns the created URL, use `flax_connect` for site management. This combined tool is the only anonymous Flax workflow.
3. Keep each active connection bound to the site returned by that origin's `/.well-known/mcp.json`. Multiple site origins may be authorized for the same Codex client, but each site must be selected explicitly with `flax_connect` before using its tools. Site management must not substitute the global endpoint or enumerate other sites. The separate anonymous onboarding tools use Flax’s public MCP endpoint without site credentials.
4. Read the current model and hash before proposing any change.
5. Only inspect analytics or search-performance tools when the user explicitly asks for analytics.
6. For public technical checks, call `flax_audit_public_site` with selected relative paths. Use its structured page, robots, and sitemap results instead of a general web reader or shell HTTP/parser commands.

## Change workflow

- Report findings before proposing edits.
- Validate proposed model operations against the current model and hash.
- Create drafts/previews only after the user confirms the focused plan.
- Never call a publish/deploy tool. The owner publishes in Flax.
- Treat a stale-model error as a reason to re-read the model and revalidate.

## Recurring updates

When the user explicitly asks for regular updates, use a task-attached heartbeat
automation. On creation, omit `id`, use uppercase `ACTIVE`, set `kind` to
`heartbeat`, set the current task as `targetThreadId`, set `destination` to
`local`, and express the schedule only with `rrule`—do not send a fixed start
timestamp. Prefer updating an existing matching automation over creating a
duplicate, and verify creation succeeded before reporting that it is scheduled.

The bridge handles PKCE, callback state, token refresh, and JSON-RPC transport. Do not recreate those mechanics in chat or expose authorization URLs as reusable links.
