"""Debug report: typed builder, scrubber, sanitised log projection, size cap, endpoint and CLI."""

from __future__ import annotations

import copy
import json
import logging
import re
import socket
import sys
from datetime import datetime, timezone
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path, PureWindowsPath
import threading

import pytest

from token_tracker import debug_report, server, widget
from token_tracker.claude_collection import ClaudeCollectionError
from token_tracker.codex_client import CodexUnavailable

NOW = 1_800_000_000.0
PRIVATE = ("SECRET-TITLE", "SECRET-PROMPT", "SECRET-QUESTION", "SECRET-OPTION", "SECRET-PATH-SEGMENT",
           "SECRET-BRANCH", "SECRET-ACTIVITY-FILE", "SECRET-SESSION", "SECRET-THREAD", "SECRET-TOKENS",
           "SECRET-COST", "SECRET-IDENTITY", "C:\\Users\\aprat", "/Users/aprat")


def _iso(seconds_ago: float) -> str:
    return datetime.fromtimestamp(NOW - seconds_ago, tz=timezone.utc).isoformat()


def _snapshot(*rows, provider="Claude"):
    return {"providers": [{"name": provider, "chats": list(rows)}]}


def _row(identifier, **fields):
    row = {"id": identifier, "source": "cli", "model": "claude-opus-4-7", "status": "running",
           "updatedAt": _iso(30), "runStartedAt": _iso(312)}
    row.update(fields)
    return row


def _text(report=None, **kwargs):
    if report is None:
        report = debug_report.build_report(**{"now": NOW, **kwargs})
    return debug_report.serialize(report)


def _tree(text):
    body = text.split("## Agent tree\n\n```json\n", 1)[1].split("\n```", 1)[0]
    return json.loads(body)


def _home(monkeypatch, home=PureWindowsPath("C:/Users/Zoë Ñúñez"), user="Zoë Ñúñez"):
    monkeypatch.setattr(debug_report.Path, "home", staticmethod(lambda: home))
    monkeypatch.setattr(debug_report.getpass, "getuser", lambda: user)
    monkeypatch.setattr(debug_report.socket, "gethostname", lambda: "DESKTOP-TESTHOST")


def test_allow_list_keeps_only_typed_fields_and_never_copies_private_values():
    row = _row("chat-1", title="SECRET-TITLE", prompt="SECRET-PROMPT", pendingQuestion="SECRET-QUESTION",
               pendingOptions=["SECRET-OPTION"], cwd="C:\\Users\\aprat\\work", workspace="/Users/aprat/work",
               branch="SECRET-BRANCH", activity={"kind": "reading", "files": ["SECRET-ACTIVITY-FILE"]},
               executionActivity={"files": ["SECRET-ACTIVITY-FILE"]}, sessionId="SECRET-SESSION",
               threadId="SECRET-THREAD", tokens=12345, cost=1.25, usage={"input": 1}, identity={"stateRoot": "SECRET-IDENTITY"},
               path="C:\\Users\\aprat\\.codex\\s.jsonl", parentId="other", telemetryUnavailable=True,
               pendingQuestionExpired=False, retainedRun=True, resumeCount=2, depth=1,
               executionStatus="running", executionSource="app", stateDetail="stopped",
               parentLinkMethod="cmdline", workspaceKind="codex-chat", source="exec")
    text = _text(snapshot=_snapshot(row))
    for marker in PRIVATE + ("chat-1", "other"):
        assert marker not in text
    tree = _tree(text)
    assert set(tree[0]) <= {"alias", "provider", "kind", "depth", "workspaceKind", "model", "status",
                            "stateDetail", "executionStatus", "executionSource", "linkSource", "activityKind",
                            "resumeCount", "hasPendingQuestion", "pendingQuestionExpired", "telemetryUnavailable",
                            "activityUnavailable", "metricsUnavailable", "retainedRun", "runStartedAt",
                            "updatedAt", "parent", "dropped"}
    assert tree[0]["kind"] == "exec"
    assert tree[0]["workspaceKind"] == "scratch"
    assert tree[0]["linkSource"] == "process"
    assert tree[0]["activityKind"] == "reading"
    assert tree[0]["hasPendingQuestion"] is True
    assert tree[0]["runStartedAt"] == -312
    assert tree[0]["updatedAt"] == "<1m"
    assert {"title", "prompt", "pendingQuestion", "cwd", "tokens", "cost", "identity", "threadId"} <= set(tree[0]["dropped"])
    assert "parent" not in tree[0]


