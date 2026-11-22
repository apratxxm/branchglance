"""Opt-in real-browser fixtures. All HTTP responses are fictional and local."""
import functools
import os
from pathlib import Path
import sys
import threading
from http.server import ThreadingHTTPServer

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts" / "screenshots"))
from fixture_server import Handler, fixtures  # noqa: E402


def pytest_configure(config):
    config.addinivalue_line("markers", "ui_rig: opt-in real-browser widget tests")


def pytest_collection_modifyitems(items):
    if os.environ.get("TOKEN_TRACKER_UI_RIG") != "1":
        skip = pytest.mark.skip(reason="set TOKEN_TRACKER_UI_RIG=1 to run browser tests")
        for item in items:
            if item.path.is_relative_to(Path(__file__).parent):
                item.add_marker(skip)


class RigHandler(Handler):
    def do_GET(self):
        if self.path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        if self.path == "/api/debug-report":
            data = b"# Branchglance debug report\nFictional report"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        super().do_GET()


@pytest.fixture(scope="session")
def server():
    responses = fixtures()
    responses.update({
        "/api/ledger": {"ok": True},
        "/api/ledger/status": {"ok": True, "state": "disabled"},
        "/api/features": {"ok": True, "features": {}},
        "/api/statusline": {"ok": True, "connected": False},
        "/api/debug-report": {"report": "Fictional debug report"},
    })
    http = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(RigHandler, responses=responses))
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{http.server_port}"
    http.shutdown()
    http.server_close()
    thread.join()


@pytest.fixture(scope="session")
def browser():
    from playwright.sync_api import Error, sync_playwright

    with sync_playwright() as playwright:
        try:
            browser = playwright.chromium.launch(channel="msedge" if sys.platform == "win32" else None)
        except Error as error:
            message = str(error)
            if "Executable doesn't exist" in message or "not found" in message or "distribution" in message:
                pytest.skip(f"Browser unavailable: {message}")
            raise
        yield browser
        browser.close()


def _rig_context(browser, server, viewport):
    """A fresh context whose page errors, failed local requests and HTTP errors fail the test."""
    context = browser.new_context(viewport=viewport, reduced_motion="reduce")
    errors = []
    page = context.new_page()
    page.on("pageerror", lambda error: errors.append(f"page error: {error}"))
    page.on("console", lambda message: errors.append(f"console: {message.text}") if message.type == "error" else None)
    # ERR_ABORTED is a page-initiated cancellation (navigation, iframe reset), not a server failure.
    page.on("requestfailed", lambda request: errors.append(f"request failed: {request.url}: {request.failure}") if request.url.startswith(server) and "ERR_ABORTED" not in (request.failure or "") else None)
    page.on("response", lambda response: errors.append(f"HTTP {response.status}: {response.url}") if response.url.startswith(server) and response.status >= 400 else None)
    context.route("**/*", lambda route: route.continue_() if route.request.url.startswith(server + "/") else route.abort())
    return context, page, errors


@pytest.fixture
def page(browser, server):
    context, page, errors = _rig_context(browser, server, {"width": 720, "height": 608})
    page.goto(server + "/runway.html?review=1&capture=tree")
    page.wait_for_function("window.__tokenTrackerTest && document.querySelector('#dashboard-open')")
    page.evaluate("""() => {
      // review=1 sets __TOKEN_TRACKER_TEST__, which skips app.js's global click/keydown
      // binding. Bind the exported handlers the same way production does.
      const t = window.__tokenTrackerTest;
      document.addEventListener("click", t.handleClick);
      document.addEventListener("keydown", t.handleKey);
      const api = window.pywebview.api;
      window.__rigCalls = [];
      window.__rigOpenSettingsResult = true;
      api.set_widget_lock = async (locked, expanded) => { window.__rigCalls.push(['lock', locked]); return {locked, expanded, revision: Date.now(), keepOpen: false, keepOpenRevision: 0}; };
      api.set_keep_open = async (keepOpen) => { window.__rigCalls.push(['keep-open', keepOpen]); return {keepOpen, keepOpenRevision: Date.now()}; };
      // Settings is its own native window; the widget only asks the bridge to open it.
      api.open_reset_actions = async (...args) => { window.__rigCalls.push(['open-settings', ...args]); return window.__rigOpenSettingsResult; };
      window.open = (...args) => { window.__rigCalls.push(['open', ...args]); return null; };
    }""")
    yield page
    try:
        assert not errors, "\n".join(errors)
    finally:
        context.close()


# widget.py opens Settings natively at 440x540 (minimum 360x360) on /reset_actions.html#t=<token>.
SETTINGS_WINDOW = {"width": 440, "height": 540}
SETTINGS_TOKEN = "ui-rig-fictional-settings-token"
GRANT_ALL_POLICY = {"schemaVersion": 1, "generation": "ui-rig-settings-window",
                    "providers": [{"name": "Codex", "collect": True, "show": True,
                                   "send": True, "sendOrigin": "provider_env",
                                   "capabilities": {"greet": True, "continue": True}}]}


@pytest.fixture
def settings_window(browser, server):
    """Open the standalone Settings page the way the native window loads it, in its own context."""
    opened = []

    def open_window(*, grant_all=False, viewport=SETTINGS_WINDOW):
        context, page, errors = _rig_context(browser, server, viewport)
        opened.append((context, errors))
        # The shared fixture policy denies automatic actions; grant_all enables every reset choice.
        if grant_all:
            context.route("**/api/provider-policy", lambda route: route.fulfill(json=GRANT_ALL_POLICY))
        # Discard closes the native window; record it instead of closing the rig page.
        context.add_init_script("window.__rigCalls = []; window.close = () => { window.__rigCalls.push(['close']); };")
        page.goto(f"{server}/reset_actions.html#t={SETTINGS_TOKEN}")
        return page

    yield open_window
    try:
        failures = [error for _, errors in opened for error in errors]
        assert not failures, "\n".join(failures)
    finally:
        for context, _ in opened:
            context.close()
