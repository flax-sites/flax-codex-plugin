---
name: flax-sites
description: Use the registered Flax Sites app to inspect an existing Flax website and prepare previewable updates, or create a new site.
---

# Flax Sites

Use the native tools supplied by this plugin's registered Flax app. ChatGPT and Codex own the connection and OAuth flow.

## Existing website

1. Call the native tool corresponding to flax.sites.list. Match the user's exact origin against the returned URLs; use the matching site's returned id as siteId. Never invent an ID or substitute another site.
2. If the tool requests authentication, let the host show its native Connect/Sign in flow. After the user connects, retry the read. If the requested site is absent, explain that the connected Flax account needs access to that site.
3. Read flax.site.get_model and its current hash. Use the returned model and advertised schema; preserve unrelated fields.
4. For a clear requested correction, such as changing insurance cover to £5 million, validate with flax.drafts.validate_model_update, then prepare the draft with flax.drafts.propose_model_update. The user's explicit edit request authorizes this preview draft; do not require them to approve the same edit again.
5. Report what changed and return the preview URL and change ID from the result. The owner reviews and publishes in Flax. Never publish or deploy from this plugin.
6. Re-read and revalidate if the model hash is stale. Inspect analytics only when requested.

Tool names may have a host namespace or normalized punctuation. Select the exposed tool with the corresponding purpose and schema. Do not invent tool arguments. Use additional tools only if they are actually advertised by the connected app.

## New website

Call flax.sites.create only when the user wants a new website. Its embedded app handles starter selection, business details, sign-up and initial publishing. Stop after opening that app and let the user finish. Use the returned site URL for subsequent management.

## Missing connection

If Flax tools are missing, say: “Flax Sites is installed, but its registered app tools are unavailable. Connect Flax Sites in the host's plugin settings, then start a new task/chat.”

Stop there. Do not use shell commands, Python bridges, manual HTTP/JSON-RPC, site discovery endpoints, loopback servers, or hand-built OAuth links as a fallback. Never ask for passwords, authorization codes or tokens in chat. Do not use the separate OpenAI Sites plugin for Flax websites.
