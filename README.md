# Flax Sites

One plugin for ChatGPT and Codex: **Flax Sites**, identified as flax-sites@flax in the GitHub marketplace.

The package uses plugins/flax-sites/.app.json to reference the registered Flax app. It has no bundled MCP connection or local bridge. The host manages OAuth and exposes the registered server's native tools.

## Before installation: create a runtime connection

An OpenAI Platform submission draft is not an installed ChatGPT developer connection. Do not copy an ID from platform.openai.com/plugins/edit into the package and assume it is callable.

For private testing, enable Developer mode in ChatGPT's Security and login settings, open https://chatgpt.com/plugins, and create a connection to https://agents.flaxsites.com/chatgpt/mcp with OAuth. Complete the native connection flow. Use the actual registered connection ID in .app.json.

Verify that Codex can resolve that exact ID and reports its runtime as enabled and callable **before** asking someone to restart and test. A successful anonymous MCP smoke check verifies the server only; it does not verify the plugin-to-account connection.

Run `python3 scripts/check_plugin_connection.py` while signed into Codex to check the package's exact registration and committed runtime. This read-only check uses experimental Codex app-server APIs and fails if the registration is missing, disabled, or not callable. It does not authenticate or edit a site.

The developer connection is private to its authorized testing account/workspace. For public distribution, use the approved published registration when it becomes available.

The current mapping is the private **Flax Sites** ChatGPT developer connection (`asdk_app_6aa2e41fe0c48191988958111c70a202`). The Platform submission draft (`asdk_app_6a7b82bb6e508191b1b5d6d82fbf163a`) is a separate review artifact and must not be used as this account's runtime connection.

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
