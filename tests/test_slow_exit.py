import concurrent.futures
import copy
import json
import os
import queue
import subprocess
import sys
import threading
import time
import pytest

from token_tracker import claude_provider, codex_client, providers, widget


@pytest.mark.parametrize("seed_checkpoint", [False, True], ids=["cold", "appended"])
def test_incremental_jsonl_cancellation_preserves_checkpoint(tmp_path, monkeypatch, seed_checkpoint):
    path = tmp_path / "events.jsonl"
    path.write_bytes(b'{"keep":1}\r\n{"skip":1}\r\n{"keep":2}\r\n{"skip":3}\r\n')
    kind = f"shutdown-{seed_checkpoint}"
    reducer = lambda state, item: state["values"].append(item["keep"])
    initial = lambda: {"values": []}
    if seed_checkpoint:
        assert providers._incremental_jsonl_reduce(kind, path, initial, reducer,
                                                   needles=(b'"keep"',)) == {"values": [1, 2]}
        with path.open("ab") as handle:
            handle.write(b'{"keep":3}\r\n{"skip":2}\r\n{"keep":4}\r\n{"skip":4}\r\n')
    key = (kind, str(path.resolve()))
    before_checkpoint = copy.deepcopy(providers._JSONL_CHECKPOINTS.get(key))
    before_bytes = providers._JSONL_CHECKPOINT_BYTES
    callbacks = []

    def cancel_after_n(state, item):
        reducer(state, item)
        callbacks.append(item["keep"])
        if len(callbacks) == 2:
            providers.stop_snapshot_workers()

    with pytest.raises(providers.SnapshotShutdown, match="provider snapshots are shutting down"):
        providers._incremental_jsonl_reduce(kind, path, initial, cancel_after_n,
                                             needles=(b'"keep"',))
    assert len(callbacks) == 2
    assert providers._JSONL_CHECKPOINTS.get(key) == before_checkpoint
    assert providers._JSONL_CHECKPOINT_BYTES == before_bytes


def test_snapshot_shutdown_escapes_provider_fallbacks_and_build(monkeypatch):
    sentinel = {"connected": True, "chats": [{"id": "prior"}], "_policyGeneration": "test"}
    monkeypatch.setitem(providers._LAST_GOOD, "Codex", copy.deepcopy(sentinel))
    monkeypatch.setitem(providers._LAST_GOOD, "Claude", copy.deepcopy(sentinel))

    def cancel(*_args, **_kwargs):
        raise providers.SnapshotShutdown("provider snapshots are shutting down")

    monkeypatch.setattr(providers.CodexProvider, "_thread", staticmethod(cancel))

    class CodexRows:
        def snapshot(self):
            return providers.CodexProvider._safe_thread({"id": "row"})

    with pytest.raises(providers.SnapshotShutdown):
        providers._provider_snapshot(CodexRows, "Codex", "#000")

    monkeypatch.setattr(claude_provider.ClaudeCodeProvider, "_base", lambda self: {"connected": True})
    monkeypatch.setattr(claude_provider, "build_rows", lambda: [])
    monkeypatch.setattr(claude_provider, "usage_aggregates", cancel)
    with pytest.raises(providers.SnapshotShutdown):
        providers._provider_snapshot(claude_provider.ClaudeCodeProvider, "Claude", "#000")
    assert providers._LAST_GOOD["Codex"] == sentinel
    assert providers._LAST_GOOD["Claude"] == sentinel

    monkeypatch.setattr(providers, "_provider_collection_sets", lambda _policies: (("Codex",), ()))
    monkeypatch.setattr(providers.CodexProvider, "snapshot", cancel)
    with pytest.raises(providers.SnapshotShutdown):
        providers.build_snapshot()


def test_codex_token_event_scan_cancels_without_cache(tmp_path, monkeypatch):
    path = tmp_path / "tokens.jsonl"
    path.write_text("".join(json.dumps({"type": "session_meta", "payload": {"timestamp": "2026-01-01T00:00:00Z"}}) + "\r\n" for _ in range(20)))
    cls = providers.CodexProvider
    monkeypatch.setattr(cls, "_token_event_cache", type(cls._token_event_cache)())
    monkeypatch.setattr(cls, "_token_event_cache_sizes", {})
    monkeypatch.setattr(cls, "_token_event_cache_bytes", 0)
    real_loads = providers.json.loads
    calls = []

    def cancel_on_third(value, *args, **kwargs):
        calls.append(value)
        result = real_loads(value, *args, **kwargs)
        if len(calls) == 3:
            providers.stop_snapshot_workers()
        return result

    monkeypatch.setattr(providers.json, "loads", cancel_on_third)
    with pytest.raises(providers.SnapshotShutdown):
        cls._token_events(path)
    assert len(calls) == 3
    assert cls._token_event_cache == {}
    assert cls._token_event_cache_sizes == {}
    assert cls._token_event_cache_bytes == 0