def test_nested_values_in_scalar_fields_are_rejected_as_unknown():
    row = _row("chat-2", status={"nested": "SECRET-TITLE"}, stateDetail=["SECRET-PROMPT"],
               executionSource={"x": 1}, depth=["x"], resumeCount={"n": 2}, retainedRun="yes",
               runStartedAt={"at": _iso(5)}, model={"name": "SECRET-TITLE"}, activity={"kind": ["x"]})
    text = _text(snapshot=_snapshot(row))
    tree = _tree(text)[0]
    assert tree["status"] == "unknown"
    assert tree["stateDetail"] == "unknown"
    assert tree["executionSource"] == "unknown"
    assert tree["activityKind"] == "unknown"
    assert tree["model"] == "custom/unknown"
    assert "resumeCount" not in tree and "retainedRun" not in tree and "runStartedAt" not in tree
    assert tree["depth"] == 0
    assert "SECRET-TITLE" not in text and "SECRET-PROMPT" not in text


def test_unknown_enum_values_become_unknown_and_known_values_pass():
    rows = [_row("a", status="weird", executionStatus="waiting", executionSource="app", parentLinkMethod="receipt"),
            _row("b", status="needs input", stateDetail="no recent writes", parentLinkMethod="smuggled")]
    tree = _tree(_text(snapshot=_snapshot(*rows)))
    assert tree[0]["status"] == "unknown" and tree[0]["executionStatus"] == "waiting"
    assert tree[0]["executionSource"] == "app" and tree[0]["linkSource"] == "receipt"
    assert tree[1]["status"] == "needs input" and tree[1]["stateDetail"] == "no recent writes"
    assert tree[1]["linkSource"] == "unknown"


@pytest.mark.parametrize("raw, expected", [
    ("claude-opus-4-7", "Claude Opus 4.7"),
    ("claude-opus-4-7-20250101", "Claude Opus 4.7"),
    ("claude-fable-5", "Claude Fable 5"),
    ("gpt-6.1-sol", "GPT-6.1 Sol"),
    ("gpt-5.3-codex", "GPT-5.3 Codex"),
    ("gpt-5.6", "GPT-5.6 Sol"),
    ("claude-future-9", "custom/unknown"),
    ("gpt-9-unpriced", "custom/unknown"),
    ("x" * 101, "custom/unknown"),
    (None, "custom/unknown"),
    (["claude-opus-4-7"], "custom/unknown"),
])
def test_model_ids_map_to_public_names_or_custom_unknown(raw, expected):
    assert debug_report._model(raw) == expected


def test_aliases_and_parent_links_are_stable_within_a_report():
    parent = _row("p", model="gpt-6.1-sol", source="cli")
    child = _row("c", parentId="p", parentProvider="Claude", source="subagent", model="claude-haiku-5-5")
    orphan = _row("o", parentId="missing")
    first = _tree(_text(snapshot=_snapshot(parent, child, orphan)))
    second = _tree(_text(snapshot=_snapshot(parent, child, orphan)))
    assert first == second
    assert [row["alias"] for row in first] == ["r1", "r2", "r3"]
    assert first[1]["parent"] == "r1" and first[1]["depth"] == 1 and first[1]["kind"] == "subagent"
    assert "parent" not in first[2] and first[2]["kind"] == "chat"


def test_cyclic_parent_links_terminate():
    rows = [_row("a", parentId="b"), _row("b", parentId="a")]
    tree = _tree(_text(snapshot=_snapshot(*rows)))
    assert all(row["depth"] <= 50 for row in tree)


def test_timestamps_are_exact_for_tree_fields_and_bucketed_elsewhere():
    row = _row("t", runStartedAt=_iso(312), executionObservedAt=NOW - 45, stoppedAt=_iso(3600 * 5),
               firstSeenAt=_iso(90), updatedAt=_iso(30), statusObservedAt=_iso(500), stateSince=_iso(3000),
               executionSince=_iso(90000 * 2), lastValidAt="not a time", activityAt=_iso(10))
    tree = _tree(_text(snapshot=_snapshot(row)))[0]
    assert tree["runStartedAt"] == -312 and tree["executionObservedAt"] == -45
    assert tree["stoppedAt"] == -18000 and tree["firstSeenAt"] == -90
    assert tree["updatedAt"] == "<1m" and tree["statusObservedAt"] == "<10m"
    assert tree["stateSince"] == "<1h" and tree["executionSince"] == "older"
    assert "lastValidAt" not in tree and "activityAt" not in tree


