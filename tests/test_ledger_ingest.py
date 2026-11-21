"""Ingestion, cursors, transactions, store integrity, policy and recovery."""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest

from test_ledger_support import (  # ledger_guard is autouse
    DAY_END, HOUR_START, append_jsonl, claude_file, claude_response, clock_at, codex_count, codex_file,
    codex_meta, ledger_guard, make_roots, stamp, write_jsonl,
)

from token_tracker import ledger_store as store
from token_tracker import ledger_ingest
from token_tracker.ledger import Ledger
from token_tracker.ledger_store import LedgerSchemaError

ALL = {"Claude": True, "Codex": True}


def test_discovery_never_probes_disabled_provider(tmp_path, monkeypatch):
    ledger, claude, codex = open_ledger(tmp_path)
    original = Path.is_dir

    def guard(path):
        assert not path.is_relative_to(codex), "disabled provider was probed"
        return original(path)

    monkeypatch.setattr(Path, "is_dir", guard)
    ledger.ingest({"Claude": True, "Codex": False})
    ledger.ingest({})


def test_provider_pending_and_timestamps_are_separate(tmp_path):
    ledger, claude, codex = open_ledger(tmp_path)
    write_jsonl(claude_file(claude, "s1"), claude_lines((10, "msg_a", 5)))
    write_jsonl(codex_file(codex, "T1"), codex_meta("T1", 10))
    ledger.ingest({"Claude": True, "Codex": False}, budget_bytes=1)
    assert store.meta_value(ledger.conn, "observedFrom:Claude") is not None
    assert store.meta_value(ledger.conn, "lastIngestedAt:Claude") is not None
    assert store.meta_value(ledger.conn, "observedFrom:Codex") is None
    assert store.meta_value(ledger.conn, "pendingBytes:Codex") == "0"


def test_invalid_last_usage_is_omitted_without_aborting(tmp_path):
    ledger, _, codex = open_ledger(tmp_path)
    bad = codex_count(11, input_tokens=10, output_tokens=5, total=15,
                      last={"total_tokens": "bad"})
    write_jsonl(codex_file(codex, "T1"), codex_meta("T1", 10) + [bad])
    ledger.ingest(ALL)
    assert total_requests(ledger) == 0
    assert ledger.conn.execute("SELECT COUNT(*) FROM ledger_omissions WHERE provider='Codex'").fetchone()[0] == 1


def test_oversized_record_is_skipped_in_bounded_chunks_and_gap_is_durable(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger_ingest, "MAX_RECORD_BYTES", 64)
    monkeypatch.setattr(ledger_ingest, "_READ_CHUNK", 16)
    ledger, claude, _ = open_ledger(tmp_path)
    path = claude_file(claude, "s1")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * 200 + b"\n" + b"{}\n")
    result = ledger.ingest(ALL, budget_bytes=8)
    assert result["bytesRead"] == 65
    assert ledger.conn.execute("SELECT COUNT(*) FROM gaps WHERE kind='oversize'").fetchone()[0] == 1
    for _ in range(30):
        result = ledger.ingest(ALL, budget_bytes=8)
        if not result["pendingBytes"]:
            break
    assert result["pendingBytes"] == 0
    assert ledger.conn.execute("SELECT COUNT(*) FROM gaps WHERE kind='oversize'").fetchone()[0] == 1


def open_ledger(tmp_path, at: int = 3600):
    data, claude, codex = make_roots(tmp_path)
    return Ledger(data, claude_root=claude, codex_root=codex, clock=clock_at(at)), claude, codex


def summary(ledger, show=ALL):
    return ledger.summary(HOUR_START, DAY_END, show=show)


def total_requests(ledger) -> int:
    return sum(row["requests"] for row in summary(ledger)["rows"])


def claude_lines(*specs):
    return [claude_response(seconds, msg, output=output) for seconds, msg, output in specs]


def test_truncation_records_gap_without_recounting(tmp_path):
    ledger, claude, _ = open_ledger(tmp_path)
    path = claude_file(claude, "s1")
    first, second = claude_lines((10, "msg_a", 5), (20, "msg_b", 7))
    write_jsonl(path, [first, second])
    ledger.ingest(ALL)
    assert total_requests(ledger) == 2

    write_jsonl(path, [first])  # truncated: nothing new, and nothing may be counted twice
    ledger.ingest(ALL)
    assert total_requests(ledger) == 2
    coverage = ledger.coverage()
    assert coverage["truncations"] == 1
    assert coverage["status"] == "partial"
    assert coverage["gaps"][0]["kind"] == "truncation"

    append_jsonl(path, [claude_response(30, "msg_c", output=9)])
    ledger.ingest(ALL)
    assert total_requests(ledger) == 2  # the post-truncation append opens debt and remains unconfirmed
    ledger._ingestor.clock = clock_at(3720)
    ledger.clock = clock_at(3720)
    ledger.ingest(ALL)
    assert total_requests(ledger) == 3


def test_replacement_records_gap(tmp_path):
    ledger, claude, _ = open_ledger(tmp_path)
    path = claude_file(claude, "s1")
    write_jsonl(path, claude_lines((10, "msg_a", 5)))
    ledger.ingest(ALL)
    write_jsonl(path, claude_lines((5, "msg_x", 9), (20, "msg_b", 7)))  # different head, larger file
    ledger.ingest(ALL)
    assert ledger.coverage()["replacements"] == 0  # replacement awaits exact prefix verification
    ledger._ingestor.clock = clock_at(3720)
    ledger.clock = clock_at(3720)
    ledger.ingest(ALL)
    assert ledger.coverage()["replacements"] == 1
    assert total_requests(ledger) == 3


def test_growing_source_defers_tail_until_idle_exact_verification(tmp_path):
    data, claude, codex = make_roots(tmp_path)
    now = [float(HOUR_START + 3600)]
    ledger = Ledger(data, claude_root=claude, codex_root=codex, clock=lambda: now[0])
    path = claude_file(claude, "idle-debt")
    write_jsonl(path, claude_lines((10, "msg_a", 5)))
    _drain_ledger(ledger)
    append_jsonl(path, claude_lines((20, "msg_b", 7)))

    result = ledger.ingest(ALL, budget_bytes=1024)
    cursor = store.load_source(ledger.conn, "Claude", ledger_ingest.path_key(path))
    assert cursor["state"]["verification_debt"]["open"] is True
    assert result["cursorAdvanced"] is False
    assert total_requests(ledger) == 1
    assert ledger.coverage()["status"] == "verifying"

    now[0] += ledger_ingest.IDLE_VERIFY_SECONDS
    for _ in range(10):
        ledger.ingest(ALL, budget_bytes=1024)
        if total_requests(ledger) == 2:
            break
    assert total_requests(ledger) == 2
    cursor = store.load_source(ledger.conn, "Claude", ledger_ingest.path_key(path))
    assert "verification_debt" not in cursor["state"]


def test_factless_debt_remains_visible_after_restart(tmp_path):
    ledger, claude, codex = open_ledger(tmp_path)
    path = claude_file(claude, "factless-debt")
    write_jsonl(path, claude_lines((10, "msg_a", 5)))
    _drain_ledger(ledger)
    with path.open("ab") as handle:
        handle.write(b'{"type":"incomplete"')
    ledger.ingest(ALL)
    cursor = store.load_source(ledger.conn, "Claude", ledger_ingest.path_key(path))
    assert cursor["state"]["verification_debt"]["open"] is True
    assert ledger.conn.execute("SELECT COUNT(*) FROM provisional_facts").fetchone()[0] == 0
    ledger.close()
    ledger = Ledger(tmp_path / "data", claude_root=claude, codex_root=codex, clock=clock_at(3600))
    coverage = ledger.coverage()
    assert coverage["status"] == "verifying"
    assert coverage["verifyingBytes"] > 0


@pytest.mark.parametrize("variant", ["same-size", "short-boundary", "shrink-above-offset", "atomic-replace"])
def test_stamp_variants_open_debt_and_use_exact_verification(tmp_path, variant):
    data, claude, codex = make_roots(tmp_path)
    now = [float(HOUR_START + 3600)]
    ledger = Ledger(data, claude_root=claude, codex_root=codex, clock=lambda: now[0])
    path = claude_file(claude, "stamp-variant")
    first = claude_response(10, "original", output=5)
    filler = {"type": "user", "message": {"content": "x" * 500}}
    write_jsonl(path, [first, filler])
    _drain_ledger(ledger)
    original = path.read_bytes()
    offset = store.load_source(ledger.conn, "Claude", ledger_ingest.path_key(path))["byte_offset"]
    tail = claude_response(20, "new-tail", output=7)

    if variant == "same-size":
        path.write_bytes(original.replace(b'"output_tokens": 5', b'"output_tokens": 7', 1))
        append_jsonl(path, [tail])
    elif variant == "short-boundary":
        index = original.rfind(b"x")
        path.write_bytes(original[:index] + b"y" + original[index + 1:])
        append_jsonl(path, [tail])
    elif variant == "shrink-above-offset":
        path.write_bytes(original + b"unfinished-tail")
        ledger.ingest(ALL)
        path.write_bytes(original + b"unfinished")
        assert path.stat().st_size > offset
        with path.open("ab") as handle:
            handle.write(b"\n" + (json.dumps(tail) + "\n").encode())
    else:
        replacement = path.with_suffix(".replacement")
        replacement.write_bytes(original + (json.dumps(tail) + "\n").encode())
        os.replace(replacement, path)

    ledger.ingest(ALL)
    cursor = store.load_source(ledger.conn, "Claude", ledger_ingest.path_key(path))
    assert cursor["state"]["verification_debt"]["open"] is True
    assert total_requests(ledger) == 1
    now[0] += ledger_ingest.IDLE_VERIFY_SECONDS
    _drain_ledger(ledger)
    expected_replacements = 1 if variant in {"same-size", "short-boundary"} else 0
    assert ledger.coverage()["replacements"] == expected_replacements
    assert total_requests(ledger) == 2


def test_deadline_scan_failures_back_off_and_rotate_due_sources(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger_ingest, "_HASH_CHUNK", 64)
    data, claude, codex = make_roots(tmp_path)
    now = [float(HOUR_START + 3600)]
    ledger = Ledger(data, claude_root=claude, codex_root=codex, clock=lambda: now[0])
    filler = {"type": "user", "message": {"content": "x" * 12000}}
    paths = [claude_file(claude, "due-a"), claude_file(claude, "due-b")]
    for index, path in enumerate(paths):
        write_jsonl(path, [filler, *claude_lines((10, f"base-{index}", 1))])
    _drain_ledger(ledger)
    for index, path in enumerate(paths):
        append_jsonl(path, claude_lines((20, f"tail-{index}", 2)))
    ledger.ingest(ALL, budget_bytes=32)  # persist both open-debt markers
    now[0] += ledger_ingest.DEADLINE_VERIFY_SECONDS

    original_plan = ledger._ingestor._plan
    appended = []

    def append_after_capped_scan(provider, path, info, cursor, budget, should_stop=None):
        plan = original_plan(provider, path, info, cursor, budget, should_stop)
        if ledger._ingestor._capped_scan_used:
            append_jsonl(path, [claude_response(30 + len(appended), f"during-{path.stem}-{len(appended)}", output=1)])
            appended.append(path)
        return plan

    monkeypatch.setattr(ledger._ingestor, "_plan", append_after_capped_scan)
    first = ledger.ingest(ALL, budget_bytes=32)
    assert first["bytesRead"] > 32
    assert first["cursorAdvanced"] is False
    assert first["verificationAdvanced"] is False
    assert appended == [paths[0]]
    state_a = store.load_source(ledger.conn, "Claude", ledger_ingest.path_key(paths[0]))["state"]
    assert state_a["verification_debt"]["next_attempt_epoch"] == now[0] + ledger_ingest.VERIFY_RETRY_START_SECONDS
    assert state_a["verification_debt"]["retry_delay_seconds"] == 120

    second = ledger.ingest(ALL, budget_bytes=32)
    assert second["bytesRead"] > 32
    assert appended == paths  # rotating order lets the other due source scan next
    state_b = store.load_source(ledger.conn, "Claude", ledger_ingest.path_key(paths[1]))["state"]
    assert state_b["verification_debt"]["retry_delay_seconds"] == 120

    now[0] += ledger_ingest.VERIFY_RETRY_START_SECONDS
    third = ledger.ingest(ALL, budget_bytes=32)
    assert third["bytesRead"] > 32
    assert appended == [paths[0], paths[1], paths[0]]
    state_a = store.load_source(ledger.conn, "Claude", ledger_ingest.path_key(paths[0]))["state"]
    assert state_a["verification_debt"]["retry_delay_seconds"] == 240


def test_verification_turn_survives_changing_due_set(tmp_path, monkeypatch):
    data, claude, codex = make_roots(tmp_path)
    now = [float(HOUR_START + 3600)]
    ledger = Ledger(data, claude_root=claude, codex_root=codex, clock=lambda: now[0])
    paths = [claude_file(claude, f"rotor-{name}") for name in ("a", "b", "c")]
    for index, path in enumerate(paths):
        write_jsonl(path, claude_lines((10, f"base-{index}", 1)))
    _drain_ledger(ledger)
    for index, path in enumerate(paths):
        append_jsonl(path, claude_lines((20, f"tail-{index}", 2)))
    ledger.ingest(ALL)
    for index, path in enumerate(paths):
        cursor = store.load_source(ledger.conn, "Claude", ledger_ingest.path_key(path))
        debt = cursor["state"]["verification_debt"]
        debt["last_change_epoch"] = now[0] - 200 if index in (0, 2) else now[0]
        with store.transaction(ledger.conn) as conn:
            store.save_source(conn, "Claude", ledger_ingest.path_key(path), generation=cursor["generation"],
                              byte_offset=cursor["byte_offset"], head_key=cursor["head_key"], state=cursor["state"])

    visits = []

    def record_due(provider, path, budget, fault, should_stop=None, *, block_events=False):
        visits.append(path)
        return int(budget or 32)

    monkeypatch.setattr(ledger._ingestor, "ingest_file", record_due)
    monkeypatch.setattr(ledger_ingest, "DEBT_BACKLOG_BYTES", 5000)
    ledger.ingest(ALL, budget_bytes=32)  # A and C due; B remains in backoff from idleness
    monkeypatch.setattr(ledger_ingest, "DEBT_BACKLOG_BYTES", 1)
    ledger.ingest(ALL, budget_bytes=32)  # B joins the due set
    monkeypatch.setattr(ledger_ingest, "DEBT_BACKLOG_BYTES", 5000)
    ledger.ingest(ALL, budget_bytes=32)  # C keeps its queued turn as B leaves eligibility
    assert visits == paths


