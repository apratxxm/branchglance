"""Local diagnostic report: one typed builder and one serializer for the endpoint, Settings preview and CLI."""
from __future__ import annotations

import builtins
import getpass
import http.client
import json
import logging
import os
import platform
import re
import socket
import sys
import time
import unicodedata
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote
from urllib.error import URLError
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener

from . import _version, features, providers

TITLE = "# Branchglance debug report"
HEADER = ("Contains app version, OS, settings switches, an anonymised agent tree and error categories. "
          "No chat text, file paths, account details or tokens are included. Review before sharing.")
MAX_BYTES = 64 * 1024
DEFAULT_PORT = 8765
DEFAULT_OUTPUT = "branchglance-debug-report.md"
PROVIDERS = ("Codex", "Claude")
EXACT_TIMES = ("runStartedAt", "executionObservedAt", "stoppedAt", "firstSeenAt")
BUCKETED_TIMES = ("updatedAt", "statusObservedAt", "stateSince", "executionSince", "executionTerminalAt",
                  "lastValidAt", "pendingQuestionAt", "questionResolutionAt", "compactionStartedAt",
                  "executionCompactionStartedAt", "identityRunStartedAt", "runEndedAt", "activeRunStartedAt",
                  "latestMessageAt", "attentionSince", "contextObservedAt")
FLAGS = ("hasPendingQuestion", "pendingQuestionExpired", "telemetryUnavailable", "activityUnavailable",
         "metricsUnavailable", "retainedRun")
# Enum values are the ones providers.py and agent_links.py actually produce; anything else is "unknown".
STATUS = frozenset({"running", "quiet", "needs input", "compacting", "idle", "failed", "unavailable", "unknown"})
STATE_DETAIL = frozenset({"errored", "completed", "interrupted", "inactive", "no recent writes", "stopped", "unknown"})
EXECUTION_STATUS = frozenset({"running", "compacting", "idle", "failed", "waiting", "unavailable", "unknown"})
EXECUTION_SOURCE = frozenset({"transcript", "app", "hook", "unavailable", "unknown"})
ACTIVITY_KINDS = frozenset({"searching", "reading", "editing", "testing", "running_command", "inspecting",
                            "coordinating", "checking_data", "using_tool", "waiting", "reading_output",
                            "searching_web", "running_code", "thinking", "unknown"})
ENUM_FIELDS = (("status", STATUS), ("stateDetail", STATE_DETAIL),
               ("executionStatus", EXECUTION_STATUS), ("executionSource", EXECUTION_SOURCE))
# parentLinkMethod values written by agent_links.py: "cmdline" and "receipt".
LINK_SOURCES = {"cmdline": "process", "receipt": "receipt"}
# Only fields the builder reads are kept; these are named so they can be reported as dropped.
DROPPED = ("id", "key", "parentKey", "threadId", "sessionId", "parentId", "parentThreadId", "title", "name",
           "displayName", "agentName", "agentRole", "path", "cwd", "workspace", "branch", "prompt",
           "pendingQuestion", "pendingOptions", "pendingQuestionIds", "activity", "executionActivity",
           "tokens", "cost", "usage", "identity")
MODULES = ("agent_links", "bg_codex", "claude_cache", "claude_collection", "claude_events", "claude_hook",
           "claude_hook_install", "claude_identity", "claude_provider", "claude_statusline", "claude_usage_api",
           "codex_avatars", "codex_client", "debug_report", "features", "insights", "ledger", "ledger_classify",
           "ledger_ingest", "ledger_model", "ledger_service", "ledger_store", "limit_reset", "macos_process",
           "macos_server", "product", "provider_policy", "providers", "reset_actions", "reset_delivery", "server",
           "statusline_connect", "widget")
