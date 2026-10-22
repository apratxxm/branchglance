"""Fail-closed inspection and queue transport for reset-triggered Codex messages."""
from __future__ import annotations

import os
import re
import shutil
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from collections.abc import Mapping

from . import providers
from .codex_client import CodexAppServer, CodexUnavailable
from .provider_policy import POLICIES, ProviderPolicy, ProviderPolicyError, policy_for


THREAD_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
QUEUE_TIMEOUT_SECONDS = 15
MAX_CANDIDATES = 100
TERMINAL_TURN_STATUSES = {"completed", "interrupted", "failed"}
WAITING_FLAGS = {"waitingOnApproval", "waitingOnUserInput"}


def _database_path() -> Path:
    root = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex"))
    return root / "state_5.sqlite"


def _metadata(*, thread_id: str | None = None, limit: int | None = None) -> list[dict[str, Any]]:
    database = _database_path()
    if not database.is_file():
        raise CodexUnavailable("Codex thread metadata is unavailable")
    try:
        with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=0.2) as connection:
            connection.row_factory = sqlite3.Row
            columns = {row[1] for row in connection.execute("PRAGMA table_info(threads)")}
            if not {"id", "archived"}.issubset(columns):
                raise CodexUnavailable("Codex thread metadata schema is unavailable")
            selected = [name for name in ("id", "rollout_path", "title", "name", "archived") if name in columns]
            projection = ", ".join(selected)
            if thread_id is not None:
                rows = connection.execute(
                    f"SELECT {projection} FROM threads WHERE id = ?",
                    (thread_id,),
                ).fetchall()
            else:
                order_by = "updated_at DESC" if "updated_at" in columns else "id"
                rows = connection.execute(
                    f"SELECT {projection} FROM threads WHERE archived = 0 ORDER BY {order_by} LIMIT ?",
                    (limit,),
                ).fetchall()
    except (sqlite3.Error, CodexUnavailable) as exc:
        if isinstance(exc, CodexUnavailable):
            raise
        raise CodexUnavailable("Codex thread metadata could not be read") from exc
    return [dict(row) for row in rows]


def _status_type(status: Any) -> str | None:
    if isinstance(status, dict):
        value = status.get("type")
    else:
        value = status
    return value if isinstance(value, str) else None


def _failure(reason: str, *, exists: bool, archived: bool = False, observed_at: float | None = None,
             running: bool = False, waiting: bool = False) -> dict[str, Any]:
    return {
        "exists": exists,
        "archived": archived,
        "running": running,
        "waiting": waiting,
        "knownIdle": False,
        "observedAt": observed_at,
        "reason": reason,
    }


def _latest_turn_status(thread: dict[str, Any]) -> str | None:
    turns = thread.get("turns")
    if not isinstance(turns, list) or not turns:
        return None
    latest = turns[-1]
    if not isinstance(latest, dict):
        return None
    value = latest.get("status")
    return value if isinstance(value, str) else None


def list_candidates(limit: int = MAX_CANDIDATES, *, client: CodexAppServer | None = None,
                    policies: Mapping[str, ProviderPolicy] | None = None) -> list[dict[str, Any]]:
    """Return bounded, available, non-archived task choices for explicit selection."""
    policy = policy_for("Codex", POLICIES if policies is None else policies)
    policy.require("collect")
    policy.require("show")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_CANDIDATES:
        raise ValueError(f"candidate limit must be between 1 and {MAX_CANDIDATES}")
    metadata = {row["id"]: row for row in _metadata(limit=limit) if not row.get("archived")}
    threads = (client or CodexAppServer(timeout=5)).list_threads(limit=limit)
    candidates = []
    for thread in threads:
        ident = thread.get("id")
        row = metadata.get(ident)
        if not isinstance(ident, str) or row is None:
            continue
        title = row.get("name") or row.get("title") or thread.get("preview") or "Untitled task"
        candidates.append({"id": ident, "title": " ".join(str(title).split())[:120],
                           "status": _status_type(thread.get("status")) or "unknown"})
    return candidates[:limit]


