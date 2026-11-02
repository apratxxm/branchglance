"""Work ledger service: the widget's single writer thread and the read-only ledger HTTP views.

Off unless TOKEN_TRACKER_LEDGER is exactly "1". The widget starts it after its HTTP server is up
and stops it on quit, so only the widget process ever writes the ledger. Standalone servers never
start a writer. The worker thread owns the writer connection: it constructs the Ledger, runs every
pass and closes it, and no other thread touches it.

Collection and display policy are resolved once at start, the same as the rest of the widget.
"""

from __future__ import annotations

import csv
import io
import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

from . import product
from .claude_hook_install import claude_config_dir
from .ledger import Ledger, LedgerUnavailable, read_summary
from .ledger_model import PROVIDERS
from .provider_policy import POLICIES, policy_for

ENV_FLAG = "TOKEN_TRACKER_LEDGER"
BUDGET_BYTES = 8 * 1024 * 1024
LIVE_INTERVAL_SECONDS = 30.0
STARTUP_DELAY_SECONDS = 60  # the first pass waits, so the widget's first live reads are not competing for CPU
MIN_BACKFILL_PAUSE_SECONDS = 0.2
BACKFILL_DUTY_MULTIPLIER = 4  # wait 4 x pass duration: the writer takes about 20% of one core while backfilling
BACKOFF_START_SECONDS = 5.0
BACKOFF_MAX_SECONDS = 300.0
RANGE_SECONDS: dict[str, int | None] = {"24h": 24 * 3600, "7d": 7 * 24 * 3600, "30d": 30 * 24 * 3600, "all": None}
EXPORT_FORMATS = ("csv", "json")
EXPORT_PATH = "/api/ledger/export"
CSV_COLUMNS = ("provider", "modelId", "displayName", "kind", "inputTotal", "cachedRead", "cacheWrite5m",
               "cacheWrite1h", "cacheWriteUnknown", "outputTotal", "reasoningOutput", "totalTokens",
               "knownCostUsd", "costComplete", "unpricedTokens", "coverageStatus", "gapCount", "rangeStart",
               "rangeEnd")
# Stored hourly rollups: a rolling window starts at the hour that contains its nominal start.
HOUR_SECONDS = 3600
FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")

_CURRENT: "LedgerService | None" = None
PURGE_WAIT_SECONDS = 10.0  # how long POST /api/ledger/purge waits for its ticket before answering 202
PURGE_OUTCOMES = ("done", "failed", "cancelled")


def current() -> "LedgerService | None":
    """The service the widget started, or None. The HTTP handler reads health and the data dir through it."""
    return _CURRENT


class PurgeTicket:
    """One accepted purge. Resolves exactly once: "done", "failed" (public error class name) or "cancelled"."""

    def __init__(self, provider: str) -> None:
        self.id = uuid.uuid4().hex
        self.provider = provider
        self.status = "queued"
        self.error: str | None = None
        self._resolved = threading.Event()

    def resolve(self, status: str, error: str | None = None) -> None:
        if not self._resolved.is_set():
            self.status, self.error = status, error
            self._resolved.set()

    def wait(self, timeout: float) -> bool:
        return self._resolved.wait(timeout)


def status_payload() -> dict[str, Any]:
    """Read-only health for the settings window: state, purging providers and the collect flags the writer uses."""
    service = _CURRENT
    if service is None:
        return {"ok": True, "state": "disabled", "pendingBytes": 0, "purging": [], "lastPurge": None, "collect": {}}
    return {"ok": True, **service.health(), "collect": dict(service.collect)}