COMPONENTS = frozenset(f"token_tracker.{name}" for name in MODULES)
_LEVELS = frozenset({"WARNING", "ERROR", "CRITICAL"})
_CLASS_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,60}")
# Fixed vocabulary: these string values are never scrubbed, so a short account name cannot corrupt them.
FIXED_VALUES = (STATUS | STATE_DETAIL | EXECUTION_STATUS | EXECUTION_SOURCE | ACTIVITY_KINDS | COMPONENTS
                | _LEVELS | frozenset(LINK_SOURCES.values()) | frozenset(DROPPED)
                | frozenset({"other", "yes", "no", "partial", "frozen", "source", "Windows", "Darwin", "Linux",
                             "WebView2", "WebKit", "scratch", "chat", "exec", "subagent", "custom/unknown",
                             "older", "<1m", "<10m", "<1h", "<1d", "Exception"}))
# Wire limits for the JSON endpoint; the CLI treats every field as untrusted.
JSON_MAX_BYTES = 4 * 1024 * 1024
MAX_ROWS = 5000
_BUCKETS = frozenset({"<1m", "<10m", "<1h", "<1d", "older"})
_ALIAS_PATTERN = r"r[1-9][0-9]{0,5}"
_ALIAS = re.compile(_ALIAS_PATTERN)
_ROW_KEYS = frozenset({"alias", "provider", "kind", "depth", "workspaceKind", "model", "parent", "linkSource",
                       "activityKind", "resumeCount", "dropped", *FLAGS, *EXACT_TIMES, *BUCKETED_TIMES,
                       *(key for key, _ in ENUM_FIELDS)})
_ROW_REQUIRED = frozenset({"alias", "provider", "kind", "depth", "workspaceKind", "model", "dropped"})
_ENV_KEYS = frozenset({"version", "commit", "run", "os", "os version", "architecture", "python", "webview",
                       "default port"})
_ENV_PATTERNS = {"version": r"\d+\.\d+\.\d+(?:[-.][A-Za-z0-9.]+)?", "commit": r"[0-9a-f]{7,40}",
                 "os version": r"[0-9A-Za-z._-]{1,40}", "architecture": r"AMD64|x86_64|ARM64|arm64|aarch64|x86|i386|i686",
                 "python": r"\d+\.\d+\.\d+"}
_CONFIG_BOOLS = frozenset({"CLAUDE_CONFIG_DIR set", "CODEX_HOME set", "status line connected",
                           *(f"{name}.{key}" for name in features.ENV
                             for key in ("persisted", "effective", "env_locked", "restart_pending"))})

# Scrubber: a backstop on the final text, applied after the typed builder has already dropped content.
_PATH = re.compile(r"""(?ix)
    [a-z]:[\\/][^\s"'<>|]*
  | \\\\[^\s"'<>|]*
  | ~[\\/][^\s"'<>|]*
  | /(?:users|home|var|tmp|etc|opt|mnt|private|root|volumes|library|srv)/[^\s"'<>|]*
""")
_SHORT_NAME = re.compile(r"(?<![\w~])[A-Za-z0-9_$-]{1,8}~\d{1,2}(?![\w~])(?:[\\/][^\s\"'<>|]*)?")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)*\.[A-Za-z]{2,}")
_IDS = re.compile(r"(?i)\b(?:sk|sess)-[A-Za-z0-9_-]+"
                  r"|\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"
                  r"|\beyJ[\w-]+\.[\w-]+\.[\w-]+"
                  r"|[A-Za-z0-9_+/-]{24,}={0,2}")


def install_handler() -> None:
    root = logging.getLogger()
    if LOG not in root.handlers:
        root.addHandler(LOG)


class ReportLogHandler(logging.Handler):
    """Keeps only (component, level, time, exception class) for the last 50 WARNING-or-higher records."""

    def __init__(self, capacity: int = 50):
        super().__init__(logging.WARNING)
        self.records: deque = deque(maxlen=capacity)

    def emit(self, record) -> None:
        try:
            component = record.name if record.name in COMPONENTS else "other"
            level = record.levelname if record.levelname in _LEVELS else "WARNING"
            self.records.append((component, level, time.time(), _exception_name(record.exc_info)))
        except Exception:
            return


LOG = ReportLogHandler()


def _exception_name(exc_info):
    cls = exc_info[0] if isinstance(exc_info, tuple) and exc_info else None
    if cls is None:
        return None
    if not (isinstance(cls, type) and issubclass(cls, BaseException)):
        return "Exception"
    if cls.__module__ == "builtins" and getattr(builtins, cls.__name__, None) is cls:
        return cls.__name__
    if cls.__module__.startswith("token_tracker.") and _CLASS_NAME.fullmatch(cls.__name__):
        return cls.__name__
    return "Exception"