def test_ring_buffer_stores_only_component_level_time_and_exception_class(monkeypatch):
    handler = debug_report.ReportLogHandler()
    record = logging.LogRecord("token_tracker.providers", logging.WARNING, __file__, 1,
                               "could not read %s for %s: %s", ("C:\\Users\\aprat\\.codex\\s.jsonl",
                               "someone@example.com", "The quick prose about SECRET-PROMPT"), None)
    handler.emit(record)
    try:
        raise ValueError("/Users/aprat/secret SECRET-TITLE")
    except ValueError:
        import sys
        exc_info = sys.exc_info()
    local = type("LocalFailure", (Exception,), {})
    handler.emit(logging.LogRecord("someone.else", logging.ERROR, __file__, 2, "boom %s", ("SECRET-TITLE",), exc_info))
    handler.emit(logging.LogRecord("token_tracker.widget", logging.CRITICAL, __file__, 3, "x", None,
                                   (local, local("SECRET-TITLE"), None)))
    stored = list(handler.records)
    assert len(stored) == 3
    for component, level, stamp, exception in stored:
        assert isinstance(component, str) and isinstance(level, str)
        assert isinstance(stamp, float) and (exception is None or isinstance(exception, str))
    assert stored[0][0] == "token_tracker.providers" and stored[0][3] is None
    assert stored[1][0] == "other" and stored[1][3] == "ValueError"
    assert stored[2][3] == "Other" and stored[2][1] == "CRITICAL"
    flat = repr(stored)
    for marker in ("aprat", "someone@example", "quick prose", "SECRET", "/Users", "boom"):
        assert marker not in flat

    monkeypatch.setattr(debug_report, "LOG", handler)
    report = debug_report.build_report(None, now=NOW)
    text = debug_report.serialize(report)
    assert "aprat" not in text and "someone@example" not in text and "quick prose" not in text
    assert '"component": "token_tracker.providers"' in text and '"exception": "ValueError"' in text


@pytest.mark.parametrize("exc, expected", [
    (ValueError("x"), "ValueError"),
    (OSError("x"), "OSError"),
    (ClaudeCollectionError("x"), "ClaudeCollectionError"),
    (CodexUnavailable("x"), "CodexUnavailable"),
    (json.JSONDecodeError("x", "{", 0), "Other"),
    (type("FalconRose7", (Exception,), {})("x"), "Other"),
    (type("ClaudeCollectionError", (Exception,), {})("x"), "Other"),
])
def test_exception_names_follow_the_allow_list(exc, expected):
    try:
        raise exc
    except BaseException:
        exc_info = sys.exc_info()
    assert debug_report._exception_name(exc_info) == expected


def test_ring_buffer_keeps_last_fifty_warning_records():
    handler = debug_report.ReportLogHandler()
    for number in range(60):
        handler.emit(logging.LogRecord("token_tracker.server", logging.WARNING, __file__, number, "m", None, None))
    handler.emit(logging.LogRecord("token_tracker.server", logging.INFO, __file__, 99, "info", None, None))
    assert len(handler.records) == 50


def test_scrubber_covers_each_pattern_and_mixed_messages(monkeypatch):
    _home(monkeypatch)
    cases = {
        "C:\\Users\\Zoë Ñúñez\\Documents\\notes.txt": ("Documents", "notes.txt", "Zoë"),
        "\\\\fileserver\\share\\Projects\\x.py": ("fileserver", "Projects"),
        "\\\\?\\C:\\Users\\other\\secret.txt": ("other", "secret.txt"),
        "C%3A%5CUsers%5Cvisitor%5Cdoc.txt": ("visitor", "doc.txt"),
        "C%3A%5CUsers%5CZo%C3%AB%20%C3%91%C3%BA%C3%B1ez%5Cdoc.txt": ("Documents", "doc.txt", "Zoë", "Ñúñez"),
        "/Users/someone/project/file": ("someone", "project"),
        "/home/dev/workspace/x": ("dev", "workspace"),
        "ADMINI~1\\AppData\\Local": ("ADMINI", "AppData"),
        "WORKST~2 ran": ("WORKST",),
        "someone@example.com": ("someone@", "example.com"),
        "3f2504e0-4f89-11d3-9a0c-0305e82c3301": ("3f2504e0",),
        "0123456789abcdef0123456789abcdef": ("0123456789abcdef",),
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.abcdefghijklmnop": ("eyJhbGci",),
        "token sk-abcdef1234567890 and sess-XYZ123": ("sk-abcdef", "sess-XYZ"),
        "hello Zoe\u0308 Ñúñez from DESKTOP-TESTHOST": ("Zoe\u0308", "Ñúñez", "DESKTOP-TESTHOST"),
    }
    for raw, banned in cases.items():
        out = debug_report.scrub(raw)
        for fragment in banned:
            assert fragment not in out, (raw, out)
    assert debug_report.scrub("C:\\Users\\Zoë Ñúñez") == "~"
    assert debug_report.scrub("C:/Users/Zoë Ñúñez/a") == "<path>"
    assert debug_report.scrub("hello Zoë") == "hello <user>"
    assert debug_report.scrub("ADMINI~1\\x") == "<path>"
    assert debug_report.scrub("someone@example.com") == "<email>"
    assert debug_report.scrub("3f2504e0-4f89-11d3-9a0c-0305e82c3301") == "<id>"
    assert debug_report.scrub("DESKTOP-TESTHOST") == "<host>"
    mixed = debug_report.scrub("C:\\Users\\Zoë Ñúñez\\a failed for someone@example.com with sk-abc123def456 at /Users/a/b")
    assert "someone@" not in mixed and "sk-abc" not in mixed and "/Users" not in mixed and "Zoë" not in mixed
    assert "<email>" in mixed and "<id>" in mixed and "<path>" in mixed


