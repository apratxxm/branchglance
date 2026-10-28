"""Install, remove and report Branchglance's Claude Code hooks in user settings.

Safety rules:
* Only ``~/.claude/settings.json`` (user scope) is touched, never project,
  local or managed settings.
* Only matcher groups whose every hook command is a Branchglance invocation
  with the standalone ``--branchglance-hook`` argument are ours. Groups with
  any other hook are user-owned and never edited.
* Refuse on an unparseable or non-object settings file, and when any path
  component from %USERPROFILE% down to settings.json is a symlink, junction
  or other reparse point.
* Writes use a bounded compare-before-replace protocol: capture bytes and file
  identity, write a unique backup and temp file, then immediately re-read and
  compare before replacement. A concurrent edit restarts the transform.
* The consent marker (what lets the hook write anything) is created last on
  install and removed on uninstall.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shlex
import shutil
import stat
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import claude_hook

MARKER = "--branchglance-hook"
HOOK_TIMEOUT_SECONDS = 10  # Claude Code's own bound; the hook also self-exits sooner
WRITE_SAFETY = ("Best-effort compare-and-swap: bytes and file identity are rechecked immediately before replacement, "
                "concurrent edits are retried, but this is not atomic CAS against another process.")
EVENTS = (
    "SessionStart", "SessionEnd", "UserPromptSubmit", "PreToolUse", "PostToolUse", "PostToolUseFailure",
    "PermissionRequest", "Notification", "Stop", "StopFailure", "SubagentStart", "SubagentStop",
    "PreCompact", "PostCompact",
)


class HookInstallError(Exception):
    """The settings file cannot be edited safely; nothing was changed."""


class _ConcurrentSettingsEdit(Exception):
    """The target changed while one consistent snapshot was being captured."""


@dataclass(frozen=True)
class _SettingsSnapshot:
    raw: bytes | None
    digest: str | None
    mtime_ns: int | None
    device: int | None
    inode: int | None
    size: int | None

    def matches(self, other: "_SettingsSnapshot") -> bool:
        return (self.raw == other.raw and self.digest == other.digest and self.mtime_ns == other.mtime_ns
                and self.device == other.device and self.inode == other.inode and self.size == other.size)


def claude_config_dir(home: Path | None = None) -> Path:
    """Resolve Claude's user configuration, retaining the legacy override priority."""
    configured = os.environ.get("CLAUDE_HOME") or os.environ.get("CLAUDE_CONFIG_DIR")
    return Path(configured) if configured else (home or Path.home()) / ".claude"


def default_settings_path() -> Path:
    return claude_config_dir() / "settings.json"


def _bash_quote(value: str | os.PathLike[str]) -> str:
    """Single-quote one literal shell argument for Claude's bash hook runner."""
    return "'" + os.fspath(value).replace("'", "'\"'\"'") + "'"


def default_command(executable: str | os.PathLike[str] | None = None,
                   script: str | os.PathLike[str] | None = None) -> str:
    """Stable hook command: the frozen launcher, or the hook script in a source checkout."""
    if getattr(sys, "frozen", False):
        from . import product
        launcher = executable or product.launcher_path().as_posix()
        return f"{_bash_quote(str(launcher).replace(chr(92), '/'))} --claude-hook {MARKER}"
    executable = executable or Path(sys.executable).as_posix()
    script = script or Path(claude_hook.__file__).resolve().as_posix()
    return f"{_bash_quote(executable)} {_bash_quote(script)} {MARKER}"


def _group(command: str) -> dict[str, Any]:
    # "shell": "bash" pins Claude Code to Git Bash; the command is bash-quoted
    # and must never be run by the PowerShell fallback.
    return {"hooks": [{"type": "command", "command": command, "shell": "bash", "async": True,
                       "timeout": HOOK_TIMEOUT_SECONDS}]}


# ------------------------------------------------------------ capabilities

