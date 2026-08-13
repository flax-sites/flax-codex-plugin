---
name: flax-sites
description: Connect Codex to one named Flax site through the local OAuth-capable MCP bridge, inspect its model, and prepare safe previewable drafts.
---

# Flax Sites in Codex

Use the `flax-sites` MCP server for Flax website work.

## Connection workflow

1. Require the exact website URL from the user.
2. Call `flax_connect` with that URL when no connection exists. The bridge opens a one-time browser OAuth flow and stores the resulting token locally; never ask the user to paste credentials or tokens.
3. Keep the connection bound to the site returned by that origin's `/.well-known/mcp.json`. Do not substitute the global endpoint or enumerate other sites.
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