def _name_values():
    try:
        user = unicodedata.normalize("NFC", getpass.getuser())
    except Exception:
        user = ""
    # Each word of a multi-word account name is redacted too, so a bare first name cannot leak.
    parts = [part for part in re.split(r"[\s._-]+", user) if len(part) >= 3]
    host = unicodedata.normalize("NFC", socket.gethostname())
    return tuple((value, "<user>") for value in [user, *parts]) + ((host, "<host>"),)


def _home_variants():
    try:
        home = unicodedata.normalize("NFC", str(Path.home()))
    except Exception:
        return ()
    if len(home) <= 3:
        return ()
    return (home, home.replace("\\", "/"))


def scrub(text) -> str:
    text = unicodedata.normalize("NFC", str(text))
    if "%" in text:
        # URL-encoded paths and names are decoded before matching.
        text = unicodedata.normalize("NFC", unquote(text))
    for variant in _home_variants():
        text = re.sub(re.escape(variant), "~", text, flags=re.I)
    text = _PATH.sub("<path>", text)
    text = _SHORT_NAME.sub("<path>", text)
    text = _EMAIL.sub("<email>", text)
    text = _IDS.sub("<id>", text)
    for value, replacement in _name_values():
        if len(value) >= 3:
            # Whole words only, so a name inside a longer token is left alone.
            text = re.sub(r"(?<!\w)" + re.escape(value) + r"(?!\w)", replacement, text, flags=re.I)
    return text


def scrub_tree(value):
    """Scrubs string values only. Keys, numbers, booleans, None and the fixed vocabulary pass through untouched."""
    if isinstance(value, dict):
        return {key: scrub_tree(item) for key, item in value.items()}
    if isinstance(value, list):
        return [scrub_tree(item) for item in value]
    if type(value) is str and value not in FIXED_VALUES:
        return scrub(value)
    return value


def _enum(value, allowed):
    """None stays absent; any non-member, including nested values, becomes 'unknown'."""
    if value is None:
        return None
    return value if type(value) is str and value in allowed else "unknown"


def _stamp(value, now):
    """Relative whole seconds for ISO strings with a timezone, or epoch numbers; None otherwise."""
    try:
        if isinstance(value, str):
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                return None
            value = dt.timestamp()
        if type(value) not in (int, float) or not 0 < value < 253402300800:
            return None
        return int(value - now)
    except (ValueError, OverflowError, OSError):
        return None


def _bucket(stamp: int) -> str:
    age = abs(stamp)
    return "<1m" if age < 60 else "<10m" if age < 600 else "<1h" if age < 3600 else "<1d" if age < 86400 else "older"


def _public_model(target: str) -> str:
    if target in providers.CLAUDE_PRICING:
        family, _, version = target.partition("-")
        return f"Claude {family.capitalize()} {version}"
    match = re.fullmatch(r"gpt-([0-9.]+)(?:-(.+))?", target)
    if not match:
        return "custom/unknown"
    suffix = f" {match.group(2).capitalize()}" if match.group(2) else ""
    return f"GPT-{match.group(1)}{suffix}"


def _model(value) -> str:
    if not isinstance(value, str) or not 0 < len(value) <= 100:
        return "custom/unknown"
    canonical = providers._normalise_model_id(value)
    target = providers.PRICE_MODEL_ALIASES.get(canonical, canonical)
    if target in providers.CLAUDE_PRICING or target in providers.CODEX_PRICING:
        return _public_model(target)
    return "custom/unknown"


def _safe_version() -> str:
    value = _version.__version__
    return value if isinstance(value, str) and re.fullmatch(r"\d+\.\d+\.\d+(?:[-.][A-Za-z0-9.]+)?", value) else "unknown"


def _commit() -> str:
    try:
        from ._build import BUILD_ID
    except ImportError:
        return "unknown"
    return BUILD_ID if isinstance(BUILD_ID, str) and re.fullmatch(r"[0-9a-f]{7,40}", BUILD_ID) else "unknown"