def test_short_verification_read_is_charged_and_backed_off(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger_ingest, "_HASH_CHUNK", 64)
    data, claude, codex = make_roots(tmp_path)
    now = [float(HOUR_START + 3600)]
    ledger = Ledger(data, claude_root=claude, codex_root=codex, clock=lambda: now[0])
    path = claude_file(claude, "short-read")
    filler = {"type": "user", "message": {"content": "x" * 4000}}
    write_jsonl(path, [claude_response(1, "first", output=5), filler])
    _drain_ledger(ledger)
    append_jsonl(path, [claude_response(2, "tail", output=3)])
    ledger.ingest(ALL)
    now[0] += ledger_ingest.IDLE_VERIFY_SECONDS

    real_open = Path.open

    class ShortReader:
        def __init__(self, handle):
            self.handle = handle
            self.short = True

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.handle.__exit__(*args)

        def read(self, size=-1):
            if self.short and size > 1:
                self.short = False
                size -= 1
            return self.handle.read(size)

        def __getattr__(self, name):
            return getattr(self.handle, name)

    def short_open(self, mode="r", *args, **kwargs):
        handle = real_open(self, mode, *args, **kwargs)
        return ShortReader(handle) if self == path and "r" in mode else handle

    monkeypatch.setattr(Path, "open", short_open)
    result = ledger.ingest(ALL, budget_bytes=1024)
    assert 0 < result["bytesRead"] < 1024
    assert ledger.coverage()["readFailures"] == 1
    debt = store.load_source(ledger.conn, "Claude", ledger_ingest.path_key(path))["state"]["verification_debt"]
    assert debt["next_attempt_epoch"] == now[0] + ledger_ingest.VERIFY_RETRY_START_SECONDS


def test_failed_final_stat_is_recorded_even_if_followup_stat_succeeds(tmp_path, monkeypatch):
    data, claude, codex = make_roots(tmp_path)
    now = [float(HOUR_START + 3600)]
    ledger = Ledger(data, claude_root=claude, codex_root=codex, clock=lambda: now[0])
    path = claude_file(claude, "final-stat-failure")
    write_jsonl(path, [claude_response(1, "first", output=5)])
    _drain_ledger(ledger)
    append_jsonl(path, [claude_response(2, "tail", output=3)])
    ledger.ingest(ALL)
    now[0] += ledger_ingest.IDLE_VERIFY_SECONDS

    original_plan = ledger._ingestor._plan
    armed = [False]

    def arm_after_scan(*args, **kwargs):
        plan = original_plan(*args, **kwargs)
        armed[0] = bool(plan.get("verification_work"))
        return plan

    monkeypatch.setattr(ledger._ingestor, "_plan", arm_after_scan)
    original_stat = Path.stat

    def fail_once_after_scan(self, *args, **kwargs):
        if self == path and armed[0]:
            armed[0] = False
            raise OSError("injected final stat failure")
        return original_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", fail_once_after_scan)
    result = ledger.ingest(ALL, budget_bytes=1024)
    assert result["bytesRead"] > 0
    assert result["verificationAdvanced"] is False  # the scan result was discarded before commit
    assert result["cursorAdvanced"] is False
    debt = store.load_source(ledger.conn, "Claude", ledger_ingest.path_key(path))["state"]["verification_debt"]
    assert debt["retry_pending"] is True and "verified_stamp" not in debt
    assert result["actionableBytes"] == 0  # the only source is still in retry backoff
    assert ledger.coverage()["readFailures"] == 1
    debt = store.load_source(ledger.conn, "Claude", ledger_ingest.path_key(path))["state"]["verification_debt"]
    assert debt["next_attempt_epoch"] == now[0] + ledger_ingest.VERIFY_RETRY_START_SECONDS


@pytest.mark.parametrize("failure", ["raised-after-chunk", "short-first-scan"])
def test_first_scan_io_failure_persists_debt_bytes_and_retry(tmp_path, monkeypatch, failure):
    monkeypatch.setattr(ledger_ingest, "DEBT_BACKLOG_BYTES", 1)
    monkeypatch.setattr(ledger_ingest, "_HASH_CHUNK", 64 * 1024)
    data, claude, codex = make_roots(tmp_path)
    now = [float(HOUR_START + 3600)]
    ledger = Ledger(data, claude_root=claude, codex_root=codex, clock=lambda: now[0])
    path = claude_file(claude, "first-scan-io-error")
    filler = {"type": "user", "message": {"content": "x" * 150000}}
    write_jsonl(path, [claude_response(1, "first", output=5), filler])
    _drain_ledger(ledger)
    append_jsonl(path, [claude_response(2, "tail", output=3)])

    real_open = Path.open
    bytes_read = [0]

    class InjectedReader:
        def __init__(self, handle):
            self.handle = handle
            self.calls = 0

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.handle.__exit__(*args)

        def read(self, size=-1):
            self.calls += 1
            if failure == "raised-after-chunk" and self.calls == 2:
                raise OSError("injected verification read failure")
            if failure == "short-first-scan" and self.calls == 1 and size > 1:
                data = self.handle.read(size - 1)
                if size > ledger_ingest.HEAD_BYTES:
                    bytes_read[0] += len(data)
                return data
            data = self.handle.read(size)
            if size > ledger_ingest.HEAD_BYTES:
                bytes_read[0] += len(data)
            return data

        def __getattr__(self, name):
            return getattr(self.handle, name)

    def injected_open(self, mode="r", *args, **kwargs):
        handle = real_open(self, mode, *args, **kwargs)
        return InjectedReader(handle) if self == path and "r" in mode else handle

    monkeypatch.setattr(Path, "open", injected_open)
    result = ledger.ingest(ALL, budget_bytes=8 * 1024 * 1024)
    cursor = store.load_source(ledger.conn, "Claude", ledger_ingest.path_key(path))
    debt = cursor["state"]["verification_debt"]
    assert result["bytesRead"] == bytes_read[0] > 0
    assert ledger.coverage()["readFailures"] == 1
    assert debt["retry_pending"] is True
    assert debt["next_attempt_epoch"] == now[0] + ledger_ingest.VERIFY_RETRY_START_SECONDS
    assert cursor["state"]["verify"]


def test_quiet_codex_pass_source_state_loads_scale_linearly(tmp_path, monkeypatch):
    ledger, _, codex = open_ledger(tmp_path)
    count = 80
    for index in range(count):
        write_jsonl(codex_file(codex, f"quiet-{index:03d}"),
                    codex_meta(f"thread-{index}", 1) + [codex_count(2, input_tokens=10, output_tokens=0)])
    _drain_ledger(ledger)
    original_load = store.load_source
    loads = [0]

    def count_loads(*args, **kwargs):
        loads[0] += 1
        return original_load(*args, **kwargs)

    monkeypatch.setattr(store, "load_source", count_loads)
    result = ledger.ingest(ALL, budget_bytes=8 * 1024 * 1024)
    assert result["bytesRead"] == 0
    assert loads[0] <= 6 * count


def test_real_prefix_reads_follow_verification_schedule_not_pass_count(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger_ingest, "_HASH_CHUNK", 64 * 1024)
    data, claude, codex = make_roots(tmp_path)
    now = [float(HOUR_START + 3600)]
    ledger = Ledger(data, claude_root=claude, codex_root=codex, clock=lambda: now[0])
    path = claude_file(claude, "read-bound")
    filler = {"type": "user", "message": {"content": "x" * 60000}}
    write_jsonl(path, [filler for _ in range(40)])
    _drain_ledger(ledger, budget=8 * 1024 * 1024)
    prefix_bytes = store.load_source(ledger.conn, "Claude", ledger_ingest.path_key(path))["byte_offset"]

    bytes_read = [0]
    real_open = Path.open

    class CountingReader:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return self.handle.__exit__(*args)

        def read(self, size=-1):
            data = self.handle.read(size)
            bytes_read[0] += len(data)
            return data

        def __getattr__(self, name):
            return getattr(self.handle, name)

    def counted_open(self, mode="r", *args, **kwargs):
        handle = real_open(self, mode, *args, **kwargs)
        return CountingReader(handle) if self == path and "r" in mode else handle

    monkeypatch.setattr(Path, "open", counted_open)
    append_jsonl(path, [claude_response(10, "first-tail", output=1)])
    ledger.ingest(ALL, budget_bytes=8 * 1024 * 1024)
    for index in range(5):
        append_jsonl(path, [claude_response(20 + index, f"next-{index}", output=1)])
        ledger.ingest(ALL, budget_bytes=8 * 1024 * 1024)
    # Deferred passes read no prefix: only a bounded head probe per observed change (replacement detection).
    assert bytes_read[0] <= 6 * ledger_ingest.HEAD_BYTES

    now[0] += ledger_ingest.IDLE_VERIFY_SECONDS
    ledger.ingest(ALL, budget_bytes=8 * 1024 * 1024)
    assert prefix_bytes <= bytes_read[0] <= prefix_bytes + 1024 * 1024


def test_partial_last_line_waits_for_newline(tmp_path):
    ledger, claude, _ = open_ledger(tmp_path)
    path = claude_file(claude, "s1")
    first, second = claude_lines((10, "msg_a", 5), (20, "msg_b", 7))
    line1 = json.dumps(first) + "\n"
    line2 = json.dumps(second) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(line1 + line2[:25], encoding="utf-8")
    ledger.ingest(ALL)
    assert total_requests(ledger) == 1
    assert ledger.coverage()["pendingBytes"] > 0
    path.write_text(line1 + line2, encoding="utf-8")
    ledger.ingest(ALL)
    assert total_requests(ledger) == 1
    ledger._ingestor.clock = clock_at(3720)
    ledger.clock = clock_at(3720)
    ledger.ingest(ALL)
    assert total_requests(ledger) == 2


@pytest.mark.parametrize("stage", ["after_facts", "after_cursor"])
def test_crash_inside_transaction_counts_nothing_twice_or_loses(tmp_path, stage):
    ledger, claude, _ = open_ledger(tmp_path)
    write_jsonl(claude_file(claude, "s1"), claude_lines((10, "msg_a", 5), (20, "msg_b", 7), (30, "msg_c", 9)))

    def crash(name):
        if name == stage:
            raise RuntimeError("simulated crash")

    with pytest.raises(RuntimeError):
        ledger.ingest(ALL, fault=crash)
    assert total_requests(ledger) == 0
    assert ledger.coverage()["observedFrom"] is None
    ledger.ingest(ALL)
    assert total_requests(ledger) == 3
    ledger.ingest(ALL)
    assert total_requests(ledger) == 3


def test_concurrent_readers_see_consistent_snapshots(tmp_path):
    ledger, claude, _ = open_ledger(tmp_path)
    write_jsonl(claude_file(claude, "s1"), [claude_response(i % 3000, f"msg_{i}", output=i + 1) for i in range(300)])
    errors: list[object] = []
    checked = [0]
    stop = threading.Event()

    def reader():
        conn = store.connect(ledger.db_path)
        try:
            while not stop.is_set():
                with store.snapshot(conn):
                    facts, rollups = store.fact_and_rollup_totals(conn)
                if facts != rollups:
                    errors.append((facts, rollups))
                checked[0] += 1
        except Exception as exc:  # any reader failure fails the test
            errors.append(repr(exc))
        finally:
            conn.close()

    readers = [threading.Thread(target=reader) for _ in range(2)]
    for thread in readers:
        thread.start()
    try:
        for _ in range(1000):
            result = ledger.ingest(ALL, budget_bytes=3000)
            if result["pendingBytes"] == 0:
                break
    finally:
        stop.set()
        for thread in readers:
            thread.join()
    assert errors == []
    assert checked[0] > 0
    assert total_requests(ledger) == 300


def test_corrupt_database_is_reported_quarantined_and_rebuilt(tmp_path):
    data, claude, codex = make_roots(tmp_path)
    write_jsonl(claude_file(claude, "s1"), claude_lines((10, "msg_a", 5), (20, "msg_b", 7)))
    data.mkdir(parents=True)
    (data / store.DB_NAME).write_bytes(b"this is not a sqlite database" * 300)
    ledger = Ledger(data, claude_root=claude, codex_root=codex, clock=clock_at(3600))
    health = ledger.health()
    assert health["status"] == "rebuilt"
    assert health["recovery"]["reason"] in {"unreadable", "corrupt"}
    assert (data / health["recovery"]["quarantined"]).exists()
    ledger.ingest(ALL)
    assert total_requests(ledger) == 2


