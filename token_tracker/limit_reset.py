from __future__ import annotations

import copy
import atexit
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from collections.abc import Mapping

from . import product
from .codex_client import CodexAppServer, CodexNotStarted, CodexUnavailable, resolve_codex, run_contained_codex
from .providers import ClaudeDesktopProvider, _usage_percent
from .claude_cache import usage_info
from . import claude_statusline
from .reset_actions import ResetActionDispatcher, default_action
from .provider_policy import POLICIES, ProviderPolicy, ProviderPolicyError, policy_for


PROVIDERS = ("Claude", "Codex")
EXHAUSTED_PERCENT = 99
WEEKLY_EXHAUSTED_PERCENT = 100
RESET_PERCENT = 95
DEFAULT_POLL_SECONDS = 2
RETRY_SECONDS = 15 * 60
AUTH_RETRY_SECONDS = 2 * 60 * 60
GREETING_PHASE_SECONDS = 5 * 60 * 60
ROLLING_WINDOW_SECONDS = 5 * 60 * 60
GREETING_VERIFY_SECONDS = 60
# Codex reports one fixed deadline as N or N+1 across polls.
DEADLINE_JITTER_SECONDS = 2
IDLE_POLL_SECONDS = 2
NEAR_DEADLINE_SECONDS = 120
MAX_UNVERIFIED_ATTEMPTS = 3
PERSIST_RETRY_BASE_SECONDS = 30
PERSIST_RETRY_MAX_SECONDS = 15 * 60
# GPT-5.6 Luna is the lowest-cost current Codex model.  Keep this explicit so
# the reset action remains deterministic even if the user's default model or
# CLI configuration changes.
CODEX_GREETING_MODEL = "gpt-5.6-luna"
CLAUDE_GREETING_MODEL = "claude-haiku-4-5-20251001"
GREETER_ENABLED_PROVIDERS = ("Claude", "Codex")
CLAUDE_RESET_DROP_PERCENT = 20
ERROR_LIMIT = 300
WEEKLY_BLOCKED_ERROR = "weekly limit exhausted"
LOGIN_ERRORS = {
    "Claude": "Claude Code sign-in expired — sign in again with the Claude CLI",
    "Codex": "Codex CLI sign-in expired — sign in again with the Codex CLI",
}

PHASE_WAITING_FOR_EXHAUSTION = "waiting_for_exhaustion"
PHASE_ARMED_WAITING_FOR_RESET = "armed_waiting_for_reset"
PHASE_RESET_OBSERVED_SENDING = "reset_observed_sending"
PHASE_NEEDS_LOGIN = "needs_login"
PHASE_BLOCKED_WEEKLY = "blocked_weekly"
PHASE_GREETING_SENT = "greeting_sent"
PHASE_KEEPALIVE_SCHEDULED = "keepalive_scheduled"
PHASE_GREETING_VERIFYING = "greeting_verifying"
PHASE_DISABLED = "disabled"

_AUTH_PATTERN = re.compile(
    r"authenticat|oauth|not\s+logged\s+in|\blog\s*-?\s*in\b|\bsign\s*-?\s*in\b|\b401\b|unauthori[sz]ed"
    r"|invalid\s+api\s+key|api\s+key\s+(?:is\s+)?(?:missing|invalid|expired)|credential",
    re.IGNORECASE,
)
_SECRET_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=\-]{8,}"),
    re.compile(r"(?i)\b((?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|secret)\s*[:=]\s*)\S+"),
)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso_now() -> str:
    return _utc_now().isoformat()