def _token(value, pattern: str) -> str:
    return value if isinstance(value, str) and re.fullmatch(pattern, value) else "unknown"


def _environment(port: int) -> dict:
    system = platform.system()
    return {
        "version": _safe_version(),
        "commit": _commit(),
        "run": "frozen" if getattr(sys, "frozen", False) else "source",
        "os": system if system in {"Windows", "Darwin", "Linux"} else "unknown",
        "os version": _token(platform.version(), r"[0-9A-Za-z._-]{1,40}"),
        "architecture": _token(platform.machine(), r"AMD64|x86_64|ARM64|arm64|aarch64|x86|i386|i686"),
        "python": _token(platform.python_version(), r"\d+\.\d+\.\d+"),
        "webview": "WebView2" if system == "Windows" else "WebKit" if system == "Darwin" else "unknown",
        "default port": port == DEFAULT_PORT,
    }


def _configuration() -> dict:
    from . import claude_collection, statusline_connect
    from .provider_policy import public_policies
    result = {"CLAUDE_CONFIG_DIR set": bool(os.environ.get("CLAUDE_CONFIG_DIR")),
              "CODEX_HOME set": bool(os.environ.get("CODEX_HOME"))}
    try:
        result["enabled providers"] = sorted(p["name"] for p in public_policies()["providers"]
                                             if p.get("name") in PROVIDERS and p.get("show") is True)
    except Exception:
        result["enabled providers"] = []
    for name in features.ENV:
        try:
            state = features.state(name)
        except Exception:
            continue
        for key in ("persisted", "effective", "env_locked", "restart_pending"):
            if type(state.get(key)) is bool:
                result[f"{name}.{key}"] = state[key]
    try:
        hooks = claude_collection.status()["hooks"]["state"]
        result["Claude hooks"] = {"installed": "yes", "missing": "no", "partial": "partial"}.get(hooks, "unknown")
    except Exception:
        result["Claude hooks"] = "unknown"
    try:
        result["status line connected"] = statusline_connect.status().get("connected") is True
    except Exception:
        result["status line connected"] = False
    return result


@dataclass(frozen=True)
class Report:
    running: bool
    environment: dict
    configuration: dict
    rows: list
    problems: list


def _depth(alias: str, parents: dict) -> int:
    depth, seen, current = 0, {alias}, parents.get(alias)
    while current is not None and current not in seen and depth < 50:
        seen.add(current)
        depth += 1
        current = parents.get(current)
    return depth


def _row(alias, name, row, parent, depth, now) -> dict:
    source = row.get("source") if isinstance(row.get("source"), str) else None
    kind = "exec" if source == "exec" else "subagent" if parent or source in {"subagent", "review"} else "chat"
    item = {"alias": alias, "provider": name, "kind": kind, "depth": depth,
            "workspaceKind": "scratch" if row.get("workspaceKind") == "codex-chat" else "unknown",
            "model": _model(row.get("model"))}
    if parent is not None:
        item["parent"] = parent
    for key, allowed in ENUM_FIELDS:
        value = _enum(row.get(key), allowed)
        if value is not None:
            item[key] = value
    method = row.get("parentLinkMethod")
    if method is not None:
        item["linkSource"] = LINK_SOURCES.get(method, "unknown") if type(method) is str else "unknown"
    activity = row.get("activity")
    if isinstance(activity, dict):
        kind_value = _enum(activity.get("kind"), ACTIVITY_KINDS)
        if kind_value is not None:
            item["activityKind"] = kind_value
    count = row.get("resumeCount")
    if type(count) is int and 0 <= count <= 100000:
        item["resumeCount"] = count
    for key in FLAGS:
        if type(row.get(key)) is bool:
            item[key] = row[key]
    if "hasPendingQuestion" not in item and isinstance(row.get("pendingQuestion"), str) and row["pendingQuestion"].strip():
        item["hasPendingQuestion"] = True
    for key in EXACT_TIMES + BUCKETED_TIMES:
        stamp = _stamp(row.get(key), now)
        if stamp is not None:
            item[key] = stamp if key in EXACT_TIMES else _bucket(stamp)
    item["dropped"] = [key for key in DROPPED if key in row]
    return item