def test_codex_turn_count_scan_cancels_without_file_memo(tmp_path, monkeypatch):
    path = tmp_path / "turns.jsonl"
    path.write_text("".join('{"type":"turn_context","payload":{}}\r\n' for _ in range(20)))
    key = ("codex-turns", str(path))
    monkeypatch.delitem(providers._FILE_MEMO, key, raising=False)
    real_loads = providers.json.loads
    calls = []

    def cancel_on_third(value, *args, **kwargs):
        calls.append(value)
        result = real_loads(value, *args, **kwargs)
        if len(calls) == 3:
            providers.stop_snapshot_workers()
        return result

    monkeypatch.setattr(providers.json, "loads", cancel_on_third)
    with pytest.raises(providers.SnapshotShutdown):
        providers.CodexProvider._turn_count(str(path))
    assert len(calls) == 3
    assert key not in providers._FILE_MEMO


def test_snapshot_shutdown_releases_executor_at_interpreter_exit(tmp_path):
    source = tmp_path / "two-records.jsonl"
    marker = tmp_path / "unexpected-second-record"
    source.write_text('{"n":1}\r\n{"n":2}\r\n')
    child = r'''import json, sys, threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from token_tracker import providers
source, marker = Path(sys.argv[1]), Path(sys.argv[2])
started, release = threading.Event(), threading.Event()
pool = ThreadPoolExecutor(max_workers=1)
providers._snapshot_executors.add(pool)
def work():
    def reduce(state, item):
        if item["n"] == 1:
            started.set()
            release.wait()
        else:
            threading.Event().wait(60)
            marker.write_text("continued")
    try:
        providers._incremental_jsonl_reduce("subprocess-exit", source, lambda: {}, reduce)
    except providers.SnapshotShutdown:
        print("CANCELLED", flush=True)
future = pool.submit(work)
if not started.wait(45):
    raise SystemExit("worker did not start")
print("READY", flush=True)
if sys.stdin.readline().strip() != "STOP":
    raise SystemExit("missing STOP")
providers.stop_snapshot_workers()
release.set()
'''
    env = os.environ.copy()
    env.update({
        "LOCALAPPDATA": str(tmp_path / "product"),
        "BRANCHGLANCE_STATE_ROOT": str(tmp_path / "product-state"),
        "CLAUDE_CONFIG_DIR": str(tmp_path / "claude"),
        "CODEX_HOME": str(tmp_path / "codex"),
    })
    process = subprocess.Popen(
        [sys.executable, "-B", "-u", "-c", child, str(source), str(marker)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, env=env,
    )
    lines = queue.Queue()

    def collect_output():
        for line in process.stdout:
            lines.put(line.rstrip("\r\n"))

    reader = threading.Thread(target=collect_output, daemon=True)
    reader.start()
    try:
        assert lines.get(timeout=45) == "READY"
        process.stdin.write("STOP\r\n")
        process.stdin.flush()
        process.wait(timeout=5)
        reader.join(timeout=5)
        assert not reader.is_alive()
        output = []
        while True:
            try:
                output.append(lines.get_nowait())
            except queue.Empty:
                break
        assert process.returncode == 0, output
        assert "CANCELLED" in output
        assert not marker.exists()
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()


def test_quit_cancels_queued_snapshot_and_finishes_running_write(tmp_path, monkeypatch):
    started, release = threading.Event(), threading.Event()
    written = tmp_path / "completed"
    real_executor = concurrent.futures.ThreadPoolExecutor
    monkeypatch.setattr(providers, "ThreadPoolExecutor", lambda **kwargs: real_executor(max_workers=1))
    monkeypatch.setattr(providers, "_provider_collection_sets", lambda policies: (("Claude", "Codex"), ()))
    monkeypatch.setattr(providers, "_structure_stamp", lambda names: None)
    calls = []

    def snapshot(factory, name, accent, generation):
        calls.append(name)
        started.set()
        assert release.wait(5)
        written.write_text("complete")
        return {"name": name}

    monkeypatch.setattr(providers, "_provider_snapshot", snapshot)
    errors = []

    def build():
        try:
            providers.build_snapshot()
        except concurrent.futures.CancelledError:
            errors.append("cancelled")

    thread = threading.Thread(target=build)
    thread.start()
    try:
        assert started.wait(5)
        # Wait until both submissions exist; the first job is deliberately held.
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with providers._snapshot_executor_lock:
                pools = list(providers._snapshot_executors)
            if pools and pools[0]._work_queue.qsize():
                break
            time.sleep(0.01)
        else:
            pytest.fail("second provider was not queued")
        providers.stop_snapshot_workers()
        with pytest.raises(RuntimeError, match="shutting down"):
            providers.build_snapshot()
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    assert written.read_text() == "complete"
    assert calls == ["Claude"] and errors == ["cancelled"]


def test_client_close_timeout_preserves_busy_lock():
    client = codex_client.CodexAppServer()
    client._persistent_lock.acquire()
    try:
        start = time.monotonic()
        client.close(timeout=0.05)
        assert time.monotonic() - start < 1
        assert client._persistent_lock.locked()
    finally:
        client._persistent_lock.release()


def test_shutdown_trace_arms_writes_and_cancels(tmp_path):
    path = tmp_path / "logs" / "shutdown-trace.log"
    path.parent.mkdir()
    path.write_bytes(b"x" * (256 * 1024 + 1))
    widget._arm_shutdown_trace(tmp_path, timeout=0.05)
    deadline = time.monotonic() + 3
    while path.stat().st_size == 0 and time.monotonic() < deadline:
        time.sleep(0.01)
    widget._cancel_shutdown_trace()
    assert b"Timeout" in path.read_bytes()
    assert path.stat().st_size < 256 * 1024


@pytest.mark.parametrize("size,expected", [
    (192 * 1024 - 1, 192 * 1024 - 1),
    (192 * 1024, 0),
    (256 * 1024 - 1, 0),
    (256 * 1024, 0),
    (256 * 1024 + 1, 0),
])
def test_shutdown_trace_rotation_headroom(tmp_path, monkeypatch, size, expected):
    path = tmp_path / "logs" / "shutdown-trace.log"
    path.parent.mkdir()
    path.write_bytes(b"x" * size)
    monkeypatch.setattr(widget.faulthandler, "dump_traceback_later", lambda *args, **kwargs: None)
    monkeypatch.setattr(widget.faulthandler, "cancel_dump_traceback_later", lambda: None)
    widget._arm_shutdown_trace(tmp_path)
    try:
        assert path.stat().st_size == expected
    finally:
        widget._cancel_shutdown_trace()


def test_overlapping_trace_arms_and_cancel_keep_descriptor_open(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    scheduled = []
    cancelled = []

    def schedule(*args, **kwargs):
        scheduled.append(kwargs["file"])
        entered.set()
        assert release.wait(5)
        assert not scheduled[0].closed

    def cancel():
        assert not scheduled[0].closed
        cancelled.append(True)

    monkeypatch.setattr(widget.faulthandler, "dump_traceback_later", schedule)
    monkeypatch.setattr(widget.faulthandler, "cancel_dump_traceback_later", cancel)
    first = threading.Thread(target=widget._arm_shutdown_trace, args=(tmp_path,))
    second = threading.Thread(target=widget._arm_shutdown_trace, args=(tmp_path,))
    first.start()
    try:
        assert entered.wait(5)
        second.start()
        release.set()
        first.join(5)
        second.join(5)
        assert not first.is_alive() and not second.is_alive()
        assert len(scheduled) == 1 and not scheduled[0].closed
        widget._cancel_shutdown_trace()
        assert cancelled == [True] and scheduled[0].closed
    finally:
        release.set()
        first.join(5)
        if second.ident is not None:
            second.join(5)
        widget._cancel_shutdown_trace()


def test_quit_arms_trace_and_stops_snapshots(monkeypatch):
    calls = []
    monkeypatch.setattr(widget, "_arm_shutdown_trace", lambda: calls.append("trace"))
    monkeypatch.setattr(providers, "stop_snapshot_workers", lambda: calls.append("snapshots"))
    monkeypatch.setattr(widget, "_stop_services_together", lambda steps: None)
    app = widget.WidgetApp()
    app.quit()
    assert calls == ["trace", "snapshots"]