def find_git_bash() -> str | None:
    """Path of the Git Bash that Claude Code runs hooks with, or None."""
    if os.name != "nt":
        return shutil.which("bash")
    candidates: list[Path] = []
    configured = os.environ.get("CLAUDE_CODE_GIT_BASH_PATH")
    if configured:
        candidates.append(Path(configured))
    git = shutil.which("git")
    if git:
        git_dir = Path(git).resolve().parent  # .../Git/cmd or .../Git/bin
        candidates += [git_dir / "bash.exe", git_dir.parent / "bin" / "bash.exe"]
    for variable in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)", "LOCALAPPDATA"):
        base = os.environ.get(variable)
        if base:
            sub = Path(base) / ("Programs/Git" if variable == "LOCALAPPDATA" else "Git")
            candidates.append(sub / "bin" / "bash.exe")
    found = shutil.which("bash")
    if found:
        lowered = found.lower().replace("/", "\\")
        # System32\bash.exe and the WindowsApps alias are WSL, not Git Bash.
        if "\\system32\\" not in lowered and "\\windowsapps\\" not in lowered:
            candidates.append(Path(found))
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return None


def _launcher_supports_hook() -> bool:
    """Whether a frozen build's launcher implements ``--claude-hook``.

    The packaging lane sets ``product.CLAUDE_HOOK_LAUNCHER``; until then a
    frozen install would write hook commands nothing can run.
    """
    try:
        from . import product  # type: ignore[attr-defined]
    except ImportError:
        return False
    return getattr(product, "CLAUDE_HOOK_LAUNCHER", False) is True


# ------------------------------------------------------------- ownership

def _norm_exe(value: str) -> str:
    return os.path.normcase(value.replace("\\", "/")).replace("\\", "/")


def _executable_of(command: Any) -> str | None:
    if not isinstance(command, str):
        return None
    try:
        words = shlex.split(command, posix=True)
    except ValueError:
        return None
    return _norm_exe(words[0]) if words else None


def default_executables() -> frozenset[str]:
    """The configured launcher (frozen) or this checkout's interpreter (dev)."""
    found = {_norm_exe(Path(sys.executable).as_posix())}
    default_exe = _executable_of(default_command())
    if default_exe:
        found.add(default_exe)
    return frozenset(found)


def _commands(group: Any) -> list[Any]:
    hooks = group.get("hooks") if isinstance(group, dict) else None
    return [h.get("command") if isinstance(h, dict) else None for h in hooks] if isinstance(hooks, list) else []


def is_ours(group: Any, executables: frozenset[str] | set[str] | None = None) -> bool:
    known = default_executables() if executables is None else executables
    commands = _commands(group)
    return bool(commands) and all(_is_branchglance_command(c, known) for c in commands)


def _is_branchglance_command(command: Any, executables: frozenset[str] | set[str] | None = None) -> bool:
    if not isinstance(command, str):
        return False
    try:
        words = shlex.split(command, posix=True)
    except ValueError:
        return False
    if MARKER not in words:
        return False
    if executables is not None and _norm_exe(words[0]) not in executables:
        return False
    # The frozen launcher's mode flag, ``-m token_tracker.claude_hook``, or the
    # hook script itself as the first argument after the interpreter.
    return ("--claude-hook" in words
            or any(words[i:i + 2] == ["-m", "token_tracker.claude_hook"] for i in range(len(words) - 1))
            or (len(words) >= 2 and words[1].replace("\\", "/").rsplit("/", 1)[-1] == "claude_hook.py"))


def _touches_marker(group: Any) -> bool:
    for command in _commands(group):
        if not isinstance(command, str):
            continue
        try:
            if MARKER in shlex.split(command, posix=True):
                return True
        except ValueError:
            continue
    return False


# --------------------------------------------------------------------- files

