"""GET /api/ledger and GET /api/ledger/export: validation, off and starting states, policy, CSV, read-only."""

from __future__ import annotations

import csv
import io
import json
import threading
import urllib.error
import urllib.request

import pytest

from test_ledger_support import (  # ledger_guard is autouse
    DAY_END, HOUR_START, claude_file, claude_response, clock_at, codex_count, codex_file, codex_meta,
    ledger_guard, make_roots, write_jsonl,
)

ALL = {"Claude": True, "Codex": True}

from token_tracker import ledger_service
from token_tracker import ledger_store as store
from token_tracker.ledger import Ledger, read_summary
from token_tracker.ledger_service import CSV_COLUMNS, ENV_FLAG, LedgerService, csv_text
from token_tracker.provider_policy import ProviderPolicy
from token_tracker.server import Handler, ThreadingHTTPServer


def policies_for(*, claude_show=True, codex_show=True):
    flags = {"Claude": claude_show, "Codex": codex_show}
    return {name: ProviderPolicy(name=name, collect=True, show=show, send=False, send_origin="default",
                                 capabilities=frozenset()) for name, show in flags.items()}


@pytest.fixture
def no_current(monkeypatch):
    monkeypatch.setattr(ledger_service, "_CURRENT", None)


@pytest.fixture
def served():
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def get(base, path):
    try:
        with OPENER.open(base + path) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers), error.read()


def error_body(body):
    return json.loads(body.decode("utf-8"))


def write_transcripts(tmp_path):
    data, claude, codex = make_roots(tmp_path)
    write_jsonl(claude_file(claude, "s1"), [claude_response(60, "c1", output=7, input_tokens=11)])
    write_jsonl(codex_file(codex, "t1"), codex_meta("codex-thread-1", 70) + [codex_count(80, input_tokens=100, output_tokens=9)])
    return data, claude, codex


def serve_populated(tmp_path, monkeypatch, *, claude_show=True, codex_show=True):
    """Start the service for one pass (it ingests, then exits its loop) so the database exists."""
    data, claude, codex = write_transcripts(tmp_path)
    monkeypatch.setattr(ledger_service, "_CURRENT", None)
    service = LedgerService(data_dir=data, claude_root=claude, codex_root=codex,
                            policies=policies_for(claude_show=claude_show, codex_show=codex_show),
                            environ={ENV_FLAG: "1"}, waiter=lambda seconds: True)
    service.start()
    service._thread.join(20)
    assert service.health()["state"] in {"live", "backfilling"}
    return service, data


# ------------------------------------------------------------------ validation and states

@pytest.mark.parametrize("path, query", [
    ("/api/ledger", "range=1y"), ("/api/ledger", "range=ALL"), ("/api/ledger", ""),
    ("/api/ledger/export", "range=2d&format=csv"), ("/api/ledger/export", "range=all"),
    ("/api/ledger/export", "range=all&format=xlsx"), ("/api/ledger/export", "range=all&format=CSV"),
    ("/api/ledger/export", "range=all&format=csv&format=json"),
])
def test_invalid_range_or_format_is_400_even_when_off(served, no_current, path, query):
    status, _headers, body = get(served, f"{path}?{query}")
    assert status == 400
    assert error_body(body)["ok"] is False


@pytest.mark.parametrize("path", ["/api/ledger?range=all", "/api/ledger/export?range=all&format=csv"])
def test_off_when_not_started_and_when_flag_is_not_one(served, tmp_path, no_current, path):
    status, _headers, body = get(served, path)
    assert status == 503 and error_body(body) == {"ok": False, "error": "Ledger is off."}
    service = LedgerService(data_dir=tmp_path / "d", environ={ENV_FLAG: "0"}, policies=policies_for())
    service.start()
    status, _headers, body = get(served, path)
    assert status == 503 and error_body(body)["error"] == "Ledger is off."


def test_starting_when_database_is_missing(served, tmp_path, monkeypatch):
    monkeypatch.setattr(ledger_service, "_CURRENT", None)

    def failing(*args, **kwargs):
        raise OSError("not ready")

    service = LedgerService(data_dir=tmp_path / "data", claude_root=tmp_path / "c", codex_root=tmp_path / "x",
                            policies=policies_for(), environ={ENV_FLAG: "1"}, ledger_factory=failing,
                            waiter=lambda seconds: True)
    service.start()
    service._thread.join(10)
    status, _headers, body = get(served, "/api/ledger?range=all")
    assert status == 503 and error_body(body)["error"] == "Ledger is starting."
    assert not (tmp_path / "data" / store.DB_NAME).exists()  # the GET did not create storage


