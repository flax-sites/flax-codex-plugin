---
name: flax-sites
description: Connect Codex to Flax through its hosted OAuth MCP server, inspect a named site, and prepare safe previewable drafts.
---

# Flax Sites in Codex

Use the `flax-sites` MCP server for Flax website work.

## Connection workflow

1. If the user names an existing site, use `flax_list_sites` first and match the user's exact origin against the returned site URLs. Pass the matching returned `siteId` to subsequent site tools. If the site is not returned, explain that the user must authorize or connect that site in Flax; never substitute another site. Never ask the user to paste credentials or tokens.
2. For any new site, call only `flax.sites.create` with the business category if known (for example `templateType: "services"`). It opens one embedded app for template selection, business details, signup, email verification and initial deployment. Stop the turn after opening it and let the user finish in that app. The app sends the published site URL back to this task. Do not call a separate template picker or account signup tool, open an agency signup page, or ask the user to supply the new site's URL. Once the app returns the created URL, call `flax_list_sites` and use the exact returned `siteId` for any follow-up site management. This combined tool is the only anonymous Flax workflow.
3. Keep every operation bound to the exact `siteId` returned by `flax_list_sites`. Do not substitute another site, infer IDs from URLs, or call tools for a different site. The anonymous onboarding flow uses the same hosted MCP server without site credentials.
4. Read the current model and hash before proposing any change.
5. Only inspect analytics or search-performance tools when the user explicitly asks for analytics.
6. For public technical checks, call `flax_audit_public_site` with selected relative paths. Use its structured page, robots, and sitemap results instead of a general web reader or shell HTTP/parser commands.

Do not launch `scripts/flax_mcp.py`, use `exec` as an MCP fallback, or render MCP tool responses as plain JSON. If the native `flax-sites` tools are unavailable, stop and report that the plugin connection needs to be restarted or repaired.

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

Codex handles OAuth and hosted MCP transport. Do not recreate those mechanics in chat or expose authorization URLs as reusable links.