def _rows(snapshot, now) -> list:
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("providers"), list):
        return []
    entries = []
    for provider in snapshot["providers"]:
        if isinstance(provider, dict) and provider.get("name") in PROVIDERS and isinstance(provider.get("chats"), list):
            entries.extend((provider["name"], row) for row in provider["chats"] if isinstance(row, dict))
    aliases = [f"r{number}" for number in range(1, len(entries) + 1)]
    by_id = {}
    for alias, (name, row) in zip(aliases, entries):
        if isinstance(row.get("id"), str):
            by_id.setdefault((name, row["id"]), alias)
    parents = {}
    for alias, (name, row) in zip(aliases, entries):
        parent_id = row.get("parentId")
        provider = row.get("parentProvider") if row.get("parentProvider") in PROVIDERS else name
        parents[alias] = by_id.get((provider, parent_id)) if isinstance(parent_id, str) else None
    return [_row(alias, name, row, parents[alias], _depth(alias, parents), now)
            for alias, (name, row) in zip(aliases, entries)]


def build_report(snapshot=None, *, running: bool = True, port: int = DEFAULT_PORT, now=None) -> Report:
    """Freshly constructed, validated report. Input values are never copied wholesale."""
    now = time.time() if now is None else now
    if not running:
        return Report(False, _environment(port), _configuration(), [], [])
    problems = [{"component": component, "level": level, "time": int(stamp - now), "exception": exception}
                for component, level, stamp, exception in list(LOG.records)]
    return Report(True, _environment(port), _configuration(), _rows(snapshot, now), problems)


def _block(value) -> str:
    return "```json\n" + json.dumps(scrub_tree(value), ensure_ascii=False, indent=2) + "\n```"


def _render(report: Report, rows: list, omitted: list, dropped: int) -> str:
    parts = [TITLE, HEADER, "Direct identifiers are removed.", "## Environment", _block(report.environment),
             "## Configuration", _block(report.configuration)]
    if report.running:
        parts += ["## Agent tree", _block(rows),
                  f"Dropped to fit the size cap: {dropped} rows. Truncated aliases: {', '.join(omitted) or 'none'}.",
                  "## Recent problems", _block(report.problems)]
    else:
        parts.append("App not running: the agent tree and recent problems are not included.")
    return "\n\n".join(parts) + "\n"


def _size(row: dict) -> int:
    return len(json.dumps(row, ensure_ascii=False, indent=2)) + 8


def _age_key(row: dict) -> int:
    for key in ("runStartedAt", "firstSeenAt"):
        if type(row.get(key)) is int:
            return row[key]
    return -10**12  # rows with no timestamp are pruned first


def _collect(root: str, live: dict, kids: dict) -> list:
    group, seen, pending = [], set(), [root]
    while pending:
        alias = pending.pop()
        if alias in live and alias not in seen:
            seen.add(alias)
            group.append(alias)
            pending.extend(kids.get(alias, []))
    return group


def _prune(live: dict, overflow: int, omitted: list) -> int:
    """Oldest root first: collapse a root's descendants into one omitted node, then drop the whole subtree.

    Returns the number of real rows removed. Mutates `live` (insertion-ordered) and `omitted`.
    """
    kids: dict = {}
    for alias, row in live.items():
        kids.setdefault(row.get("parent"), []).append(alias)
    roots = [a for a, r in live.items() if r.get("parent") not in live] or list(live)
    roots.sort(key=lambda a: _age_key(live[a]))
    freed = dropped = 0
    for root in roots:
        if freed >= overflow:
            break
        if root not in live:
            continue
        group = _collect(root, live, kids)
        has_node = any(live[a].get("kind") == "omitted" for a in group)
        if len(group) > 1 and not has_node:
            descendants = [a for a in group if a != root]
            freed += sum(_size(live.pop(a)) for a in descendants)
            node_alias = f"{root}-omitted"
            node = {"alias": node_alias, "parent": root, "kind": "omitted", "count": len(descendants)}
            live[node_alias] = node
            kids.setdefault(root, []).append(node_alias)
            freed -= _size(node)
            omitted.extend(sorted(descendants, key=lambda a: int(a[1:]) if a[1:].isdigit() else 0))
            dropped += len(descendants)
        else:
            removed = [live.pop(a) for a in group]
            freed += sum(_size(row) for row in removed)
            # Omitted nodes stand for rows already counted when their root was collapsed, so they add nothing.
            dropped += sum(1 for row in removed if row.get("kind") != "omitted")
            omitted.extend(row["alias"] for row in removed if row.get("kind") != "omitted")
    return dropped


