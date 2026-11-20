"""Render Branchglance screenshots with a local server and Edge.

By default the server answers from fictional fixtures. With --live it proxies
read-only GET requests to the running Branchglance app on 127.0.0.1, so the
captures show the user's real payloads. Nothing is written to the app, to
~/.claude, to ~/.codex or to %LOCALAPPDATA%: the browser profile is a temporary
directory and the proxy never forwards anything but GET.
"""

import argparse
import datetime as dt
import functools
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from urllib.error import URLError
from urllib.parse import urlsplit
import urllib.request

if __package__:
    from .fixture_server import Handler, fixtures
else:
    from fixture_server import Handler, fixtures


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "token_tracker" / "static"
OUTPUT = ROOT / "docs" / "release" / "screenshots"
DOCS_IMAGES = ROOT / "docs" / "images"
HERE = Path(__file__).resolve().parent
DEFAULT_LIVE_PORT = 8765  # token_tracker/widget.py HTTP_PORT, the running app's fixed local server
LIVE_ENDPOINTS = {"/api/stats", "/api/live", "/api/insights", "/api/provider-policy", "/api/identity", "/api/claude-collection", "/api/reset-action",
                  "/api/quota", "/api/today", "/api/limit-reset", "/api/ledger"}
# Direct connection to loopback only: a system proxy must never see the app's payloads.
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def live_base(port):
    """Return the base URL of the running Branchglance app, or stop if it is not one."""
    base = f"http://127.0.0.1:{port}"
    try:
        with OPENER.open(base + "/api/identity", timeout=5) as response:
            identity = json.load(response)
    except (URLError, OSError, ValueError) as error:
        raise SystemExit(f"No running Branchglance app answered at {base} ({error}). Start the app or pass --live-port.")
    if identity.get("product") != "Branchglance":
        raise SystemExit(f"Port {port} is not Branchglance (identity: {identity.get('product')!r}).")
    return base


def live_get(base, raw_path):
    """GET one whitelisted endpoint from the running app. Returns bytes, or None for anything else."""
    if urlsplit(raw_path).path not in LIVE_ENDPOINTS:
        return None
    with OPENER.open(urllib.request.Request(base + raw_path, method="GET"), timeout=15) as response:
        return response.read()


def capture_payload(base, attempts=6):
    """Fetch a compatible stats/live pair before starting the browser.

    The app caches each endpoint separately, so a live snapshot can briefly trail the stats one;
    retry until they line up instead of failing the capture.
    """
    for attempt in range(attempts):
        try:
            return _capture_payload_once(base)
        except ValueError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.5)


def prepare_capture(capture, mode, live):
    """Load the prefetched API pair required by live widget captures."""
    live_widget = bool(live and (mode == "collapsed" or mode.startswith("tree")))
    capture["payload"] = capture_payload(live) if live_widget else None
    capture["ready"] = False
    return live_widget


def _capture_payload_once(base):
    data = []
    for path in ("/api/stats", "/api/live"):
        started = time.monotonic()
        payload = json.loads(live_get(base, path))
        print(f"prefetch {path}: {(time.monotonic() - started)*1000:.0f} ms", flush=True)
        data.append(payload)
    stats, live = data
    try:
        stats_at = dt.datetime.fromisoformat(stats["generatedAt"].replace("Z", "+00:00"))
        live_at = dt.datetime.fromisoformat(live["generatedAt"].replace("Z", "+00:00"))
        if not isinstance(stats["providers"], list) or not isinstance(live["chats"], list):
            raise ValueError("invalid lists")
        if live_at < stats_at or live_at > dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=60):
            raise ValueError("live timestamp is older than stats or in the future")
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(f"Cannot capture live snapshot: {error}") from error
    return {"stats": stats, "live": live}