def inspect_target(thread_id: str, *, client: CodexAppServer | None = None,
                   policies: Mapping[str, ProviderPolicy] | None = None) -> dict[str, Any]:
    """Inspect one selected thread and mark it idle only with positive evidence."""
    policy_for("Codex", POLICIES if policies is None else policies).require("collect")
    if not isinstance(thread_id, str) or not THREAD_ID.fullmatch(thread_id):
        return _failure("invalid_thread_id", exists=False)
    try:
        rows = _metadata(thread_id=thread_id)
    except CodexUnavailable:
        return _failure("metadata_unavailable", exists=False)
    if not rows:
        return _failure("not_found", exists=False)
    metadata = rows[0]
    archived = bool(metadata.get("archived"))
    if archived:
        return _failure("archived", exists=True, archived=True)
    rollout_path = metadata.get("rollout_path")
    if not isinstance(rollout_path, str) or not rollout_path:
        return _failure("rollout_metadata_missing", exists=True)
    if not Path(rollout_path).is_file():
        return _failure("rollout_unavailable", exists=True)

    observed_at = time.time()
    app_client = client or CodexAppServer(timeout=5)
    try:
        thread = app_client.read_thread(thread_id, include_turns=False)
    except (CodexUnavailable, OSError, TimeoutError):
        return _failure("thread_read_unavailable", exists=True, observed_at=observed_at)
    if thread.get("id") != thread_id:
        return _failure("thread_identity_mismatch", exists=True, observed_at=observed_at)
    observed_at = time.time()
    observed_iso = datetime.fromtimestamp(observed_at, timezone.utc).isoformat()

    status = thread.get("status")
    status_type = _status_type(status)
    active_flags = status.get("activeFlags") if isinstance(status, dict) else None
    if status_type == "active":
        if not isinstance(active_flags, list) or any(not isinstance(flag, str) for flag in active_flags):
            return _failure("active_status_malformed", exists=True, observed_at=observed_at, running=True)
        waiting = any(flag in WAITING_FLAGS for flag in active_flags)
        return _failure("waiting_for_input" if waiting else "active", exists=True,
                        observed_at=observed_at, running=True, waiting=waiting)

    latest_turn_status = _latest_turn_status(thread)
    projection_status = status
    if status_type == "notLoaded":
        try:
            latest_turn = app_client.latest_turn(thread_id)
        except (CodexUnavailable, OSError, TimeoutError):
            return _failure("not_loaded_turn_unavailable", exists=True, observed_at=observed_at)
        latest_turn_status = latest_turn.get("status") if isinstance(latest_turn, dict) else None
        if latest_turn_status not in TERMINAL_TURN_STATUSES:
            return _failure("not_loaded_without_terminal_turn", exists=True, observed_at=observed_at,
                            running=latest_turn_status == "inProgress")
        recorded = providers._codex_rollout_state(rollout_path)
        recorded_status = recorded.get("status")
        if recorded.get("telemetryUnavailable") or recorded.get("questionCorrelationUnavailable"):
            return _failure("rollout_lifecycle_unknown", exists=True, observed_at=observed_at)
        if recorded_status in {"running", "compacting"}:
            return _failure("unfinished_rollout", exists=True, observed_at=observed_at, running=True)
        if recorded_status == "needs input":
            return _failure("waiting_for_input", exists=True, observed_at=observed_at, waiting=True)
        if recorded_status not in {"idle", "failed"}:
            return _failure("rollout_lifecycle_unknown", exists=True, observed_at=observed_at)
        if ((recorded.get("question") or recorded.get("questionIds"))
                and not recorded.get("questionResolutionAt")):
            return _failure("waiting_for_input", exists=True, observed_at=observed_at, waiting=True)
        projection_status = {"type": "idle"}
        thread = {**thread, "turns": [latest_turn]}
    elif status_type == "idle":
        if latest_turn_status == "inProgress":
            return _failure("turn_in_progress", exists=True, observed_at=observed_at, running=True)
    elif status_type == "systemError":
        return _failure("system_error", exists=True, observed_at=observed_at)
    else:
        return _failure("unknown_status", exists=True, observed_at=observed_at)

    try:
        projected = providers.CodexProvider._thread({
            **thread,
            "id": thread_id,
            "path": rollout_path,
            "status": projection_status,
            "statusObservedAt": observed_iso,
        })
    except Exception:
        return _failure("question_projection_unavailable", exists=True, observed_at=observed_at)
    if projected.get("statusSource") == "unavailable" or projected.get("telemetryUnavailable"):
        return _failure("question_projection_unavailable", exists=True, observed_at=observed_at)
    if projected.get("questionCorrelationUnavailable"):
        return _failure("question_state_unknown", exists=True, observed_at=observed_at)
    projected_status = projected.get("status")
    if projected_status in {"running", "quiet", "compacting"}:
        return _failure("active", exists=True, observed_at=observed_at, running=True)
    if projected_status not in {"idle", "failed", "needs input"}:
        return _failure("question_projection_unavailable", exists=True, observed_at=observed_at)
    unresolved_question = (
        projected.get("status") == "needs input"
        or bool(projected.get("pendingQuestion") or projected.get("pendingQuestionIds"))
        and not projected.get("questionResolutionAt")
    )
    if unresolved_question:
        return _failure("waiting_for_input", exists=True, observed_at=observed_at, waiting=True)
    return {
        "exists": True,
        "archived": False,
        "running": False,
        "waiting": False,
        "knownIdle": True,
        "observedAt": observed_at,
        "reason": "idle" if status_type == "idle" else "not_loaded_terminal_turn",
    }


def queue_continue(
    thread_id: str,
    *,
    executable: str | None = None,
    runner: Callable[..., Any] = subprocess.run,
    policies: Mapping[str, ProviderPolicy] | None = None,
) -> dict[str, Any]:
    """Queue the exact user message `continue`; this does not confirm execution."""
    if not isinstance(thread_id, str) or not THREAD_ID.fullmatch(thread_id):
        return {"status": "rejected", "error": "invalid thread id"}
    try:
        policy_for("Codex", POLICIES if policies is None else policies).require("continue")
    except ProviderPolicyError as exc:
        return {"status": "rejected", "error": str(exc)}
    executable = executable or shutil.which("codex")
    if not executable:
        return {"status": "rejected", "error": "Codex CLI is unavailable"}
    flags = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    try:
        result = runner(
            [executable, "queue", "--thread", thread_id, "--message", "continue"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=QUEUE_TIMEOUT_SECONDS,
            check=False,
            shell=False,
            **flags,
        )
    except subprocess.TimeoutExpired:
        return {"status": "unknown", "error": "queue status unknown; check the Codex task"}
    except OSError:
        return {"status": "rejected", "error": "could not start Codex CLI"}
    if result.returncode:
        return {"status": "rejected", "error": "Codex CLI rejected the queue request"}
    return {"status": "queued"}
