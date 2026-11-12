"""Loopback server entry point for the macOS notch host (macos/BranchglanceNotch.swift).

The Windows widget runs the server in-process and hands the settings window a
one-time token. The macOS host runs this module as a child process instead and
passes the token through BRANCHGLANCE_SETTINGS_TOKEN.
"""
from __future__ import annotations

import argparse
import hashlib
import hmac
import os
import sys
import re
import threading
import time
from urllib.parse import urlsplit

from . import debug_report, product, server

SECRET_ENV = "BRANCHGLANCE_MACOS_HOST_SECRET"
PROOF_PATH = "/api/macos-host-proof"
_HEX_32 = re.compile(r"[0-9a-f]{64}\Z")


def proof_handler(key: bytes):
    class MacOSHandler(server.Handler):
        def do_GET(self):
            if urlsplit(self.path).path != PROOF_PATH:
                return super().do_GET()
            challenge = self.headers.get("X-Branchglance-Challenge", "")
            auth = self.headers.get("X-Branchglance-Auth", "")
            if (self.path != PROOF_PATH or not self._valid_local_host()
                    or self.headers.get("Origin") is not None
                    or not _HEX_32.fullmatch(challenge) or not _HEX_32.fullmatch(auth)):
                return self._send_json({"error": "invalid proof request"}, 403)
            raw = bytes.fromhex(challenge)
            expected = hmac.new(key, b"branchglance-macos-proof-v1/request:" + raw, hashlib.sha256).hexdigest()
            if not hmac.compare_digest(auth, expected):
                return self._send_json({"error": "invalid proof request"}, 403)
            response = hmac.new(key, b"branchglance-macos-proof-v1/response:" + raw, hashlib.sha256).hexdigest()
            return self._send_json({"version": 1, "proof": response})

        def log_message(self, fmt, *args):
            if PROOF_PATH in self.path:
                return
            super().log_message(fmt, *args)

    return MacOSHandler


def main() -> None:
    secret = os.environ.pop(SECRET_ENV, "")
    if not _HEX_32.fullmatch(secret):
        raise SystemExit("missing or invalid macOS host secret")
    key = bytes.fromhex(secret)
    parser = argparse.ArgumentParser(description="Branchglance server for the macOS notch host")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    debug_report.install_handler()
    if not os.environ.get(product.POLICY_FILE_ENV):
        os.environ[product.POLICY_FILE_ENV] = str(product.hook_data_root() / "policy.json")
    product.hook_data_root().mkdir(parents=True, exist_ok=True)
    server.set_settings_token(os.environ.pop("BRANCHGLANCE_SETTINGS_TOKEN", None) or None)
    httpd = server.ThreadingHTTPServer(("127.0.0.1", args.port), proof_handler(key))
    # One server per user owns the Insights stores and the bg-codex receipt inbox,
    # as the instance mutex does on Windows.
    # A restarted app's previous server can take a moment to notice its parent left.
    deadline = time.monotonic() + 6
    while not product.acquire_macos_instance_lock() and time.monotonic() < deadline:
        time.sleep(0.25)
    if not product.macos_instance_held():
        print("Branchglance: another instance is already running", file=sys.stderr)
        httpd.server_close()
        raise SystemExit(3)
    from . import agent_links
    agent_links.initialize_receipt_inbox()
    from . import claude_usage_api
    claude_usage_api.REFRESHER.start()
    parent = os.getppid()

    def exit_with_parent() -> None:
        # A force-quit app never runs its cleanup; don't outlive it.
        while os.getppid() == parent:
            time.sleep(2)
        httpd.shutdown()

    threading.Thread(target=exit_with_parent, name="parent-watch", daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        claude_usage_api.REFRESHER.stop()
        httpd.server_close()


if __name__ == "__main__":
    main()
