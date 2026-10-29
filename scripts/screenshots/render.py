"""Render fictional Branchglance screenshots with a local fixture server and Edge."""

import datetime as dt
import functools
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "token_tracker" / "static"
OUTPUT = ROOT / "docs" / "release" / "screenshots"
HERE = Path(__file__).resolve().parent


def fixtures():
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    iso = lambda minutes: (now - dt.timedelta(minutes=minutes)).isoformat()
    reset = lambda minutes: (now + dt.timedelta(minutes=minutes)).timestamp()
    policy = {"schemaVersion": 1, "generation": "synthetic", "providers": [
        {"name": name, "collect": True, "show": True, "send": False, "capabilities": {}}
        for name in ("Codex", "Claude")
    ]}

    def chat(provider, ident, title, model, parent=None, *, elapsed=12, context=68000,
             window=200000, cache=84, activity="thinking", **extra):
        return {"id": ident, "key": provider + ":" + ident, "parentKey": parent,
                "title": title, "model": model, "status": "running", "statusSource": "transcript",
                "executionStatus": "running", "executionSource": "transcript", "executionObservedAt": iso(0),
                "executionSince": iso(elapsed), "executionActivity": {"kind": activity, "observedAt": iso(0)},
                "activeRunStartedAt": iso(elapsed), "updatedAt": iso(0), "usageUpdatedAt": iso(0),
                "contextObservedAt": iso(0), "contextModel": model, "contextTokens": context,
                "contextWindow": window, "contextPercent": round(context * 100 / window), "tokens": 180000, "turns": 4,
                "toolCalls": 11, "filesEdited": 2, "workspace": "C:\\fictional\\acme-api", "workspaceKind": "project",
                "lastRequest": {"input": 100000, "cached": cache * 1000}, **extra}

    lead = chat("Claude", "lead", "Refactor auth", "claude-opus-5-5",
                elapsed=56, context=132000, window=272000, cache=93, activity="checking_data")
    worker = chat("Codex", "worker", "Implement token validation", "gpt-6.1-terra", "Claude:lead",
                  agentName="API worker", elapsed=47, context=188000, cache=76, activity="editing")
    nested = chat("Codex", "review", "Review edge cases", "gpt-6.1-sol", "Codex:worker",
                  agentName="Review worker", elapsed=18, context=41000, cache=97, activity="testing")
    waiting = chat("Codex", "question", "Check session expiry", "gpt-6.1-terra", "Claude:lead",
                   agentName="Test worker", context=79000, cache=61, status="needs input", executionStatus="waiting",
                   pendingQuestion="Which session expiry should I use?", pendingQuestionAt=iso(4.2),
                   pendingOptions=["Thirty minutes", "One hour"])
    other = chat("Codex", "docs", "Update API docs", "gpt-6.1-sol", workspace="C:\\fictional\\docs-site",
                 elapsed=18, context=86000, window=272000, cache=88, activity="editing", updatedAt=iso(1))
    docs_child = chat("Codex", "docs-review", "Check examples", "gpt-6.1-terra", "Codex:docs",
                      agentName="Docs reviewer", workspace="C:\\fictional\\billing-service", elapsed=4.2,
                      context=54000, cache=71, activity="testing", updatedAt=iso(1))
    providers = []
    for name, chats, five, weekly, minutes, weekly_minutes in (("Codex", [worker, nested, waiting, other, docs_child], 37, 52, 161, 4500),
                                                                ("Claude", [lead], 24, 41, 47, 7360)):
        providers.append({"name": name, "connected": True, "chats": chats, "activity": [],
                          "fiveHour": {"percent": five, "resetAt": reset(minutes)},
                          "weekly": {"percent": weekly, "resetAt": reset(weekly_minutes)},
                          "limitsUpdatedAt": iso(0)})
    snapshot = {"generatedAt": iso(0), "enabledProviders": ["Codex", "Claude"], "providerPolicies": policy,
                "providers": providers, "graph": {"revision": 1, "edges": {
                    "Claude:lead": None, "Codex:worker": "Claude:lead", "Codex:review": "Codex:worker",
                    "Codex:question": "Claude:lead", "Codex:docs": None, "Codex:docs-review": "Codex:docs"}}}
    insights = []
    for name, base, model in (("Codex", 120000, "gpt-6.1-terra"), ("Claude", 90000, "claude-opus-5-5")):
        variation = [1.05, 1.32, .95, 1.27, 1.06, .86, 1.16, 1.45, 1.12, 1.0,
                     1.31, 1.12, .91, 1.22, 1.86, 1.14, 1.35, 1.05, 1.18, .96,
                     1.29, 1.07, 1.42, .94, 1.11, 1.82, 1.16, 1.37, .9, 1.24]
        series = []
        for index, factor in enumerate(variation):
            date = now.date() - dt.timedelta(days=29-index)
            tokens = round(base * factor * (.55 if date.weekday() >= 5 else 1) * (1.08 if name == "Claude" and index % 3 == 0 else 1))
            cost = round(tokens * .000008, 2) if name == "Claude" else None
            series.append({"date": date.isoformat(), "tokens": tokens, "apiEquivalentCost": cost})
        total = sum(point["tokens"] for point in series)
        cost = round(sum(point["apiEquivalentCost"] for point in series), 2) if name == "Claude" else None
        insights.append({"name": name, "totals": {"tokens": total, "apiEquivalentCost": cost,
                         "costPartial": name == "Codex", "unpricedModels": [model] if name == "Codex" else [],
                         "sessions": 24, "subagentsActive": 8, "activeHours": 35, "cacheHitRate": 78},
                         "series": series, "models": [{"name": model, "tokens": total, "apiEquivalentCost": cost}],
                         "limitPeaks": {"fiveHour": 68 if name == "Codex" else 83, "weekly": 52 if name == "Codex" else 37}})
    return {"/api/stats": snapshot, "/api/live": snapshot, "/api/insights": {"range": "30d", "providers": insights},
            "/api/provider-policy": policy, "/api/identity": {}, "/api/quota": {}, "/api/today": {},
            "/api/limit-reset": {}, "/api/avatars": {}}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, responses, **kwargs):
        self.responses = responses
        super().__init__(*args, directory=str(STATIC), **kwargs)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path.startswith("/api/"):
            if path not in self.responses:
                self.send_error(404)
                return
            data = json.dumps(self.responses[path]).encode()
            content_type = "application/json"
        elif path == "/screenshot-fixture.js":
            data = (HERE / "fixture.js").read_bytes()
            content_type = "application/javascript"
        elif path == "/capture.html":
            data = (b'<!doctype html><style>html,body{margin:0;background:#000} '
                    b'iframe{display:block;width:328px;height:110px;border:0}</style>'
                    b'<iframe src="/runway.html?review=1&amp;capture=collapsed"></iframe>')
            content_type = "text/html; charset=utf-8"
        elif path == "/runway.html":
            html = (STATIC / "runway.html").read_text(encoding="utf-8")
            data = html.replace("</body>", '<script src="/screenshot-fixture.js"></script></body>').encode()
            content_type = "text/html; charset=utf-8"
        else:
            return super().do_GET()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format, *args):
        pass


