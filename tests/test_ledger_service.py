"""Ledger service: opt-in flag, worker cadence and backoff, cooperative stop, read-only reads, widget wiring."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import types

import pytest

from test_ledger_support import (  # ledger_guard is autouse
    DAY_END, HOUR_START, claude_file, claude_response, clock_at, ledger_guard, make_roots, write_jsonl,
)

from token_tracker import ledger_service
from token_tracker import ledger_store as store
from token_tracker.ledger import Ledger, LedgerUnavailable, read_summary
from token_tracker.ledger_service import ENV_FLAG, LIVE_INTERVAL_SECONDS, LedgerService
from token_tracker.ledger_ingest import path_key
from token_tracker.provider_policy import ProviderPolicy

ALL = {"Claude": True, "Codex": True}


def policies_for(*, claude_show=True, codex_show=True, codex_collect=True):
    flags = {"Claude": (True, claude_show), "Codex": (codex_collect, codex_show)}
    return {name: ProviderPolicy(name=name, collect=collect, show=show, send=False, send_origin="default",
                                 capabilities=frozenset())
            for name, (collect, show) in flags.items()}


def make_service(tmp_path, waiter, *, flag="1", **kwargs):
    data, claude, codex = make_roots(tmp_path)
    environ = {} if flag is None else {ENV_FLAG: flag}
    kwargs.setdefault("policies", policies_for())
    service = LedgerService(data_dir=data, claude_root=claude, codex_root=codex, environ=environ,
                            waiter=waiter, **kwargs)
    return service, data, claude, codex


def requests_in(ledger) -> int:
    return sum(row["requests"] for row in ledger.summary(HOUR_START, DAY_END, show=ALL)["rows"])


# ------------------------------------------------------------------ opt-in flag

@pytest.mark.parametrize("flag", [None, "", "0", "true", "yes", " 1", "1 ", "01"])
def test_service_is_disabled_unless_flag_is_exactly_one(tmp_path, flag):
    service, data, _claude, _codex = make_service(tmp_path, waiter=lambda seconds: True, flag=flag)
    service.start()
    assert service.health()["state"] == "disabled"
    assert service._thread is None
    assert not data.exists()  # no writer, so the data dir is never created


def test_policy_is_resolved_once_at_start_and_gates_collection(tmp_path):
    service, _data, claude, codex = make_service(tmp_path, waiter=lambda seconds: True,
                                                 policies=policies_for(codex_collect=False, codex_show=False))
    write_jsonl(claude_file(claude, "s1"), [claude_response(60, "m1", output=5)])
    service.start()
    service._thread.join(10)
    assert service.collect == {"Claude": True, "Codex": False}
    assert service.show == {"Claude": True, "Codex": False}
    assert service.health()["state"] in {"live", "backfilling"}


# ------------------------------------------------------------------ cadence

def test_backfill_progresses_then_goes_live(tmp_path):
    data, claude, codex = make_roots(tmp_path)
    write_jsonl(claude_file(claude, "s1"), [claude_response(60 + i, f"m{i}", output=5) for i in range(6)])
    waits, states = [], []
    holder = {}

    def waiter(seconds):
        waits.append(seconds)
        states.append(holder["service"].health()["state"])
        return seconds == LIVE_INTERVAL_SECONDS or len(waits) >= 20

    service = LedgerService(data_dir=data, claude_root=claude, codex_root=codex, policies=policies_for(),
                            environ={ENV_FLAG: "1"}, waiter=waiter, budget_bytes=300)
    holder["service"] = service
    service.start()
    service._thread.join(20)
    assert waits[-1] == LIVE_INTERVAL_SECONDS and states[-1] == "live"
    assert "backfilling" in states[:-1]
    assert all(seconds >= 0.2 for seconds in waits[:-1])
    ledger = Ledger(data, claude_root=claude, codex_root=codex, clock=clock_at(3600))
    try:
        assert requests_in(ledger) == 6
    finally:
        ledger.close()


def test_unfinished_tail_waits_at_live_cadence_instead_of_spinning(tmp_path):
    data, claude, codex = make_roots(tmp_path)
    path = claude_file(claude, "s1")
    write_jsonl(path, [claude_response(60, "m0", output=5)])
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"type": "assistant", "timestamp"')  # unfinished last line: pending, never progressing
    waits, states = [], []
    holder = {}

    def waiter(seconds):
        waits.append(seconds)
        states.append(holder["service"].health()["state"])
        return len(waits) >= 4

    service = LedgerService(data_dir=data, claude_root=claude, codex_root=codex, policies=policies_for(),
                            environ={ENV_FLAG: "1"}, waiter=waiter)
    holder["service"] = service
    service.start()
    service._thread.join(20)
    assert waits[0] < LIVE_INTERVAL_SECONDS  # the first pass read a complete line: backfill cadence
    assert waits[1:] == [LIVE_INTERVAL_SECONDS] * 3
    assert states[1:] == ["live"] * 3
    assert service.health()["pendingBytes"] > 0


# ------------------------------------------------------------------ errors and backoff

class DiskFailure(Exception):
    pass


class FlakyLedger:
    constructed = 0
    closed = 0

    def __init__(self, *args, **kwargs):
        FlakyLedger.constructed += 1

    def ingest(self, *args, **kwargs):
        raise DiskFailure("cannot read C:\\Users\\someone\\secret.jsonl")

    def pending_bytes(self, show):
        return 0

    def close(self):
        FlakyLedger.closed += 1


def test_exception_backs_off_exponentially_and_records_only_class_name(tmp_path):
    FlakyLedger.constructed = FlakyLedger.closed = 0
    waits = []

    def waiter(seconds):
        waits.append(seconds)
        return len(waits) >= 8

    service, *_ = make_service(tmp_path, waiter=waiter, ledger_factory=lambda *a, **k: FlakyLedger())
    service.start()
    service._thread.join(20)
    assert waits == [5.0, 10.0, 20.0, 40.0, 80.0, 160.0, 300.0, 300.0]
    health = service.health()
    assert health["state"] == "error" and health["lastError"] == "DiskFailure"
    assert "secret" not in repr(health) and "Users" not in repr(health)
    assert FlakyLedger.closed == FlakyLedger.constructed == 8  # a failed writer is dropped and rebuilt on retry


def test_construction_failure_is_error_state_and_retries(tmp_path):
    waits = []

    def factory(*args, **kwargs):
        raise PermissionError("denied")

    def waiter(seconds):
        waits.append(seconds)
        return len(waits) >= 3

    service, *_ = make_service(tmp_path, waiter=waiter, ledger_factory=factory)
    service.start()
    service._thread.join(20)
    assert waits == [5.0, 10.0, 20.0]
    assert service.health()["state"] == "error"
    assert service.health()["lastError"] == "PermissionError"


# ------------------------------------------------------------------ cooperative stop

def test_stop_returns_within_timeout_while_a_pass_is_running(tmp_path):
    entered, release, closed = threading.Event(), threading.Event(), []

    class BlockingLedger:
        def ingest(self, *args, **kwargs):
            entered.set()
            release.wait(10)  # ignores should_stop on purpose: stop() must still return
            return {"bytesRead": 0, "pendingBytes": 0, "filesVisited": 0}

        def pending_bytes(self, show):
            return 0

        def close(self):
            closed.append(True)

    service, *_ = make_service(tmp_path, waiter=None, ledger_factory=lambda *a, **k: BlockingLedger())
    service.start()
    assert entered.wait(5)
    began = time.monotonic()
    service.stop(timeout=0.2)
    assert time.monotonic() - began < 2
    assert service._thread.is_alive()
    release.set()
    service._thread.join(5)
    assert closed == [True]  # the worker closed its own writer when the pass returned


def test_should_stop_between_files_commits_nothing_from_later_files(tmp_path):
    data, claude, codex = make_roots(tmp_path)
    for name in ("a", "b", "c"):
        write_jsonl(claude_file(claude, name), [claude_response(60, f"{name}1", output=5)])
    ledger = Ledger(data, claude_root=claude, codex_root=codex, clock=clock_at(3600))
    try:
        result = ledger.ingest(ALL, should_stop=lambda: True)
        assert result["stopped"] is True and result["filesVisited"] == 0
        assert requests_in(ledger) == 0

        calls = {"n": 0}

        def stop_before_second_file():
            calls["n"] += 1
            return calls["n"] >= 2

        result = ledger.ingest(ALL, should_stop=stop_before_second_file)
        assert result["stopped"] is True and result["filesVisited"] == 1
        assert requests_in(ledger) == 1
        ledger.ingest(ALL)
        assert requests_in(ledger) == 3
    finally:
        ledger.close()


def test_should_stop_inside_verification_leaves_cursor_and_totals_unchanged(tmp_path):
    data, claude, codex = make_roots(tmp_path)
    path = claude_file(claude, "s1")
    write_jsonl(path, [claude_response(60, "m1", output=5), claude_response(61, "m2", output=5)])
    ledger = Ledger(data, claude_root=claude, codex_root=codex, clock=clock_at(3600))
    try:
        ledger.ingest(ALL)
        assert requests_in(ledger) == 2
        key = path_key(path)
        offset = store.load_source(ledger.conn, "Claude", key)["byte_offset"]
        with path.open("a", encoding="utf-8") as handle:  # changed file stamp: the next pass must verify the prefix
            handle.write(json.dumps(claude_response(62, "m3", output=5)) + "\n")
        calls = {"n": 0}

        def stop_inside_verification():
            calls["n"] += 1
            return calls["n"] >= 2  # call 1 is the between-files check; call 2 is inside prefix verification

        ledger.ingest(ALL, should_stop=stop_inside_verification)
        assert calls["n"] == 2
        assert store.load_source(ledger.conn, "Claude", key)["byte_offset"] == offset
        assert requests_in(ledger) == 2
        assert ledger._ingestor._partial_verification == {}
        ledger.ingest(ALL)
        assert requests_in(ledger) == 3
    finally:
        ledger.close()


# ------------------------------------------------------------------ read-only reads

def test_read_summary_on_missing_database_raises_and_creates_nothing(tmp_path):
    data, _claude, _codex = make_roots(tmp_path)
    with pytest.raises(LedgerUnavailable):
        read_summary(data, 0, DAY_END, show=ALL, time_zone="UTC")
    assert not (data / store.DB_NAME).exists()
    data.mkdir()
    with pytest.raises(LedgerUnavailable):
        read_summary(data, 0, DAY_END, show=ALL, time_zone="UTC")
    assert not (data / store.DB_NAME).exists()


def test_read_summary_never_migrates_or_repairs_a_damaged_file(tmp_path):
    data, _claude, _codex = make_roots(tmp_path)
    data.mkdir(parents=True)
    damaged = data / store.DB_NAME
    damaged.write_bytes(b"this is not a sqlite database" * 64)
    before = (damaged.read_bytes(), damaged.stat().st_mtime_ns)
    with pytest.raises(LedgerUnavailable):
        read_summary(data, 0, DAY_END, show=ALL, time_zone="UTC")
    assert (damaged.read_bytes(), damaged.stat().st_mtime_ns) == before
    assert sorted(p.name for p in data.iterdir()) == [store.DB_NAME]


def test_connect_readonly_refuses_writes(tmp_path):
    data, claude, codex = make_roots(tmp_path)
    ledger = Ledger(data, claude_root=claude, codex_root=codex, clock=clock_at(3600))
    ledger.close()
    conn = store.connect_readonly(data / store.DB_NAME)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO meta(key, value) VALUES('x', 'y')")
    finally:
        conn.close()


# ------------------------------------------------------------------ widget wiring

class Recorder:
    def __init__(self, calls, name):
        self.calls, self.name = calls, name

    def start(self):
        self.calls.append(f"{self.name}.start")

    def stop(self, *args, **kwargs):
        self.calls.append(f"{self.name}.stop")


def test_widget_starts_ledger_after_http_and_stops_it_on_quit(monkeypatch):
    from token_tracker import widget

    calls = []
    app = widget.WidgetApp()
    app.limit_reset = Recorder(calls, "reset")
    app.ledger = Recorder(calls, "ledger")
    monkeypatch.setattr(app, "start_http", lambda: calls.append("http") or True)
    monkeypatch.setattr(app, "_create_main_window", lambda: calls.append("window"))
    monkeypatch.setattr(app, "_start_tray", lambda: calls.append("tray"))
    monkeypatch.setattr(widget.webview, "start", lambda **kwargs: calls.append("webview"))
    app._run_claimed()
    assert calls == ["http", "reset.start", "ledger.start", "window", "tray", "webview"]
    calls.clear()
    app.quit()
    assert "ledger.stop" in calls


# ------------------------------------------------------------------ lifecycle races (P8) and scheduling (P11)

def test_concurrent_start_launches_exactly_one_worker(tmp_path):
    built = []

    class QuietLedger:
        def ingest(self, *args, **kwargs):
            return {"bytesRead": 0, "pendingBytes": 0, "filesVisited": 0}

        def pending_bytes(self, show):
            return 0

        def close(self):
            pass

    def factory(*args, **kwargs):
        built.append(1)
        return QuietLedger()

    service, *_ = make_service(tmp_path, waiter=lambda seconds: True, ledger_factory=factory)
    barrier = threading.Barrier(8)

    def call_start():
        barrier.wait()
        service.start()

    callers = [threading.Thread(target=call_start) for _ in range(8)]
    for caller in callers:
        caller.start()
    for caller in callers:
        caller.join(10)
    service._thread.join(10)
    assert len(built) == 1  # one worker, so one writer, however many callers raced


def test_thread_launch_failure_is_error_health_not_an_exception(tmp_path, monkeypatch):
    class FailingThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            raise RuntimeError("can't start new thread")

    shim = types.SimpleNamespace(Lock=threading.Lock, Event=threading.Event, Thread=FailingThread,
                                 current_thread=threading.current_thread)
    service, *_ = make_service(tmp_path, waiter=lambda seconds: True)
    monkeypatch.setattr(ledger_service, "threading", shim)
    service.start()  # must not raise into the widget
    health = service.health()
    assert health["state"] == "error" and health["lastError"] == "RuntimeError"
    assert service._thread is None
    service.stop()  # still safe after a failed launch


def test_hidden_but_collected_backlog_keeps_backfill_cadence(tmp_path):
    data, claude, codex = make_roots(tmp_path)
    write_jsonl(claude_file(claude, "s1"), [claude_response(60 + i, f"m{i}", output=5) for i in range(6)])
    waits, shown_pending = [], []
    holder = {}

    def waiter(seconds):
        waits.append(seconds)
        shown_pending.append(holder["service"].health()["pendingBytes"])
        return len(waits) >= 3

    service = LedgerService(data_dir=data, claude_root=claude, codex_root=codex,
                            policies=policies_for(claude_show=False), environ={ENV_FLAG: "1"},
                            waiter=waiter, budget_bytes=300)
    holder["service"] = service
    service.start()
    service._thread.join(20)
    assert all(seconds < LIVE_INTERVAL_SECONDS for seconds in waits)  # collected backlog is still being read
    assert shown_pending == [0, 0, 0]  # the public health never reports hidden backlog