def _iso_from_epoch(seconds: int | float | None) -> str | None:
    try:
        value = float(seconds)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    if value > 10_000_000_000:
        value /= 1000
    try:
        return datetime.fromtimestamp(value, timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _epoch_seconds(value: Any) -> float | None:
    """Normalize a scheduled reset value (epoch s/ms or ISO text) to epoch seconds."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        if value <= 0:
            return None
        return float(value) / 1000 if value > 10_000_000_000 else float(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    return None


def _valid_quota_percent(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) and 0 <= result <= 100 else None


def redact_secrets(text: str) -> str:
    """Strip anything that looks like a credential from CLI output before storing it."""
    for pattern in _SECRET_PATTERNS:
        if pattern.groups:
            text = pattern.sub(lambda match: match.group(1) + "[redacted]", text)
        else:
            text = pattern.sub("[redacted]", text)
    return text


def is_auth_failure(text: str | None) -> bool:
    """True when greeting CLI output points at a sign-in/credential problem."""
    return bool(text) and bool(_AUTH_PATTERN.search(str(text)))


def classify_send_failure(result: dict[str, Any]) -> str:
    """Return "auth" for sign-in/credential failures, otherwise "transient"."""
    text = " ".join(str(result.get(key) or "") for key in ("error", "output"))
    return "auth" if is_auth_failure(text) else "transient"


def _runtime_dir() -> Path:
    return product.state_root()


def _find_executable(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    if name == "claude" and os.name == "nt":
        app_data = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        candidates = [
            app_data / "npm" / "claude.exe",
            app_data / "npm" / "node_modules" / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe",
        ]
        for candidate in candidates:
            if candidate.is_file():
                return str(candidate)
    return None


@dataclass(frozen=True)
class LimitSample:
    five_hour_percent: int | float | None
    weekly_percent: int | float | None
    observed_at: float
    reset_at: int | float | None = None
    weekly_reset_at: int | float | None = None


def _claude_desktop_data(now: float | None = None, max_usage_age: float | None = None, expected_org_id: str | None = None):
    try:
        provider = ClaudeDesktopProvider()
        samples, _ = provider._usage()
        org = samples[-1].get("org") if samples else None
        if expected_org_id is not None and org != expected_org_id:
            return org, {}
        kwargs = {} if now is None else {"now": now, "max_usage_age": max_usage_age}
        return org, usage_info(provider.root, samples, **kwargs)
    except (OSError, ValueError, TypeError, AttributeError, RuntimeError, IndexError, KeyError):
        return None, {"fiveHour": None, "weekly": None, "fiveHourPercent": None, "weeklyPercent": None, "observedAt": None}


def claude_limit_sample() -> LimitSample:
    org, desktop = _claude_desktop_data()
    usage = claude_statusline.merge_usage(desktop, org_id=org, max_age=90)
    observed_at = float(usage.get("observedAt") or 0)
    return LimitSample(
        _usage_percent(usage.get("fiveHourPercent")),
        _usage_percent(usage.get("weeklyPercent")),
        observed_at,
        usage["fiveHour"] or usage.get("lastKnownFiveHour"),
        usage["weekly"] or usage.get("lastKnownWeekly"),
    )


def _read_codex_limit_sample(client) -> LimitSample:
    rate = client.rate_limits().get("rateLimits") or {}
    primary, secondary = rate.get("primary") or {}, rate.get("secondary") or {}
    return LimitSample(
        _usage_percent(primary.get("usedPercent")),
        _usage_percent(secondary.get("usedPercent")),
        time.time(),
        primary.get("resetsAt"),
        secondary.get("resetsAt"),
    )


CODEX_QUOTA_TTL_SECONDS = 2.0
CODEX_QUOTA_RETRY_SECONDS = 5.0


def _checked_at(sample: LimitSample) -> str | None:
    observed = sample.observed_at
    if isinstance(observed, bool) or not isinstance(observed, (int, float)) or not math.isfinite(float(observed)):
        return None
    return _iso_from_epoch(observed)


def _claude_checked_at(sample: LimitSample, now_iso: str) -> str | None:
    """A Claude poll with no fresh-enough percent is not a dated observation.

    The greeter's strict freshness gate leaves both percents empty between Claude
    Desktop's few-minute refetches. Dating that gap with the poll time would make it
    look newer than the display's valid meters and blank them as an explicit null.
    """
    if sample.five_hour_percent is None and sample.weekly_percent is None:
        return None
    return now_iso


def _codex_quota_error_category(exc: Exception, *, worker_start: bool = False) -> str:
    if worker_start:
        return "worker_start_failed"
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, OSError):
        return "io_error"
    if isinstance(exc, CodexUnavailable):
        return "unavailable"
    if isinstance(exc, (TypeError, ValueError)):
        return "invalid_response"
    return "read_failed"


class CodexQuotaCache:
    """Single-flight read-only quota samples shared by the UI and reset watcher."""

    def __init__(self, *, client, clock=time.monotonic, wall_clock=time.time, thread_factory=None):
        self.client = client
        self.clock = clock
        self.wall_clock = wall_clock
        self.thread_factory = thread_factory or threading.Thread
        self.condition = threading.Condition(threading.Lock())
        self.sample: LimitSample | None = None
        self.sampled_at: float | None = None
        self.generation = 0
        self.refreshing = False
        self.retry_after = 0.0
        self.error_category: str | None = None
        self._last_error: Exception | None = None
        self._attempt = 0
        self._completed_attempt = 0

    def _age_locked(self, now: float) -> float | None:
        if self.sample is None or self.sampled_at is None:
            return None
        return max(0.0, now - self.sampled_at)

    def _metadata_locked(self, now: float) -> dict[str, Any]:
        age = self._age_locked(now)
        return {
            "ready": self.sample is not None,
            "fresh": age is not None and age <= CODEX_QUOTA_TTL_SECONDS,
            "ageSeconds": round(age, 3) if age is not None else None,
            "refreshing": self.refreshing,
            "errorCategory": self.error_category,
        }

    @staticmethod
    def _last_status(sample: LimitSample | None) -> dict[str, Any] | None:
        if sample is None:
            return None
        result: dict[str, Any] = {"observed_at": sample.observed_at}
        checked_at = _checked_at(sample)
        if checked_at is not None:
            result["checkedAt"] = checked_at
        for key in ("five_hour_percent", "weekly_percent", "reset_at", "weekly_reset_at"):
            value = getattr(sample, key)
            if value is not None:
                result[key] = value
        return result

    def http_view(self) -> dict[str, Any]:
        with self.condition:
            now = self.clock()
            age = self._age_locked(now)
            if ((age is None or age > CODEX_QUOTA_TTL_SECONDS) and not self.refreshing
                    and now >= self.retry_after):
                self._start_locked()
            # Select the sample and metadata under one lock; LimitSample is frozen.
            sample = self.sample
            metadata = self._metadata_locked(self.clock())
        last_status = self._last_status(sample)
        codex: dict[str, Any] = {}
        if last_status is not None:
            codex["lastStatus"] = last_status
        return {"providers": {"Codex": codex}, "quotaCache": metadata}

    def _start_locked(self) -> int:
        self._attempt += 1
        attempt = self._attempt
        self.refreshing = True
        try:
            worker = self.thread_factory(
                target=self._refresh, args=(attempt,), name="token-codex-quota", daemon=True,
            )
            worker.start()
        except Exception as exc:
            self._record_failure_locked(exc, attempt, worker_start=True)
            self.condition.notify_all()
        return attempt

    def _record_failure_locked(self, exc: Exception, attempt: int, *, worker_start: bool = False) -> None:
        self.refreshing = False
        self.retry_after = self.clock() + CODEX_QUOTA_RETRY_SECONDS
        self.error_category = _codex_quota_error_category(exc, worker_start=worker_start)
        self._last_error = exc
        self._completed_attempt = attempt

    def _refresh(self, attempt: int) -> None:
        try:
            sample = _read_codex_limit_sample(self.client)
            if not isinstance(sample, LimitSample):
                raise TypeError("quota reader returned an invalid sample")
            checked_at = _checked_at(sample)
            if checked_at is None:
                raise ValueError("quota sample observation time is invalid")
            observed = float(sample.observed_at)
            if observed > self.wall_clock() + 1.0:
                raise ValueError("quota sample observation time is in the future")
        except Exception as exc:
            with self.condition:
                self._record_failure_locked(exc, attempt)
                self.condition.notify_all()
            return

        with self.condition:
            self.sample = sample
            self.sampled_at = self.clock()
            self.generation += 1
            self.refreshing = False
            self.retry_after = 0.0
            self.error_category = None
            self._last_error = None
            self._completed_attempt = attempt
            self.condition.notify_all()

    def read_for_action(self) -> LimitSample:
        """Require a successful read newer than this call's entry generation."""
        with self.condition:
            generation_at_entry = self.generation
            if self.refreshing:
                attempt = self._attempt
            else:
                if self.clock() < self.retry_after:
                    if self._last_error is not None:
                        raise self._last_error
                    raise CodexUnavailable("Codex quota sample is temporarily unavailable")
                attempt = self._start_locked()
            while self._completed_attempt < attempt:
                self.condition.wait()
            if self.generation <= generation_at_entry or self.sample is None:
                if self._last_error is not None:
                    raise self._last_error
                raise CodexUnavailable("Codex quota sample was not refreshed")
            return self.sample


# One persistent local app-server client backs both consumers. HTTP is stale-while-revalidate;
# reset actions require a fresh generation and never consume this last-good HTTP fallback.
_CODEX_LIMIT_CLIENT = CodexAppServer(timeout=10)
_CODEX_QUOTA_CACHE = CodexQuotaCache(client=_CODEX_LIMIT_CLIENT)
atexit.register(_CODEX_LIMIT_CLIENT.close)


def codex_limit_sample() -> LimitSample:
    return _CODEX_QUOTA_CACHE.read_for_action()


def codex_quota_http_view() -> dict[str, Any]:
    return _CODEX_QUOTA_CACHE.http_view()


def _run_command(command: list[str], cwd: Path, timeout: int = 180) -> dict[str, Any]:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    completed = subprocess.run(
        command,
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=timeout,
        creationflags=flags,
        env={**os.environ, "NO_COLOR": "1"},
    )
    output = (completed.stdout or completed.stderr or "").strip()
    return {"ok": completed.returncode == 0, "returnCode": completed.returncode, "output": output[-300:]}


def send_codex_greeting(cwd: Path, *, policies: Mapping[str, ProviderPolicy] | None = None) -> dict[str, Any]:
    policy_for("Codex", policies).require("greet")
    try:
        executable = resolve_codex()
    except OSError as exc:
        return {"ok": False, "outcome": "not_started", "error": str(exc)}
    if not executable:
        return {"ok": False, "outcome": "not_started", "error": "Codex CLI is not installed"}
    try:
        completed = run_contained_codex(
            executable, [
            "exec", "--ephemeral", "--ignore-user-config", "--ignore-rules",
            "--skip-git-repo-check", "--sandbox", "read-only", "--model", CODEX_GREETING_MODEL,
            "--config", 'model_reasoning_effort="low"', "--config", 'model_verbosity="low"',
            "--cd", str(cwd), "Reply with exactly: hey",
        ],
            cwd=cwd, timeout=180, env={"NO_COLOR": "1"},
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "outcome": "unknown", "error": "greeting delivery is unknown"}
    except CodexNotStarted as exc:
        return {"ok": False, "outcome": "not_started", "error": str(exc)}
    output = (completed.stdout or completed.stderr or "").strip()
    return {"ok": completed.returncode == 0, "returnCode": completed.returncode, "output": output[-300:]}


_CLAUDE_ENV_BLOCK = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX", "CLAUDE_CODE_USE_FOUNDRY")
_CLAUDE_OUTPUT_CAP = 256 * 1024


def _claude_process(command: list[str], cwd: Path, timeout: int) -> dict[str, Any]:
    env = {key: value for key, value in os.environ.items() if key.upper() not in _CLAUDE_ENV_BLOCK}
    env["NO_COLOR"] = "1"
    try:
        process = subprocess.Popen(command, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), env=env)
    except OSError:
        return {"started": False, "error": "Claude Code CLI could not be started"}
    chunks: list[bytes] = []
    def drain() -> None:
        remaining = _CLAUDE_OUTPUT_CAP
        while True:
            chunk = process.stdout.read(8192)
            if not chunk:
                break
            if remaining:
                chunks.append(chunk[:remaining])
                remaining -= min(remaining, len(chunk))
    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    try:
        code = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        reader.join(timeout=2)
        return {"started": True, "error": "Claude Code CLI timed out", "data": b"".join(chunks)}
    reader.join(timeout=2)
    return {"started": True, "returnCode": code, "data": b"".join(chunks)}


def _claude_auth_status(cwd: Path) -> dict[str, Any]:
    executable = _find_executable("claude")
    if not executable:
        return {"ok": False, "error": "Claude Code CLI is not installed"}
    result = _claude_process([executable, "auth", "status", "--json"], cwd, 15)
    try:
        data = json.loads(result.get("data", b""))
        org = data.get("orgId")
        valid = (result.get("returnCode") == 0 and data.get("loggedIn") is True
                 and data.get("authMethod") == "claude.ai" and data.get("apiProvider") == "firstParty"
                 and isinstance(org, str) and bool(org.strip()))
    except (ValueError, TypeError, AttributeError):
        valid = False
    if not valid:
        return {"ok": False, "error": "Claude Code subscription sign-in required"}
    return {"ok": True, "orgId": org}