# ------------------------------------------------------------------ payload

def test_summary_payload_reports_service_health_and_no_api_ratio(served, tmp_path, monkeypatch):
    serve_populated(tmp_path, monkeypatch)
    for name in ("24h", "7d", "30d", "all"):
        status, _headers, body = get(served, f"/api/ledger?range={name}")
        assert status == 200, name
    status, headers, body = get(served, "/api/ledger?range=all")
    payload = json.loads(body)
    assert headers["Cache-Control"] == "no-store"
    assert payload["service"]["state"] in {"live", "backfilling"}
    assert payload["range"]["start"].startswith("1970-01-01")
    assert payload["apiRatio"]["value"] is None
    assert payload["apiRatio"]["planFeeUsd"] is None
    assert payload["apiRatio"]["unavailableReason"] == "plan fee not provided"
    assert payload["planConsumedPercent"] is None
    assert {row["provider"] for row in payload["rows"]} == {"Claude", "Codex"}


def test_show_policy_filters_both_endpoints(served, tmp_path, monkeypatch):
    serve_populated(tmp_path, monkeypatch, claude_show=False)
    _status, _headers, body = get(served, "/api/ledger?range=all")
    assert {row["provider"] for row in json.loads(body)["rows"]} == {"Codex"}
    _status, _headers, body = get(served, "/api/ledger/export?range=all&format=csv")
    providers = {row[0] for row in list(csv.reader(io.StringIO(body.decode("utf-8"))))[1:]}
    assert providers == {"Codex", "total"}
    _status, _headers, body = get(served, "/api/ledger/export?range=all&format=json")
    assert {row["provider"] for row in json.loads(body)["rows"]} == {"Codex"}


def test_nothing_shown_yields_no_rows_or_history(served, tmp_path, monkeypatch):
    serve_populated(tmp_path, monkeypatch, claude_show=False, codex_show=False)
    _status, _headers, body = get(served, "/api/ledger?range=all")
    payload = json.loads(body)
    assert payload["rows"] == [] and payload["totals"]["requests"] == 0
    _status, _headers, body = get(served, "/api/ledger/export?range=all&format=csv")
    rows = list(csv.reader(io.StringIO(body.decode("utf-8"))))
    assert len(rows) == 2 and rows[0] == list(CSV_COLUMNS) and rows[1][0] == "total"


# ------------------------------------------------------------------ export

def test_csv_export_has_fixed_filename_and_columns(served, tmp_path, monkeypatch):
    serve_populated(tmp_path, monkeypatch)
    status, headers, body = get(served, "/api/ledger/export?range=7d&format=csv")
    assert status == 200
    assert headers["Content-Disposition"] == 'attachment; filename="branchglance-ledger-7d.csv"'
    assert headers["Content-Type"].startswith("text/csv")
    rows = list(csv.reader(io.StringIO(body.decode("utf-8"))))
    assert rows[0] == list(CSV_COLUMNS)


def test_json_export_is_the_summary_payload_with_fixed_filename(served, tmp_path, monkeypatch):
    serve_populated(tmp_path, monkeypatch)
    status, headers, body = get(served, "/api/ledger/export?range=all&format=json")
    assert status == 200
    assert headers["Content-Disposition"] == 'attachment; filename="branchglance-ledger-all.json"'
    payload = json.loads(body)
    assert "service" not in payload
    assert payload["apiRatio"]["value"] is None and payload["rows"]


def test_get_never_changes_database_bytes_or_mtime(served, tmp_path, monkeypatch):
    serve_populated(tmp_path, monkeypatch)
    database = tmp_path / "data" / store.DB_NAME
    before = (database.read_bytes(), database.stat().st_mtime_ns)
    for path in ("/api/ledger?range=all", "/api/ledger/export?range=all&format=csv",
                 "/api/ledger/export?range=all&format=json", "/api/ledger?range=24h"):
        assert get(served, path)[0] == 200
    assert (database.read_bytes(), database.stat().st_mtime_ns) == before


# ------------------------------------------------------------------ CSV neutralisation

