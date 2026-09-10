# Flax Sites

One plugin for ChatGPT and Codex: **Flax Sites**, identified as flax-sites@flax in the GitHub marketplace.

The package uses plugins/flax-sites/.app.json to reference the registered Flax app. It has no bundled MCP connection or local bridge. The host manages OAuth and exposes the registered server's native tools.

## Install in Codex from GitHub

Run these commands:

    codex plugin marketplace add https://github.com/flax-sites/flax-codex-plugin.git --ref main
    codex plugin marketplace upgrade flax
    codex plugin add flax-sites@flax

Connect the required Flax app in plugin settings if prompted. Fully quit and reopen Codex, then start a **new task** and select **Flax Sites**. Existing tasks can retain old skills and tools.

To refresh an existing install, run the upgrade and add commands again before restarting.

## ChatGPT compatibility

The registered MCP service is https://agents.flaxsites.com/chatgpt/mcp. The app mapping is a registration reference, not a server URL. The registered app must be accessible to the signed-in account.

GitHub distribution does not by itself publish a plugin to chatgpt.com. Before public approval, test using the registered app in ChatGPT developer mode or an authorized workspace. For public users, submit the remote service through the OpenAI **With MCP** flow and publish the approved plugin to the shared directory.

Keep the app ID in .app.json synchronized with the actual registration. Do not add a second MCP-only plugin or a fallback .mcp.json.

## Smoke test

In a fresh Codex task, select **Flax Sites** and send:

> Please update my site https://ab-roofing-ltd.flaxsites.com/. The FAQ about insurance is wrong: the amount is £5 million. Prepare a preview draft for me to review.

Expected sequence: native site list / native account connection if needed → exact site match → model read → validation → preview draft. The owner publishes in Flax.

There should be no shell commands, discovery URL fetches, Python bridge, loopback callback server or hand-built consent URL. Missing native tools must produce a short connection-repair message, not a fallback.

Repeat with the registered app in a new chat at chatgpt.com. Then test a second fresh task: an existing valid connection should be reused.

## Architecture and checks

- One marketplace entry: flax-sites.
- One required registered-app mapping.
- One bundled skill using the server's namespaced tool names.
- Editing tools are discoverable before authentication, declare OAuth, and return a native linking challenge when called without credentials.
- Site reads/writes remain protected by server-side authorization.
- Existing-site edits create previews; the owner publishes in Flax.

Validate the package with the Codex plugin-creator validator. Test the live server contract with python3 scripts/check_mcp_interface.py; this sends only anonymous discovery and read attempts and never signs in or changes a site.