class LedgerService:
    def __init__(self, *, data_dir: Path | str | None = None, claude_root: Path | str | None = None,
                 codex_root: Path | str | None = None, policies: Mapping[str, Any] | None = None,
                 environ: Mapping[str, str] | None = None, clock: Callable[[], float] = time.time,
                 monotonic: Callable[[], float] = time.monotonic,
                 waiter: Callable[[float], bool] | None = None, budget_bytes: int = BUDGET_BYTES,
                 ledger_factory: Callable[..., Any] = Ledger) -> None:
        self.environ = os.environ if environ is None else environ
        self.data_dir = Path(data_dir) if data_dir is not None else product.state_root(environ=environ) / "ledger"
        self.claude_root = Path(claude_root) if claude_root is not None else claude_config_dir()
        self.codex_root = Path(codex_root) if codex_root is not None else Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
        self._policies = policies
        self._clock = clock
        self._monotonic = monotonic
        self._budget = budget_bytes
        self._factory = ledger_factory
        self._stop = threading.Event()
        self._wake = threading.Event()  # set by purge requests and stop(): ends the worker's wait early
        self._wait = waiter or self._wait_wake
        self._lock = threading.Lock()  # guards the published state read by health() and the purge queue
        self._lifecycle = threading.Lock()  # serializes start() and stop()
        self._thread: threading.Thread | None = None
        self._ledger: Any = None  # worker-only: the writer connection never leaves the worker thread
        self._state = "disabled"
        self._last_pass_at: str | None = None
        self._last_error: str | None = None
        self._pending = 0
        self._tickets: dict[str, PurgeTicket] = {}  # unresolved purges per provider, queued or running
        self._queue: list[str] = []  # providers whose purge has not started, in request order
        self._last_purge: dict[str, dict[str, Any]] = {}  # newest result per provider, keyed by ticket
        self.collect: dict[str, bool] = {}
        self.show: dict[str, bool] = {}

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> None:
        """Idempotent: a second call while a worker exists does nothing. Never raises into the widget."""
        global _CURRENT
        with self._lifecycle:
            _CURRENT = self
            if self._thread is not None:
                return
            if self.environ.get(ENV_FLAG) != "1":
                self._set_state("disabled")
                return
            try:
                policies = self._policies if self._policies is not None else POLICIES
                self.collect = {name: policy_for(name, policies).allows("collect") for name in PROVIDERS}
                self.show = {name: policy_for(name, policies).allows("show") for name in PROVIDERS}
            except Exception as error:
                self._fail(error)
                return
            self._set_state("starting")
            try:
                thread = threading.Thread(target=self._run, name="branchglance-ledger", daemon=True)
                thread.start()
            except Exception as error:
                self._thread = None
                self._fail(error)
                return
            self._thread = thread

    def stop(self, timeout: float = 5.0) -> None:
        """Signal the worker and wait up to timeout. A worker still inside a pass is left to finish as a daemon.

        Queued purges cancel at once: nothing was deleted. A purge already running finishes its own
        transaction and resolves with its real outcome.
        """
        with self._lifecycle:
            self._stop.set()
            self._wake.set()
            thread = self._thread
        self._cancel_queued()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    def health(self) -> dict[str, Any]:
        with self._lock:
            return {"state": self._state, "lastPassAt": self._last_pass_at,
                    "lastError": self._last_error, "pendingBytes": self._pending,
                    "purging": sorted(self._tickets),
                    "lastPurge": {provider: dict(result) for provider, result in self._last_purge.items()}}

    # ------------------------------------------------------------------ purge queue

    def request_purge(self, provider: str) -> PurgeTicket:
        """Queue a purge of one provider's stored history. Coalesces with an unresolved request for it.

        The worker runs it between passes; a queued request also ends the running pass at its next
        safe point. Nothing is deleted on this thread.
        """
        with self._lock:
            ticket = self._tickets.get(provider)
            if ticket is not None:
                return ticket
            ticket = PurgeTicket(provider)
            if self._thread is None or self._stop.is_set():
                ticket.resolve("cancelled")
                return ticket
            self._tickets[provider] = ticket
            self._queue.append(provider)
        self._wake.set()
        return ticket

    def _purge_requested(self) -> bool:
        with self._lock:
            return bool(self._queue)

    def _cancel_queued(self) -> None:
        with self._lock:
            cancelled = [provider for provider in self._queue]
            tickets = [self._tickets.pop(provider) for provider in cancelled if provider in self._tickets]
            self._queue.clear()
            for ticket in tickets:
                self._last_purge[ticket.provider] = {"ticket": ticket.id, "result": "cancelled", "error": None}
        for ticket in tickets:
            ticket.resolve("cancelled")

    def _run_purges(self) -> None:
        """Worker only: run queued purges one at a time, before the next pass."""
        while True:
            with self._lock:
                if not self._queue:
                    return
                provider = self._queue.pop(0)
                ticket = self._tickets[provider]
                ticket.status = "running"
            if self._stop.is_set():
                self._finish_purge(ticket, "cancelled", None)
                continue
            try:
                if self._ledger is None:
                    self._ledger = self._factory(self.data_dir, claude_root=self.claude_root,
                                                 codex_root=self.codex_root)
                self._ledger.purge(provider)
            except Exception as error:
                # Drop the writer so the next purge or pass reopens it cleanly.
                self._close_ledger()
                self._finish_purge(ticket, "failed", type(error).__name__)
            else:
                self._finish_purge(ticket, "done", None)

    def _finish_purge(self, ticket: PurgeTicket, status: str, error: str | None) -> None:
        with self._lock:
            if self._tickets.get(ticket.provider) is ticket:
                del self._tickets[ticket.provider]
            self._last_purge[ticket.provider] = {"ticket": ticket.id, "result": status, "error": error}
        ticket.resolve(status, error)

    def _stop_requested(self) -> bool:
        return self._stop.is_set() or self._purge_requested()

    def _wait_wake(self, seconds: float) -> bool:
        """Wait for the cadence or a purge or stop wake-up. True means stop."""
        self._wake.wait(seconds)
        self._wake.clear()
        return self._stop.is_set()

    # ------------------------------------------------------------------ worker

    def _run(self) -> None:
        backoff = BACKOFF_START_SECONDS
        try:
            # The loop checks _stop itself, so a waiter that returns True here still lets the first pass run.
            self._pause(STARTUP_DELAY_SECONDS)
            while not self._stop.is_set():
                self._run_purges()
                if self._stop.is_set():
                    break
                if self._ledger is None:
                    try:
                        self._ledger = self._factory(self.data_dir, claude_root=self.claude_root,
                                                     codex_root=self.codex_root)
                    except Exception as error:
                        self._fail(error)
                        wait, backoff = backoff, min(backoff * 2, BACKOFF_MAX_SECONDS)
                        if self._pause(wait):
                            break
                        continue
                try:
                    began = self._monotonic()
                    result = self._ledger.ingest(self.collect, budget_bytes=self._budget,
                                                 should_stop=self._stop_requested)
                    if result.get("stopped"):
                        if self._stop.is_set():
                            break
                        continue  # a queued purge ended this pass: run it at the top of the loop
                    duration = self._monotonic() - began
                    # Scheduling uses every collected provider's backlog, not the shown subset.
                    internal = int(result["pendingBytes"])
                    progressing = result["bytesRead"] > 0 and internal > 0
                    self._succeed(self._ledger.pending_bytes(self.show), progressing)
                    backoff = BACKOFF_START_SECONDS
                    wait = max(MIN_BACKFILL_PAUSE_SECONDS, BACKFILL_DUTY_MULTIPLIER * duration) if progressing \
                        else LIVE_INTERVAL_SECONDS
                except Exception as error:
                    # Drop the writer and rebuild it on retry, so recovery runs again if the file is damaged.
                    self._close_ledger()
                    self._fail(error)
                    wait, backoff = backoff, min(backoff * 2, BACKOFF_MAX_SECONDS)
                if self._pause(wait):
                    break
        finally:
            self._cancel_queued()
            self._close_ledger()

    def _pause(self, seconds: float) -> bool:
        return bool(self._wait(seconds))

    def _close_ledger(self) -> None:
        ledger, self._ledger = self._ledger, None
        if ledger is not None:
            try:
                ledger.close()
            except Exception:
                pass

    def _succeed(self, pending: int, progressing: bool) -> None:
        with self._lock:
            self._state = "backfilling" if progressing else "live"
            self._last_pass_at = _iso(self._clock())
            self._last_error = None
            self._pending = pending

    def _fail(self, error: BaseException) -> None:
        # The public error code is the exception class name only: never a path or a message.
        with self._lock:
            self._state = "error"
            self._last_error = type(error).__name__

    def _set_state(self, state: str) -> None:
        with self._lock:
            self._state = state


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(int(epoch), timezone.utc).isoformat()