def _is_reparse(path: Path, info: os.stat_result) -> bool:
    if os.path.islink(path):
        return True
    attr = getattr(info, "st_file_attributes", 0)
    return bool(attr & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def _chain_root(absolute: Path) -> Path:
    """%USERPROFILE% when settings live under it (the normal case), else the volume root."""
    try:
        home = Path(os.path.abspath(Path.home()))
        absolute.relative_to(home)
        return home
    except (ValueError, RuntimeError, OSError):
        return Path(absolute.anchor)


def _validate_path_chain(path: Path) -> None:
    """Refuse symlink/junction components from %USERPROFILE% down to settings."""
    absolute = Path(os.path.abspath(path))
    cursor = _chain_root(absolute)
    for part in absolute.parts[len(cursor.parts):]:
        cursor = cursor / part
        try:
            info = cursor.lstat()
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise HookInstallError("cannot validate settings path chain") from exc
        if _is_reparse(cursor, info):
            raise HookInstallError("settings path contains a symlink or reparse point; refusing to edit it")


def _snapshot_file(path: Path) -> _SettingsSnapshot:
    """Capture bytes and identity from one stable regular-file observation."""
    _validate_path_chain(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return _SettingsSnapshot(None, None, None, None, None, None)
    except OSError as exc:
        raise HookInstallError(f"cannot inspect settings.json ({exc.__class__.__name__})") from None
    if _is_reparse(path, info):
        raise HookInstallError("settings.json is a symlink or reparse point; refusing to edit it")
    if not stat.S_ISREG(info.st_mode):
        raise HookInstallError("settings.json is not a regular file")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        if isinstance(exc, FileNotFoundError):
            raise _ConcurrentSettingsEdit from None
        raise HookInstallError(f"cannot read settings.json ({exc.__class__.__name__})") from None
    try:
        after = path.lstat()
    except FileNotFoundError:
        raise _ConcurrentSettingsEdit from None
    except OSError as exc:
        raise HookInstallError(f"cannot recheck settings.json ({exc.__class__.__name__})") from None
    if _is_reparse(path, after) or not stat.S_ISREG(after.st_mode):
        raise HookInstallError("settings.json changed to an unsafe file; aborted")
    before_identity = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
    after_identity = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
    if before_identity != after_identity or len(raw) != after.st_size:
        raise _ConcurrentSettingsEdit
    return _SettingsSnapshot(raw, _digest(raw), after.st_mtime_ns, after.st_dev, after.st_ino, after.st_size)


def _parse_snapshot(snapshot: _SettingsSnapshot) -> tuple[dict[str, Any], bool]:
    raw = snapshot.raw
    if raw is None:
        return {}, False
    bom = raw.startswith(b"\xef\xbb\xbf")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise HookInstallError("settings.json is not valid UTF-8") from exc
    if not text.strip():
        return {}, bom
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise HookInstallError("settings.json is not parseable JSON; refusing to edit it") from exc
    if not isinstance(data, dict):
        raise HookInstallError("settings.json root is not an object")
    hooks = data.get("hooks")
    if hooks is not None and not isinstance(hooks, dict):
        raise HookInstallError("settings.json 'hooks' is not an object")
    for event, groups in (hooks or {}).items():
        if not isinstance(groups, list):
            raise HookInstallError(f"hooks.{event} is not a list")
    return data, bom


def _read_versioned(path: Path) -> tuple[_SettingsSnapshot, dict[str, Any], bool]:
    snapshot = _snapshot_file(path)
    data, bom = _parse_snapshot(snapshot)
    return snapshot, data, bom


def _read(path: Path) -> tuple[bytes | None, dict[str, Any], bool]:
    """(raw bytes or None, parsed settings, had_bom). Refuses unsafe files."""
    snapshot, data, bom = _read_versioned(path)
    return snapshot.raw, data, bom


def _serialize(data: dict[str, Any], bom: bool, original: bytes | None = None) -> bytes:
    if original is None:
        indent, compact, newline, trailing_newline, ensure_ascii = 2, False, "\n", True, False
    else:
        source = original.decode("utf-8-sig")
        newline = "\r\n" if "\r\n" in source else "\n"
        trailing_newline = source.endswith(("\n", "\r"))
        match = re.search(r"(?m)^([ \t]+)\"[^\"\r\n]+\"\s*:", source)
        indent = match.group(1) if match else 2
        compact = not ("\n" in source or "\r" in source)
        ensure_ascii = not any(ord(char) > 127 for char in source)
    options = ({"separators": (",", ":")} if compact else {"indent": indent})
    text = json.dumps(data, ensure_ascii=ensure_ascii, **options)
    if newline != "\n":
        text = text.replace("\n", newline)
    if trailing_newline:
        text += newline
    return (b"\xef\xbb\xbf" if bom else b"") + text.encode("utf-8")


def _digest(raw: bytes | None) -> str | None:
    return hashlib.sha256(raw).hexdigest() if raw is not None else None


def _exclusive_file(path: Path, payload: bytes) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        view = memoryview(payload)
        while view:
            count = os.write(fd, view)
            if count <= 0:
                raise OSError("short write")
            view = view[count:]
        os.fsync(fd)
    finally:
        os.close(fd)


def _unique_adjacent(path: Path, tag: str, payload: bytes) -> Path:
    for _ in range(32):
        candidate = path.with_name(f"{path.name}.branchglance-{tag}-{secrets.token_hex(8)}")
        try:
            _exclusive_file(candidate, payload)
            return candidate
        except FileExistsError:
            continue
    raise HookInstallError(f"could not create a unique {tag} file")


KEEP_BACKUPS = 5


def _backup_pattern(path: Path) -> re.Pattern[str]:
    return re.compile(re.escape(path.name) + r"\.branchglance-backup-\d{8}-\d{6}-[0-9a-f]{16}")


def _prune_backups(path: Path, keep: int = KEEP_BACKUPS) -> list[str]:
    """Delete all but the newest ``keep`` backups matching our exact name pattern.

    Only regular files are removed; links, directories and anything not
    matching the pattern are left alone. Failures are ignored.
    """
    pattern = _backup_pattern(path)
    found: list[tuple[str, Path]] = []
    try:
        with os.scandir(path.parent) as entries:
            for entry in entries:
                if pattern.fullmatch(entry.name) and entry.is_file(follow_symlinks=False):
                    info = entry.stat(follow_symlinks=False)
                    if _is_reparse(Path(entry.path), info):
                        continue
                    stamp = entry.name[len(path.name) + len(".branchglance-backup-"):][:15]
                    found.append((stamp + f"{info.st_mtime_ns:020d}", Path(entry.path)))
    except OSError:
        return []
    removed = []
    for _key, old in sorted(found)[:-keep] if keep > 0 else sorted(found):
        try:
            old.unlink()
            removed.append(old.name)
        except OSError:
            pass
    return removed


def _replace_with_retry(temp: Path, path: Path, expected: _SettingsSnapshot) -> None:
    """Recheck and replace, briefly retrying Windows sharing violations."""
    for retry in range(3):
        current = _snapshot_file(path)
        if not expected.matches(current):
            raise _ConcurrentSettingsEdit
        try:
            os.replace(temp, path)
            return
        except PermissionError:
            if retry == 2:
                raise
            time.sleep(0.025 * (retry + 1))


def _write_transform_checked(path: Path, transform, now: float) -> str | None:
    """Apply a transform only to a stable, freshly rechecked snapshot."""
    saw_existing = False
    for attempt in range(3):
        try:
            snapshot, data, bom = _read_versioned(path)
        except _ConcurrentSettingsEdit:
            if attempt == 2:
                raise HookInstallError("settings.json kept changing; latest content was left untouched") from None
            continue
        original = snapshot.raw
        if original is None and saw_existing:
            raise HookInstallError("settings.json changed while editing; it disappeared and was left untouched")
        saw_existing = saw_existing or original is not None
        payload = transform(data, bom, original)
        if payload is None:
            return None
        backup: str | None = None
        temp: Path | None = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            _validate_path_chain(path)
            if original is not None:
                stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
                backup = str(_unique_adjacent(path, f"backup-{stamp}", original))
            temp = _unique_adjacent(path, "tmp", payload)
            try:
                _replace_with_retry(temp, path, snapshot)
            except _ConcurrentSettingsEdit:
                if attempt == 2:
                    raise HookInstallError("settings.json kept changing; latest content was left untouched") from None
                continue
            temp = None
            try:
                after = _snapshot_file(path)
            except _ConcurrentSettingsEdit:
                after = None
            if after is not None and after.raw == payload:
                _prune_backups(path)
                return backup
            if attempt == 2:
                raise HookInstallError("settings.json changed during verification; update refused")
        except OSError as exc:
            action = "backup" if original is not None and backup is None else "update"
            raise HookInstallError(f"could not write settings {action} ({exc.__class__.__name__}); settings were left unchanged") from None
        finally:
            if temp is not None:
                try:
                    temp.unlink()
                except OSError:
                    pass
    raise HookInstallError("settings.json kept changing; update refused")


# ------------------------------------------------------------------ consent

def _consent_file(data_directory: str | os.PathLike[str] | None) -> Path:
    return (Path(data_directory) if data_directory is not None else Path(claude_hook.data_dir())) / claude_hook.CONSENT_NAME


def _write_consent(data_directory: str | os.PathLike[str] | None, now: float, command: str,
                   settings_path: Path) -> None:
    marker = _consent_file(data_directory)
    marker.parent.mkdir(parents=True, exist_ok=True)
    # The installed executable is recorded so a later uninstall/status can
    # recognise our groups even when the configured launcher path changed.
    marker.write_text(json.dumps({"version": 1, "enabled": True, "createdAt": round(now, 3),
                                  "executable": _executable_of(command),
                                  "settingsPath": str(settings_path)}), encoding="utf-8")


def _recorded_settings_path(data_directory: str | os.PathLike[str] | None) -> Path | None:
    try:
        value = json.loads(_consent_file(data_directory).read_text(encoding="utf-8")).get("settingsPath")
        if not isinstance(value, str) or not value:
            return None
        path = Path(value)
        if not path.is_absolute() or path.resolve() != path:
            return None
        return path
    except (OSError, ValueError, AttributeError, RuntimeError):
        return None


def _recorded_executable(data_directory: str | os.PathLike[str] | None) -> str | None:
    try:
        value = json.loads(_consent_file(data_directory).read_text(encoding="utf-8")).get("executable")
    except (OSError, ValueError, AttributeError):
        return None
    return _norm_exe(value) if isinstance(value, str) and value else None


def _remove_consent(data_directory: str | os.PathLike[str] | None) -> bool:
    """Remove the consent marker; True when it no longer exists."""
    marker = _consent_file(data_directory)
    try:
        marker.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        return False
    return not marker.exists()


def _known_executables(data_directory: str | os.PathLike[str] | None,
                       command: str | None = None) -> frozenset[str]:
    known = set(default_executables())
    for value in (_recorded_executable(data_directory), _executable_of(command)):
        if value:
            known.add(value)
    return frozenset(known)


def _preflight() -> None:
    if getattr(sys, "frozen", False) and not _launcher_supports_hook():
        raise HookInstallError("blocked: this build's launcher cannot run the Claude hook yet "
                               "(--claude-hook unsupported); refusing to install")
    if find_git_bash() is None:
        raise HookInstallError("blocked: Git Bash not found; Claude Code needs it to run Branchglance hooks")


# --------------------------------------------------------------------- public

def install(settings_path: str | os.PathLike[str] | None = None, command: str | None = None,
            data_directory: str | os.PathLike[str] | None = None, now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    _preflight()
    path = Path(settings_path) if settings_path is not None else default_settings_path()
    canonical_path = path.resolve()
    command = command or default_command()
    if not _is_branchglance_command(command):
        raise HookInstallError("hook command must be a Branchglance invocation with a standalone marker argument")
    known = _known_executables(data_directory, command)
    desired = _group(command)
    changed = False

    def ensure_hooks(data, bom, original):
        nonlocal changed
        changed = False
        hooks = data.setdefault("hooks", {})
        for event in EVENTS:
            groups = hooks.setdefault(event, [])
            ours = [g for g in groups if is_ours(g, known)]
            if ours == [desired]:
                continue
            hooks[event] = [g for g in groups if not is_ours(g, known)] + [desired]
            changed = True
        return _serialize(data, bom, original) if changed else None

    backup = _write_transform_checked(path, ensure_hooks, now)
    try:
        _write_consent(data_directory, now, command, canonical_path)
    except OSError as exc:
        raise HookInstallError(f"hooks installed but the consent marker could not be written "
                               f"({exc.__class__.__name__}); the hook stays a no-op") from None
    return {"ok": True, "changed": changed, "backup": backup, "events": list(EVENTS),
            "writeSafety": WRITE_SAFETY}


def _hooks_left(path: Path, known: frozenset[str]) -> list[str] | None:
    """Events still carrying our groups or the marker; None when unreadable."""
    try:
        _raw, data, _bom = _read(path)
    except HookInstallError:
        return None
    hooks = data.get("hooks") if isinstance(data.get("hooks"), dict) else {}
    return sorted(event for event, groups in hooks.items()
                  if any(is_ours(g, known) or _touches_marker(g) for g in groups))


def uninstall(settings_path: str | os.PathLike[str] | None = None,
              data_directory: str | os.PathLike[str] | None = None, now: float | None = None,
              command: str | None = None) -> dict[str, Any]:
    """Consent goes first and unconditionally, so the hook is inert even if
    the settings edit then fails; any hooks left behind are reported."""
    now = time.time() if now is None else now
    marker_present = _consent_file(data_directory).exists()
    path = _recorded_settings_path(data_directory)
    if path is None and marker_present and not (os.environ.get("CLAUDE_HOME") or os.environ.get("CLAUDE_CONFIG_DIR")):
        # Consent written before the target was recorded: those installs used
        # ~/.claude unless CLAUDE_HOME was set, so with no override now that is
        # the only file they can have edited.
        path = Path(settings_path) if settings_path is not None else default_settings_path()
    known = _known_executables(data_directory, command)  # read before the marker is removed
    consent_removed = _remove_consent(data_directory)
    result: dict[str, Any] = {"ok": True, "changed": False, "backup": None, "consentRemoved": consent_removed,
                              "hooksLeft": [], "mixedGroupsLeft": [], "error": None, "writeSafety": WRITE_SAFETY}
    if path is None:
        if not marker_present:
            current = Path(settings_path) if settings_path is not None else default_settings_path()
            left = _hooks_left(current, known)
            if left == []:
                return result
        result.update(ok=False, hooksLeft="unknown",
                      error="hooks retained: the installed settings target has no verifiable provenance")
        return result
    try:
        changed = False
        left_mixed: list[str] = []

        def remove_owned(data, bom, original):
            nonlocal changed, left_mixed
            hooks = data.get("hooks") if isinstance(data.get("hooks"), dict) else {}
            changed, left_mixed = False, []
            for event in list(hooks):
                groups = hooks[event]
                kept = [g for g in groups if not is_ours(g, known)]
                left_mixed += [event for g in kept if _touches_marker(g)]
                if len(kept) != len(groups):
                    changed = True
                    if kept:
                        hooks[event] = kept
                    else:
                        del hooks[event]
            if changed and not hooks:
                data.pop("hooks", None)
            return _serialize(data, bom, original) if changed else None

        backup = _write_transform_checked(path, remove_owned, now)
    except HookInstallError as exc:
        left = _hooks_left(path, known)
        result.update(ok=False, error=str(exc), hooksLeft=left if left is not None else "unknown")
        return result
    result.update(changed=changed, backup=backup, mixedGroupsLeft=sorted(set(left_mixed)),
                  hooksLeft=sorted(set(left_mixed)))
    if not consent_removed:
        result.update(ok=False, error="the consent marker could not be removed")
    return result


def status(settings_path: str | os.PathLike[str] | None = None,
           data_directory: str | os.PathLike[str] | None = None,
           command: str | None = None) -> dict[str, Any]:
    path = Path(settings_path) if settings_path is not None else default_settings_path()
    note = "Managed (organization) policy cannot be detected; it may still disable hooks."
    result: dict[str, Any] = {"state": "missing", "installed": [], "missing": list(EVENTS),
                              "consent": _consent_file(data_directory).is_file(), "reason": None,
                              "gitBash": find_git_bash() is not None,
                              "managedPolicy": "not detectable", "note": note, "writeSafety": WRITE_SAFETY}
    known = _known_executables(data_directory, command)
    try:
        _raw, data, _bom = _read(path)
    except HookInstallError as exc:
        result.update(state="blocked", reason=str(exc))
        return result
    hooks = data.get("hooks") if isinstance(data.get("hooks"), dict) else {}
    installed = [event for event in EVENTS if any(is_ours(g, known) for g in hooks.get(event, []))]
    result["installed"] = installed
    result["missing"] = [event for event in EVENTS if event not in installed]
    if not result["gitBash"]:
        result.update(state="blocked", reason="blocked: Git Bash not found")
    elif getattr(sys, "frozen", False) and not _launcher_supports_hook():
        result.update(state="blocked", reason="blocked: this build's launcher cannot run the Claude hook yet")
    elif data.get("disableAllHooks") is True:
        result.update(state="blocked", reason="disableAllHooks is true in user settings")
    elif data.get("allowManagedHooksOnly") is True:
        result.update(state="blocked", reason="allowManagedHooksOnly is true")
    elif len(installed) == len(EVENTS):
        result["state"] = "installed" if result["consent"] else "partial"
        if not result["consent"]:
            result["reason"] = "hooks are installed but the consent marker is missing"
    elif installed:
        result.update(state="partial", reason="some events are not installed")
    return result


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    action = args[0] if args else "status"
    try:
        if action == "install":
            result = install()
        elif action == "uninstall":
            result = uninstall()
        elif action == "status":
            result = status()
        else:
            print("usage: claude_hook_install [install|uninstall|status]", file=sys.stderr)
            return 2
    except HookInstallError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        return 1
    print(json.dumps(result, indent=2))
    return 0 if result.get("ok", True) else 1


if __name__ == "__main__":
    raise SystemExit(main())