def edge_path():
    candidates = [Path(os.environ.get(name, "")) / "Microsoft/Edge/Application/msedge.exe"
                  for name in ("PROGRAMFILES(X86)", "PROGRAMFILES", "LOCALAPPDATA")]
    for path in candidates:
        if path.is_file():
            return path
    raise FileNotFoundError("Microsoft Edge was not found in its usual installation paths")


def main():
    server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Handler, responses=fixtures()))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    OUTPUT.mkdir(parents=True, exist_ok=True)
    try:
        with tempfile.TemporaryDirectory(prefix="branchglance-screenshots-") as profile:
            for mode, filename, width, height in (("collapsed", "widget-collapsed.png", 328, 110),
                                                   ("tree", "tree-expanded.png", 560, 420),
                                                   ("insights", "insights.png", 720, 608)):
                page = "capture.html" if mode == "collapsed" else f"runway.html?review=1&capture={mode}"
                url = f"http://127.0.0.1:{server.server_port}/{page}"
                subprocess.run([str(edge_path()), "--headless", "--disable-gpu", "--hide-scrollbars",
                                "--force-device-scale-factor=2", "--force-prefers-reduced-motion",
                                "--no-first-run", "--no-default-browser-check", f"--user-data-dir={profile}",
                                f"--window-size={width},{height}", "--virtual-time-budget=4000",
                                f"--screenshot={OUTPUT / filename}", url], check=True, timeout=60)
                print(OUTPUT / filename)
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


if __name__ == "__main__":
    main()