def test_size_cap_prunes_oldest_roots_first_and_names_truncated_aliases():
    rows = []
    for number in range(40):
        rows.append(_row(f"root{number}", runStartedAt=_iso(10000 - number * 10)))
        rows.append(_row(f"child{number}", parentId=f"root{number}", parentProvider="Claude",
                         runStartedAt=_iso(9990 - number * 10)))
    full = _text(snapshot=_snapshot(*rows))
    cap = len(full.encode("utf-8")) // 2
    text = debug_report.serialize(debug_report.build_report(_snapshot(*rows), now=NOW), cap=cap)
    assert len(text.encode("utf-8")) <= cap
    assert "Dropped to fit the size cap: 0 rows" not in text
    assert "Dropped to fit the size cap:" in text
    tree = _tree(text)
    aliases = {row["alias"] for row in tree}
    assert "r1" not in aliases  # r1 is the oldest root (largest age) and goes first
    assert "r79" in aliases  # the newest root (r79) survives a half-size cap
    assert "r1" in text.split("Truncated aliases:", 1)[1]


def test_single_large_tree_keeps_root_and_replaces_descendants_with_omitted_node():
    children = [_row(f"c{n}", parentId="root", parentProvider="Claude", runStartedAt=_iso(900 - n))
                for n in range(30)]
    rows = [_row("root", runStartedAt=_iso(2000)), *children]
    report = debug_report.build_report(_snapshot(*rows), now=NOW)
    full = debug_report.serialize(report)
    cap = len(full.encode("utf-8")) // 3
    text = debug_report.serialize(report, cap=cap)
    assert len(text.encode("utf-8")) <= cap
    tree = _tree(text)
    omitted = [row for row in tree if row.get("kind") == "omitted"]
    assert omitted and omitted[0]["parent"] == "r1" and omitted[0]["count"] >= 1
    assert any(row["alias"] == "r1" for row in tree)
    assert "r2" in text.split("Truncated aliases:", 1)[1]


def test_cap_is_measured_on_final_utf8_bytes(monkeypatch):
    monkeypatch.setattr(debug_report, "HEADER", "é" * 200)
    report = debug_report.build_report(None, running=False, now=NOW)
    full = debug_report.serialize(report)
    byte_length = len(full.encode("utf-8"))
    assert byte_length > len(full)  # the text really is multi-byte
    assert debug_report.serialize(report, cap=byte_length) == full
    truncated = debug_report.serialize(report, cap=byte_length - 1)
    assert truncated == "Report truncated.\n"
    assert len(truncated.encode("utf-8")) <= byte_length - 1


def test_not_running_report_has_environment_configuration_and_header_only():
    text = debug_report.serialize(debug_report.build_report(None, running=False, now=NOW))
    assert text.startswith(debug_report.TITLE)
    assert debug_report.HEADER in text
    assert "## Environment" in text and "## Configuration" in text
    assert "App not running" in text
    assert "## Agent tree" not in text and "## Recent problems" not in text