def test_collection_policy_gates_reads(tmp_path):
    ledger, claude, codex = open_ledger(tmp_path)
    write_jsonl(claude_file(claude, "s1"), claude_lines((10, "msg_a", 5)))
    write_jsonl(codex_file(codex, "T1"), codex_meta("T1", 10) + [codex_count(
        11, input_tokens=10, output_tokens=5, total=15,
        last={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})])
    ledger.ingest({"Claude": False, "Codex": True})
    assert ledger._ingestor.stats["sourceReads"] == 1  # the Codex file only
    rows = summary(ledger)["rows"]
    assert {row["provider"] for row in rows} == {"Codex"}
    assert ledger.coverage()["providersNotCollected"] == ["Claude"]
    ledger.ingest({})  # fail closed: nothing collected, nothing read
    assert ledger._ingestor.stats["sourceReads"] == 1


def test_show_policy_hides_provider_history(tmp_path):
    ledger, claude, codex = open_ledger(tmp_path)
    write_jsonl(claude_file(claude, "s1"), claude_lines((10, "msg_a", 5)))
    write_jsonl(codex_file(codex, "T1"), codex_meta("T1", 10) + [codex_count(
        11, input_tokens=10, output_tokens=5, total=15,
        last={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})])
    ledger.ingest(ALL)
    shown = summary(ledger, show={"Claude": False, "Codex": True})
    assert {row["provider"] for row in shown["rows"]} == {"Codex"}
    assert shown["totals"]["totalTokens"] == 15


def test_budgeted_passes_converge_to_full_result(tmp_path):
    ledger, claude, _ = open_ledger(tmp_path)
    write_jsonl(claude_file(claude, "s1"), [claude_response(i, f"msg_{i}", output=i + 1) for i in range(60)])
    passes = 0
    while True:
        passes += 1
        result = ledger.ingest(ALL, budget_bytes=1500)
        if result["pendingBytes"] == 0:
            break
        assert passes < 500
    assert passes > 1
    assert total_requests(ledger) == 60


def test_purge_removes_one_provider(tmp_path):
    ledger, claude, codex = open_ledger(tmp_path)
    write_jsonl(claude_file(claude, "s1"), claude_lines((10, "msg_a", 5)))
    write_jsonl(codex_file(codex, "T1"), codex_meta("T1", 10) + [codex_count(
        11, input_tokens=10, output_tokens=5, total=15,
        last={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15})])
    ledger.ingest(ALL)
    ledger.purge("Claude")
    assert {row["provider"] for row in summary(ledger)["rows"]} == {"Codex"}
    assert ledger.coverage()["gapCount"] == 0


def test_rebuild_rollups_reproduces_summary(tmp_path):
    ledger, claude, _ = open_ledger(tmp_path)
    write_jsonl(claude_file(claude, "s1"), [claude_response(10, "msg_1", output=5),
                                            claude_response(12, "msg_1", output=20)])
    ledger.ingest(ALL)
    before = summary(ledger)["rows"]
    ledger.rebuild_rollups()
    assert summary(ledger)["rows"] == before


def test_override_applies_and_clears(tmp_path):
    ledger, claude, _ = open_ledger(tmp_path)
    write_jsonl(claude_file(claude, "s1"), claude_lines((10, "msg_a", 5)))
    ledger.ingest(ALL)
    assert summary(ledger)["rows"][0]["kind"] == "lead"
    ledger.set_override("Claude", "s1", "build")
    row = summary(ledger)["rows"][0]
    assert row["kind"] == "build"
    assert row["classification"] == {"status": "override", "source": "override"}
    ledger.clear_override("Claude", "s1")
    assert summary(ledger)["rows"][0]["kind"] == "lead"
    with pytest.raises(ValueError):
        ledger.set_override("Claude", "s1", "banana")


def test_migration_hook_runs_forward(tmp_path, monkeypatch):
    next_version = max(store.MIGRATIONS) + 1
    monkeypatch.setitem(store.MIGRATIONS, next_version, ("CREATE TABLE IF NOT EXISTS probe(x INTEGER)",))
    ledger, _, _ = open_ledger(tmp_path)
    assert ledger.health()["schemaVersion"] == next_version
    assert ledger.conn.execute("SELECT name FROM sqlite_master WHERE name='probe'").fetchone() is not None


def test_newer_schema_is_refused(tmp_path):
    ledger, _, _ = open_ledger(tmp_path)
    ledger.conn.execute("UPDATE meta SET value='99' WHERE key='schemaVersion'")
    ledger.close()
    with pytest.raises(LedgerSchemaError):
        open_ledger(tmp_path)


def test_coverage_states(tmp_path):
    ledger, claude, _ = open_ledger(tmp_path)
    assert ledger.coverage()["status"] == "empty"
    write_jsonl(claude_file(claude, "s1"), claude_lines((10, "msg_a", 5)))
    ledger.ingest(ALL)
    coverage = ledger.coverage()
    assert coverage["status"] == "complete"
    assert coverage["observedFrom"] is not None
    assert coverage["pendingBytes"] == 0


def test_replayed_older_codex_source_preserves_active_counter(tmp_path):
    ledger, _, codex = open_ledger(tmp_path)
    a, b = codex_file(codex, "a"), codex_file(codex, "b")
    first = codex_meta("T1", 1) + [codex_count(2, input_tokens=160, output_tokens=0)]
    second = codex_meta("T1", 3) + [codex_count(4, input_tokens=180, output_tokens=0)]
    write_jsonl(a, first)
    ledger.ingest(ALL)
    write_jsonl(b, second)
    ledger.ingest(ALL)
    # Rewrite the older source, including its already counted event and an unseen reset.
    write_jsonl(a, first + [codex_count(5, input_tokens=100, output_tokens=0)])
    a.write_bytes(b"\n" + a.read_bytes())
    ledger.ingest(ALL)
    append_jsonl(b, [codex_count(6, input_tokens=185, output_tokens=0)])
    ledger.ingest(ALL)
    assert summary(ledger)["totals"]["totalTokens"] == 180  # neither source tail is confirmed while debt is open
    ledger._ingestor.clock = clock_at(3720)
    ledger.clock = clock_at(3720)
    _drain_ledger(ledger)
    assert summary(ledger)["totals"]["totalTokens"] == 185
    detail = ledger.conn.execute("SELECT thread_id, interval_start, interval_end FROM ledger_gap_details").fetchone()
    assert detail[0] == "T1" and detail[1] <= detail[2]
    assert ledger.conn.execute("SELECT COUNT(*) FROM gaps WHERE kind='ambiguous-reset'").fetchone()[0] == 1
    gap = next(gap for gap in ledger.coverage()["gaps"] if gap["kind"] == "ambiguous-reset")
    assert gap["threadId"] == "T1"
    assert gap["intervalStart"] <= gap["intervalEnd"]
    ledger.close()
    ledger = Ledger(tmp_path / "data", claude_root=tmp_path / "claude", codex_root=codex, clock=clock_at(3600))
    assert any(gap["kind"] == "ambiguous-reset" and gap["threadId"] == "T1"
               for gap in ledger.coverage()["gaps"])


def test_prefix_verification_resumes_and_detects_middle_edit_with_append(tmp_path):
    ledger, claude, _ = open_ledger(tmp_path)
    path = claude_file(claude, "s1")
    filler = {"type": "user", "message": {"content": "x" * 70000}}
    lines = [filler, *claude_lines((10, "msg_a", 5)), filler, *claude_lines((20, "msg_b", 7))]
    write_jsonl(path, lines)
    while ledger.ingest(ALL)["pendingBytes"]:
        pass
    assert total_requests(ledger) == 2
    original = path.read_bytes()
    old = b'"output_tokens": 5'
    assert old in original
    path.write_bytes(original.replace(old, b'"output_tokens": 9', 1))
    append_jsonl(path, claude_lines((30, "msg_c", 3)))
    first = ledger.ingest(ALL, budget_bytes=32768)
    assert first["bytesRead"] <= 32768
    assert first["pendingBytes"] > 0
    assert int(store.meta_value(ledger.conn, "verifyingBytes:Claude")) > 0
    assert total_requests(ledger) == 2
    append_jsonl(path, claude_lines((40, "msg_d", 4)))  # change while verification is pending
    for _ in range(20):
        result = ledger.ingest(ALL, budget_bytes=32768)
    assert total_requests(ledger) == 2  # continued growth resets idle time without exposing tail facts
    ledger._ingestor.clock = clock_at(3720)
    ledger.clock = clock_at(3720)
    for _ in range(100):
        result = ledger.ingest(ALL, budget_bytes=32768)
        if not result["pendingBytes"]:
            break
    assert result["pendingBytes"] == 0
    assert ledger.coverage()["replacements"] == 1
    assert total_requests(ledger) == 4


def test_verification_honors_small_budget_and_restarts_partial_chunk(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger_ingest, "_HASH_CHUNK", 32)
    ledger, claude, codex = open_ledger(tmp_path)
    path = claude_file(claude, "s1")
    write_jsonl(path, claude_lines((10, "msg_a", 5), (20, "msg_b", 7)))
    while ledger.ingest(ALL)["pendingBytes"]:
        pass
    append_jsonl(path, claude_lines((30, "msg_c", 9)))
    ledger.ingest(ALL, budget_bytes=3)  # first observation opens debt
    ledger._ingestor.clock = clock_at(3720)
    ledger.clock = clock_at(3720)
    first = ledger.ingest(ALL, budget_bytes=3)
    assert first["bytesRead"] <= 3
    cursor = store.load_source(ledger.conn, "Claude", ledger_ingest.path_key(path))
    assert not cursor["state"]["verify"]["boundary"]
    ledger.close()
    ledger = Ledger(tmp_path / "data", claude_root=claude, codex_root=codex, clock=clock_at(3600))
    ledger._ingestor.clock = clock_at(3720)
    ledger.clock = clock_at(3720)
    second = ledger.ingest(ALL, budget_bytes=3)
    assert second["bytesRead"] <= 3
    cursor = store.load_source(ledger.conn, "Claude", ledger_ingest.path_key(path))
    assert not cursor["state"]["verify"]["boundary"]
    tail_bytes = path.stat().st_size - cursor["byte_offset"]
    for _ in range(1000):
        result = ledger.ingest(ALL, budget_bytes=3)
        cursor = store.load_source(ledger.conn, "Claude", ledger_ingest.path_key(path))
        if not cursor["state"].get("verify"):
            # The pass that completes exact verification may finish one supported tail record.
            assert result["bytesRead"] <= 3 + tail_bytes
            break
        assert result["bytesRead"] <= 3
    else:
        pytest.fail("verification did not finish")
    _drain_ledger(ledger, budget=3)
    assert total_requests(ledger) == 3


def test_continuous_appends_remain_unconfirmed_until_verification_is_due(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger_ingest, "_HASH_CHUNK", 32)
    ledger, claude, _ = open_ledger(tmp_path)
    path = claude_file(claude, "continuous")
    write_jsonl(path, [claude_response(1, "first", output=1)])
    ledger.ingest(ALL)
    for index in range(30):
        append_jsonl(path, [claude_response(2 + index, f"next-{index}", output=1)])
        ledger.ingest(ALL, budget_bytes=16)
    assert total_requests(ledger) == 1
    cursor = store.load_source(ledger.conn, "Claude", ledger_ingest.path_key(path))
    assert cursor["state"]["verification_debt"]["open"] is True
    assert ledger.conn.execute("SELECT COUNT(*) FROM provisional_facts").fetchone()[0] == 0


def test_successful_idle_scans_advance_each_new_tail_in_the_same_plan(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger_ingest, "DEBT_BACKLOG_BYTES", 1)
    ledger, claude, _ = open_ledger(tmp_path)
    path = claude_file(claude, "append-between-scans")
    write_jsonl(path, [claude_response(1, "base", output=1)])
    _drain_ledger(ledger)
    cursor = store.load_source(ledger.conn, "Claude", ledger_ingest.path_key(path))
    for index in range(8):
        old_offset = cursor["byte_offset"]
        append_jsonl(path, [claude_response(10 + index, f"tail-{index}", output=1)])
        result = ledger.ingest(ALL, budget_bytes=1024 * 1024)
        cursor = store.load_source(ledger.conn, "Claude", ledger_ingest.path_key(path))
        assert cursor["byte_offset"] > old_offset
        assert "verification_debt" not in cursor["state"]
        assert result["cursorAdvanced"] is True
        assert total_requests(ledger) == index + 2


def test_verification_finishes_one_supported_record_larger_than_tail_reserve(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger_ingest, "DEBT_BACKLOG_BYTES", 1)
    ledger, claude, _ = open_ledger(tmp_path)
    path = claude_file(claude, "large-supported-record")
    write_jsonl(path, [claude_response(1, "base", output=1)])
    _drain_ledger(ledger)
    budget = 8 * 1024 * 1024
    cursor = store.load_source(ledger.conn, "Claude", ledger_ingest.path_key(path))

    for index in range(2):
        old_offset = cursor["byte_offset"]
        append_jsonl(path, [{"type": "user", "message": {"content": "x" * (1024 * 1024 + 128 * 1024)}}])
        result = ledger.ingest(ALL, budget_bytes=budget)
        cursor = store.load_source(ledger.conn, "Claude", ledger_ingest.path_key(path))
        assert cursor["byte_offset"] > old_offset
        assert cursor["byte_offset"] == path.stat().st_size
        assert "verification_debt" not in cursor["state"]
        assert result["cursorAdvanced"] is True
        assert total_requests(ledger) == 1