def edge_path():
    candidates = [Path(os.environ.get(name, "")) / "Microsoft/Edge/Application/msedge.exe"
                  for name in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA")]
    for path in candidates:
        if path.is_file():
            return path
    raise FileNotFoundError("Microsoft Edge was not found in its usual installation paths")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--insights-only", action="store_true")
    parser.add_argument("--settings-only", action="store_true")
    parser.add_argument("--routing", choices=("codex", "claude", "both"), action="append",
                        help="capture the routing alert state (repeatable): the widget tree after a quota crossing")
    parser.add_argument("--insights-ranges", default="24h,30d",
                        help="comma-separated: 24h,7d,30d,all (default 24h,30d)")
    parser.add_argument("--models", type=int, default=2, help="fixture models per run (fixture mode only)")
    parser.add_argument("--reset-continue", action="store_true",
                        help="fixture mode: Codex may send, the reset frame opens in Continue mode with nine chats")
    parser.add_argument("--tree-space", action="store_true", help="fixture mode: six children under one of two parents")
    parser.add_argument("--live", action="store_true",
                        help="serve the running Branchglance app's real GET payloads instead of fixtures")
    parser.add_argument("--docs-images", action="store_true",
                        help="fixture-only: write the canonical Insights and Settings captures to docs/images")
    parser.add_argument("--live-port", type=int, default=DEFAULT_LIVE_PORT)
    parser.add_argument("--output-dir", type=Path, default=OUTPUT)
    parser.add_argument("--prefix", default="")
    args = parser.parse_args()
    if args.docs_images and (args.live or args.routing or args.insights_only or args.settings_only or args.reset_continue or args.tree_space):
        raise SystemExit("--docs-images requires the default fixture capture set; drop --live and specialized capture options")
    if args.live and args.output_dir.resolve() == DOCS_IMAGES.resolve():
        raise SystemExit("live captures cannot target docs/images; choose a separate --output-dir")
    if os.name == "nt":
        # Headless Edge sometimes crashes on exit; children inherit this error mode,
        # so Windows logs the crash instead of popping an Application Error dialog.
        import ctypes
        ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002 | 0x8000)
    if args.live and (args.reset_continue or args.tree_space):
        raise SystemExit("--reset-continue and --tree-space use fixtures; drop --live")
    live = live_base(args.live_port) if args.live else None
    ranges = [item.strip() for item in args.insights_ranges.split(",") if item.strip()]
    capture = {"payload": None, "ready": False}
    server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(
        Handler, responses=None if live else fixtures(args.models, args.reset_continue, args.tree_space), live=live, capture=capture, live_get=live_get))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="branchglance-screenshots-") as profile:
            if args.routing:
                captures = [(f"tree&routing={kind}", f"routing-{kind}.png", 560, 420, args.output_dir) for kind in args.routing]
            elif args.insights_only:
                captures = [(f"insights-{item}", f"insights-{item}.png", 720, 608, args.output_dir) for item in ranges]
                captures.append(("insights-30d-work", "insights-30d-work.png", 720, 608, args.output_dir))
            elif args.settings_only:
                captures = [("settings", "settings.png", 720, 608, args.output_dir), ("reset-frame", "reset-frame.png", 320, 354, args.output_dir)]
            else:
                docs_images = DOCS_IMAGES if args.docs_images else args.output_dir
                insights_7d_name = "insights.png" if args.docs_images else "insights-7d.png"
                captures = [("collapsed", "widget-collapsed.png", 328, 110, args.output_dir),
                            ("tree", "tree-expanded.png", 560, 420, args.output_dir),
                            ("insights-24h", "insights.png", 720, 608, args.output_dir),
                            ("insights-7d", insights_7d_name, 720, 608, docs_images),
                            ("settings", "settings.png", 720, 608, docs_images)]
            for mode, filename, width, height, output_dir in captures:
                output_dir.mkdir(parents=True, exist_ok=True)
                live_tree = prepare_capture(capture, mode, live)
                page = {"collapsed": "capture.html", "reset-frame": "reset-frame.html"}.get(mode, f"runway.html?review=1&capture={mode}" + ("&live=1" if live else ""))
                url = f"http://127.0.0.1:{server.server_port}/{page}"
                command = [str(edge_path()), "--headless", "--disable-gpu", "--hide-scrollbars",
                                "--force-device-scale-factor=2", "--force-prefers-reduced-motion",
                                "--no-first-run", "--no-default-browser-check", f"--user-data-dir={profile}",
                                f"--window-size={width},{height}", "--virtual-time-budget=4000",
                                f"--screenshot={output_dir / (args.prefix + filename)}", url]
                for attempt in range(2):
                    # Headless Edge occasionally hangs on a page that polls the live app; one retry is enough.
                    try:
                        capture["ready"] = False
                        subprocess.run(command, check=True, timeout=60)
                        if live_tree and not capture["ready"]:
                            raise RuntimeError("Live capture did not apply and render its snapshot")
                        break
                    except subprocess.TimeoutExpired:
                        if attempt:
                            raise
                print(output_dir / (args.prefix + filename))
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


if __name__ == "__main__":
    main()