def send_claude_greeting(cwd: Path, *, policies: Mapping[str, ProviderPolicy] | None = None, expected_org_id: str | None = None) -> dict[str, Any]:
    policy_for("Claude", policies).require("greet")
    auth = _claude_auth_status(cwd)
    if not auth.get("ok"):
        return {**auth, "preSendFailure": True}
    if expected_org_id is not None and auth.get("orgId") != expected_org_id:
        return {"ok": False, "error": "Claude Code subscription sign-in changed", "preSendFailure": True}
    executable = _find_executable("claude")
    if not executable:
        return {"ok": False, "error": "Claude Code CLI is not installed", "preSendFailure": True}
    result = _claude_process(
        [
            executable, "--print", "Reply with exactly: Hi", "--model", CLAUDE_GREETING_MODEL,
            "--effort", "low", "--max-turns", "1", "--permission-mode", "plan",
            "--tools", "", "--no-session-persistence", "--disable-slash-commands",
            "--setting-sources", "", "--strict-mcp-config", "--system-prompt", "Reply briefly.",
            "--output-format", "stream-json", "--verbose",
        ],
        cwd, 180,
    )
    if not result.get("started"):
        return {"ok": False, "error": result.get("error"), "preSendFailure": True}
    auth_source = None
    windows = None
    final = None
    for line in result.get("data", b"").splitlines():
        try:
            event = json.loads(line)
            if not isinstance(event, dict):
                continue
            if event.get("type") == "system" and event.get("subtype") == "init":
                auth_source = event.get("apiKeySource")
            elif event.get("type") == "rate_limit_event" and isinstance(event.get("rate_limit_info"), dict):
                windows = event["rate_limit_info"].get("unifiedWindows")
            elif event.get("type") == "result":
                final = event
        except (ValueError, TypeError):
            continue
    now = time.time()
    converted = {}
    if isinstance(windows, dict):
        for name, value in windows.items():
            if name not in claude_statusline.WINDOWS or not isinstance(value, dict):
                continue
            utilization = value.get("utilization")
            if claude_statusline._number(utilization) and 0 <= utilization <= 1:
                converted[name] = {"used_percentage": utilization * 100, "resets_at": value.get("resetsAt")}
    limits = claude_statusline.parse({"rate_limits": converted}, now=now)
    subscription = auth_source == "none"
    not_subscription = auth_source is not None and not subscription
    ok = subscription and result.get("returnCode") == 0 and final is not None and final.get("is_error") is False
    explicit_failure = final is not None and (final.get("is_error") is True or (result.get("returnCode") is not None and result.get("returnCode") != 0))
    outcome = "uncertain" if auth_source is None else "completed" if ok else "failed" if explicit_failure else "uncertain"
    return {"ok": ok, "returnCode": result.get("returnCode"), "output": "Claude greeting completed" if ok else "Claude greeting failed or was unverified",
            "delivered": final is not None, "authSource": auth_source, "orgId": auth["orgId"],
            "limits": limits if subscription else {}, "outcome": outcome, "failureKind": "not_subscription" if not_subscription else None}