@pytest.mark.parametrize("remove", [False, True])
def test_lowered_or_removed_confirmed_usage_is_retained_and_reported(tmp_path, remove):
    ledger, claude, _ = open_ledger(tmp_path)
    path = claude_file(claude, "rewrite-confirmed")
    write_jsonl(path, [claude_response(1, "confirmed", output=9)])
    ledger.ingest(ALL)
    replacement = [] if remove else [claude_response(1, "confirmed", output=5)]
    write_jsonl(path, replacement + [claude_response(2, "new", output=3)])
    ledger.ingest(ALL, budget_bytes=256)  # observe debt before advancing the idle clock
    ledger._ingestor.clock = clock_at(3720)
    ledger.clock = clock_at(3720)
    for _ in range(100):
        if not ledger.ingest(ALL, budget_bytes=256)["pendingBytes"]:
            break
    else:
        pytest.fail("rewrite replay did not finish")
    assert summary(ledger)["totals"]["outputTotal"] == 12
    assert any(gap["kind"] == "rewrite-lowered-confirmed" for gap in ledger.coverage()["gaps"])


def test_replacement_replay_preserves_append_after_detected_boundary(tmp_path):
    ledger, _, codex = open_ledger(tmp_path)
    path = codex_file(codex, 'T1')
    initial = codex_meta('T1', 1) + [codex_count(2, input_tokens=180, output_tokens=0)]
    write_jsonl(path, initial)
    ledger.ingest(ALL)
    path.write_bytes(b'\n' + path.read_bytes())
    ledger.ingest(ALL)
    ledger.close()
    ledger = Ledger(tmp_path / 'data', claude_root=tmp_path / 'claude', codex_root=codex, clock=clock_at(3600))
    append_jsonl(path, [codex_count(3, input_tokens=40, output_tokens=0), codex_count(4, input_tokens=45, output_tokens=0)])
    ledger.ingest(ALL)
    assert summary(ledger)['totals']['totalTokens'] == 180
    ledger._ingestor.clock = clock_at(3720)
    ledger.clock = clock_at(3720)
    _drain_ledger(ledger)
    assert summary(ledger)['totals']['totalTokens'] == 225


def test_clean_append_then_rewrite_does_not_reuse_the_append_boundary(tmp_path):
    data, claude, codex = make_roots(tmp_path)
    now = [float(HOUR_START + 3600)]
    ledger = Ledger(data, claude_root=claude, codex_root=codex, clock=lambda: now[0])
    path = codex_file(codex, 'clean-then-rewrite')
    initial = codex_meta('T1', 1) + [codex_count(2, input_tokens=180, output_tokens=0)]
    write_jsonl(path, initial)
    ledger.ingest(ALL)

    append_jsonl(path, [codex_count(3, input_tokens=190, output_tokens=0)])
    ledger.ingest(ALL)  # clean append opens debt, not replacement evidence
    cursor = store.load_source(ledger.conn, 'Codex', ledger_ingest.path_key(path))
    assert cursor['state']['verification_debt'].get('replacement_observed', False) is False

    replacement = b'\n' + b''.join((json.dumps(item) + '\n').encode() for item in
                                    initial + [codex_count(4, input_tokens=40, output_tokens=0),
                                              codex_count(5, input_tokens=45, output_tokens=0)])
    path.write_bytes(replacement)
    ledger.ingest(ALL)  # this stamp is the first observed replacement boundary
    cursor = store.load_source(ledger.conn, 'Codex', ledger_ingest.path_key(path))
    assert cursor['state']['verification_debt']['replacement_observed'] is True
    boundary = cursor['state']['verification_debt']['replacement_boundary']
    assert boundary == path.stat().st_size

    now[0] += ledger_ingest.IDLE_VERIFY_SECONDS
    _drain_ledger(ledger)
    assert summary(ledger)['totals']['totalTokens'] == 180


@pytest.mark.parametrize('valid_first', [True, False])
def test_matching_older_incomplete_copy_resolves_in_both_orders(tmp_path, valid_first):
    ledger, claude, _ = open_ledger(tmp_path)
    path = claude_file(claude, 's1')
    valid = claude_response(20, 'same', output=5)
    missing = claude_response(10, 'same', output=0)
    del missing['message']['usage']
    write_jsonl(path, [valid if valid_first else missing])
    ledger.ingest(ALL)
    append_jsonl(path, [missing if valid_first else valid])
    ledger.ingest(ALL)
    ledger._ingestor.clock = clock_at(3720)
    ledger.clock = clock_at(3720)
    _drain_ledger(ledger)
    assert ledger.conn.execute('SELECT COUNT(*) FROM ledger_omissions').fetchone()[0] == 0


def test_later_incomplete_revision_remains_unresolved(tmp_path):
    ledger, claude, _ = open_ledger(tmp_path)
    path = claude_file(claude, 's1')
    missing = claude_response(30, 'same', output=0)
    del missing['message']['usage']
    write_jsonl(path, [missing, claude_response(20, 'same', output=5)])
    ledger.ingest(ALL)
    assert ledger.conn.execute('SELECT COUNT(*) FROM ledger_omissions').fetchone()[0] == 1


def test_replay_endpoint_persists_until_replacement_interval_finishes(tmp_path):
    ledger, _, codex = open_ledger(tmp_path)
    path = codex_file(codex, 'T1')
    original = codex_meta('T1', 1) + [codex_count(2, input_tokens=180, output_tokens=0)]
    write_jsonl(path, original)
    ledger.ingest(ALL)
    write_jsonl(path, codex_meta('T1', 1) + [codex_count(2, input_tokens=180, output_tokens=0),
                                              codex_count(3, input_tokens=100, output_tokens=0)])
    path.write_bytes(b'\n' + path.read_bytes())
    endpoint = path.stat().st_size
    ledger.ingest(ALL, budget_bytes=1)  # first pass persists the debt marker
    ledger._ingestor.clock = clock_at(3720)
    ledger.clock = clock_at(3720)
    for _ in range(1000):
        ledger.ingest(ALL, budget_bytes=1)
        cursor = store.load_source(ledger.conn, 'Codex', ledger_ingest.path_key(path))
        if cursor['state'].get('replay_end') is not None:
            break
    assert cursor['state']['replay_end'] == endpoint
    ledger.close()
    append_jsonl(path, [codex_count(4, input_tokens=40, output_tokens=0), codex_count(5, input_tokens=45, output_tokens=0)])
    ledger = Ledger(tmp_path / 'data', claude_root=tmp_path / 'claude', codex_root=codex, clock=clock_at(3600))
    ledger.ingest(ALL, budget_bytes=100)
    ledger._ingestor.clock = clock_at(3720)
    ledger.clock = clock_at(3720)
    for _ in range(2000):
        result = ledger.ingest(ALL, budget_bytes=100)
        if not result['pendingBytes']:
            break
    assert result['pendingBytes'] == 0
    assert summary(ledger)['totals']['totalTokens'] == 225
    assert ledger.conn.execute("SELECT COUNT(*) FROM gaps WHERE kind='ambiguous-reset'").fetchone()[0] == 1


def test_incomplete_copy_without_timestamp_remains_unresolved(tmp_path):
    ledger, claude, _ = open_ledger(tmp_path)
    path = claude_file(claude, 's1')
    missing = claude_response(10, 'same', output=0)
    del missing['message']['usage']
    del missing['timestamp']
    write_jsonl(path, [missing, claude_response(20, 'same', output=5)])
    ledger.ingest(ALL)
    assert ledger.conn.execute('SELECT COUNT(*) FROM ledger_omissions').fetchone()[0] == 1


def _drain_ledger(ledger, budget=4096):
    for _ in range(1000):
        if not ledger.ingest(ALL, budget_bytes=budget)['pendingBytes']:
            return
        debts = [json.loads(row[0]).get('verification_debt')
                 for row in ledger.conn.execute('SELECT state FROM sources')]
        due_at = max((max(float(debt.get('last_change_epoch', 0)) + ledger_ingest.IDLE_VERIFY_SECONDS,
                          float(debt.get('next_attempt_epoch', 0)))
                      for debt in debts if debt), default=0)
        now = ledger._ingestor.clock()
        if due_at > now:
            ledger._ingestor.clock = lambda due_at=due_at: due_at
            ledger.clock = ledger._ingestor.clock
    pytest.fail('ingestion did not converge')


def _await_deferred_exact(ledger, expected_output):
    confirmed_before = summary(ledger)['totals']['outputTotal']
    debt_seen = False
    for _ in range(100):
        ledger.ingest(ALL, budget_bytes=1024)
        sources = ledger.conn.execute("SELECT state FROM sources").fetchall()
        if any(json.loads(row[0]).get('verification_debt') for row in sources):
            debt_seen = True
            break
    assert debt_seen or summary(ledger)['totals']['outputTotal'] == expected_output
    assert summary(ledger)['totals']['outputTotal'] == confirmed_before
    assert ledger.conn.execute('SELECT COUNT(*) FROM provisional_facts').fetchone()[0] == 0
    now = ledger._ingestor.clock()
    ledger._ingestor.clock = lambda: now + ledger_ingest.IDLE_VERIFY_SECONDS
    ledger.clock = ledger._ingestor.clock
    for _ in range(1000):
        ledger.ingest(ALL, budget_bytes=1024)
        if summary(ledger)['totals']['outputTotal'] == expected_output:
            assert ledger.conn.execute('SELECT COUNT(*) FROM provisional_facts').fetchone()[0] == 0
            return
    pytest.fail('deferred exact verification did not confirm appended usage')


def test_deferred_claude_revision_is_discarded_if_rewritten_before_verification(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger_ingest, '_HASH_CHUNK', 64)
    ledger, claude, _ = open_ledger(tmp_path)
    a, b = claude_file(claude, 'a'), claude_file(claude, 'b')
    filler = {'type': 'user', 'message': {'content': 'x' * 30000}}
    confirmed = claude_response(1, 'shared', output=5)
    write_jsonl(a, [confirmed, filler])
    write_jsonl(b, [confirmed])
    _drain_ledger(ledger)
    append_jsonl(a, [claude_response(2, 'shared', output=9)])
    ledger.ingest(ALL)
    assert summary(ledger)['totals']['outputTotal'] == 5
    assert ledger.conn.execute('SELECT COUNT(*) FROM provisional_facts').fetchone()[0] == 0
    # Replace the unseen tail before it becomes eligible; the independent confirmed copy survives.
    write_jsonl(a, [confirmed, filler, claude_response(3, 'new', output=3)])
    ledger._ingestor.clock = clock_at(3720)
    ledger.clock = clock_at(3720)
    _drain_ledger(ledger)
    assert summary(ledger)['totals']['outputTotal'] == 8
    assert total_requests(ledger) == 2
    ledger.rebuild_rollups()
    assert summary(ledger)['totals']['outputTotal'] == 8


def test_clean_provisional_promotion_survives_reopen_and_later_rewrite(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger_ingest, '_HASH_CHUNK', 64)
    ledger, claude, codex = open_ledger(tmp_path)
    path = claude_file(claude, 'promote')
    filler = {'type': 'user', 'message': {'content': 'x' * 30000}}
    first = claude_response(1, 'first', output=5)
    later = claude_response(2, 'later', output=9)
    write_jsonl(path, [first, filler])
    _drain_ledger(ledger)
    append_jsonl(path, [later])
    _await_deferred_exact(ledger, 14)
    _drain_ledger(ledger)
    assert summary(ledger)['coverage']['verifyingBytes'] == 0
    ledger.close()
    ledger = Ledger(tmp_path / 'data', claude_root=claude, codex_root=codex, clock=clock_at(3600))
    write_jsonl(path, [first, filler, claude_response(2, 'later', output=3)])
    _drain_ledger(ledger)
    assert summary(ledger)['totals']['outputTotal'] == 14
    assert any(g['kind'] == 'rewrite-lowered-confirmed' for g in ledger.coverage()['gaps'])


def test_stamp_change_after_verification_checkpoint_retries_before_replay(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger_ingest, '_HASH_CHUNK', 64)
    data, claude, codex = make_roots(tmp_path)
    now = [float(HOUR_START + 3600)]
    ledger = Ledger(data, claude_root=claude, codex_root=codex, clock=lambda: now[0])
    path = claude_file(claude, 'moving')
    filler = {'type': 'user', 'message': {'content': 'x' * 70000}}
    first = claude_response(1, 'first', output=5)
    write_jsonl(path, [first, filler])
    _drain_ledger(ledger)
    append_jsonl(path, [claude_response(2, 'discard', output=9)])
    ledger.ingest(ALL, budget_bytes=32768)
    assert summary(ledger)['totals']['outputTotal'] == 5
    now[0] += ledger_ingest.IDLE_VERIFY_SECONDS
    checkpointed = ledger.ingest(ALL, budget_bytes=32768)
    assert checkpointed['verificationAdvanced'] is True
    assert checkpointed['verificationRemaining'] > 32768
    cursor = store.load_source(ledger.conn, 'Claude', ledger_ingest.path_key(path))
    assert cursor['state']['verify']['boundary'] is True
    assert summary(ledger)['totals']['outputTotal'] == 5

    current = path.read_bytes()
    current = current.replace(b'"output_tokens": 5', b'"output_tokens": 6', 1)
    current = current.replace(b'"output_tokens": 9', b'"output_tokens": 3', 1)
    path.write_bytes(current)
    append_jsonl(path, [claude_response(4, 'more', output=2)])
    aborted = ledger.ingest(ALL, budget_bytes=32768)
    cursor = store.load_source(ledger.conn, 'Claude', ledger_ingest.path_key(path))
    assert 'verify' not in cursor['state']
    assert cursor['state']['verification_debt']['next_attempt_epoch'] == now[0] + ledger_ingest.VERIFY_RETRY_START_SECONDS
    assert aborted['verificationAdvanced'] is False
    assert aborted['cursorAdvanced'] is False
    assert aborted['actionableBytes'] == 0
    assert summary(ledger)['totals']['outputTotal'] == 5

    now[0] += ledger_ingest.VERIFY_RETRY_START_SECONDS
    _drain_ledger(ledger)
    assert summary(ledger)['totals']['outputTotal'] == 11
    assert total_requests(ledger) == 3


