#!/usr/bin/env python3
"""Read-only check of this package's registration in the signed-in Codex account.

Uses experimental Codex app-server APIs; does not perform OAuth or site calls.
"""
import json
import os
from pathlib import Path
import select
import subprocess
import time


def main():
    root = Path(__file__).resolve().parents[1]
    mapping = json.loads((root / "plugins/flax-sites/.app.json").read_text())
    app_id = mapping["apps"]["flax-sites"]["id"]
    process = subprocess.Popen(
        ["codex", "app-server", "--stdio"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    buffer = b""

    def call(request_id, method, params):
        nonlocal buffer
        message = {"id": request_id, "method": method, "params": params}
        process.stdin.write((json.dumps(message) + "\n").encode())
        process.stdin.flush()
        deadline = time.monotonic() + 45
        while time.monotonic() < deadline:
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                response = json.loads(line)
                if response.get("id") == request_id:
                    if "error" in response:
                        raise RuntimeError(f"{method}: {response['error']}")
                    return response["result"]
            if select.select([process.stdout], [], [], 1)[0]:
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    raise RuntimeError("Codex app-server exited before responding")
                buffer += chunk
        raise RuntimeError(f"Timed out waiting for {method}")

    try:
        call(1, "initialize", {
            "clientInfo": {"name": "flax-connection-check", "version": "1"},
            "capabilities": {"experimentalApi": True},
        })
        metadata = call(2, "app/read", {"appIds": [app_id], "includeTools": True})
        if app_id in metadata.get("missingAppIds", []):
            raise RuntimeError(f"Registration {app_id} is unavailable to this account")
        if not any(app.get("id") == app_id for app in metadata.get("apps", [])):
            raise RuntimeError("Registration metadata was not returned")
        installed = call(3, "app/installed", {"forceRefresh": True})
        app = next((app for app in installed.get("apps", []) if app.get("id") == app_id), None)
        if not app or not app.get("enabled") or not app.get("callable"):
            raise RuntimeError(f"Registration resolves, but runtime is not callable: {app}")
        print(f"PASS: {app_id} resolves; runtime is enabled and callable.")
        print("Next: test native Flax tools in a fresh task; this check does not edit a site.")
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


if __name__ == "__main__":
    try:
        main()
    except (OSError, RuntimeError) as error:
        raise SystemExit(f"FAIL: {error}")