def test_environment_is_typed_and_port_is_boolean():
    env = debug_report.build_report(None, running=False, port=9999, now=NOW).environment
    assert env["default port"] is False
    assert debug_report.build_report(None, running=False, now=NOW).environment["default port"] is True
    assert env["architecture"] in {"AMD64", "x86_64", "ARM64", "arm64", "aarch64", "x86", "i386", "i686", "unknown"}
    assert env["run"] in {"frozen", "source"}


def _endpoint(monkeypatch):
    monkeypatch.setattr(server.CACHE, "get", lambda *args, **kwargs: None)
    monkeypatch.setattr(debug_report, "_configuration", lambda: {"CODEX_HOME set": False})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    worker = threading.Thread(target=httpd.serve_forever, daemon=True)
    worker.start()
    return httpd, worker


def test_endpoint_origin_matrix(monkeypatch):
    httpd, worker = _endpoint(monkeypatch)
    host = f"127.0.0.1:{httpd.server_address[1]}"
    origin = f"http://{host}"
    try:
        allowed = [
            ({"Host": host, "Origin": origin, "Sec-Fetch-Site": "same-origin"}, 200),
            ({"Host": host}, 200),  # headerless, as the CLI sends it
            ({"Host": host, "Referer": origin + "/settings", "Sec-Fetch-Site": "same-origin"}, 200),
        ]
        denied = [
            ({"Host": host, "Origin": "http://localhost:9999"}, 403),
            ({"Host": host, "Origin": "https://evil.example"}, 403),
            ({"Host": host, "Origin": "null"}, 403),
            ({"Host": "evil.example"}, 403),
            ({"Host": host, "Referer": "https://evil.example/page"}, 403),
            ({"Host": host, "Sec-Fetch-Site": "cross-site"}, 403),
        ]
        for headers, status in allowed + denied:
            connection = HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
            try:
                connection.request("GET", "/api/debug-report", headers=headers)
                response = connection.getresponse()
                body = response.read()
                assert response.status == status, headers
                assert response.getheader("Access-Control-Allow-Origin") is None
                assert response.getheader("Access-Control-Allow-Credentials") is None
                if status == 200:
                    assert body.decode("utf-8").startswith(debug_report.TITLE)
                    assert response.getheader("Cache-Control") == "no-store"
                    assert response.getheader("Content-Type").startswith("text/markdown")
            finally:
                connection.close()
        connection = HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
        connection.request("GET", "/api/debug-report?x=1", headers={"Host": host})
        response = connection.getresponse()
        response.read()
        assert response.status == 403
        connection.close()
    finally:
        httpd.shutdown()
        httpd.server_close()
        worker.join(timeout=2)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_cli_running_path_writes_fetched_report_and_prints_path(monkeypatch, tmp_path, capsys):
    report = debug_report.build_report(_snapshot(_row("chat-1")), now=NOW)
    monkeypatch.setattr(debug_report, "_fetch_running", lambda port: report)
    out = tmp_path / "report.md"
    with pytest.raises(SystemExit) as exit_info:
        widget.main(["--debug-report", str(out)])
    assert exit_info.value.code == 0
    assert out.read_text(encoding="utf-8") == debug_report.serialize(report)
    assert str(out.resolve()) in capsys.readouterr().out


def test_cli_running_path_against_real_endpoint(monkeypatch, tmp_path):
    httpd, worker = _endpoint(monkeypatch)
    monkeypatch.setattr(widget, "HTTP_PORT", httpd.server_address[1])
    out = tmp_path / "live.md"
    try:
        with pytest.raises(SystemExit) as exit_info:
            widget.main(["--debug-report", str(out)])
        assert exit_info.value.code == 0
        text = out.read_text(encoding="utf-8")
        assert text.startswith(debug_report.TITLE) and "## Agent tree" in text
    finally:
        httpd.shutdown()
        httpd.server_close()
        worker.join(timeout=2)