def test_codex_deferred_tail_preserves_shared_thread_baseline_and_tag(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger_ingest, '_HASH_CHUNK', 64)
    ledger, _, codex = open_ledger(tmp_path)
    a, b = codex_file(codex, 'a'), codex_file(codex, 'b')
    filler = {'type': 'unknown', 'payload': {'padding': 'x' * 30000}}
    initial = codex_meta('T1', 1, prompt='kind: review') + [codex_count(2, input_tokens=100, output_tokens=0), filler]
    write_jsonl(a, initial)
    _drain_ledger(ledger)
    write_jsonl(b, codex_meta('T1', 3) + [codex_count(4, input_tokens=160, output_tokens=0)])
    _drain_ledger(ledger)
    tag = codex_meta('T1', 5, prompt='kind: build')[-1]
    append_jsonl(a, [tag, codex_count(6, input_tokens=180, output_tokens=0)])
    for _ in range(100):
        ledger.ingest(ALL, budget_bytes=1024)
        snap = summary(ledger)
        if snap['coverage']['status'] == 'verifying':
            # Codex tails wait for prefix verification, so the appended 180 is not counted yet.
            assert snap['totals']['totalTokens'] == 160
            break
    else:
        pytest.fail('Codex append did not reach verification')
    assert ledger.conn.execute("SELECT COUNT(*) FROM provisional_facts WHERE provider='Codex'").fetchone()[0] == 0
    write_jsonl(a, initial + [{'type': 'unknown', 'payload': {'changed': True}}])
    ledger._ingestor.clock = clock_at(3720)
    ledger.clock = clock_at(3720)
    _drain_ledger(ledger)
    append_jsonl(b, [codex_count(7, input_tokens=165, output_tokens=0)])
    ledger.ingest(ALL)
    assert summary(ledger)['totals']['totalTokens'] == 160
    ledger._ingestor.clock = clock_at(3840)
    ledger.clock = clock_at(3840)
    _drain_ledger(ledger)
    snap = summary(ledger)
    assert snap['totals']['totalTokens'] == 165
    assert {row['kind'] for row in snap['rows']} == {'review'}


@pytest.mark.parametrize('stage', ['after_facts', 'after_cursor'])
def test_crash_in_debt_closing_transaction_is_atomic(tmp_path, monkeypatch, stage):
    monkeypatch.setattr(ledger_ingest, '_HASH_CHUNK', 64)
    ledger, claude, codex = open_ledger(tmp_path)
    path = claude_file(claude, 'crash-promotion')
    filler = {'type': 'user', 'message': {'content': 'x' * 30000}}
    first = claude_response(1, 'first', output=5)
    later = claude_response(2, 'later', output=9)
    write_jsonl(path, [first, filler])
    _drain_ledger(ledger)
    append_jsonl(path, [later])
    ledger.ingest(ALL, budget_bytes=1024)
    assert summary(ledger)['totals']['outputTotal'] == 5
    ledger._ingestor.clock = clock_at(3720)
    ledger.clock = clock_at(3720)

    def crash(name):
        if name == stage:
            raise RuntimeError('debt close crash')

    with pytest.raises(RuntimeError, match='debt close crash'):
        ledger.ingest(ALL, budget_bytes=None, fault=crash)
    cursor = store.load_source(ledger.conn, 'Claude', ledger_ingest.path_key(path))
    assert cursor['state']['verification_debt']['open'] is True
    assert summary(ledger)['totals']['outputTotal'] == 5
    assert ledger.conn.execute('SELECT COUNT(*) FROM provisional_facts').fetchone()[0] == 0
    ledger.close()
    ledger = Ledger(tmp_path / 'data', claude_root=claude, codex_root=codex, clock=clock_at(3600))
    assert ledger.coverage()['status'] == 'verifying'
    assert summary(ledger)['totals']['outputTotal'] == 5
    _drain_ledger(ledger)
    assert summary(ledger)['totals']['outputTotal'] == 14
    write_jsonl(path, [first, filler, claude_response(2, 'later', output=3)])
    _drain_ledger(ledger)
    assert summary(ledger)['totals']['outputTotal'] == 14
    assert any(gap['kind'] == 'rewrite-lowered-confirmed' for gap in ledger.coverage()['gaps'])


def _omission_count(ledger) -> int:
    return ledger.conn.execute("SELECT COUNT(*) FROM ledger_omissions").fetchone()[0]


def test_codex_tail_is_held_until_exact_prefix_verification_then_counts_once(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger_ingest, '_HASH_CHUNK', 64)
    ledger, _, codex = open_ledger(tmp_path)
    path = codex_file(codex, 'T1')
    filler = {'type': 'unknown', 'payload': {'padding': 'x' * 30000}}
    write_jsonl(path, codex_meta('T1', 1) + [codex_count(2, input_tokens=100, output_tokens=0), filler])
    _drain_ledger(ledger)
    append_jsonl(path, [codex_count(3, input_tokens=180, output_tokens=0)])
    ledger.ingest(ALL, budget_bytes=1024)
    assert summary(ledger)['totals']['totalTokens'] == 100
    assert ledger.coverage()['status'] == 'verifying'
    assert ledger.conn.execute("SELECT COUNT(*) FROM provisional_facts WHERE provider='Codex'").fetchone()[0] == 0
    ledger._ingestor.clock = clock_at(3720)
    ledger.clock = clock_at(3720)
    for _ in range(1000):
        ledger.ingest(ALL, budget_bytes=1024)
        cursor = store.load_source(ledger.conn, 'Codex', ledger_ingest.path_key(path))
        if cursor['state'].get('verification_debt') or cursor['state'].get('verify'):
            assert summary(ledger)['totals']['totalTokens'] == 100
            assert store.load_thread(ledger.conn, 'Codex', 'T1')['counters']['total_tokens'] == 100
            assert ledger.conn.execute("SELECT COUNT(*) FROM provisional_facts WHERE provider='Codex'").fetchone()[0] == 0
        elif cursor['byte_offset'] >= path.stat().st_size:
            break
    else:
        pytest.fail('Codex exact verification and append did not complete')
    assert summary(ledger)['totals']['totalTokens'] == 180


def test_resumed_codex_rollout_waits_for_earlier_deferred_delta_and_tag(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger_ingest, '_HASH_CHUNK', 64)
    data, claude, codex = make_roots(tmp_path)
    now = [float(HOUR_START + 3600)]
    ledger = Ledger(data, claude_root=claude, codex_root=codex, clock=lambda: now[0])
    a, b = codex_file(codex, 'a-earlier'), codex_file(codex, 'b-resumed')
    filler = {'type': 'unknown', 'payload': {'padding': 'x' * 2048}}
    write_jsonl(a, codex_meta('T1', 1) + [codex_count(2, input_tokens=100, output_tokens=0), filler])
    _drain_ledger(ledger)
    write_jsonl(b, codex_meta('T1', 3) + [codex_count(4, input_tokens=100, output_tokens=0), filler])
    _drain_ledger(ledger)

    append_jsonl(a, [codex_count(3600, input_tokens=120, output_tokens=0)])
    ledger.ingest(ALL, budget_bytes=256)
    append_jsonl(b, [
        {'type': 'event_msg', 'timestamp': stamp(7200),
         'payload': {'type': 'user_message', 'message': 'kind: build'}},
        codex_count(7201, input_tokens=150, output_tokens=0),
    ])
    ledger.ingest(ALL, budget_bytes=256)
    assert summary(ledger)['totals']['totalTokens'] == 100
    assert ledger.conn.execute("SELECT COUNT(*) FROM facts WHERE provider='Codex' AND hour>=?",
                               (HOUR_START + 3600,)).fetchone()[0] == 0
    assert store.load_thread(ledger.conn, 'Codex', 'T1')['tag_count'] == 0

    now[0] += ledger_ingest.IDLE_VERIFY_SECONDS
    for _ in range(100):
        ledger.ingest(ALL, budget_bytes=256)
        state_a = store.load_source(ledger.conn, 'Codex', ledger_ingest.path_key(a))['state']
        cursor_b = store.load_source(ledger.conn, 'Codex', ledger_ingest.path_key(b))
        state_b = cursor_b['state']
        total = summary(ledger)['totals']['totalTokens']
        if state_a.get('verification_debt'):
            assert total == 100
            assert store.load_thread(ledger.conn, 'Codex', 'T1')['tag_count'] == 0
        elif state_b.get('verification_debt'):
            assert total == 120
            assert store.load_thread(ledger.conn, 'Codex', 'T1')['tag_count'] in {0, 1}
            if (not state_b['verification_debt'].get('verified_stamp')
                    and float(state_b['verification_debt'].get('last_change_epoch', 0)) == now[0]):
                now[0] += ledger_ingest.IDLE_VERIFY_SECONDS
        elif cursor_b['byte_offset'] < b.stat().st_size:
            assert total == 120
        if (not state_a.get('verification_debt') and not state_b.get('verification_debt')
                and cursor_b['byte_offset'] >= b.stat().st_size):
            break
    else:
        pytest.fail('ordered Codex verification did not drain both deferred tails')

    by_hour = dict(ledger.conn.execute(
        "SELECT hour, SUM(total) FROM facts WHERE provider='Codex' GROUP BY hour"))
    assert by_hour[HOUR_START] == 100
    assert by_hour[HOUR_START + 3600] == 20
    assert by_hour.get(HOUR_START + 7200) == 30, (
        by_hour,
        store.load_source(ledger.conn, 'Codex', ledger_ingest.path_key(b)),
        list(ledger.conn.execute("SELECT fact_key, hour, total FROM facts WHERE provider='Codex'")),
        list(ledger.conn.execute("SELECT kind, at_epoch FROM gaps WHERE provider='Codex'")))
    assert store.load_thread(ledger.conn, 'Codex', 'T1')['tag_count'] == 1
    assert ledger.conn.execute("SELECT kind, total FROM facts WHERE provider='Codex' AND hour=?",
                               (HOUR_START + 7200,)).fetchone() == ('build', 30)


def test_first_discovered_codex_rollout_is_queued_from_session_metadata(tmp_path):
    data, claude, codex = make_roots(tmp_path)
    now = [float(HOUR_START + 3600)]
    ledger = Ledger(data, claude_root=claude, codex_root=codex, clock=lambda: now[0])
    earlier = codex_file(codex, 'a-known')
    first_seen = codex_file(codex, 'b-first-discovery')
    write_jsonl(earlier, codex_meta('T1', 1) + [codex_count(2, input_tokens=100, output_tokens=0)])
    _drain_ledger(ledger)
    append_jsonl(earlier, [codex_count(3600, input_tokens=120, output_tokens=0)])
    ledger.ingest(ALL)
    write_jsonl(first_seen, codex_meta('T1', 7200, prompt='kind: build')
                + [codex_count(7201, input_tokens=150, output_tokens=0)])
    ledger.ingest(ALL)
    assert store.load_source(ledger.conn, 'Codex', ledger_ingest.path_key(first_seen)) is None
    assert summary(ledger)['totals']['totalTokens'] == 100
    assert ledger.conn.execute("SELECT COUNT(*) FROM facts WHERE provider='Codex' AND hour>=?",
                               (HOUR_START + 3600,)).fetchone()[0] == 0

    now[0] += ledger_ingest.IDLE_VERIFY_SECONDS
    _drain_ledger(ledger)
    by_hour = dict(ledger.conn.execute(
        "SELECT hour, SUM(total) FROM facts WHERE provider='Codex' GROUP BY hour"))
    assert by_hour[HOUR_START] == 100
    assert by_hour[HOUR_START + 3600] == 20
    assert by_hour[HOUR_START + 7200] == 30
    assert ledger.conn.execute("SELECT kind,total FROM facts WHERE provider='Codex' AND hour=?",
                               (HOUR_START + 7200,)).fetchone() == ('build', 30)


def test_incomplete_codex_tail_does_not_queue_a_later_complete_rollout(tmp_path):
    data, claude, codex = make_roots(tmp_path)
    now = [float(HOUR_START + 3600)]
    ledger = Ledger(data, claude_root=claude, codex_root=codex, clock=lambda: now[0])
    older = codex_file(codex, 'a-partial')
    resumed = codex_file(codex, 'b-complete')
    write_jsonl(older, codex_meta('T1', 1) + [codex_count(2, input_tokens=100, output_tokens=0)])
    _drain_ledger(ledger)
    write_jsonl(resumed, codex_meta('T1', 3) + [codex_count(4, input_tokens=100, output_tokens=0)])
    _drain_ledger(ledger)
    with older.open('ab') as handle:
        handle.write(b'{"type":"event_msg","payload":{"type":"incomplete"')
    ledger.ingest(ALL)  # open exact-verification debt on the older source
    append_jsonl(resumed, [codex_count(7201, input_tokens=150, output_tokens=0)])
    ledger.ingest(ALL)  # the known resumed source waits while exact debt remains
    assert summary(ledger)['totals']['totalTokens'] == 100

    now[0] += ledger_ingest.IDLE_VERIFY_SECONDS
    # The older partial line never completes, so pendingBytes never reaches zero: run bounded passes.
    for _ in range(20):
        ledger.ingest(ALL)
        now[0] += ledger_ingest.IDLE_VERIFY_SECONDS
    older_cursor = store.load_source(ledger.conn, 'Codex', ledger_ingest.path_key(older))
    older_state = older_cursor['state']
    assert 'verification_debt' not in older_state
    assert older.stat().st_size > older_cursor['byte_offset']  # partial line remains, but is not an ordering debt
    assert summary(ledger)['totals']['totalTokens'] == 150
    assert ledger.conn.execute("SELECT total FROM facts WHERE provider='Codex' AND hour=?",
                               (HOUR_START + 7200,)).fetchone()[0] == 50