# ---------------------------------------------------------------------- HTTP views

def http_response(path: str, query: Mapping[str, list[str]], *, now: float | None = None) -> tuple[int, dict[str, str], bytes]:
    """(status, headers, body) for GET /api/ledger and GET /api/ledger/export. Read-only: never creates or writes storage."""
    range_name = _single(query, "range")
    if range_name not in RANGE_SECONDS:
        return _error(400, "Unknown ledger range.")
    export_format = None
    if path == EXPORT_PATH:
        export_format = _single(query, "format")
        if export_format not in EXPORT_FORMATS:
            return _error(400, "Unknown ledger export format.")
    service = _CURRENT
    if service is None or service.health()["state"] == "disabled":
        return _error(503, "Ledger is off.")
    now = time.time() if now is None else now
    span = RANGE_SECONDS[range_name]
    # Windows are hour-aligned to the stored grain, so the reported start is the usage actually included.
    start, end = (0.0, now) if span is None else (int((now - span) // HOUR_SECONDS) * HOUR_SECONDS, now)
    try:
        payload = read_summary(service.data_dir, start, end, show=service.show, time_zone=_time_zone(),
                               plan_fee_usd=None)
    except LedgerUnavailable:
        return _error(503, "Ledger is starting.")
    if export_format is None:
        payload["service"] = service.health()
        return _respond(200, "application/json; charset=utf-8", json.dumps(payload).encode("utf-8"))
    if export_format == "csv":
        body, content_type = csv_text(payload).encode("utf-8"), "text/csv; charset=utf-8"
    else:
        body, content_type = json.dumps(payload).encode("utf-8"), "application/json; charset=utf-8"
    headers = {"Content-Disposition": f'attachment; filename="branchglance-ledger-{range_name}.{export_format}"'}
    return _respond(200, content_type, body, headers)


def csv_text(payload: Mapping[str, Any]) -> str:
    """One row per summary row, then a total row, so an empty export still has one data row.

    Every row carries the coverage status, gap count and range, so partial data cannot be read as complete.
    String cells that a spreadsheet would evaluate get a leading apostrophe.
    """
    shared = {"coverageStatus": payload["coverage"]["status"], "gapCount": payload["coverage"]["gapCount"],
              "rangeStart": payload["range"]["start"], "rangeEnd": payload["range"]["end"]}
    records = [{**row, **shared} for row in payload["rows"]]
    records.append({**payload["totals"], "provider": "total", **shared})
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(CSV_COLUMNS)
    for record in records:
        writer.writerow([_cell(record.get(name)) for name in CSV_COLUMNS])
    return buffer.getvalue()


def _cell(value: Any) -> Any:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str) and value.startswith(FORMULA_PREFIXES):
        return "'" + value
    return "" if value is None else value


def _single(query: Mapping[str, list[str]], name: str) -> str | None:
    values = query.get(name, [])
    return values[0] if len(values) == 1 else None


def _time_zone() -> str:
    """The local IANA zone when TZ names one; otherwise UTC. Reading the Windows zone is not cheap."""
    name = os.environ.get("TZ", "")
    if "/" in name:
        try:
            ZoneInfo(name)
            return name
        except Exception:
            pass
    return "UTC"


def _error(status: int, message: str) -> tuple[int, dict[str, str], bytes]:
    return _respond(status, "application/json; charset=utf-8", json.dumps({"ok": False, "error": message}).encode("utf-8"))


def _respond(status: int, content_type: str, body: bytes, extra: Mapping[str, str] | None = None) -> tuple[int, dict[str, str], bytes]:
    headers = {"Content-Type": content_type, "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
    headers.update(extra or {})
    return status, headers, body
