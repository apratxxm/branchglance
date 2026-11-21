import tracemalloc

from token_tracker import memory_diagnostics


def test_off_without_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(memory_diagnostics.product, "state_root", lambda: tmp_path)
    monkeypatch.setattr(memory_diagnostics, "_started", False)
    was_tracing = tracemalloc.is_tracing()
    assert memory_diagnostics.start_if_requested() is False
    assert tracemalloc.is_tracing() == was_tracing
    assert not (tmp_path / "diag").exists()


def test_report_holds_locations_sizes_and_type_names_only(tmp_path):
    tracemalloc.start(memory_diagnostics.FRAMES)
    try:
        secret = ["do-not-log-this-content"] * 1000
        baseline = tracemalloc.take_snapshot()
        held = [bytearray(4096) for _ in range(64)]
        snap = memory_diagnostics._write_report(tmp_path, baseline, baseline, 1)
    finally:
        tracemalloc.stop()
    text = (tmp_path / "tracemalloc-001.txt").read_text(encoding="utf-8")
    assert "growth since first report" in text and "most numerous object types" in text
    assert "test_memory_diagnostics.py" in text
    assert "do-not-log-this-content" not in text
    assert snap is not None and held and secret


def test_marker_starts_tracing_and_writes_first_report(tmp_path, monkeypatch):
    monkeypatch.setattr(memory_diagnostics.product, "state_root", lambda: tmp_path)
    monkeypatch.setattr(memory_diagnostics, "_started", False)
    (tmp_path / "diag").mkdir()
    (tmp_path / "diag" / memory_diagnostics.MARKER).write_text("", encoding="utf-8")
    monkeypatch.setattr(memory_diagnostics, "MAX_REPORTS", 1)
    try:
        assert memory_diagnostics.start_if_requested(interval=0.01) is True
        for _ in range(200):
            if (tmp_path / "diag" / "tracemalloc-000.txt").exists():
                break
            import time; time.sleep(0.05)
        assert (tmp_path / "diag" / "tracemalloc-000.txt").exists()
    finally:
        tracemalloc.stop()