def test_queued_codex_truncation_is_recorded_before_older_debt_closes(tmp_path):
    data, claude, codex = make_roots(tmp_path)
    now = [float(HOUR_START + 3600)]
    ledger = Ledger(data, claude_root=claude, codex_root=codex, clock=lambda: now[0])
    older = codex_file(codex, 'a-debt')
    queued = codex_file(codex, 'b-truncated')
    write_jsonl(older, codex_meta('T1', 1) + [codex_count(2, input_tokens=100, output_tokens=0)])
    _drain_ledger(ledger)
    write_jsonl(queued, codex_meta('T1', 3) + [codex_count(4, input_tokens=100, output_tokens=0)])
    _drain_ledger(ledger)
    append_jsonl(older, [codex_count(5, input_tokens=120, output_tokens=0)])
    ledger.ingest(ALL)
    queued_cursor = store.load_source(ledger.conn, 'Codex', ledger_ingest.path_key(queued))
    assert queued_cursor['byte_offset'] > 0
    write_jsonl(queued, codex_meta('T1', 3))

    ledger.ingest(ALL)

    after = store.load_source(ledger.conn, 'Codex', ledger_ingest.path_key(queued))
    assert after['generation'] == queued_cursor['generation'] + 1
    assert after['byte_offset'] == 0  # queued: the truncated file's events wait for the older debt
    assert ledger.coverage()['truncations'] == 1
    assert store.load_source(ledger.conn, 'Codex', ledger_ingest.path_key(older))['state'].get('verification_debt')

    now[0] += ledger_ingest.IDLE_VERIFY_SECONDS
    _drain_ledger(ledger)
    assert 'verification_debt' not in store.load_source(ledger.conn, 'Codex', ledger_ingest.path_key(older))['state']
    assert store.load_source(ledger.conn, 'Codex', ledger_ingest.path_key(queued))['byte_offset'] == queued.stat().st_size
    assert ledger.coverage()['truncations'] == 1


def test_sol_resumed_codex_baseline_survives_verifying_sibling(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger_ingest, '_HASH_CHUNK', 64)
    ledger, _, codex = open_ledger(tmp_path)
    a, b = codex_file(codex, 'a'), codex_file(codex, 'b')
    filler = {'type': 'unknown', 'payload': {'padding': 'x' * 30000}}
    write_jsonl(a, codex_meta('T1', 1) + [codex_count(2, input_tokens=100, output_tokens=0), filler])
    _drain_ledger(ledger)
    append_jsonl(a, [codex_count(3, input_tokens=180, output_tokens=0)])
    ledger.ingest(ALL, budget_bytes=1024)
    assert ledger.coverage()['status'] == 'verifying'
    write_jsonl(b, codex_meta('T1', 4) + [codex_count(5, input_tokens=185, output_tokens=0)])
    ledger._ingestor.ingest_file('Codex', b, None, None)  # resumed file confirms while A verifies
    assert summary(ledger)['totals']['totalTokens'] == 185
    _drain_ledger(ledger)
    assert summary(ledger)['totals']['totalTokens'] == 185
    baseline = store.load_thread(ledger.conn, 'Codex', 'T1')
    assert baseline['counters']['total_tokens'] == 185
    # Design change (F30): A's tail is withheld as a cross-file earlier lower snapshot, and the gap says so.
    assert ledger.conn.execute("SELECT COUNT(*) FROM gaps WHERE kind='ambiguous-reset'").fetchone()[0] == 1


@pytest.mark.parametrize('vanish', ['delete', 'move'])
def test_unverified_source_gone_resolves_deferred_usage_with_gap(tmp_path, monkeypatch, vanish):
    monkeypatch.setattr(ledger_ingest, '_HASH_CHUNK', 64)
    ledger, claude, _ = open_ledger(tmp_path)
    path = claude_file(claude, 'vanished')
    filler = {'type': 'user', 'message': {'content': 'x' * 30000}}
    write_jsonl(path, [claude_response(1, 'first', output=5), filler])
    _drain_ledger(ledger)
    append_jsonl(path, [claude_response(2, 'later', output=9)])
    ledger.ingest(ALL, budget_bytes=1024)
    assert summary(ledger)['totals']['outputTotal'] == 5
    if vanish == 'delete':
        path.unlink()
    else:
        path.rename(path.parent / (path.name + '.bak'))  # still on disk, no longer discovered
    _drain_ledger(ledger)
    snap = summary(ledger)
    assert snap['totals']['outputTotal'] == 5
    assert ledger.conn.execute('SELECT COUNT(*) FROM provisional_facts').fetchone()[0] == 0
    assert ledger.conn.execute("SELECT value FROM counters WHERE name='missingUnverified'").fetchone()[0] == 1
    coverage = ledger.coverage()
    assert any(gap['kind'] == 'source-missing-unverified' for gap in coverage['gaps'])
    assert coverage['verifyingBytes'] == 0
    assert coverage['status'] != 'verifying'


def test_deferred_copy_rollback_keeps_other_sources_omission(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger_ingest, '_HASH_CHUNK', 64)
    ledger, claude, _ = open_ledger(tmp_path)
    a, b = claude_file(claude, 'a'), claude_file(claude, 'b')
    filler = {'type': 'user', 'message': {'content': 'x' * 30000}}
    missing = claude_response(10, 'm', output=0)
    del missing['message']['usage']
    write_jsonl(b, [missing])
    write_jsonl(a, [filler])
    _drain_ledger(ledger)
    assert _omission_count(ledger) == 1
    append_jsonl(a, [claude_response(20, 'm', output=5)])
    for _ in range(100):
        ledger.ingest(ALL, budget_bytes=1024)
        assert _omission_count(ledger) == 1
        if store.load_source(ledger.conn, 'Claude', ledger_ingest.path_key(a))['state'].get('verification_debt'):
            break
    else:
        pytest.fail('verification debt was not opened')
    write_jsonl(a, [filler])  # rewrite removes the deferred copy
    for _ in range(200):
        result = ledger.ingest(ALL, budget_bytes=1024)
        assert _omission_count(ledger) == 1
        if not result['pendingBytes']:
            break
    assert _omission_count(ledger) == 1


def test_verified_copy_clears_other_sources_omission(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger_ingest, '_HASH_CHUNK', 64)
    ledger, claude, _ = open_ledger(tmp_path)
    a, b = claude_file(claude, 'a'), claude_file(claude, 'b')
    filler = {'type': 'user', 'message': {'content': 'x' * 30000}}
    missing = claude_response(10, 'm', output=0)
    del missing['message']['usage']
    write_jsonl(b, [missing])
    write_jsonl(a, [filler])
    _drain_ledger(ledger)
    assert _omission_count(ledger) == 1
    append_jsonl(a, [claude_response(20, 'm', output=5)])
    _drain_ledger(ledger)
    assert _omission_count(ledger) == 0
    assert ledger.conn.execute("SELECT COUNT(*) FROM ledger_message_evidence").fetchone()[0] == 1


def test_codex_genuine_reset_with_later_epoch_still_counts(tmp_path):
    ledger, _, codex = open_ledger(tmp_path)
    a, b = codex_file(codex, 'a'), codex_file(codex, 'b')
    write_jsonl(a, codex_meta('T1', 1) + [codex_count(2, input_tokens=100, output_tokens=0)])
    _drain_ledger(ledger)
    write_jsonl(b, codex_meta('T1', 3) + [codex_count(6, input_tokens=40, output_tokens=0)])
    _drain_ledger(ledger)
    assert summary(ledger)['totals']['totalTokens'] == 140


def test_codex_stale_lower_snapshot_without_baseline_epoch_keeps_current_behaviour(tmp_path):
    ledger, _, codex = open_ledger(tmp_path)
    a, b = codex_file(codex, 'a'), codex_file(codex, 'b')
    write_jsonl(a, codex_meta('T1', 1) + [codex_count(2, input_tokens=100, output_tokens=0)])
    _drain_ledger(ledger)
    row = store.load_thread(ledger.conn, 'Codex', 'T1')
    row['counter_epoch'] = None
    with store.transaction(ledger.conn) as conn:
        store.save_thread(conn, 'Codex', 'T1', row)
    write_jsonl(b, codex_meta('T1', 1) + [codex_count(1, input_tokens=50, output_tokens=0)])
    _drain_ledger(ledger)
    assert summary(ledger)['totals']['totalTokens'] == 150


def test_same_source_reset_after_clock_correction_still_counts(tmp_path):
    ledger, _, codex = open_ledger(tmp_path)
    write_jsonl(codex_file(codex, 'a'), codex_meta('T1', 1) + [
        codex_count(100, input_tokens=100, output_tokens=0),
        codex_count(90, input_tokens=40, output_tokens=0),
        codex_count(91, input_tokens=80, output_tokens=0),
        codex_count(101, input_tokens=120, output_tokens=0)])
    _drain_ledger(ledger)
    assert summary(ledger)['totals']['totalTokens'] == 220


def test_discarded_deferred_source_is_replayed_when_it_reappears(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger_ingest, '_HASH_CHUNK', 64)
    ledger, claude, _ = open_ledger(tmp_path)
    path = claude_file(claude, 'reappears')
    filler = {'type': 'user', 'message': {'content': 'x' * 30000}}
    write_jsonl(path, [claude_response(1, 'first', output=5), filler])
    _drain_ledger(ledger)
    append_jsonl(path, [claude_response(2, 'later', output=9)])
    ledger.ingest(ALL)
    assert summary(ledger)['totals']['outputTotal'] == 5
    hidden = path.with_name(path.name + '.bak')
    path.rename(hidden)
    _drain_ledger(ledger)
    assert summary(ledger)['totals']['outputTotal'] == 5
    hidden.rename(path)
    _drain_ledger(ledger)
    assert summary(ledger)['totals']['outputTotal'] == 14
    assert total_requests(ledger) == 2
    assert ledger.conn.execute("SELECT COUNT(*) FROM gaps WHERE kind='source-missing-unverified'").fetchone()[0] == 1


def test_interrupted_run_persists_factless_verification_debt(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger_ingest, '_HASH_CHUNK', 64)
    ledger, claude, _ = open_ledger(tmp_path)
    path = claude_file(claude, 'interrupted')
    filler = {'type': 'user', 'message': {'content': 'x' * 30000}}
    write_jsonl(path, [claude_response(1, 'first', output=5), filler])
    _drain_ledger(ledger)
    assert summary(ledger)['coverage']['status'] == 'complete'
    append_jsonl(path, [claude_response(2, 'later', output=9)])

    def interrupted(*args, **kwargs):
        raise RuntimeError('interrupted before the run transaction')

    monkeypatch.setattr(ledger._ingestor, '_pending', interrupted)
    for _ in range(100):
        try:
            ledger.ingest(ALL, budget_bytes=1024)
        except RuntimeError:
            pass
        if store.verification_debt_bytes(ledger.conn, ['Claude']):
            break
    else:
        pytest.fail('verification debt was not persisted before the interrupted run')
    assert summary(ledger)['coverage']['status'] == 'verifying'


def test_legacy_replay_audit_waits_for_debt_closure_and_stage_promotion(tmp_path):
    data, claude, codex = make_roots(tmp_path)
    now = [float(HOUR_START + 3600)]
    ledger = Ledger(data, claude_root=claude, codex_root=codex, clock=lambda: now[0])
    path = claude_file(claude, 'legacy-audit')
    first = claude_response(1, 'confirmed', output=9)
    write_jsonl(path, [first])
    ledger.ingest(ALL)
    key = ledger_ingest.path_key(path)
    append_jsonl(path, [claude_response(2, 'later', output=3)])
    cursor = store.load_source(ledger.conn, 'Claude', key)
    cursor['generation'] = 2
    cursor['state'].update(audit=True, provisional=True, epoch=1)
    info = path.stat()
    cursor['state']['verification_debt'] = {
        'open': True, 'generation': 2, 'first_deferred_epoch': now[0],
        'last_change_epoch': now[0], 'last_stamp': ledger_ingest._file_stamp(info),
        'last_size': info.st_size, 'replacement_boundary': info.st_size,
        'tail_boundary': info.st_size, 'next_attempt_epoch': now[0] + 60,
    }
    columns = ['fact_key', 'thread_id', 'model', 'price_tier', 'prompt_peak', 'hour', 'kind', 'rule', 'slug',
               'classification_version', 'segment_turn', *store.FACT_COLUMNS]
    row = ledger.conn.execute("SELECT " + ", ".join(columns) + " FROM facts WHERE provider='Claude'").fetchone()
    fact = dict(zip(columns, row))
    fact.update(provider='Claude', merge=True,
                fields=dict(zip(store.FACT_COLUMNS, row[len(columns) - len(store.FACT_COLUMNS):])))
    fact['prompt_evidence'] = fact.pop('prompt_peak')
    with store.transaction(ledger.conn) as conn:
        store.save_source(conn, 'Claude', key, generation=2, byte_offset=cursor['byte_offset'],
                          head_key=cursor['head_key'], state=cursor['state'])
        store.stage_observation(conn, key, 2, 1, fact)

    append_jsonl(path, [claude_response(3, 'newer-during-backoff', output=2)])
    ledger.ingest(ALL)
    state = store.load_source(ledger.conn, 'Claude', key)['state']
    assert state['audit'] is True
    assert state['verification_debt']['next_attempt_epoch'] > now[0]
    assert store.has_provisional(ledger.conn, 'Claude', key)
    assert ledger.conn.execute("SELECT COUNT(*) FROM gaps WHERE kind='rewrite-lowered-confirmed'").fetchone()[0] == 0
    assert ledger.conn.execute("SELECT generation FROM source_facts WHERE provider='Claude' AND source_key=? LIMIT 1",
                               (key,)).fetchone()[0] == 1

    now[0] += ledger_ingest.DEADLINE_VERIFY_SECONDS
    _drain_ledger(ledger)
    assert not store.has_provisional(ledger.conn, 'Claude', key)
    assert ledger.conn.execute("SELECT COUNT(*) FROM gaps WHERE kind='rewrite-lowered-confirmed'").fetchone()[0] == 0
    assert ledger.conn.execute("SELECT generation FROM source_facts WHERE provider='Claude' AND source_key=? LIMIT 1",
                               (key,)).fetchone()[0] == 2