class LimitResetService:
    """Arm on exhaustion and send one cheap greeting after a verified reset.

    Three facts are tracked separately:
      * scheduled deadline -- ``lastStatus.reset_at`` from the source (may be None);
        a deadline passing is never treated as a reset;
      * observed reset -- a *new* sample at <= RESET_PERCENT after exhaustion
        (``resetObservedAt`` + one ``reset_observed`` event per arming);
      * successful greeting -- ``lastTriggerAt`` + ``greeting_sent`` event.
    """

    def __init__(
        self,
        *,
        sources: dict[str, Callable[[], LimitSample]] | None = None,
        senders: dict[str, Callable[[Path], dict[str, Any]]] | None = None,
        state_path: Path | None = None,
        log_path: Path | None = None,
        poll_seconds: int | float = DEFAULT_POLL_SECONDS,
        cwd: Path | None = None,
        enabled: bool | None = None,
        clock: Callable[[], float] | None = None,
        monotonic: Callable[[], float] | None = None,
        list_candidates: Callable[..., list[dict[str, Any]]] | None = None,
        inspect_target: Callable[[str], dict[str, Any]] | None = None,
        queue_continue: Callable[[str], dict[str, Any]] | None = None,
        policies: Mapping[str, ProviderPolicy] | None = None,
    ) -> None:
        if enabled is not None and type(enabled) is not bool:
            raise ValueError("enabled must be a bool or None")
        root = _runtime_dir()
        self.state_path = state_path or root / "limit-reset-state.json"
        self.log_path = log_path or root / "limit-reset.jsonl"
        self.poll_seconds = max(2, float(poll_seconds))
        self.cwd = cwd or root
        self.policies = POLICIES if policies is None else policies
        for provider_name in PROVIDERS:
            policy_for(provider_name, self.policies)
        all_sources = {"Claude": claude_limit_sample, "Codex": codex_limit_sample}
        all_senders = {"Claude": send_claude_greeting, "Codex": send_codex_greeting}
        if sources is None:
            self.sources = {name: source for name, source in all_sources.items()
                            if name in GREETER_ENABLED_PROVIDERS
                            and policy_for(name, self.policies).allows("collect")}
        else:
            for name in sources:
                policy_for(name, self.policies)
            self.sources = {name: source for name, source in sources.items()
                            if policy_for(name, self.policies).allows("collect")}
        if senders is None:
            # Injected sources never acquire production CLI senders implicitly.
            self.senders = {} if sources is not None else {name: all_senders[name] for name in self.sources}
        else:
            for name in senders:
                policy_for(name, self.policies)
            self.senders = dict(senders)
        self._enabled_names = tuple(name for name in PROVIDERS
                                    if policy_for(name, self.policies).allows("show"))
        self.enabled = (enabled if enabled is not None else
                        any(policy.send for policy in self.policies.values()))
        self._clock = clock or time.time
        self._monotonic = monotonic or time.monotonic
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._started_at: str | None = None
        self._events: deque[dict[str, Any]] = deque(maxlen=100)
        self._persistence_error: str | None = None
        self._persistence_retry_at = 0.0
        self._persistence_failures = 0
        self._persistence_errors: dict[str, str] = {}
        self._sending: set[str] = set()
        self._state = self._load_state()
        claude_state = self._state.get("providers", {}).get("Claude", {})
        mono_now = self._monotonic()
        saved_cycles = claude_state.get("consumedCycles") or {}
        self._claude_cycle_mono = {key: mono_now for key in saved_cycles} if isinstance(saved_cycles, dict) else {}
        saved_intent = claude_state.get("dispatchIntent")
        if isinstance(saved_intent, dict) and isinstance(saved_intent.get("cycleKey"), str):
            self._claude_cycle_mono[saved_intent["cycleKey"]] = mono_now
        self._claude_bootstrap_mono = mono_now if isinstance(claude_state.get("lastBootstrapAt"), (int, float)) else None
        self._claude_deadline_mono: dict[str, float] = {}
        self._claude_last_wall = None
        if not isinstance(self._state.get("resetAction"), dict):
            self._state["resetAction"] = default_action()
        self._reset_actions = ResetActionDispatcher(
            list_candidates=list_candidates,
            inspect_target=inspect_target,
            queue_continue=queue_continue,
            policies=self.policies,
            clock=self._clock,
            save=self._save_state,
            action_state=lambda: self._state.setdefault("resetAction", default_action()),
            lock=self._lock,
        )
        self._load_events()
        for event in self._events:
            if event.get("event") == "reset_observed" and event.get("provider") in PROVIDERS:
                state = self._provider_state(event["provider"])
                observed = event.get("observedAt") or event.get("at")
                if (_epoch_seconds(observed) or 0) > (_epoch_seconds(state.get("lastResetAt")) or 0):
                    state["lastResetAt"] = observed

    def _now_iso(self) -> str:
        return _iso_from_epoch(self._clock()) or _iso_now()

    def get_reset_action(self) -> dict[str, Any]:
        with self._lock:
            return self._reset_actions.get()

    def configure_reset_action(self, mode: str, thread_ids: list[str]) -> dict[str, Any]:
        validated_ids = self._reset_actions.validate_configuration(mode, thread_ids)
        with self._lock:
            if mode == "continue":
                policy_for("Codex", self.policies).require("continue")
            if self._sending:
                raise RuntimeError("reset action cannot change while a Codex action is running")
            baseline: dict[str, Any] = {}
            if mode == "continue":
                codex = self._provider_state("Codex")
                sample = codex.get("lastStatus") or {}
                observed_at = sample.get("observed_at")
                now = self._clock()
                five = _valid_quota_percent(sample.get("five_hour_percent"))
                weekly = _valid_quota_percent(sample.get("weekly_percent"))
                try:
                    if isinstance(observed_at, bool):
                        raise ValueError("boolean timestamp")
                    observed_value = float(observed_at)
                except (TypeError, ValueError):
                    observed_value = float("nan")
                baseline_valid = (
                    math.isfinite(observed_value) and 0 <= now - observed_value <= 5
                    and five is not None and weekly is not None
                )
                if not baseline_valid:
                    five = weekly = None
                    observed_at = None
                scheduled = _epoch_seconds(sample.get("reset_at")) if baseline_valid else None
                if (five is not None and float(five) > 0 and scheduled is not None and scheduled > now):
                    frozen_deadline = scheduled
                else:
                    frozen_deadline = _epoch_seconds(codex.get("nextGreetingAt"))
                cycle_id = f"deadline:{frozen_deadline:.3f}" if frozen_deadline is not None else f"observation:{uuid.uuid4()}"
                baseline = {
                    "armedAt": now,
                    "previousSample": {
                        "fiveHourPercent": five, "weeklyPercent": weekly,
                        "observedAt": observed_at, "resetAt": scheduled,
                    },
                    "frozenDeadline": frozen_deadline,
                    "sawBlocked": bool(
                        five is not None and float(five) >= EXHAUSTED_PERCENT
                        or weekly is not None and float(weekly) >= WEEKLY_EXHAUSTED_PERCENT
                    ),
                    "cycleId": cycle_id,
                }
            return self._reset_actions.configure(mode, thread_ids, baseline=baseline, validated_ids=validated_ids)

    _PROVIDER_DEFAULTS: dict[str, Any] = {
        # original keys
        "armed": False, "exhaustedAt": None, "exhaustedSampleAt": None,
        "lastTriggerAt": None, "lastStatus": None, "lastError": None,
        "lastPollError": None, "retryAfter": 0,
        # observed reset (distinct from the scheduled deadline)
        "resetObservedAt": None, "resetObservedSampleAt": None, "resetObservedPercent": None,
        "lastResetAt": None,
        # Rolling Codex keepalive.  A successful CLI exit is only an accepted
        # send; a later quota sample with a new future reset deadline is the
        # evidence for the next window; it does not establish that the greeting
        # caused quota usage, since other account activity can also consume quota.
        "nextGreetingAt": None, "awaitingVerification": False,
        "verificationDeadline": None, "preSendResetAt": None, "preSendPercent": None,
        "lastVerifiedAt": None, "verificationEvidence": None, "lastWindowConfirmedAt": None,
        "unverifiedAttempts": 0,
        # send failure tracking
        "needsLogin": False, "failedAttempts": 0, "lastFailureKind": None,
        "lastFailureAt": None, "lastAttemptAt": None, "lastErrorDetail": None,
        "lastSampleAt": None, "previousFiveHourPercent": None, "previousWeeklyPercent": None,
        "lastAttemptSampleAt": None, "sourceStatus": None, "quotaEffectVerified": None,
    }

    def _provider_state(self, name: str) -> dict[str, Any]:
        state = self._state.setdefault("providers", {}).setdefault(name, {})
        for key, value in self._PROVIDER_DEFAULTS.items():
            state.setdefault(key, value)
        if not state["lastResetAt"] and state.get("resetObservedAt"):
            state["lastResetAt"] = state["resetObservedAt"]
        return state

    def _load_state(self) -> dict[str, Any]:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {"providers": {}}
        except (OSError, json.JSONDecodeError):
            return {"providers": {}}

    def _save_state(self) -> bool:
        with self._lock:
            if "Codex" not in self._sending:
                self._reconcile_codex_finalization()
            if not self._persistence_ready():
                return False
            temporary = None
            fd = None
            try:
                self.state_path.parent.mkdir(parents=True, exist_ok=True)
                fd, temporary = tempfile.mkstemp(prefix=self.state_path.name + ".", suffix=".tmp", dir=self.state_path.parent)
                stream = os.fdopen(fd, "w", encoding="utf-8")
                fd = None  # the stream now owns the descriptor
                with stream:
                    json.dump(self._state, stream, indent=2, sort_keys=True)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.state_path)
                temporary = None
            except OSError as exc:
                self._persistence_failed("state", exc)
                return False
            else:
                self._persistence_succeeded("state")
                return True
            finally:
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                if temporary is not None:
                    try:
                        os.unlink(temporary)
                    except OSError:
                        pass

    def _reconcile_codex_finalization(self) -> None:
        action = self._state.get("resetAction")
        if isinstance(action, dict):
            pending = action.get("finalizePending")
            if isinstance(pending, dict):
                if action.get("armGeneration") == pending.get("armGeneration"):
                    action["armed"] = False
                action.pop("finalizePending", None)
        state = self._state.get("providers", {}).get("Codex", {})
        pending = state.get("greetingIntent") if isinstance(state, dict) else None
        if isinstance(pending, dict):
            if state.get("greetingGeneration") == pending.get("generation"):
                state["armed"] = False
                state["lastError"] = "greeting delivery is unknown"
                state["lastGreetingOutcome"] = "unknown"
            state.pop("greetingIntent", None)

    def _persistence_ready(self) -> bool:
        return self._clock() >= self._persistence_retry_at

    def _persistence_failed(self, kind: str, exc: OSError) -> None:
        self._persistence_failures += 1
        delay = min(PERSIST_RETRY_MAX_SECONDS, PERSIST_RETRY_BASE_SECONDS * (2 ** min(self._persistence_failures - 1, 10)))
        self._persistence_retry_at = self._clock() + delay
        self._persistence_errors[kind] = f"{kind} persistence failed: {redact_secrets(str(exc))[-ERROR_LIMIT:]}"
        self._persistence_error = "; ".join(self._persistence_errors.values())

    def _persistence_succeeded(self, kind: str) -> None:
        self._persistence_errors.pop(kind, None)
        self._persistence_error = "; ".join(self._persistence_errors.values()) or None
        if not self._persistence_errors:
            self._persistence_retry_at = 0.0
            self._persistence_failures = 0

    def _load_events(self) -> None:
        try:
            lines = self.log_path.read_text(encoding="utf-8", errors="ignore").splitlines()[-100:]
        except OSError:
            return
        for line in lines:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                self._events.append(row)

    def _record(self, provider: str | None, event: str, **details: Any) -> None:
        row = {"at": self._now_iso(), "provider": provider, "event": event, **details}
        self._events.append(row)
        if not self._persistence_ready():
            return
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, separators=(",", ":")) + "\n")
        except OSError as exc:
            self._persistence_failed("event log", exc)
        else:
            self._persistence_succeeded("event log")

    def _arm(self, name: str, state: dict[str, Any], sample: LimitSample, *, rearmed: bool) -> None:
        state.update({
            "greetingGeneration": str(uuid.uuid4()),
            "armed": True, "exhaustedAt": self._now_iso(), "exhaustedSampleAt": sample.observed_at,
            "resetObservedAt": None, "resetObservedSampleAt": None, "resetObservedPercent": None,
        })
        if not rearmed:
            state.update({"lastError": None, "retryAfter": 0})
        details: dict[str, Any] = {
            "fiveHourPercent": sample.five_hour_percent, "resetAt": sample.reset_at,
            "scheduledResetAt": _epoch_seconds(sample.reset_at),
        }
        if rearmed:
            details["rearmed"] = True
        self._record(name, "armed", **details)

    def check_provider(self, name: str, sample: LimitSample) -> None:
        if not self.enabled:
            return
        policy = policy_for(name, self.policies)
        policy.require("collect")
        if not (policy.allows("greet") or policy.allows("continue")):
            self._record_collected_status(name, sample)
            return
        if name == "Claude" and policy.allows("greet") and self.senders.get(name) is not None:
            self._check_claude_keepalive(sample)
            return
        if name == "Codex":
            with self._lock:
                if name not in self._sending:
                    self._reconcile_codex_finalization()
                action = self._state.setdefault("resetAction", default_action())
                if action.get("mode") == "continue" and not action.get("armed"):
                    self._record_collected_status(name, sample)
                    return
        # Codex exposes an authoritative five-hour reset deadline.  Once one is
        # available, maintain a continuous rolling window instead of requiring
        # the account to hit 99% usage before every cycle.  Keep the legacy
        # exhaustion path as a fallback when no deadline has ever been exposed.
        if name == "Codex":
            with self._lock:
                state = self._provider_state(name)
                keepalive_known = any((
                    _epoch_seconds(sample.reset_at),
                    _epoch_seconds(state.get("nextGreetingAt")),
                    state.get("awaitingVerification"),
                    _epoch_seconds(state.get("preSendResetAt")),
                ))
            if keepalive_known:
                self._check_codex_keepalive(name, sample)
                return
        with self._lock:
            if name in self._sending:
                return
            state = self._provider_state(name)
            checked_at = _checked_at(sample) if name == "Codex" else _claude_checked_at(sample, self._now_iso())
            state["lastStatus"] = {**asdict(sample), "checkedAt": checked_at}
            five = sample.five_hour_percent
            weekly = sample.weekly_percent
            if five is None:
                self._save_state()
                return
            sample_is_new = sample.observed_at > float(state.get("exhaustedSampleAt") or 0)
            if five >= EXHAUSTED_PERCENT:
                if not state["armed"]:
                    self._arm(name, state, sample, rearmed=False)
                elif state.get("resetObservedAt") and sample_is_new:
                    # The greeting never went out and the new window is exhausted
                    # again: start a fresh cycle so the next reset is observed anew.
                    self._arm(name, state, sample, rearmed=True)
                self._save_state()
                return
            # Only a *new* sample showing a real drop counts as a reset. A scheduled
            # deadline (sample.reset_at) passing is deliberately ignored here.
            if not (state["armed"] and five <= RESET_PERCENT and sample_is_new):
                self._save_state()
                return
            if not state.get("resetObservedAt"):
                observed_iso = _iso_from_epoch(sample.observed_at) or self._now_iso()
                state.update({
                    "resetObservedAt": observed_iso, "resetObservedSampleAt": sample.observed_at,
                    "resetObservedPercent": five, "lastResetAt": observed_iso,
                })
                self._record(
                    name, "reset_observed", observedAt=observed_iso, sampleAt=sample.observed_at,
                    fiveHourPercent=five, weeklyPercent=weekly, scheduledResetAt=_epoch_seconds(sample.reset_at),
                )
            if weekly is not None and weekly >= WEEKLY_EXHAUSTED_PERCENT:
                if state.get("lastError") != WEEKLY_BLOCKED_ERROR:
                    state["lastError"] = WEEKLY_BLOCKED_ERROR
                    self._record(name, "blocked", reason=WEEKLY_BLOCKED_ERROR, weeklyPercent=weekly)
                self._save_state()
                return
            if state.get("lastError") == WEEKLY_BLOCKED_ERROR:
                state["lastError"] = None
            now = self._clock()
            if now < float(state.get("retryAfter") or 0):
                self._save_state()
                return
            state["lastAttemptAt"] = self._now_iso()
            self._sending.add(name)
            greeting_generation = None
            if name == "Codex":
                greeting_generation = state.get("greetingGeneration") or str(uuid.uuid4())
                state["greetingGeneration"] = greeting_generation
                state["armed"] = False
                state["greetingIntent"] = {"generation": greeting_generation, "cycle": state.get("exhaustedAt")}
            saved = self._save_state()
            if name == "Codex" and not saved:
                state["armed"] = True
                state.pop("greetingIntent", None)
                self._sending.discard(name)
                return
        # CLI startup/network waits must not block the status endpoint or another
        # provider. Reserve this provider while the lock is released.
        sender = None
        try:
            policy_for(name, self.policies).require("greet")
            sender = self.senders.get(name)
            if sender is None:
                raise ProviderPolicyError("greeting sender is unavailable")
            if sender is send_codex_greeting or sender is send_claude_greeting:
                result = sender(self.cwd, policies=self.policies)
            else:
                result = sender(self.cwd)
        except subprocess.TimeoutExpired as exc:
            result = {"ok": False, "outcome": "unknown", "error": str(exc)}
        except Exception as exc:  # injected senders raise only before starting a child
            result = {"ok": False, "outcome": "unknown" if sender is send_codex_greeting else "not_started",
                      "error": str(exc)}
        with self._lock:
            self._sending.discard(name)
            if name == "Codex" and state.get("greetingGeneration") != greeting_generation:
                return
            if name == "Codex":
                state.pop("greetingIntent", None)
                state["lastGreetingOutcome"] = ("completed" if isinstance(result, dict) and result.get("ok") else
                                                "not_started" if isinstance(result, dict) and result.get("outcome") in {"not_started", "rejected"}
                                                else "unknown")
                if state["lastGreetingOutcome"] == "not_started":
                    state["armed"] = True
            if not isinstance(result, dict):
                result = {"ok": False, "error": "greeting sender returned no result"}
            if result.get("ok"):
                attempts = int(state.get("failedAttempts") or 0) + 1
                state.update({
                    "armed": False, "lastTriggerAt": self._now_iso(), "lastError": None, "retryAfter": 0,
                    "needsLogin": False, "failedAttempts": 0, "lastFailureKind": None, "lastErrorDetail": None,
                })
                self._record(
                    name, "greeting_sent", model=CLAUDE_GREETING_MODEL if name == "Claude" else CODEX_GREETING_MODEL, attempts=attempts,
                )
            else:
                self._record_failure(name, state, result, now)
            self._save_state()

    def _record_collected_status(self, name: str, sample: LimitSample) -> None:
        with self._lock:
            state = self._provider_state(name)
            checked_at = _checked_at(sample) if name == "Codex" else _claude_checked_at(sample, self._now_iso())
            state["lastStatus"] = {**asdict(sample), "checkedAt": checked_at}
            state["lastPollError"] = None
            self._save_state()

    def _continue_trigger_ready(self, action: dict[str, Any], sample: LimitSample) -> bool:
        now = self._clock()
        observed_value = sample.observed_at
        try:
            if isinstance(observed_value, bool):
                raise ValueError("boolean timestamp")
            observed = float(observed_value)
        except (TypeError, ValueError):
            observed = float("nan")
        five = _valid_quota_percent(sample.five_hour_percent)
        weekly = _valid_quota_percent(sample.weekly_percent)
        fresh = math.isfinite(observed) and 0 <= now - observed <= 5
        if not fresh or five is None or weekly is None:
            return False
        previous = action.get("previousSample") or {}
        previous_five = _valid_quota_percent(previous.get("fiveHourPercent"))
        previous_weekly = _valid_quota_percent(previous.get("weeklyPercent"))
        previous_observed = previous.get("observedAt")
        previous_reset = _epoch_seconds(previous.get("resetAt"))
        current_reset = _epoch_seconds(sample.reset_at)
        try:
            armed_at = float(action.get("armedAt") or now)
        except (TypeError, ValueError):
            armed_at = now
        if observed <= armed_at:
            return False
        try:
            prior_observed = float(previous_observed)
        except (TypeError, ValueError):
            prior_observed = float("nan")
        if math.isfinite(prior_observed) and observed <= prior_observed:
            return False

        saw_blocked = bool(action.get("sawBlocked")) or five >= EXHAUSTED_PERCENT or weekly >= WEEKLY_EXHAUSTED_PERCENT
        action["sawBlocked"] = saw_blocked
        available = five < EXHAUSTED_PERCENT and weekly < WEEKLY_EXHAUSTED_PERCENT
        # If the user armed before any usable sample arrived, pin the first
        # active window deadline once. Never promote a zero-usage synthetic
        # rolling deadline into a frozen cycle target.
        if (action.get("frozenDeadline") is None and five > 0
                and current_reset is not None and current_reset > now
                and math.isfinite(observed) and observed >= armed_at):
            action["frozenDeadline"] = current_reset
            action["cycleId"] = f"deadline:{current_reset:.3f}"
        current_sample = {
            "fiveHourPercent": sample.five_hour_percent,
            "weeklyPercent": sample.weekly_percent,
            "observedAt": sample.observed_at,
            "resetAt": sample.reset_at,
        }
        if not available:
            action["previousSample"] = current_sample
            return False

        # A manual/weekly reset can restore allowance without the five-hour
        # counter reaching zero. The last observed blocked sample proves it.
        if saw_blocked:
            action["previousSample"] = current_sample
            return True

        frozen_deadline = _epoch_seconds(action.get("frozenDeadline"))
        if frozen_deadline is not None and observed >= frozen_deadline:
            action["previousSample"] = current_sample
            return True

        if previous_five is not None and previous_weekly is not None:
            drop = previous_five - five
            genuine_reset = (previous_five > 0 and five == 0) or drop >= 20
            bonus_advance = (
                drop > 0 and previous_five > 0 and current_reset is not None
                and previous_reset is not None and current_reset - previous_reset > 60
            )
            weekly_drop = previous_weekly - weekly
            weekly_bonus = previous_weekly > 0 and (weekly == 0 or weekly_drop >= 20)
            if genuine_reset or bonus_advance or weekly_bonus:
                action["previousSample"] = current_sample
                return True

        action["previousSample"] = current_sample
        return False

    def _dispatch_continue_action(self, name: str, cycle_id: str) -> None:
        try:
            run = self._reset_actions.dispatch(cycle_id)
        except Exception as exc:
            run = {"cycleId": cycle_id, "at": self._clock(), "targets": [
                {"threadId": thread_id, "status": "unknown", "reason": redact_secrets(str(exc))[-ERROR_LIMIT:]}
                for thread_id in self.get_reset_action().get("threadIds", [])
            ]}
        with self._lock:
            action = self._state.setdefault("resetAction", default_action())
            generation = run.get("armGeneration") if run else None
            if not generation or action.get("armGeneration") != generation:
                self._sending.discard(name)
                return
            if run:
                action["lastRun"] = run
            state = self._provider_state(name)
            state.update({
                "nextGreetingAt": None,
                "lastAction": "continue",
                "awaitingVerification": False,
                "verificationDeadline": None,
                "preSendResetAt": None,
                "preSendPercent": None,
                "retryAfter": 0,
                "lastError": None,
                "unverifiedAttempts": 0,
            })
            targets = run.get("targets", []) if run else []
            queued = any(target.get("status") == "queued" for target in targets)
            if queued:
                state["lastTriggerAt"] = self._now_iso()
            self._record(
                name, "continue_action_queued" if queued else "continue_action_skipped",
                cycleId=cycle_id, targets=targets,
            )
            self._save_state()
            self._sending.discard(name)

    def _claude_account_sample(self, org_id: str, now: float) -> LimitSample:
        """Only account-bound CLI and matching Desktop data can drive sends."""
        desktop_org, desktop = _claude_desktop_data(now=now, max_usage_age=604800, expected_org_id=org_id)
        if desktop_org != org_id:
            desktop = {}
        cli = claude_statusline.read(self.state_path.parent / claude_statusline.CLI_FILENAME, now=now,
                                    source="claude-cli", include_org=True)
        if cli.get("orgId") != org_id:
            cli = {}
        deadlines = [_epoch_seconds(desktop.get("fiveHour") or desktop.get("lastKnownFiveHour")) if _valid_quota_percent(desktop.get("fiveHourPercent")) is not None else None]
        five = cli.get("five_hour") or {}
        deadlines.append(_epoch_seconds(five.get("resets_at")))
        valid = [x for x in deadlines if x is not None and math.isfinite(x)]
        weekly = None
        desktop_weekly_reset = _epoch_seconds(desktop.get("weekly"))
        desktop_weekly_percent = _valid_quota_percent(desktop.get("weeklyPercent"))
        best_weekly = None
        if desktop_weekly_reset is not None and desktop_weekly_reset > now and desktop_weekly_percent is not None:
            best_weekly = {"resets_at": desktop_weekly_reset, "used_percentage": desktop_weekly_percent, "receivedAt": desktop.get("observedAt") or now}
        cw = cli.get("seven_day") or {}
        if cw.get("resets_at", 0) > now:
            best_weekly = claude_statusline.choose(best_weekly, cw, now)
        if best_weekly:
            weekly = best_weekly["used_percentage"]
        return LimitSample(None, weekly, now, max(valid) if valid else None)

    def _check_claude_keepalive(self, sample: LimitSample) -> None:
        name = "Claude"
        with self._lock:
            if name in self._sending:
                return
            state = self._provider_state(name)
            state["lastStatus"] = {**asdict(sample), "checkedAt": _claude_checked_at(sample, self._now_iso())}
            intent = state.get("dispatchIntent")
            if isinstance(intent, dict) and intent.get("cycleKey"):
                state.setdefault("consumedCycles", {})[intent["cycleKey"]] = intent.get("at")
                state["dispatchIntent"] = None
                self._record(name, "dispatch_recovered", cycleKey=intent["cycleKey"])
                if not self._save_state():
                    return
            now = self._clock()
            mono = self._monotonic()
            if self._claude_last_wall is not None and now < self._claude_last_wall:
                self._claude_cycle_mono = {key: mono for key in state.get("consumedCycles", {})}
                if state.get("lastBootstrapAt") is not None:
                    self._claude_bootstrap_mono = mono
            self._claude_last_wall = now
            if now < float(state.get("retryAfter") or 0):
                return
            self._sending.add(name)
        try:
            cached = getattr(self, "_claude_auth_cache", None)
            if state.get("needsLogin") and state.get("lastFailureKind") == "not_subscription":
                cached = None
            # Each check starts a CLI process, so the cached result only schedules sends.
            if cached and 0 <= mono - cached[0] < 1800:
                auth = cached[1]
            else:
                auth = _claude_auth_status(self.cwd)
                self._claude_auth_cache = (self._monotonic(), auth)
            if not auth.get("ok"):
                with self._lock:
                    self._record_failure(name, state, auth, now)
                    self._save_state()
                return
            org = auth["orgId"]
            evidence = self._claude_account_sample(org, now)
            with self._lock:
                if state.get("claudeOrgId") != org:
                    state["claudeOrgId"] = org
                    state["claudeDeadline"] = None
                    state["lastFailureKind"] = None
                state["needsLogin"] = False
                if state.get("lastFailureKind") == "not_subscription":
                    state["lastFailureKind"] = None
                deadline = _epoch_seconds(evidence.reset_at)
                old = _epoch_seconds(state.get("claudeDeadline"))
                if old is not None and math.isfinite(old):
                    deadline = max(deadline, old) if deadline else old
                if deadline is not None:
                    state["claudeDeadline"] = deadline
                    deadline_key = f"claude:{deadline}"
                    if now < deadline + 30:
                        target = mono + deadline + 30 - now
                        self._claude_deadline_mono[deadline_key] = max(target, self._claude_deadline_mono.get(deadline_key, target))
                    if state.get("nextGreetingAt") != deadline:
                        state["nextGreetingAt"] = deadline
                        self._record(name, "keepalive_scheduled", nextGreetingAt=deadline)
                weekly = _valid_quota_percent(evidence.weekly_percent)
                if weekly is not None and weekly >= WEEKLY_EXHAUSTED_PERCENT:
                    self._save_state()
                    return
                if deadline is not None and now < deadline + 30:
                    self._save_state()
                    return
                consumed = state.setdefault("consumedCycles", {})
                cycle = f"claude:{deadline}" if deadline is not None else None
                deadline_cycle = cycle
                if cycle and mono < self._claude_deadline_mono.get(cycle, mono):
                    self._save_state()
                    return
                last_dispatch = consumed.get(cycle) if cycle else None
                bootstrap = cycle is None or cycle in consumed
                if bootstrap:
                    if cycle is not None and (not isinstance(last_dispatch, (int, float)) or now - last_dispatch < ROLLING_WINDOW_SECONDS):
                        self._save_state()
                        return
                    if cycle is not None and mono - self._claude_cycle_mono.setdefault(cycle, mono) < ROLLING_WINDOW_SECONDS:
                        self._save_state()
                        return
                    last_bootstrap = state.get("lastBootstrapAt")
                    if isinstance(last_bootstrap, (int, float)) and now - last_bootstrap < ROLLING_WINDOW_SECONDS:
                        self._save_state()
                        return
                    if self._claude_bootstrap_mono is not None and mono - self._claude_bootstrap_mono < ROLLING_WINDOW_SECONDS:
                        self._save_state()
                        return
                    cycle = f"claude:bootstrap:{now}"
                # Old cycle keys only guard against resends; a week is plenty.
                for key in [key for key, at in consumed.items() if key != deadline_cycle and isinstance(at, (int, float)) and now - at > 604800 and mono - self._claude_cycle_mono.setdefault(key, mono) > 604800]:
                    del consumed[key]
                    self._claude_cycle_mono.pop(key, None)
            # The cached account can schedule, but never authorize a send. The
            # CLI check runs unlocked; this provider stays reserved in _sending.
            fresh_auth = _claude_auth_status(self.cwd)
            with self._lock:
                if not fresh_auth.get("ok") or fresh_auth.get("orgId") != org:
                    self._claude_auth_cache = None
                    failure = fresh_auth if not fresh_auth.get("ok") else {"error": "Claude Code subscription sign-in changed"}
                    self._record_failure(name, state, failure, now)
                    self._save_state()
                    return
                previous_bootstrap = state.get("lastBootstrapAt")
                state["dispatchIntent"] = {"cycleKey": cycle, "at": now}
                if bootstrap:
                    state["lastBootstrapAt"] = now
                if not self._save_state():
                    state["dispatchIntent"] = None
                    state["lastBootstrapAt"] = previous_bootstrap
                    return
                previous_mono = self._claude_bootstrap_mono
                self._claude_cycle_mono[cycle] = self._monotonic()
                if bootstrap:
                    self._claude_bootstrap_mono = self._claude_cycle_mono[cycle]
                if bootstrap:
                    self._record(name, "bootstrap_send", cycleKey=cycle)
                self._record(name, "send_started", cycleKey=cycle)
            try:
                sender = self.senders.get(name)
                if sender is None:
                    result = {"ok": False, "error": "Claude greeting sender is unavailable", "preSendFailure": True}
                elif sender is send_claude_greeting:
                    result = sender(self.cwd, policies=self.policies, expected_org_id=org)
                else:
                    result = sender(self.cwd)
                if not isinstance(result, dict):
                    result = {"ok": False, "error": "Claude greeting outcome is uncertain"}
            except Exception:
                result = {"ok": False, "error": "Claude greeting outcome is uncertain"}
            with self._lock:
                state["dispatchIntent"] = None
                if result.get("preSendFailure"):
                    self._claude_cycle_mono.pop(cycle, None)
                    if bootstrap:
                        state["lastBootstrapAt"] = previous_bootstrap
                        self._claude_bootstrap_mono = previous_mono
                    self._record_failure(name, state, result, now)
                else:
                    consumed[cycle] = now
                    if result.get("failureKind") == "not_subscription" or (result.get("orgId") and result["orgId"] != org):
                        state.update({"needsLogin": True, "lastFailureKind": "not_subscription", "lastError": "Claude Code is not using a Claude subscription sign-in", "retryAfter": now + AUTH_RETRY_SECONDS})
                        self._record(name, "send_failed", failureKind="not_subscription", needsLogin=True)
                    else:
                        limits = result.get("limits") if isinstance(result.get("limits"), dict) else {}
                        if limits and result.get("orgId") == org:
                            try:
                                claude_statusline.write(limits, self.state_path.parent, now=self._clock(), filename=claude_statusline.CLI_FILENAME, source="claude-cli", org_id=org)
                            except (OSError, TimeoutError, ValueError):
                                limits = {}
                        new_deadline = _epoch_seconds((limits.get("five_hour") or {}).get("resets_at"))
                        outcome = result.get("outcome") or ("completed" if result.get("ok") else "uncertain")
                        if outcome == "completed":
                            state.update({"lastTriggerAt": self._now_iso(), "lastError": None, "retryAfter": 0})
                            self._record(name, "greeting_sent", cycleKey=cycle)
                        else:
                            category = "cli_error" if outcome == "failed" else "delivery_unknown"
                            state["lastError"] = state.get("lastError") or ("Claude greeting failed" if outcome == "failed" else "Claude greeting delivery is uncertain")
                            self._record(name, "greeting_failed" if outcome == "failed" else "greeting_uncertain", cycleKey=cycle, failureKind=category)
                        if new_deadline and new_deadline > self._clock() and (deadline is None or new_deadline > deadline):
                            state.update({"claudeDeadline": new_deadline, "nextGreetingAt": new_deadline})
                            self._record(name, "greeting_verified", nextGreetingAt=new_deadline)
                        else:
                            self._record(name, "greeting_unverified", cycleKey=cycle)
                self._save_state()
        finally:
            with self._lock:
                self._sending.discard(name)

    def _check_codex_keepalive(self, name: str, sample: LimitSample) -> None:
        """Send at every observed Codex deadline and verify the next window."""
        continue_cycle: str | None = None
        with self._lock:
            if name in self._sending:
                return
            self._reconcile_codex_finalization()
            action = self._state.setdefault("resetAction", default_action())
            if action.get("mode") == "continue" and action.get("armed"):
                state = self._provider_state(name)
                state["lastStatus"] = {**asdict(sample), "checkedAt": _checked_at(sample)}
                should_dispatch = self._continue_trigger_ready(action, sample)
                if not self._save_state():
                    return
                if not should_dispatch:
                    return
                continue_cycle = str(action.get("cycleId") or "")
                self._sending.add(name)
        if continue_cycle is not None:
            self._dispatch_continue_action(name, continue_cycle)
            return
        with self._lock:
            if name in self._sending:
                return
            action = self._state.setdefault("resetAction", default_action())
            if action.get("mode") == "continue" and action.get("armed"):
                # A configuration that raced the initial check owns this sample;
                # the next poll evaluates it under the continuation rules.
                state = self._provider_state(name)
                state["lastStatus"] = {**asdict(sample), "checkedAt": _checked_at(sample)}
                self._save_state()
                return
            if action.get("mode") == "continue":
                state = self._provider_state(name)
                state["lastStatus"] = {**asdict(sample), "checkedAt": _checked_at(sample)}
                self._save_state()
                return
            state = self._provider_state(name)
            previous = state.get("lastStatus") or {}
            prev_scheduled = _epoch_seconds(previous.get("reset_at"))
            prev_observed = previous.get("observed_at")
            prev_checked = _epoch_seconds(previous.get("checkedAt"))
            state["lastStatus"] = {**asdict(sample), "checkedAt": _checked_at(sample)}
            now = self._clock()
            scheduled = _epoch_seconds(sample.reset_at)
            # Codex returns a synthetic now+5h deadline at 0% that moves on every
            # poll. A future deadline that holds still across two samples taken
            # a few seconds apart is a real, active window -- even at 0% usage.
            stable = (
                scheduled is not None and scheduled > now and prev_scheduled is not None
                and abs(scheduled - prev_scheduled) <= DEADLINE_JITTER_SECONDS
                and isinstance(prev_observed, (int, float)) and sample.observed_at is not None
                # The watcher intentionally samples quota every two seconds.
                # Requiring a four-second gap made this branch impossible during
                # normal operation and incorrectly marked successful greetings
                # as unverified. Allow one normal polling interval plus clock
                # jitter; the deadline itself still has to remain stable.
                and float(sample.observed_at) - float(prev_observed) >= max(1.5, self._poll_interval() * 0.75)
            )

            if sample.five_hour_percent is None:
                self._save_state()
                return

            if state.get("awaitingVerification"):
                baseline = _epoch_seconds(state.get("preSendResetAt")) or 0
                pre_send_percent = state.get("preSendPercent")
                if float(pre_send_percent or 0) > 0 and scheduled is not None and scheduled > now:
                    # A real window was already active before this send. There is
                    # nothing to prove or restart; follow its fixed deadline.
                    state.update({
                        "awaitingVerification": False,
                        "verificationDeadline": None,
                        "preSendResetAt": None,
                        "preSendPercent": None,
                        "nextGreetingAt": scheduled,
                    })
                    self._record(
                        name, "active_window_observed", nextGreetingAt=scheduled,
                        fiveHourPercent=sample.five_hour_percent,
                    )
                    self._save_state()
                    return
                usage_advanced = (
                    sample.five_hour_percent is not None
                    and float(sample.five_hour_percent) > float(pre_send_percent or 0)
                )
                # Increased usage with a new future deadline confirms an active
                # window after the send. It cannot establish causation because
                # other account activity may explain the increase.
                if usage_advanced and scheduled is not None and scheduled > max(now, baseline):
                    triggered = _epoch_seconds(state.get("lastTriggerAt")) or now
                    next_greeting = triggered + ROLLING_WINDOW_SECONDS
                    state.update({
                        "awaitingVerification": False,
                        "verificationDeadline": None,
                        "preSendResetAt": None,
                        "preSendPercent": None,
                        "nextGreetingAt": next_greeting,
                        "lastVerifiedAt": self._now_iso(),
                        "verificationEvidence": "usage_and_deadline",
                        "lastResetAt": self._now_iso(),
                        "lastError": None,
                        "unverifiedAttempts": 0,
                    })
                    self._record(
                        name, "greeting_verified", nextGreetingAt=next_greeting,
                        fiveHourPercent=sample.five_hour_percent,
                    )
                    self._save_state()
                    return
                # Only compare two samples that were both taken after the send
                # started; the pre-send sample must never confirm a window.
                attempt_at = _epoch_seconds(state.get("lastAttemptAt"))
                post_send_pair = prev_checked is not None and (attempt_at is None or prev_checked > attempt_at)
                if stable and post_send_pair:
                    state.update({
                        "awaitingVerification": False,
                        "verificationDeadline": None,
                        "preSendResetAt": None,
                        "preSendPercent": None,
                        "nextGreetingAt": scheduled,
                        "lastWindowConfirmedAt": self._now_iso(),
                        "verificationEvidence": "stable_deadline",
                        "lastResetAt": self._now_iso(),
                        "lastError": None,
                        "unverifiedAttempts": 0,
                    })
                    self._record(
                        name, "quota_deadline_confirmed", nextGreetingAt=scheduled,
                        fiveHourPercent=sample.five_hour_percent, reason="stable_deadline",
                    )
                    # Older versions labeled a stable deadline as greeting
                    # verification. Remove that mark when it belongs to this
                    # attempt, while preserving any earlier verified attempt.
                    last_verified = _epoch_seconds(state.get("lastVerifiedAt"))
                    last_trigger = _epoch_seconds(state.get("lastTriggerAt"))
                    if last_verified is not None and last_trigger is not None and last_verified >= last_trigger:
                        state["lastVerifiedAt"] = None
                    self._save_state()
                    return
                verify_by = float(state.get("verificationDeadline") or 0)
                if now < verify_by:
                    self._save_state()
                    return
                attempts = int(state.get("unverifiedAttempts") or 0) + 1
                retry_at = now + RETRY_SECONDS
                state.update({
                    "awaitingVerification": False,
                    "verificationDeadline": None,
                    "lastError": "greeting was accepted but no new reset deadline was observed",
                    "retryAfter": retry_at,
                    "nextGreetingAt": retry_at,
                    "unverifiedAttempts": attempts,
                })
                self._record(name, "greeting_unverified", previousResetAt=baseline or None, unverifiedAttempts=attempts)
                if attempts >= MAX_UNVERIFIED_ATTEMPTS:
                    paused_until = scheduled if scheduled is not None and scheduled > now else now + ROLLING_WINDOW_SECONDS
                    state["nextGreetingAt"] = paused_until
                    state["retryAfter"] = max(retry_at, paused_until)
                    if attempts == MAX_UNVERIFIED_ATTEMPTS:
                        self._record(name, "keepalive_paused", nextGreetingAt=paused_until, unverifiedAttempts=attempts)

            # Any non-zero usage with a future deadline is an already-active
            # window. Its provider deadline is authoritative, even if a stale
            # local anchor is overdue.
            next_at = _epoch_seconds(state.get("nextGreetingAt"))
            if (float(sample.five_hour_percent or 0) > 0 or stable) and scheduled is not None and scheduled > now:
                # Codex jitters the same deadline by a second; only a real move is news.
                if next_at is None or abs(next_at - scheduled) > DEADLINE_JITTER_SECONDS:
                    state["nextGreetingAt"] = scheduled
                    self._record(name, "active_window_observed", nextGreetingAt=scheduled, fiveHourPercent=sample.five_hour_percent)
                # A later authoritative window reconciles a prior accepted-but-
                # unconfirmed send.  It does not prove that the greeting caused
                # the window, so retain lastVerifiedAt separately, but do not
                # keep surfacing the stale verification error forever.
                if state.get("lastError") == "greeting was accepted but no new reset deadline was observed":
                    state["lastError"] = None
                    state["unverifiedAttempts"] = 0
                    self._record(name, "window_reconciled", fiveHourPercent=sample.five_hour_percent)
                self._save_state()
                return

            # Freeze the earliest credible target. At zero usage Codex returns a
            # synthetic now+5h value that moves forward on every poll; it must
            # never push an established target later.
            last_trigger = _epoch_seconds(state.get("lastTriggerAt"))
            anchored = last_trigger + ROLLING_WINDOW_SECONDS if last_trigger is not None else None
            candidates = [value for value in (next_at, scheduled, anchored) if value is not None]
            fixed_target = min(candidates) if candidates else None
            if fixed_target is not None and fixed_target != next_at:
                state["nextGreetingAt"] = fixed_target
                self._record(name, "keepalive_scheduled", nextGreetingAt=fixed_target)
            next_at = fixed_target

            if next_at is None or now < next_at:
                self._save_state()
                return
            # A reset has not actually become available until usage falls to
            # zero. Keep polling an overdue fixed target instead of sending early.
            if float(sample.five_hour_percent or 0) > 0:
                self._save_state()
                return
            if sample.weekly_percent is not None and sample.weekly_percent >= WEEKLY_EXHAUSTED_PERCENT:
                if state.get("lastError") != WEEKLY_BLOCKED_ERROR:
                    state["lastError"] = WEEKLY_BLOCKED_ERROR
                    self._record(name, "blocked", reason=WEEKLY_BLOCKED_ERROR, weeklyPercent=sample.weekly_percent)
                self._save_state()
                return
            if state.get("lastError") == WEEKLY_BLOCKED_ERROR:
                state["lastError"] = None
            if now < float(state.get("retryAfter") or 0):
                self._save_state()
                return

            cycle_key = str(next_at)
            if (state.get("lastConsumedGreetingCycle") == cycle_key
                    and state.get("lastGreetingOutcome") not in {"completed", "not_started", "rejected"}):
                self._save_state()
                return

            greeting_generation = (state.get("greetingGeneration")
                                   if state.get("lastGreetingOutcome") == "not_started"
                                   and state.get("releasedGreetingCycle") == cycle_key else None) or str(uuid.uuid4())
            previous_consumed_cycle = state.get("lastConsumedGreetingCycle")
            state.update({
                "lastAttemptAt": self._now_iso(),
                "preSendResetAt": scheduled or next_at,
                "preSendPercent": sample.five_hour_percent,
                "greetingGeneration": greeting_generation,
                "greetingIntent": {"generation": greeting_generation, "cycle": cycle_key},
                "lastConsumedGreetingCycle": cycle_key,
            })
            self._record(name, "send_started", scheduledResetAt=scheduled or next_at)
            self._sending.add(name)
            if not self._save_state():
                state.pop("greetingIntent", None)
                if previous_consumed_cycle is None:
                    state.pop("lastConsumedGreetingCycle", None)
                else:
                    state["lastConsumedGreetingCycle"] = previous_consumed_cycle
                self._sending.discard(name)
                return

        sender = None
        try:
            policy_for(name, self.policies).require("greet")
            sender = self.senders.get(name)
            if sender is None:
                raise ProviderPolicyError("greeting sender is unavailable")
            if sender is send_codex_greeting or sender is send_claude_greeting:
                result = sender(self.cwd, policies=self.policies)
            else:
                result = sender(self.cwd)
        except subprocess.TimeoutExpired as exc:
            result = {"ok": False, "outcome": "unknown", "error": str(exc)}
        except Exception as exc:  # injected senders raise only before starting a child
            result = {"ok": False, "outcome": "unknown" if sender is send_codex_greeting else "not_started",
                      "error": str(exc)}

        with self._lock:
            self._sending.discard(name)
            if state.get("greetingGeneration") != greeting_generation:
                return
            state.pop("greetingIntent", None)
            state["lastGreetingOutcome"] = ("completed" if isinstance(result, dict) and result.get("ok") else
                                            "not_started" if isinstance(result, dict) and result.get("outcome") in {"not_started", "rejected"}
                                            else "unknown")
            if state["lastGreetingOutcome"] == "not_started":
                state["releasedGreetingCycle"] = cycle_key
                if previous_consumed_cycle is None:
                    state.pop("lastConsumedGreetingCycle", None)
                else:
                    state["lastConsumedGreetingCycle"] = previous_consumed_cycle
            else:
                state.pop("releasedGreetingCycle", None)
            if not isinstance(result, dict):
                result = {"ok": False, "error": "greeting sender returned no result"}
            if result.get("ok"):
                attempts = int(state.get("failedAttempts") or 0) + 1
                state.update({
                    "armed": False,
                    "lastTriggerAt": self._now_iso(),
                    "lastError": None,
                    "retryAfter": 0,
                    "needsLogin": False,
                    "failedAttempts": 0,
                    "lastFailureKind": None,
                    "lastErrorDetail": None,
                    "awaitingVerification": True,
                    "verificationDeadline": self._clock() + GREETING_VERIFY_SECONDS,
                    "nextGreetingAt": None,
                })
                self._record(
                    name, "greeting_sent", model=CODEX_GREETING_MODEL,
                    attempts=attempts, awaitingVerification=True,
                )
            else:
                state["awaitingVerification"] = False
                state["nextGreetingAt"] = next_at
                self._record_failure(name, state, result, self._clock())
            self._save_state()

    def _record_failure(self, name: str, state: dict[str, Any], result: dict[str, Any], now: float) -> None:
        raw = redact_secrets(str(result.get("error") or result.get("output") or "command failed"))[-ERROR_LIMIT:]
        kind = classify_send_failure(result)
        if kind == "auth":
            error = LOGIN_ERRORS.get(name, f"{name} CLI sign-in expired — sign in again")
            delay = AUTH_RETRY_SECONDS
        else:
            error = raw
            delay = RETRY_SECONDS
        repeated = state.get("lastFailureKind") == kind and state.get("lastError") == error
        state.update({
            "lastError": error, "lastErrorDetail": raw, "lastFailureKind": kind,
            "needsLogin": kind == "auth", "failedAttempts": int(state.get("failedAttempts") or 0) + 1,
            "lastFailureAt": self._now_iso(), "retryAfter": now + delay,
        })
        if not repeated:
            self._record(
                name, "send_failed", error=error, returnCode=result.get("returnCode"),
                needsLogin=kind == "auth", failureKind=kind, failedAttempts=state["failedAttempts"],
                retryAfter=state["retryAfter"],
            )

    def check_once(self) -> None:
        if not self.enabled:
            return
        for name in self.sources:
            try:
                policy_for(name, self.policies).require("collect")
                sample = self.sources[name]()
                with self._lock:
                    state = self._provider_state(name)
                    state["lastPollError"] = None
                    if str(state.get("lastError") or "").startswith("Codex app server timed out"):
                        state["lastError"] = None
                self.check_provider(name, sample)
            except Exception as exc:
                with self._lock:
                    state = self._provider_state(name)
                    error = redact_secrets(str(exc))[-ERROR_LIMIT:]
                    if state.get("lastPollError") != error:
                        state["lastPollError"] = error
                        self._record(name, "poll_failed", error=error)
                    self._save_state()

    def _run(self) -> None:
        with self._lock:
            self._started_at = self._now_iso()
            last = self._events[-1] if self._events else None
            # Back-to-back restarts with nothing in between only add noise.
            if not (last and last.get("event") == "service_started" and last.get("pollSeconds") == self.poll_seconds):
                self._record(None, "service_started", pollSeconds=self.poll_seconds)
        while not self._stop.is_set():
            self.check_once()
            self._stop.wait(self._poll_interval())

    def _poll_interval(self) -> float:
        """Refresh displayed quota samples at the configured two-second cadence."""
        with self._lock:
            if self._sending:
                return self.poll_seconds
            now = self._clock()
            for name, state in self._state.get("providers", {}).items():
                if self._enabled_names is not None and name not in self._enabled_names:
                    continue
                if state.get("awaitingVerification") or state.get("armed"):
                    return self.poll_seconds
                deadlines = (
                    _epoch_seconds(state.get("nextGreetingAt")),
                    _epoch_seconds((state.get("lastStatus") or {}).get("reset_at")),
                )
                if any(value is not None and value - now <= NEAR_DEADLINE_SECONDS for value in deadlines):
                    return self.poll_seconds
            return max(self.poll_seconds, float(IDLE_POLL_SECONDS))

    def start(self) -> None:
        if not self.enabled or (self._thread and self._thread.is_alive()):
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="token-limit-reset", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)

    def _phase(self, state: dict[str, Any], now: float) -> str:
        if state.get("awaitingVerification"):
            return PHASE_GREETING_VERIFYING
        next_greeting = _epoch_seconds(state.get("nextGreetingAt"))
        if next_greeting is not None:
            return PHASE_KEEPALIVE_SCHEDULED
        if state.get("armed"):
            if state.get("needsLogin"):
                return PHASE_NEEDS_LOGIN
            if not state.get("resetObservedAt"):
                return PHASE_ARMED_WAITING_FOR_RESET
            weekly = (state.get("lastStatus") or {}).get("weekly_percent")
            if (weekly is not None and weekly >= WEEKLY_EXHAUSTED_PERCENT) or state.get("lastError") == WEEKLY_BLOCKED_ERROR:
                return PHASE_BLOCKED_WEEKLY
            return PHASE_RESET_OBSERVED_SENDING
        triggered = _epoch_seconds(state.get("lastTriggerAt"))
        if triggered is not None and now - triggered < GREETING_PHASE_SECONDS:
            return PHASE_GREETING_SENT
        return PHASE_WAITING_FOR_EXHAUSTION

    def _provider_summary(self, state: dict[str, Any], now: float) -> dict[str, Any]:
        summary = copy.deepcopy(state)
        for key, value in self._PROVIDER_DEFAULTS.items():
            summary.setdefault(key, value)
        scheduled = _epoch_seconds((summary.get("lastStatus") or {}).get("reset_at"))
        retry_after = float(summary.get("retryAfter") or 0)
        summary.update({
            "phase": self._phase(summary, now) if self.enabled else PHASE_DISABLED,
            "scheduledResetAt": scheduled,
            "scheduledResetPassed": scheduled is not None and scheduled <= now,
            "nextRetryAt": retry_after if (summary.get("armed") or summary.get("nextGreetingAt")) and retry_after > now else None,
            "nextGreetingAt": _epoch_seconds(summary.get("nextGreetingAt")),
        })
        return summary

    def status(self) -> dict[str, Any]:
        with self._lock:
            now = self._clock()
            providers = self._state.get("providers", {})
            visible = lambda name: self._enabled_names is None or name in self._enabled_names
            provider_names = dict.fromkeys((*providers, *self.sources))
            return {
                "enabled": self.enabled,
                "running": bool(self._thread and self._thread.is_alive()),
                "pollSeconds": self._poll_interval(),
                "statePath": str(self.state_path),
                "logPath": str(self.log_path),
                "startedAt": self._started_at,
                "persistenceError": self._persistence_error,
                "enabledProviders": list(self._enabled_names) if self._enabled_names is not None else list(providers),
                "providers": {
                    name: self._provider_summary(providers.get(name, {}), now)
                    for name in provider_names if visible(name)
                },
                "events": [event for event in self._events if event.get("provider") is None or visible(event.get("provider"))][-25:],
            }
