"""Fictional screenshot payloads and HTTP handler shared with the UI rig."""

import datetime as dt
import json
import math
import time
from pathlib import Path
from http.server import SimpleHTTPRequestHandler
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[2]
STATIC = ROOT / "token_tracker" / "static"
HERE = Path(__file__).resolve().parent

MODEL_NAMES = {"Codex": ["gpt-6.1-terra", "gpt-6.1-sol", "gpt-6.1-luna", "gpt-6-codex-mini"],
               "Claude": ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"]}
MODEL_SHARES = [.4, .3, .2, .1]


def fixtures(models=2, reset_continue=False, tree_space=False):
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    iso = lambda minutes: (now - dt.timedelta(minutes=minutes)).isoformat()
    reset = lambda minutes: (now + dt.timedelta(minutes=minutes)).timestamp()
    policy = {"schemaVersion": 1, "generation": "synthetic", "providers": [
        {"name": name, "collect": True, "show": True, "send": False, "sendOrigin": "provider_env",
         "capabilities": {"greet": False, "continue": False}}
        for name in ("Codex", "Claude")
    ]}
    if reset_continue:
        # Codex may send: the reset frame offers Continue mode with a chat list longer than one page.
        policy["providers"][0].update({"send": True, "sendOrigin": "provider_env",
                                       "capabilities": {"greet": True, "continue": True}})

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
    if tree_space:
        # Six direct active children exercise the compact tree and its overflow control.
        extra = [chat("Codex", f"tree-{i}", f"Review compact tree layout {i}", "gpt-6.1-terra", "Claude:lead",
                      agentName=("Review a particularly long subagent name to check the single line truncation and full tooltip" if i == 0 else f"Compact tree reviewer {i}"), elapsed=i + 1, updatedAt=iso(i)) for i in range(6)]
        worker = extra[0]
        nested = extra[1]
        waiting = extra[2]
    providers = []
    for name, chats, five, weekly, minutes, weekly_minutes in (("Codex", [worker, nested, waiting, other, docs_child], 37, 52, 161, 4500),
                                                                ("Claude", [lead], 24, 41, 47, 7360)):
        providers.append({"name": name, "connected": True, "chats": chats, "activity": [],
                          "fiveHour": {"percent": five, "resetAt": reset(minutes)},
                          "weekly": {"percent": weekly, "resetAt": reset(weekly_minutes)},
                          "limitsUpdatedAt": iso(0)})
    providers[0]["bankedResets"] = {"available": 1, "items": [{"title": "Full reset (Weekly + 5 hr)", "expiresAt": reset(30 * 1440)}]}
    providers[1]["identityVerified"] = True
    if tree_space:
        providers[0]["chats"] = extra + [other, docs_child]
    snapshot = {"generatedAt": iso(0), "enabledProviders": ["Codex", "Claude"], "providerPolicies": policy,
                "providers": providers, "graph": {"revision": 1, "edges": {
                    "Claude:lead": None, "Codex:worker": "Claude:lead", "Codex:review": "Codex:worker",
                    "Codex:question": "Claude:lead", "Codex:docs": None, "Codex:docs-review": "Codex:docs"}}}
    if tree_space:
        snapshot["graph"]["edges"] = {"Claude:lead": None, "Codex:docs": None, "Codex:docs-review": "Codex:docs",
                                       **{item["key"]: "Claude:lead" for item in extra}}
    counts = {"Codex": math.ceil(models / 2), "Claude": models // 2}
    insights = []
    for name, base in (("Codex", 120000), ("Claude", 90000)):
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
        names = MODEL_NAMES[name][:counts[name]]
        weights = MODEL_SHARES[:len(names)]
        scale = sum(weights)
        model_rows = []
        for model, weight in zip(names, weights):
            share = weight / scale
            model_rows.append({"name": model, "tokens": round(total * share),
                               "apiEquivalentCost": round(cost * share, 2) if cost is not None else None})
        insights.append({"name": name, "totals": {"tokens": total, "apiEquivalentCost": cost,
                         "costPartial": name == "Codex", "unpricedModels": names if name == "Codex" else [],
                         "sessions": 24, "subagentsActive": 8, "activeHours": 35, "cacheHitRate": 78},
                         "series": series, "models": model_rows,
                         "limitPeaks": {"fiveHour": 68 if name == "Codex" else 83, "weekly": 52 if name == "Codex" else 37}})
    overflow_cells = [
        ("claude-custom-long-model-name-for-overflow-fixture", "build", 603400000),
        ("claude-custom-long-model-name-for-overflow-fixture", "review", 87100000),
        ("claude-custom-long-model-name-for-overflow-fixture", "check", 609500000),
        ("claude-research-model", "research", 455500000),
        ("claude-research-model", "design", 317300000),
        ("claude-research-model", "chore", 179500000),
        ("claude-research-model", "build", 15600000),
        ("claude-research-model", "lead", 100000),
        ("claude-check-model", "build", 680500000),
        *((f"claude-overflow-other-{index+1}", kind, 30000000 + (index+1)*1000000)
          for index, kind in enumerate(("explore", "lead", "design", "chore", "research", "check", "build"))),
    ]
    model_tokens = {}
    for model, _kind, tokens in overflow_cells:
        model_tokens[model] = model_tokens.get(model, 0) + tokens
    overflow_total = sum(model_tokens.values())
    overflow_series = [{"date": (now.date() - dt.timedelta(days=29-index)).isoformat(),
                        "tokens": round(overflow_total / 30), "apiEquivalentCost": None}
                       for index in range(30)]
    overflow_insights = {"range": "30d", "generatedAt": iso(0), "providers": [{
        "name": "Claude",
        "totals": {"tokens": overflow_total, "apiEquivalentCost": None, "sessions": 24,
                   "subagentsActive": 8, "activeHours": 35, "cacheHitRate": 78},
        "previous": None, "series": overflow_series,
        "models": [{"name": model, "tokens": tokens, "apiEquivalentCost": None}
                   for model, tokens in model_tokens.items()],
        "limitPeaks": {"fiveHour": 83, "weekly": 37},
    }]}
    overflow_ledger = {"range": "30d", "rows": [
        {"provider": "Claude", "modelId": model, "kind": kind, "totalTokens": tokens,
         "knownCostUsd": None, "costComplete": True, "unpricedTokens": 0}
        for model, kind, tokens in overflow_cells],
        "coverage": {"status": "partial", "gapCount": 3}, "service": {"state": "ready"}}
    return {"/api/stats": snapshot, "/api/live": snapshot, "/api/insights": {"range": "30d", "providers": insights},
            "/api/insights-work-overflow": overflow_insights,
            "/api/ledger-work-overflow": overflow_ledger,
            "/api/provider-policy": policy, "/api/identity": {}, "/api/quota": {}, "/api/today": {},
            "/api/limit-reset": {},
            "/api/claude-collection": {"ok": True, "state": "off", "reason": "Not collecting Claude Code activity.", "restartRequired": False, "hooks": {"installed": 0}},
            "/api/reset-action": reset_action(reset_continue, iso)}


def reset_action(reset_continue, iso):
    titles = ["Refactor auth", "Implement token validation", "Review edge cases", "Update API docs", "Check examples",
              "Plan the billing migration", "Fix flaky session tests", "Draft release notes", "Profile the runner"]
    thread_ids = [f"00000000-0000-4000-8000-{index + 1:012x}" for index in range(len(titles))]
    if not reset_continue:
        action = {"schemaVersion": 2, "mode": "greet", "armed": False, "threadIds": [],
                  "oneShot": True, "lastRun": None}
        return {"ok": True, "action": action, "threads": [], "threadsAvailable": True}
    threads = [{"id": thread_id, "title": title, "status": "idle" if index % 3 else "running"}
               for index, (thread_id, title) in enumerate(zip(thread_ids, titles))]
    selected = [thread_ids[0], thread_ids[2], thread_ids[4]]
    action = {"mode": "continue", "schemaVersion": 2, "armed": True, "threadIds": selected,
              "oneShot": True,
              "lastRun": {"at": iso(90), "targets": [{"threadId": selected[0], "status": "queued"},
                                                     {"threadId": selected[1], "status": "queued"},
                                                     {"threadId": selected[2], "status": "skipped", "reason": "chat archived"}]}}
    return {"ok": True, "action": action, "threads": threads, "threadsAvailable": True}


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, responses=None, live=None, capture=None, live_get=None, **kwargs):
        self.live_get = live_get
        self.responses = responses or {}
        self.live = live
        self.capture = capture
        super().__init__(*args, directory=str(STATIC), **kwargs)

    def do_GET(self):
        path = urlsplit(self.path).path
        print(f"page request {self.path} at {time.monotonic():.3f}", flush=True)
        if path == "/capture-ready":
            if self.capture is None or self.capture.get("payload") is None:
                self.send_error(400)
                return
            self.capture["ready"] = True
            print(f"live snapshot applied and rendered at {time.monotonic():.3f}", flush=True)
            data, content_type = b"ok", "text/plain"
        elif path.startswith("/api/"):
            if self.live is not None:
                data = self.live_get(self.live, self.path)
                if data is None:
                    self.send_error(404)
                    return
                content_type = "application/json"
            else:
                if path not in self.responses:
                    self.send_error(404)
                    return
                response = self.responses[path]
                requested = parse_qs(urlsplit(self.path).query).get("range", [None])[0]
                if path in ("/api/insights", "/api/insights-work-overflow") and requested in ("7d", "30d", "all"):
                    response = {**response, "range": requested}
                if path == "/api/insights" and requested == "24h":
                    providers = []
                    for provider in response["providers"]:
                        current, previous = provider["series"][-1], provider["series"][-2]
                        totals = {**provider["totals"], "tokens": current["tokens"],
                                  "apiEquivalentCost": current["apiEquivalentCost"], "sessions": 12,
                                  "subagentsActive": 8, "activeHours": 7}
                        models = [{**model, "tokens": current["tokens"], "apiEquivalentCost": current["apiEquivalentCost"]}
                                  for model in provider["models"]]
                        # Hourly points, like build_insights(range=24h): date is the local hour start.
                        weights = [0.2 + abs(math.sin(hour / 3.0)) * (1.6 if 8 <= hour <= 20 else 0.4) for hour in range(24)]
                        total_weight = sum(weights)
                        start = dt.datetime.now().astimezone().replace(minute=0, second=0, microsecond=0) - dt.timedelta(hours=23)
                        hourly = [{"date": (start + dt.timedelta(hours=hour)).isoformat(),
                                   "tokens": round(current["tokens"] * weights[hour] / total_weight),
                                   "apiEquivalentCost": (round(current["apiEquivalentCost"] * weights[hour] / total_weight, 4)
                                                         if current["apiEquivalentCost"] is not None else None)}
                                  for hour in range(24)]
                        providers.append({**provider, "totals": totals, "models": models,
                                          "series": hourly, "previous": previous})
                    response = {"range": "24h", "providers": providers}
                data = json.dumps(response).encode()
                content_type = "application/json"
        elif path == "/screenshot-fixture.js":
            data = (HERE / "fixture.js").read_bytes()
            content_type = "application/javascript"
        elif path == "/reset-frame.html":
            data = (b'<!doctype html><style>html,body{margin:0;background:#0b0e15} '
                    b'iframe{display:block;width:316px;height:352px;border:0}</style>'
                    b'<iframe src="/reset_actions.html?embedded=1&amp;source=Codex"></iframe>')
            content_type = "text/html; charset=utf-8"
        elif path == "/capture.html":
            data = (b'<!doctype html><style>html,body{margin:0;background:#000} '
                    b'iframe{display:block;width:328px;height:110px;border:0}</style>'
                    b'<iframe src="/runway.html?review=1&amp;capture=collapsed' + (b'&amp;live=1' if self.live else b'') + b'"></iframe>')
            content_type = "text/html; charset=utf-8"
        elif path == "/runway.html":
            html = (STATIC / "runway.html").read_text(encoding="utf-8")
            payload = self.capture.get("payload") if self.capture else None
            inline = ("<script>window.__captureLive=" + json.dumps(payload).replace("<", "\\u003c") + ";</script>") if payload else ""
            data = html.replace("</body>", inline + '<script src="/screenshot-fixture.js"></script></body>').encode()
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




