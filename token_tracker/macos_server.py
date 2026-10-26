"""Loopback server entry point for the macOS notch host (macos/BranchglanceNotch.swift).

The Windows widget runs the server in-process and hands the settings window a
one-time token. The macOS host runs this module as a child process instead and
passes the token through BRANCHGLANCE_SETTINGS_TOKEN.
"""
from __future__ import annotations

import argparse
import os
import threading
import time

from . import product, server


def main() -> None:
    parser = argparse.ArgumentParser(description="Branchglance server for the macOS notch host")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    if not os.environ.get(product.POLICY_FILE_ENV):
        os.environ[product.POLICY_FILE_ENV] = str(product.hook_data_root() / "policy.json")
    product.hook_data_root().mkdir(parents=True, exist_ok=True)
    server.set_settings_token(os.environ.pop("BRANCHGLANCE_SETTINGS_TOKEN", None) or None)
    httpd = server.ThreadingHTTPServer(("127.0.0.1", args.port), server.Handler)
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
        httpd.server_close()


if __name__ == "__main__":
    main()