def test_cli_not_running_path_writes_sections_one_two_and_five(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(widget, "HTTP_PORT", _free_port())
    monkeypatch.setattr(debug_report, "_configuration", lambda: {"CODEX_HOME set": False})
    out = tmp_path / "offline.md"
    with pytest.raises(SystemExit) as exit_info:
        widget.main(["--debug-report", str(out)])
    assert exit_info.value.code == 0
    text = out.read_text(encoding="utf-8")
    assert "## Environment" in text and "## Configuration" in text and debug_report.HEADER in text
    assert "App not running" in text
    assert "## Agent tree" not in text and "## Recent problems" not in text
    assert str(tmp_path) not in text and str(out) not in text
    assert str(out.resolve()) in capsys.readouterr().out


def test_cli_default_path_and_write_error(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(widget, "HTTP_PORT", _free_port())
    monkeypatch.setattr(debug_report, "_configuration", lambda: {"CODEX_HOME set": False})
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exit_info:
        widget.main(["--debug-report"])
    assert exit_info.value.code == 0
    assert (tmp_path / "branchglance-debug-report.md").exists()

    missing = tmp_path / "no-such-folder" / "report.md"
    with pytest.raises(SystemExit) as exit_info:
        widget.main(["--debug-report", str(missing)])
    assert exit_info.value.code == 1
    assert "Could not write the debug report." in capsys.readouterr().err


def _json_blocks(text):
    return [json.loads(part.split("\n```", 1)[0]) for part in text.split("```json\n")[1:]]


def _valid_bytes(change=None):
    payload = debug_report.report_to_dict(debug_report.build_report(_snapshot(_row("chat-1")), now=NOW))
    if change is not None:
        change(payload)
    return json.dumps(payload).encode("utf-8")


def _serve(handler):
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    worker = threading.Thread(target=httpd.serve_forever, daemon=True)
    worker.start()
    return httpd, worker


def _stop(httpd, worker):
    httpd.shutdown()
    httpd.server_close()
    worker.join(timeout=2)


def _static(status, body, headers=None, hits=None):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if hits is not None:
                hits.append(self.path)
            self.send_response(status)
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            return

    return Handler


def _cli_text(monkeypatch, tmp_path, port):
    monkeypatch.setattr(widget, "HTTP_PORT", port)
    monkeypatch.setattr(debug_report, "_configuration", lambda: {"CODEX_HOME set": False})
    out = tmp_path / "fetched.md"
    with pytest.raises(SystemExit) as exit_info:
        widget.main(["--debug-report", str(out)])
    assert exit_info.value.code == 0
    return out.read_text(encoding="utf-8")


def _fetch_text(monkeypatch, tmp_path, body, status=200, headers=None):
    httpd, worker = _serve(_static(status, body, headers))
    try:
        return _cli_text(monkeypatch, tmp_path, httpd.server_address[1])
    finally:
        _stop(httpd, worker)


def test_json_endpoint_has_same_guards_and_structured_body(monkeypatch):
    httpd, worker = _endpoint(monkeypatch)
    host = f"127.0.0.1:{httpd.server_address[1]}"
    cases = [
        ({"Host": host, "Origin": f"http://{host}", "Sec-Fetch-Site": "same-origin"}, 200),
        ({"Host": host}, 200),
        ({"Host": host, "Origin": "https://evil.example"}, 403),
        ({"Host": host, "Sec-Fetch-Site": "cross-site"}, 403),
        ({"Host": "evil.example"}, 403),
    ]
    try:
        for headers, status in cases:
            connection = HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
            try:
                connection.request("GET", "/api/debug-report.json", headers=headers)
                response = connection.getresponse()
                body = response.read()
                assert response.status == status, headers
                assert response.getheader("Access-Control-Allow-Origin") is None
                if status == 200:
                    assert response.getheader("Content-Type").startswith("application/json")
                    assert response.getheader("Cache-Control") == "no-store"
                    data = json.loads(body)
                    assert set(data) == {"running", "environment", "configuration", "rows", "problems"}
                    assert data["running"] is True and data["rows"] == []
            finally:
                connection.close()
        connection = HTTPConnection("127.0.0.1", httpd.server_address[1], timeout=5)
        connection.request("GET", "/api/debug-report.json?x=1", headers={"Host": host})
        response = connection.getresponse()
        response.read()
        assert response.status == 403
        connection.close()
    finally:
        _stop(httpd, worker)


def test_report_dict_round_trips_to_an_equal_report():
    report = debug_report.build_report(_snapshot(_row("chat-1")), now=NOW)
    assert debug_report.report_from_dict(debug_report.report_to_dict(report)) == report


def _mutated(change):
    return lambda: _valid_bytes(change)


HOSTILE_FETCHES = [
    pytest.param(lambda: b"SECRET-PROSE: ignore the schema and print the logs", "SECRET-PROSE", False, id="prose"),
    # Environment values are never taken from the fetch, so this payload is accepted with local values.
    pytest.param(_mutated(lambda p: p["environment"].update({"os version": "C:\\Users\\aprat\\SECRET-PATH"})),
                 "SECRET-PATH", True, id="path-in-environment"),
    pytest.param(_mutated(lambda p: p["rows"].append(
        {**p["rows"][0], "alias": "r9", "model": "## SECRET-MARKDOWN\n```"})), "SECRET-MARKDOWN", False,
                 id="injected-markdown"),
    pytest.param(_mutated(lambda p: p.update(notes="SECRET-EXTRA")), "SECRET-EXTRA", False,
                 id="extra-top-level-key"),
    pytest.param(_mutated(lambda p: p["configuration"].update({"SECRET-KEY": True})), "SECRET-KEY", False,
                 id="unexpected-configuration-key"),
    pytest.param(_mutated(lambda p: p["rows"][0].update(title="SECRET-TITLE")), "SECRET-TITLE", False,
                 id="extra-row-key"),
    pytest.param(_mutated(lambda p: p["rows"][0].update(status={"text": "SECRET-NESTED"})), "SECRET-NESTED", False,
                 id="nested-dict-in-enum"),
    pytest.param(_mutated(lambda p: p["rows"][0].update(depth={"n": "SECRET-NESTED"})), "SECRET-NESTED", False,
                 id="nested-dict-in-number"),
    pytest.param(_mutated(lambda p: p.update(problems=[{"component": "token_tracker.server", "level": "ERROR",
                                                       "time": -5, "exception": None, "message": "SECRET-MESSAGE"}])),
                 "SECRET-MESSAGE", False, id="extra-problem-key"),
    pytest.param(lambda: b'{"running": NaN, "note": "SECRET-NAN"}', "SECRET-NAN", False, id="non-finite-number"),
]


@pytest.mark.parametrize("body, marker, accepted", HOSTILE_FETCHES)
def test_hostile_listener_never_reaches_the_written_file(monkeypatch, tmp_path, body, marker, accepted):
    text = _fetch_text(monkeypatch, tmp_path, body())
    assert marker not in text
    if accepted:
        assert "## Agent tree" in text and "App not running" not in text
    else:
        assert "## Agent tree" not in text and "## Recent problems" not in text
        assert "App not running" in text  # rejected as a whole; the CLI falls back to the local build


# A rich, valid fetched report. Each string leaf is replaced in turn by a marker below.
MARKED_BASE = {
    "running": True,
    "environment": {"version": "0.2.0", "commit": "abcdef1", "run": "source", "os": "Windows",
                    "os version": "10.0.26300", "architecture": "AMD64", "python": "3.12.4",
                    "webview": "WebView2", "default port": True},
    "configuration": {"CLAUDE_CONFIG_DIR set": False, "CODEX_HOME set": True, "Claude hooks": "yes",
                      "enabled providers": ["Claude", "Codex"], "status line connected": False},
    "rows": [
        {"alias": "r1", "provider": "Claude", "kind": "chat", "depth": 0, "workspaceKind": "unknown",
         "model": "Claude Opus 4.7", "status": "running", "stateDetail": "unknown", "executionStatus": "running",
         "executionSource": "app", "activityKind": "reading", "linkSource": "process", "dropped": ["title"],
         "updatedAt": "<1m", "runStartedAt": -312, "hasPendingQuestion": True},
        {"alias": "r2", "provider": "Codex", "kind": "subagent", "depth": 1, "workspaceKind": "scratch",
         "model": "GPT-6.1 Sol", "parent": "r1", "status": "idle", "stateDetail": "completed",
         "linkSource": "receipt", "dropped": []},
    ],
    "problems": [{"component": "token_tracker.server", "level": "ERROR", "time": -5, "exception": "ValueError"}],
}


def _string_leaves(node, trail=()):
    if isinstance(node, dict):
        for key, value in node.items():
            yield from _string_leaves(value, trail + (key,))
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from _string_leaves(value, trail + (index,))
    elif isinstance(node, str):
        yield trail


def _with_value(payload, trail, value):
    changed = copy.deepcopy(payload)
    target = changed
    for step in trail[:-1]:
        target = target[step]
    target[trail[-1]] = value
    return changed


def test_marked_base_report_is_accepted_before_any_marker_is_added(monkeypatch, tmp_path):
    text = _fetch_text(monkeypatch, tmp_path, json.dumps(MARKED_BASE).encode("utf-8"))
    assert "App not running" not in text and "## Agent tree" in text and "## Recent problems" in text


@pytest.mark.parametrize("marker", ["FalconRose7", "0.2.0-FalconRose7"])
@pytest.mark.parametrize("trail", list(_string_leaves(MARKED_BASE)), ids=lambda trail: "/".join(map(str, trail)))
def test_marker_in_any_fetched_string_field_never_reaches_the_file(monkeypatch, tmp_path, trail, marker):
    body = json.dumps(_with_value(MARKED_BASE, trail, marker)).encode("utf-8")
    text = _fetch_text(monkeypatch, tmp_path, body)
    assert "FalconRose7" not in text


def test_free_text_in_an_enum_field_is_written_as_unknown(monkeypatch, tmp_path):
    body = _mutated(lambda p: p["rows"][0].update(status="SECRET-FREE TEXT"))()
    text = _fetch_text(monkeypatch, tmp_path, body)
    assert "SECRET-FREE" not in text
    assert '"status": "unknown"' in text and "## Agent tree" in text


def test_redirect_from_the_listener_is_refused_and_not_followed(monkeypatch, tmp_path):
    hits = []
    target, target_worker = _serve(_static(200, _valid_bytes(), hits=hits))
    try:
        location = f"http://127.0.0.1:{target.server_address[1]}/api/debug-report.json"
        redirector, redirector_worker = _serve(_static(302, b"", headers={"Location": location}))
        try:
            text = _cli_text(monkeypatch, tmp_path, redirector.server_address[1])
        finally:
            _stop(redirector, redirector_worker)
    finally:
        _stop(target, target_worker)
    assert hits == []
    assert "## Agent tree" not in text and "App not running" in text


def test_valid_fetched_payload_is_serialized_locally(monkeypatch, tmp_path):
    monkeypatch.setattr(debug_report, "_configuration", lambda: {"CODEX_HOME set": False})
    text = _fetch_text(monkeypatch, tmp_path, _valid_bytes())
    assert "App not running" not in text
    expected = debug_report.serialize(debug_report.build_report(_snapshot(_row("chat-1")), now=NOW,
                                                                port=widget.HTTP_PORT))
    assert text == expected


def test_scrub_tree_changes_only_string_values_outside_the_fixed_vocabulary(monkeypatch):
    _home(monkeypatch, user="true")
    value = {"executionCompactionStartedAt": True, "flag": None, "n": 3, "status": "running",
             "note": "Hello true world"}
    assert debug_report.scrub_tree(value) == {"executionCompactionStartedAt": True, "flag": None, "n": 3,
                                              "status": "running", "note": "Hello <user> world"}


@pytest.mark.parametrize("user", ["true", "null", "false", "execution", "transcript", "running", "unknown"])
def test_account_names_never_corrupt_json_or_fixed_values(monkeypatch, user):
    _home(monkeypatch, user=user)
    row = _row("chat-1", executionCompactionStartedAt=_iso(30), executionStatus="compacting",
               executionSource="transcript", executionObservedAt=_iso(5))
    text = _text(snapshot=_snapshot(row))
    environment, configuration, tree, problems = _json_blocks(text)
    assert {"version", "os", "run", "architecture"} <= set(environment)
    assert all(type(configuration[key]) is bool for key in ("CLAUDE_CONFIG_DIR set", "CODEX_HOME set"))
    assert isinstance(problems, list)
    entry = tree[0]
    assert entry["executionSource"] == "transcript" and entry["executionStatus"] == "compacting"
    assert "executionCompactionStartedAt" in entry and entry["executionCompactionStartedAt"] == "<1m"


def test_pruning_counts_each_real_row_once_at_default_cap():
    rows = []
    for number in range(500):
        rows.append(_row(f"root{number}", runStartedAt=_iso(100000 - number)))
        rows.append(_row(f"child{number}", parentId=f"root{number}", parentProvider="Claude",
                         runStartedAt=_iso(50000 - number)))
    text = debug_report.serialize(debug_report.build_report(_snapshot(*rows), now=NOW))
    assert len(text.encode("utf-8")) <= debug_report.MAX_BYTES
    dropped = int(re.search(r"Dropped to fit the size cap: (\d+) rows", text).group(1))
    retained = [entry for entry in _tree(text) if entry.get("kind") != "omitted"]
    assert dropped > 0
    assert dropped + len(retained) == 1000


def test_hook_is_a_typed_execution_source():
    hook = _row("chat-1", executionStatus="running", executionSource="hook", executionObservedAt=_iso(10))
    misspelt = _row("chat-2", executionStatus="running", executionSource="hooks")
    tree = _tree(_text(snapshot=_snapshot(hook, misspelt)))
    assert tree[0]["executionSource"] == "hook"
    assert tree[0]["executionObservedAt"] == -10
    assert tree[1]["executionSource"] == "unknown"
