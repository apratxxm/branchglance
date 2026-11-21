"""Opt-in memory diagnostics for the widget process.

Off unless the marker file ``<state root>/diag/tracemalloc.on`` exists when the
widget starts. When on, tracemalloc records allocations and a daemon thread
writes a report every ``INTERVAL_SECONDS`` to ``<state root>/diag/``: the top
allocation growth since the first report (grouped by traceback) and the most
numerous object types. Reports hold only code locations, sizes and type names,
never object contents. Delete the marker and restart to turn it off.
"""
from __future__ import annotations

import collections
import gc
import threading
import time
import tracemalloc
from pathlib import Path

from . import product

MARKER = "tracemalloc.on"
INTERVAL_SECONDS = 600.0
FRAMES = 6
TOP = 30
MAX_REPORTS = 48

_started = False


def diag_dir() -> Path:
    return product.state_root() / "diag"


def _type_counts(limit: int = 25) -> list[tuple[str, int]]:
    counts: collections.Counter[str] = collections.Counter(type(obj).__qualname__ for obj in gc.get_objects())
    return counts.most_common(limit)


def _write_report(directory: Path, baseline: tracemalloc.Snapshot, previous: tracemalloc.Snapshot | None,
                  index: int) -> tracemalloc.Snapshot:
    current = tracemalloc.take_snapshot().filter_traces((
        tracemalloc.Filter(False, tracemalloc.__file__),
        tracemalloc.Filter(False, "<frozen importlib._bootstrap>"),
    ))
    traced, peak = tracemalloc.get_traced_memory()
    lines = [f"report {index} at {time.strftime('%Y-%m-%dT%H:%M:%S')}",
             f"traced_mb={traced / 2**20:.1f} peak_mb={peak / 2**20:.1f}", ""]
    for title, against in (("growth since first report", baseline), ("growth since previous report", previous)):
        if against is None:
            continue
        lines.append(f"== {title} (top {TOP} by size_diff, {FRAMES} frames) ==")
        for stat in current.compare_to(against, "traceback")[:TOP]:
            lines.append(f"{stat.size_diff / 2**20:+.2f} MB  {stat.count_diff:+d} blocks  now {stat.size / 2**20:.2f} MB")
            lines.extend(f"    {frame.filename}:{frame.lineno}" for frame in stat.traceback)
        lines.append("")
    lines.append("== largest live allocations by traceback ==")
    for stat in current.statistics("traceback")[:15]:
        lines.append(f"{stat.size / 2**20:.2f} MB  {stat.count} blocks")
        lines.extend(f"    {frame.filename}:{frame.lineno}" for frame in stat.traceback)
    lines.append("")
    lines.append("== most numerous object types ==")
    lines.extend(f"{count:>10}  {name}" for name, count in _type_counts())
    (directory / f"tracemalloc-{index:03d}.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return current


def _run(directory: Path, interval: float) -> None:
    baseline = None
    previous = None
    for index in range(MAX_REPORTS):
        try:
            if baseline is None:
                baseline = previous = _write_report(directory, tracemalloc.take_snapshot(), None, index)
            else:
                previous = _write_report(directory, baseline, previous, index)
        except Exception as error:  # diagnostics must never take the widget down
            (directory / "tracemalloc-error.txt").write_text(type(error).__name__ + "\n", encoding="utf-8")
            return
        time.sleep(interval)


def start_if_requested(interval: float = INTERVAL_SECONDS) -> bool:
    """Start tracemalloc and the report thread when the marker file exists."""
    global _started
    if _started:
        return True
    directory = diag_dir()
    if not (directory / MARKER).is_file():
        return False
    tracemalloc.start(FRAMES)
    _started = True
    threading.Thread(target=_run, args=(directory, interval), name="branchglance-memory-diag", daemon=True).start()
    return True