def test_csv_neutralises_formula_prefixes_and_keeps_plain_cells():
    dangerous = ["=HYPERLINK(\"http://x\")", "+SUM(A1)", "-2+3", "@cmd", "\tcmd", "\rcmd"]
    payload = {"rows": [{"provider": "Claude", "modelId": name, "displayName": "Model", "kind": "build",
                         "inputTotal": 1, "costComplete": False, "knownCostUsd": None}
                        for name in dangerous + ["plain", "a=b"]],
               "coverage": {"status": "complete", "gapCount": 0},
               "range": {"start": "2026-11-01T10:00:00+00:00", "end": "2026-11-02T10:30:00+00:00"},
               "totals": {"requests": 0, "inputTotal": 0, "outputTotal": 0, "totalTokens": 0,
                          "knownCostUsd": None, "costComplete": True, "unpricedTokens": 0}}
    parsed = list(csv.reader(io.StringIO(csv_text(payload))))
    assert parsed[0] == list(CSV_COLUMNS)
    model_cells = [row[1] for row in parsed[1:-1]]
    assert model_cells[:len(dangerous)] == ["'" + name for name in dangerous]
    assert model_cells[len(dangerous):] == ["plain", "a=b"]
    assert parsed[1][CSV_COLUMNS.index("costComplete")] == "false"
    assert parsed[1][CSV_COLUMNS.index("knownCostUsd")] == ""


# ------------------------------------------------------------------ hour-aligned windows (P9)

def test_rolling_window_starts_at_hour_boundary_and_includes_exactly_that_bucket(tmp_path, no_current):
    data, claude, codex = make_roots(tmp_path)
    # 10:30 on 2026-11-02 (HOUR_START is 10:00): the 24h window starts at yesterday 10:00.
    write_jsonl(claude_file(claude, "s1"), [claude_response(-87000, "before", output=5),
                                            claude_response(-85500, "inside", output=7)])
    service = LedgerService(data_dir=data, claude_root=claude, codex_root=codex, policies=policies_for(),
                            environ={ENV_FLAG: "1"}, waiter=lambda seconds: True)
    service.start()
    service._thread.join(20)
    now = HOUR_START + 1800
    status, _headers, body = ledger_service.http_response("/api/ledger", {"range": ["24h"]}, now=now)
    payload = json.loads(body)
    assert status == 200
    assert payload["range"]["start"] == "2026-11-01T10:00:00+00:00"
    assert payload["totals"]["requests"] == 1 and payload["totals"]["outputTotal"] == 7


# ------------------------------------------------------------------ coverage flagged in exports (P10)

def test_partial_coverage_is_flagged_on_every_row_and_the_total_row(tmp_path):
    data, claude, codex = make_roots(tmp_path)
    path = claude_file(claude, "s1")
    write_jsonl(path, [claude_response(60, "m1", output=5), claude_response(61, "m2", output=5)])
    ledger = Ledger(data, claude_root=claude, codex_root=codex, clock=clock_at(3600))
    try:
        ledger.ingest(ALL)
        path.write_text(json.dumps(claude_response(60, "m1", output=5)) + "\n", encoding="utf-8")  # truncation gap
        ledger.ingest(ALL)
    finally:
        ledger.close()
    payload = read_summary(data, HOUR_START, DAY_END, show=ALL, time_zone="UTC")
    assert payload["coverage"]["status"] == "partial" and payload["coverage"]["gapCount"] >= 1
    rows = list(csv.reader(io.StringIO(csv_text(payload))))
    column = {name: index for index, name in enumerate(rows[0])}
    data_rows = rows[1:]
    assert data_rows[-1][0] == "total"
    assert len(data_rows) >= 2
    for row in data_rows:
        assert row[column["coverageStatus"]] == "partial"
        assert row[column["gapCount"]] == str(payload["coverage"]["gapCount"])
        assert row[column["rangeStart"]] == payload["range"]["start"]
        assert row[column["rangeEnd"]] == payload["range"]["end"]


def test_empty_export_has_header_and_total_row(tmp_path):
    data, claude, codex = make_roots(tmp_path)
    ledger = Ledger(data, claude_root=claude, codex_root=codex, clock=clock_at(3600))
    try:
        ledger.ingest(ALL)
    finally:
        ledger.close()
    payload = read_summary(data, HOUR_START, DAY_END, show=ALL, time_zone="UTC")
    rows = list(csv.reader(io.StringIO(csv_text(payload))))
    assert len(rows) == 2 and rows[0] == list(CSV_COLUMNS)
    assert rows[1][0] == "total" and rows[1][CSV_COLUMNS.index("coverageStatus")] == payload["coverage"]["status"]