def serialize(report: Report, cap: int = MAX_BYTES) -> str:
    """The one serializer. The cap is measured on final UTF-8 bytes."""
    live = {row["alias"]: dict(row) for row in report.rows}
    omitted: list = []
    dropped = 0
    text = _render(report, list(live.values()), omitted, dropped)
    while len(text.encode("utf-8")) > cap and live:
        overflow = len(text.encode("utf-8")) - cap
        dropped += _prune(live, overflow, omitted)
        text = _render(report, list(live.values()), omitted, dropped)
    if len(text.encode("utf-8")) > cap:
        # Fails closed when even the fixed sections exceed the cap.
        text = "Report truncated.\n"
        if len(text.encode("utf-8")) > cap:
            text = ""
    return text


def report_to_dict(report: Report) -> dict:
    """Structured form served by /api/debug-report.json. Rows are not pruned here; the CLI prunes locally."""
    return {"running": report.running, "environment": dict(report.environment),
            "configuration": dict(report.configuration), "rows": [dict(row) for row in report.rows],
            "problems": [dict(problem) for problem in report.problems]}


def _wire_bool(value) -> bool:
    if type(value) is not bool:
        raise ValueError("expected boolean")
    return value


def _wire_int(value, low: int, high: int) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ValueError("expected integer in range")
    return value


def _wire_choice(value, allowed) -> str:
    if type(value) is not str or value not in allowed:
        raise ValueError("unexpected value")
    return value


def _wire_pattern(value, pattern: str) -> str:
    if type(value) is not str or not re.fullmatch(pattern, value):
        raise ValueError("unexpected string")
    return value


def _wire_enum(value, allowed) -> str:
    """Strings outside the set become 'unknown' as in the builder; nested or non-string values are rejected."""
    if type(value) is not str:
        raise ValueError("expected string enum")
    return _enum(value, allowed)


def _public_models() -> frozenset:
    return frozenset({"custom/unknown", *(_public_model(t) for t in providers.CLAUDE_PRICING),
                      *(_public_model(t) for t in providers.CODEX_PRICING)})


def _check_environment(data) -> dict:
    if type(data) is not dict or set(data) != _ENV_KEYS:
        raise ValueError("environment keys")
    out = {}
    for key, pattern in _ENV_PATTERNS.items():
        out[key] = "unknown" if data[key] == "unknown" else _wire_pattern(data[key], pattern)
    out["run"] = _wire_choice(data["run"], {"frozen", "source"})
    out["os"] = _wire_choice(data["os"], {"Windows", "Darwin", "Linux", "unknown"})
    out["webview"] = _wire_choice(data["webview"], {"WebView2", "WebKit", "unknown"})
    out["default port"] = _wire_bool(data["default port"])
    # Builder key order, so a fetched report serializes byte-for-byte like a local one.
    out = {key: out[key] for key in ("version", "commit", "run", "os", "os version", "architecture", "python",
                                     "webview", "default port")}
    return out


def _check_configuration(data) -> dict:
    if type(data) is not dict:
        raise ValueError("configuration")
    out = {}
    for key, value in data.items():
        if key in _CONFIG_BOOLS:
            out[key] = _wire_bool(value)
        elif key == "Claude hooks":
            out[key] = _wire_choice(value, {"yes", "no", "partial", "unknown"})
        elif key == "enabled providers":
            if type(value) is not list:
                raise ValueError("providers")
            out[key] = [_wire_choice(item, PROVIDERS) for item in value]
        else:
            raise ValueError("unexpected configuration key")
    return out


