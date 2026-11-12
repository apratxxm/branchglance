import concurrent.futures
import threading
import time
import pytest

from token_tracker import codex_client, providers, widget


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