def test_codex_debt_waits_for_idle_then_confirms_all_tail_counts(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger_ingest, '_HASH_CHUNK', 64)
    ledger, _, codex = open_ledger(tmp_path)
    path = codex_file(codex, 'T1')
    filler = {'type': 'unknown', 'payload': {'padding': 'x' * 30000}}
    write_jsonl(path, codex_meta('T1', 1) + [codex_count(2, input_tokens=100, output_tokens=0), filler])
    _drain_ledger(ledger)
    for index in range(30):
        append_jsonl(path, [codex_count(3 + index, input_tokens=101 + index, output_tokens=0)])
        ledger.ingest(ALL, budget_bytes=1024)
    assert summary(ledger)['totals']['totalTokens'] == 100
    ledger._ingestor.clock = clock_at(3720)
    ledger.clock = clock_at(3720)
    for _ in range(100):
        ledger.ingest(ALL, budget_bytes=1024)
        cursor = store.load_source(ledger.conn, 'Codex', ledger_ingest.path_key(path))
        debt = cursor['state'].get('verification_debt')
        if debt and not debt.get('verified_stamp'):
            assert summary(ledger)['totals']['totalTokens'] == 100
            assert store.load_thread(ledger.conn, 'Codex', 'T1')['counters']['total_tokens'] == 100
            assert ledger.conn.execute("SELECT COUNT(*) FROM provisional_facts WHERE provider='Codex'").fetchone()[0] == 0
        else:
            if not debt:
                break
    else:
        pytest.fail('Codex debt remained open after the idle verification completed')
    # The closing pass finishes one record; the verified tail then drains under the normal budget.
    _drain_ledger(ledger, budget=1024)
    assert summary(ledger)['totals']['totalTokens'] == 130


def test_codex_tail_stays_unconfirmed_while_file_keeps_growing(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger_ingest, '_HASH_CHUNK', 64)
    ledger, _, codex = open_ledger(tmp_path)
    path = codex_file(codex, 'T1')
    filler = {'type': 'unknown', 'payload': {'padding': 'x' * 30000}}
    write_jsonl(path, codex_meta('T1', 1) + [codex_count(2, input_tokens=100, output_tokens=0), filler])
    _drain_ledger(ledger)
    for index in range(100):
        append_jsonl(path, [codex_count(3 + index, input_tokens=101 + index, output_tokens=0)])
        ledger.ingest(ALL, budget_bytes=1024)
    assert summary(ledger)['totals']['totalTokens'] == 100
    cursor = store.load_source(ledger.conn, 'Codex', ledger_ingest.path_key(path))
    assert cursor['state']['verification_debt']['open'] is True
    assert ledger.conn.execute("SELECT COUNT(*) FROM provisional_facts WHERE provider='Codex'").fetchone()[0] == 0


def test_source_below_verify_restart_cap_never_exceeds_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger_ingest, '_HASH_CHUNK', 64)
    ledger, _, codex = open_ledger(tmp_path)
    path = codex_file(codex, 'T1')
    filler = {'type': 'unknown', 'payload': {'padding': 'x' * 30000}}
    write_jsonl(path, codex_meta('T1', 1) + [codex_count(2, input_tokens=100, output_tokens=0), filler])
    _drain_ledger(ledger)
    for index in range(ledger_ingest._VERIFY_RESTART_LIMIT - 1):
        append_jsonl(path, [codex_count(3 + index, input_tokens=101 + index, output_tokens=0)])
        result = ledger.ingest(ALL, budget_bytes=1024)
        assert result['bytesRead'] <= 1024


def test_same_source_clock_correction_across_passes_still_counts(tmp_path):
    ledger, _, codex = open_ledger(tmp_path)
    path = codex_file(codex, 'a')
    write_jsonl(path, codex_meta('T1', 1) + [codex_count(100, input_tokens=100, output_tokens=0)])
    _drain_ledger(ledger)
    append_jsonl(path, [codex_count(90, input_tokens=40, output_tokens=0),
                        codex_count(91, input_tokens=80, output_tokens=0),
                        codex_count(101, input_tokens=120, output_tokens=0)])
    _drain_ledger(ledger)
    assert summary(ledger)['totals']['totalTokens'] == 220
    assert ledger.conn.execute("SELECT COUNT(*) FROM gaps WHERE kind='ambiguous-reset'").fetchone()[0] == 0


def test_sol_cross_file_clock_correction_is_withheld_with_gap(tmp_path):
    # Sol F30: A establishes 100@t100. Resumed B then reports 40@t90 and 80@t91 (earlier, lower, cross-file),
    # and 120@t101. The two earlier lower events are withheld, because a clock correction and a reset look
    # the same from timestamps alone. The baseline stays at 100, so 120@t101 adds 20: total 120.
    ledger, _, codex = open_ledger(tmp_path)
    write_jsonl(codex_file(codex, 'a'), codex_meta('T1', 1) + [codex_count(100, input_tokens=100, output_tokens=0)])
    ledger.ingest(ALL)
    write_jsonl(codex_file(codex, 'b'), codex_meta('T1', 3) + [
        codex_count(90, input_tokens=40, output_tokens=0),
        codex_count(91, input_tokens=80, output_tokens=0),
        codex_count(101, input_tokens=120, output_tokens=0)])
    _drain_ledger(ledger)
    assert summary(ledger)['totals']['totalTokens'] == 120
    gaps = ledger.conn.execute("SELECT COUNT(*) FROM gaps WHERE kind='ambiguous-reset'").fetchone()[0]
    assert gaps == 1  # one per thread per pass, not one per withheld event
    gap = next(gap for gap in ledger.coverage()['gaps'] if gap['kind'] == 'ambiguous-reset')
    assert gap['threadId'] == 'T1' and gap['intervalStart'] <= gap['intervalEnd']


def test_metadata_only_stamp_churn_stays_within_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(ledger_ingest, '_HASH_CHUNK', 64)
    ledger, _, codex = open_ledger(tmp_path)
    path = codex_file(codex, 'T1')
    filler = {'type': 'unknown', 'payload': {'padding': 'x' * 30000}}
    write_jsonl(path, codex_meta('T1', 1) + [codex_count(2, input_tokens=100, output_tokens=0), filler])
    _drain_ledger(ledger)
    base = path.stat().st_mtime_ns
    for index in range(20):
        os.utime(path, ns=(base + (index + 1) * 10**9, base + (index + 1) * 10**9))  # stamp changes, size does not
        result = ledger.ingest(ALL, budget_bytes=1024)
        assert result['bytesRead'] <= 1024
    cursor = store.load_source(ledger.conn, 'Codex', ledger_ingest.path_key(path))
    assert cursor['state']['verification_debt']['open'] is True
    assert 'verify' not in cursor['state']


def _meta_line(seconds: int, thread: str, *, source: str = 'cli', parent: str | None = None) -> dict:
    payload = {'id': thread, 'timestamp': stamp(seconds), 'source': source}
    if parent is not None:
        payload['parent_thread_id'] = parent
    return {'type': 'session_meta', 'timestamp': stamp(seconds), 'payload': payload}


def _child_rollout(codex, name='child'):
    """Forked rollout: its own meta, a copied parent meta and parent history, then its own counters from 0."""
    return codex_file(codex, name), [
        _meta_line(10, 'child', parent='parent'),
        _meta_line(1, 'parent', source='exec'),  # copied from the parent
        codex_count(2, input_tokens=600000, output_tokens=0),  # copied parent history, before this session began
        codex_count(4, input_tokens=900000, output_tokens=0),
        codex_count(20, input_tokens=10, output_tokens=0),
        codex_count(25, input_tokens=25, output_tokens=0),
        codex_count(40, input_tokens=40, output_tokens=0),
    ]


def test_child_rollout_attributes_usage_to_child_thread_with_parent_ingested_first(tmp_path):
    ledger, _, codex = open_ledger(tmp_path)
    write_jsonl(codex_file(codex, 'parent'), [_meta_line(1, 'parent', source='exec'),
                                              codex_count(2, input_tokens=600000, output_tokens=0),
                                              codex_count(4, input_tokens=900000, output_tokens=0)])
    _drain_ledger(ledger)
    path, lines = _child_rollout(codex)
    write_jsonl(path, lines)
    _drain_ledger(ledger)
    by_thread = dict(ledger.conn.execute(
        "SELECT thread_id, SUM(total) FROM facts WHERE provider='Codex' GROUP BY thread_id").fetchall())
    assert by_thread == {'parent': 900000, 'child': 40}
    assert ledger.conn.execute("SELECT COUNT(*) FROM gaps WHERE provider='Codex'").fetchone()[0] == 0


def test_copied_parent_history_alone_produces_no_fact_or_gap(tmp_path):
    ledger, _, codex = open_ledger(tmp_path)
    path, lines = _child_rollout(codex)
    write_jsonl(path, lines)
    _drain_ledger(ledger)
    assert summary(ledger)['totals']['totalTokens'] == 40
    assert ledger.conn.execute("SELECT COUNT(*) FROM facts WHERE thread_id='parent'").fetchone()[0] == 0
    assert ledger.conn.execute("SELECT COUNT(*) FROM gaps").fetchone()[0] == 0


def _claude_and_codex_rows(ledger):
    tables = ('facts', 'sources', 'threads', 'hourly', 'counters', 'gaps')
    return {table: ledger.conn.execute(f"SELECT * FROM {table} WHERE provider='Claude' ORDER BY 1, 2").fetchall()
            for table in tables}


def test_schema_three_clears_codex_keeps_claude_and_reingests(tmp_path, monkeypatch):
    ledger, claude, codex = open_ledger(tmp_path)
    write_jsonl(claude_file(claude, 's1'), claude_lines((10, 'msg_a', 5)))
    write_jsonl(codex_file(codex, 'T1'), codex_meta('T1', 10) + [codex_count(11, input_tokens=10, output_tokens=5)])
    ledger.ingest(ALL)
    codex_total = summary(ledger)['totals']['totalTokens']
    assert ledger.conn.execute("SELECT COUNT(*) FROM facts WHERE provider='Codex'").fetchone()[0] == 1
    ledger.close()
    raw = store.connect(tmp_path / 'data' / store.DB_NAME)
    # Give the file the genuine schema-2 shape: schema 4 only added facts.prompt_peak; 3 changed no tables.
    raw.execute("ALTER TABLE facts DROP COLUMN prompt_peak")
    raw.execute("UPDATE meta SET value='2' WHERE key='schemaVersion'")  # the previous version's ledger
    raw.execute("INSERT INTO overrides(provider, thread_id, kind) VALUES('Codex', 'T1', 'review')")  # a user decision
    claude_before = {table: raw.execute(f"SELECT * FROM {table} WHERE provider='Claude' ORDER BY 1, 2").fetchall()
                     for table in ('facts', 'sources', 'threads', 'hourly', 'counters', 'gaps')}
    raw.close()
    original = store.MIGRATIONS.copy()
    monkeypatch.setattr(store, "MIGRATIONS", {version: steps for version, steps in original.items() if version <= 3})
    ledger = Ledger(tmp_path / 'data', claude_root=claude, codex_root=codex, clock=clock_at(3600))
    assert ledger.health()['schemaVersion'] == 3
    assert ledger.conn.execute("SELECT COUNT(*) FROM facts WHERE provider='Codex'").fetchone()[0] == 0
    assert ledger.conn.execute("SELECT COUNT(*) FROM sources WHERE provider='Codex'").fetchone()[0] == 0
    assert ledger.conn.execute("SELECT COUNT(*) FROM meta WHERE key LIKE '%:Codex'").fetchone()[0] == 0
    assert _claude_and_codex_rows(ledger) == claude_before
    assert ledger.conn.execute("SELECT kind FROM overrides WHERE provider='Codex' AND thread_id='T1'").fetchone() == ('review',)
    ledger.close()
    # Re-ingestion runs with the current writer, which needs schema 4.
    monkeypatch.setattr(store, "MIGRATIONS", original)
    ledger = Ledger(tmp_path / 'data', claude_root=claude, codex_root=codex, clock=clock_at(3600))
    assert ledger.conn.execute("SELECT kind FROM overrides WHERE provider='Codex' AND thread_id='T1'").fetchone() == ('review',)
    ledger.ingest(ALL)
    assert summary(ledger)['totals']['totalTokens'] == codex_total
    columns = [row[1] for row in ledger.conn.execute("PRAGMA table_info(facts)")]
    assert columns[-1] == 'prompt_peak'
    facts = ledger.conn.execute("SELECT * FROM facts WHERE provider='Claude' ORDER BY 1, 2").fetchall()
    assert [row[:-1] for row in facts] == claude_before['facts']


def _seed_schema_three_facts(conn):
    def fact(provider, key, thread, model, tier, total):
        conn.execute(
            "INSERT INTO facts(provider,fact_key,thread_id,model,price_tier,hour,kind,rule,slug,classification_version,"
            "segment_turn,input_total,output_total,total,cached_read,cache_write_5m,cache_write_1h,cache_write_unknown,"
            "reasoning_output,revisions) VALUES(?,?,?,?,?,0,'lead','default',NULL,1,NULL,?,0,?,NULL,NULL,NULL,NULL,NULL,0)",
            (provider,key,thread,model,tier,total,total))
    fact('Claude','c-long','C1','claude-haiku-5-5','standard',100001)
    fact('Claude','c-old-long','C4','claude-haiku-5-5','long-context',40)
    fact('Claude','c-short','C2','claude-sonnet-4-5','standard',40)
    fact('Codex','x-long','X1','gpt-5-codex','long-context',70)