def _check_row(data) -> dict:
    if type(data) is not dict or not _ROW_REQUIRED <= set(data) <= _ROW_KEYS:
        raise ValueError("row keys")
    item = {"alias": _wire_pattern(data["alias"], _ALIAS_PATTERN),
            "provider": _wire_choice(data["provider"], PROVIDERS),
            "kind": _wire_choice(data["kind"], {"exec", "subagent", "chat"}),
            "depth": _wire_int(data["depth"], 0, 50),
            "workspaceKind": _wire_choice(data["workspaceKind"], {"scratch", "unknown"}),
            "model": _wire_choice(data["model"], _public_models())}
    if "parent" in data:
        item["parent"] = _wire_pattern(data["parent"], _ALIAS_PATTERN)
    for key, allowed in ENUM_FIELDS:
        if key in data:
            item[key] = _wire_enum(data[key], allowed)
    if "linkSource" in data:
        item["linkSource"] = _wire_choice(data["linkSource"], {"process", "receipt", "unknown"})
    if "activityKind" in data:
        item["activityKind"] = _wire_enum(data["activityKind"], ACTIVITY_KINDS)
    if "resumeCount" in data:
        item["resumeCount"] = _wire_int(data["resumeCount"], 0, 100000)
    for key in FLAGS:
        if key in data:
            item[key] = _wire_bool(data[key])
    for key in EXACT_TIMES:
        if key in data:
            item[key] = _wire_int(data[key], -10**10, 10**10)
    for key in BUCKETED_TIMES:
        if key in data:
            item[key] = _wire_choice(data[key], _BUCKETS)
    if type(data["dropped"]) is not list:
        raise ValueError("dropped")
    item["dropped"] = [_wire_choice(name, DROPPED) for name in data["dropped"]]
    return item


def _check_problem(data) -> dict:
    if type(data) is not dict or set(data) != {"component", "level", "time", "exception"}:
        raise ValueError("problem keys")
    exception = None if data["exception"] is None else _wire_pattern(data["exception"], _CLASS_NAME.pattern)
    return {"component": _wire_choice(data["component"], COMPONENTS | {"other"}),
            "level": _wire_choice(data["level"], _LEVELS),
            "time": _wire_int(data["time"], -10**10, 10**10), "exception": exception}


def report_from_dict(data) -> Report:
    """Strict typed reconstruction of a fetched report. Raises ValueError on any unexpected shape or value."""
    if type(data) is not dict or set(data) != {"running", "environment", "configuration", "rows", "problems"}:
        raise ValueError("report keys")
    rows, problems = data["rows"], data["problems"]
    if type(rows) is not list or len(rows) > MAX_ROWS or type(problems) is not list or len(problems) > 50:
        raise ValueError("report size")
    return Report(_wire_bool(data["running"]), _check_environment(data["environment"]),
                  _check_configuration(data["configuration"]), [_check_row(row) for row in rows],
                  [_check_problem(problem) for problem in problems])


def _reject_constant(name):
    raise ValueError(f"non-finite number {name}")


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise URLError(f"redirect refused ({code})")


def _fetch_running(port: int):
    """Returns a validated Report from the running app, or None on any failure."""
    try:
        opener = build_opener(ProxyHandler({}), _NoRedirect)
        with opener.open(f"http://127.0.0.1:{int(port)}/api/debug-report.json", timeout=2) as response:
            if response.status != 200:
                return None
            data = response.read(JSON_MAX_BYTES + 1)
    except (OSError, ValueError, http.client.HTTPException):
        return None
    if len(data) > JSON_MAX_BYTES:
        return None
    try:
        return report_from_dict(json.loads(data.decode("utf-8"), parse_constant=_reject_constant))
    except Exception:
        return None


def write_cli(path, *, port: int = DEFAULT_PORT) -> int:
    """Writes the running instance's report (serialized here), or the not-running report on any failure."""
    report = _fetch_running(port)
    if report is None:
        report = build_report(running=False, port=port)
    text = serialize(report)
    target = Path(path)
    try:
        target.write_text(text, encoding="utf-8", newline="\n")
    except OSError:
        print("Could not write the debug report.", file=sys.stderr)
        return 1
    print(os.path.abspath(target))
    return 0