def test_schema_four_migrates_prompt_tiers_and_preserves_other_state(tmp_path, monkeypatch):
    original = store.MIGRATIONS.copy()
    monkeypatch.setattr(store, 'MIGRATIONS', {v: steps for v, steps in original.items() if v <= 3})
    ledger, claude, codex = open_ledger(tmp_path)
    with store.transaction(ledger.conn):
        _seed_schema_three_facts(ledger.conn)
        ledger.conn.execute("INSERT INTO overrides VALUES('Claude','C1','review')")
        store.stage_observation(ledger.conn, 'source', 1, 1, {
            'provider':'Claude','fact_key':'staged','thread_id':'C3','model':'claude-haiku-5-5',
            'price_tier':'standard','prompt_evidence':100001,'hour':0,'kind':'lead','rule':'default',
            'classification_version':1,'merge':True,'fields':{'input_total':100001,'output_total':0,'total':100001,
            'cached_read':None,'cache_write_5m':None,'cache_write_1h':None,'cache_write_unknown':None,'reasoning_output':None}})
        ledger.conn.execute("UPDATE meta SET value='30' WHERE key='revision'")
        store.rebuild_rollups(ledger.conn, store.load_overrides(ledger.conn))
    staged_before = ledger.conn.execute("SELECT observation FROM provisional_facts").fetchall()
    codex_before = ledger.conn.execute("SELECT provider,fact_key,thread_id,model,price_tier,hour,kind,rule,slug,"
                                       "classification_version,segment_turn,input_total,output_total,total,cached_read,"
                                       "cache_write_5m,cache_write_1h,cache_write_unknown,reasoning_output,revisions "
                                       "FROM facts WHERE provider='Codex'").fetchall()
    ledger.close()
    monkeypatch.setattr(store, 'MIGRATIONS', original)
    upgraded = Ledger(tmp_path / 'data', claude_root=claude, codex_root=codex, clock=clock_at(3600))
    assert upgraded.health()['schemaVersion'] == 4
    assert upgraded.conn.execute("SELECT value FROM meta WHERE key='revision'").fetchone() == ('31',)
    assert upgraded.conn.execute("SELECT fact_key,price_tier,prompt_peak FROM facts WHERE provider='Claude' ORDER BY fact_key").fetchall() == [
        ('c-long','long-context',100001), ('c-old-long','standard',40), ('c-short','standard',40)]
    assert upgraded.conn.execute("SELECT provider,fact_key,thread_id,model,price_tier,hour,kind,rule,slug,"
                                 "classification_version,segment_turn,input_total,output_total,total,cached_read,"
                                 "cache_write_5m,cache_write_1h,cache_write_unknown,reasoning_output,revisions "
                                 "FROM facts WHERE provider='Codex'").fetchall() == codex_before
    assert upgraded.conn.execute("SELECT provider,thread_id,kind FROM overrides").fetchall() == [('Claude','C1','review')]
    assert upgraded.conn.execute("SELECT observation FROM provisional_facts").fetchall() == staged_before
    expected = upgraded.conn.execute("SELECT * FROM hourly ORDER BY 1,2,3,4,5,6").fetchall()
    with store.transaction(upgraded.conn):
        store.rebuild_rollups(upgraded.conn, store.load_overrides(upgraded.conn))
    assert upgraded.conn.execute("SELECT * FROM hourly ORDER BY 1,2,3,4,5,6").fetchall() == expected
    upgraded.close()


def test_schema_four_gives_legacy_staged_observations_the_stored_total_estimate(tmp_path, monkeypatch):
    import json as _json
    original = store.MIGRATIONS.copy()
    monkeypatch.setattr(store, 'MIGRATIONS', {v: steps for v, steps in original.items() if v <= 3})
    ledger, claude, codex = open_ledger(tmp_path)
    legacy = {'provider': 'Claude', 'fact_key': 'staged', 'thread_id': 'C3', 'model': 'claude-haiku-5-5',
              'price_tier': 'standard', 'hour': 0, 'kind': 'lead', 'rule': 'default', 'slug': None,
              'classification_version': 1, 'segment_turn': None, 'merge': True,
              'fields': {'input_total': 101000, 'output_total': 0, 'total': 101000, 'cached_read': None,
                         'cache_write_5m': None, 'cache_write_1h': None, 'cache_write_unknown': None,
                         'reasoning_output': None}}
    explicit_null = dict(legacy, fact_key='staged-null', prompt_evidence=None)
    with store.transaction(ledger.conn):
        for record in (legacy, explicit_null):
            ledger.conn.execute("INSERT INTO provisional_facts(provider, source_key, generation, epoch, fact_key, observation) "
                                "VALUES('Claude', 'source', 1, 1, ?, ?)", (record['fact_key'], _json.dumps(record, sort_keys=True)))
    ledger.close()
    monkeypatch.setattr(store, 'MIGRATIONS', original)
    upgraded = Ledger(tmp_path / 'data', claude_root=claude, codex_root=codex, clock=clock_at(3600))
    rows = dict(upgraded.conn.execute("SELECT fact_key, observation FROM provisional_facts").fetchall())
    migrated, kept = _json.loads(rows['staged']), _json.loads(rows['staged-null'])
    assert migrated['prompt_evidence'] == 101000 and migrated['price_tier'] == 'long-context'
    assert 'prompt_evidence' in kept and kept['prompt_evidence'] is None and kept['price_tier'] == 'standard'
    upgraded.close()


def test_migration_failure_keeps_schema_three_file_in_place(tmp_path, monkeypatch):
    import sqlite3
    original = store.MIGRATIONS.copy()
    monkeypatch.setattr(store, 'MIGRATIONS', {v: steps for v, steps in original.items() if v <= 3})
    ledger, claude, codex = open_ledger(tmp_path)
    with store.transaction(ledger.conn):
        _seed_schema_three_facts(ledger.conn)
        ledger.conn.execute("UPDATE meta SET value='12' WHERE key='revision'")
    ledger.close()

    def fail_after_write(conn):
        conn.execute("UPDATE facts SET input_total=0 WHERE provider='Claude'")
        raise sqlite3.IntegrityError('injected migration failure')
    monkeypatch.setattr(store, 'MIGRATIONS', {**original, 4: (fail_after_write,)})
    data = tmp_path / 'data'
    db_path = data / store.DB_NAME
    with pytest.raises(sqlite3.IntegrityError, match='injected migration failure'):
        Ledger(data, claude_root=claude, codex_root=codex, clock=clock_at(3600))
    assert db_path.exists()
    assert not list(data.glob(store.DB_NAME + '.corrupt-*'))
    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT value FROM meta WHERE key='schemaVersion'").fetchone() == ('3',)
    assert conn.execute("SELECT input_total FROM facts WHERE provider='Claude' ORDER BY fact_key").fetchall() == [(100001,), (40,), (40,)]
    assert 'prompt_peak' not in {row[1] for row in conn.execute('PRAGMA table_info(facts)')}
    conn.close()


def _own_first_counter(seconds: int, total: int) -> dict:
    last = {'input_tokens': total, 'output_tokens': 0, 'total_tokens': total}
    return codex_count(seconds, input_tokens=total, output_tokens=0, last=last)


def test_copied_parent_counters_do_not_seed_child_counter_domain(tmp_path):
    ledger, _, codex = open_ledger(tmp_path)
    write_jsonl(codex_file(codex, 'child'), [
        _meta_line(10, 'child', parent='parent'),
        _meta_line(1, 'parent', source='exec'),
        codex_count(2, input_tokens=100, output_tokens=0),
        codex_count(4, input_tokens=200, output_tokens=0),  # copied parent counters, before this session began
        _own_first_counter(20, 250),  # the child's own counter domain starts fresh
    ])
    _drain_ledger(ledger)
    assert summary(ledger)['totals']['totalTokens'] == 250
    assert ledger.conn.execute("SELECT COUNT(*) FROM gaps").fetchone()[0] == 0


def test_single_copied_parent_counter_does_not_seed_child_counter_domain(tmp_path):
    ledger, _, codex = open_ledger(tmp_path)
    write_jsonl(codex_file(codex, 'child'), [
        _meta_line(10, 'child', parent='parent'),
        _meta_line(1, 'parent', source='exec'),
        codex_count(2, input_tokens=100, output_tokens=0),
        _own_first_counter(20, 250),
    ])
    _drain_ledger(ledger)
    assert summary(ledger)['totals']['totalTokens'] == 250


def test_same_thread_resume_still_seeds_from_earlier_history(tmp_path):
    ledger, _, codex = open_ledger(tmp_path)
    write_jsonl(codex_file(codex, 'resumed'), [
        _meta_line(10, 'T1'),  # no copied foreign meta: the same thread resumed in a new file
        codex_count(5, input_tokens=150, output_tokens=0),  # earlier history of this thread: seeds the baseline
        codex_count(20, input_tokens=200, output_tokens=0),
    ])
    _drain_ledger(ledger)
    assert summary(ledger)['totals']['totalTokens'] == 50


def _resume_lines(seconds_history=((2, 100), (5, 150))):
    lines = [_meta_line(10, 'T1')]
    lines += [codex_count(at, input_tokens=total, output_tokens=0) for at, total in seconds_history]
    return lines


def test_resumed_same_thread_history_advances_seeded_baseline_to_latest(tmp_path):
    ledger, _, codex = open_ledger(tmp_path)
    own = codex_count(20, input_tokens=200, output_tokens=0,
                      last={'input_tokens': 50, 'output_tokens': 0, 'total_tokens': 50})
    write_jsonl(codex_file(codex, 'resumed'), _resume_lines() + [own])
    _drain_ledger(ledger)
    assert summary(ledger)['totals']['totalTokens'] == 50
    assert ledger.conn.execute("SELECT COUNT(*) FROM gaps").fetchone()[0] == 0


def test_resumed_history_split_across_budgeted_passes_gives_same_total(tmp_path):
    ledger, _, codex = open_ledger(tmp_path)
    path = codex_file(codex, 'resumed')
    own = codex_count(20, input_tokens=200, output_tokens=0,
                      last={'input_tokens': 50, 'output_tokens': 0, 'total_tokens': 50})
    write_jsonl(path, _resume_lines() + [own])
    _drain_ledger(ledger, budget=64)  # roughly one record per pass, so history reports land in separate passes
    assert summary(ledger)['totals']['totalTokens'] == 50
    assert ledger.conn.execute("SELECT COUNT(*) FROM gaps").fetchone()[0] == 0


def test_history_never_rewinds_or_advances_another_sources_confirmed_baseline(tmp_path):
    ledger, _, codex = open_ledger(tmp_path)
    write_jsonl(codex_file(codex, 'a'), codex_meta('T1', 1) + [codex_count(2, input_tokens=300, output_tokens=0)])
    ledger.ingest(ALL)  # A confirms 300 at t2
    write_jsonl(codex_file(codex, 'b'), [_meta_line(10, 'T1'),
                                         codex_count(4, input_tokens=350, output_tokens=0),  # history, after A's report
                                         codex_count(20, input_tokens=400, output_tokens=0)])  # own: +100 over 300
    _drain_ledger(ledger)
    assert summary(ledger)['totals']['totalTokens'] == 400
    assert ledger.conn.execute("SELECT COUNT(*) FROM gaps").fetchone()[0] == 0

def test_seeded_baseline_follows_a_historical_reset_in_time_order(tmp_path):
    # History 100, 150, then a compaction reset to 100 before the resume; own 200 with last=100 counts 100.
    for budget in (None, 64):
        ledger, _, codex = open_ledger(tmp_path / str(budget))
        own = codex_count(20, input_tokens=200, output_tokens=0,
                          last={'input_tokens': 100, 'output_tokens': 0, 'total_tokens': 100})
        write_jsonl(codex_file(codex, 'resumed'), _resume_lines(((2, 100), (5, 150), (7, 100))) + [own])
        _drain_ledger(ledger, budget=budget) if budget else _drain_ledger(ledger)
        assert summary(ledger)['totals']['totalTokens'] == 100
        assert ledger.conn.execute("SELECT COUNT(*) FROM gaps").fetchone()[0] == 0
        ledger.close()


def test_locked_integrity_check_leaves_a_healthy_ledger_in_place(tmp_path, monkeypatch):
    import sqlite3
    ledger, claude, codex = open_ledger(tmp_path)
    write_jsonl(claude_file(claude, 's1'), claude_lines((10, 'msg_a', 5)))
    ledger.ingest(ALL)
    ledger.close()
    data = tmp_path / 'data'

    def locked(_conn):
        raise sqlite3.OperationalError('database schema is locked: main')
    monkeypatch.setattr(store, 'quick_check', locked)
    with pytest.raises(sqlite3.OperationalError, match='locked'):
        Ledger(data, claude_root=claude, codex_root=codex, clock=clock_at(3600))
    assert (data / store.DB_NAME).exists()
    assert not list(data.glob(store.DB_NAME + '.corrupt-*'))
    assert not (data / (store.DB_NAME + '.recovery-intent')).exists()
    monkeypatch.undo()
    reopened = Ledger(data, claude_root=claude, codex_root=codex, clock=clock_at(3600))
    assert reopened.conn.execute("SELECT COUNT(*) FROM facts WHERE provider='Claude'").fetchone()[0] == 1
    reopened.close()
