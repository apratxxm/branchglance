"""User-facing enable/disable flow for local Claude Code collection."""

from __future__ import annotations

import copy
import ctypes
import json
import os
import secrets
import stat
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from . import claude_hook_install, product, provider_policy

_LOCK = threading.RLock()
_MAX_POLICY_BYTES = 64 * 1024


class ClaudeCollectionError(Exception):
    """A safe, user-displayable collection setting error."""


def _same_file(info: os.stat_result, other: os.stat_result) -> bool:
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns) == (
        other.st_dev, other.st_ino, other.st_size, other.st_mtime_ns, other.st_ctime_ns)


def _info_is_reparse(info: os.stat_result) -> bool:
    return (stat.S_ISLNK(info.st_mode)
            or bool(getattr(info, "st_file_attributes", 0)
                    & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)))


def _validate_policy_path(path: Path) -> None:
    """Check app storage and every path component before file operations."""
    try:
        product.validate_storage()
        claude_hook_install._validate_path_chain(path)
    except (product.StorageRedirectedError, claude_hook_install.HookInstallError, OSError) as exc:
        raise ClaudeCollectionError("Policy file path is redirected or unavailable.") from exc


def _read_policy_bytes(path: Path) -> bytes | None:
    _validate_policy_path(path)
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ClaudeCollectionError("Policy file could not be inspected.") from exc
    if _info_is_reparse(before) or not stat.S_ISREG(before.st_mode):
        raise ClaudeCollectionError("Policy file is redirected or is not a regular file.")
    if before.st_size > _MAX_POLICY_BYTES:
        raise ClaudeCollectionError("Policy file is too large to edit safely.")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        raise ClaudeCollectionError("Policy file could not be opened safely.") from exc
    try:
        opened = os.fstat(fd)
        if _info_is_reparse(opened) or not stat.S_ISREG(opened.st_mode) or not _same_file(before, opened):
            raise ClaudeCollectionError("Policy file changed while being opened.")
        with os.fdopen(fd, "rb", closefd=False) as stream:
            raw = stream.read(_MAX_POLICY_BYTES + 1)
        if len(raw) > _MAX_POLICY_BYTES:
            raise ClaudeCollectionError("Policy file is too large to edit safely.")
    except OSError as exc:
        raise ClaudeCollectionError("Policy file could not be read safely.") from exc
    finally:
        os.close(fd)
    _validate_policy_path(path)
    try:
        after = path.lstat()
    except OSError as exc:
        raise ClaudeCollectionError("Policy file changed while being read.") from exc
    if _info_is_reparse(after) or not _same_file(opened, after):
        raise ClaudeCollectionError("Policy file changed while being read.")
    return raw


def _read_policy(path: Path) -> tuple[dict[str, Any], bool, bool, bytes | None]:
    """Read a small JSON object without following a reparse-point policy file."""
    raw = _read_policy_bytes(path)
    if raw is None:
        return {"schemaVersion": 1, "providers": {}}, False, False, None
    try:
        document = json.loads(raw.decode("utf-8-sig"))
    except (OSError, ValueError, UnicodeError, RecursionError) as exc:
        raise ClaudeCollectionError("Policy file is malformed and was left unchanged.") from exc
    if (not isinstance(document, dict) or document.get("schemaVersion") != 1
            or type(document.get("schemaVersion")) is not int):
        raise ClaudeCollectionError("Policy file is malformed and was left unchanged.")
    providers = document.get("providers")
    if not isinstance(providers, dict):
        raise ClaudeCollectionError("Policy file is malformed and was left unchanged.")
    for name, flags in providers.items():
        if not isinstance(name, str) or not isinstance(flags, dict):
            raise ClaudeCollectionError("Policy file is malformed and was left unchanged.")
        for key in ("collect", "show", "send"):
            if key in flags and type(flags[key]) is not bool:
                raise ClaudeCollectionError("Policy file is malformed and was left unchanged.")
        if (flags.get("show") or flags.get("send")) and not flags.get("collect"):
            raise ClaudeCollectionError("Policy file is malformed and was left unchanged.")
    for key in ("origin", "writtenAt"):
        if key in document and not isinstance(document[key], str):
            raise ClaudeCollectionError("Policy file is malformed and was left unchanged.")
    # The startup reader's strict schema is the contract: never edit a file it would ignore.
    if provider_policy.read_policy_file(path, claude_consent=True).status != "ok":
        raise ClaudeCollectionError("Policy file has entries this version cannot read and was left unchanged.")
    try:
        provider_policy._validate_policy_document(document)
    except provider_policy.ProviderPolicyError as exc:
        raise ClaudeCollectionError("Policy file is malformed and was left unchanged.") from exc
    return document, True, raw.startswith(b"\xef\xbb\xbf"), raw


@contextmanager
def _installer_update_lock():
    """Try-own the same secured Install mutex as the application installer."""
    if os.name != "nt":
        yield
        return
    try:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        kernel.WaitForSingleObject.restype = ctypes.c_ulong
        kernel.ReleaseMutex.argtypes = [ctypes.c_void_p]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = product.create_secured_mutex(product.mutex_name("Install"))
    except Exception as exc:
        raise ClaudeCollectionError("Branchglance is being updated; try again.") from exc
    result = kernel.WaitForSingleObject(handle, 0)
    if result not in (0x0, 0x80):  # WAIT_OBJECT_0 or WAIT_ABANDONED
        kernel.CloseHandle(handle)
        raise ClaudeCollectionError("Branchglance is being updated; try again.")
    try:
        yield
    finally:
        kernel.ReleaseMutex(handle)
        kernel.CloseHandle(handle)


def _exclusive_temp(path: Path, payload: bytes) -> Path:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    for _ in range(32):
        candidate = path.with_name(f".{path.name}.{secrets.token_hex(12)}.tmp")
        try:
            fd = os.open(candidate, flags, 0o600)
        except FileExistsError:
            continue
        try:
            view = memoryview(payload)
            while view:
                n = os.write(fd, view)
                if n <= 0:
                    raise OSError("short policy write")
                view = view[n:]
            os.fsync(fd)
        except BaseException:
            os.close(fd)
            candidate.unlink(missing_ok=True)
            raise
        os.close(fd)
        return candidate
    raise ClaudeCollectionError("Could not create a unique policy temp file.")


def _validate_policy_bytes(data: bytes) -> None:
    if len(data) > provider_policy._FILE_MAX_BYTES:
        raise ClaudeCollectionError("Updated policy would exceed the supported size limit.")
    try:
        document = json.loads(data.decode("utf-8-sig"))
        provider_policy._validate_policy_document(document)
    except (ValueError, UnicodeError, RecursionError, provider_policy.ProviderPolicyError) as exc:
        raise ClaudeCollectionError("Updated policy is not readable by this version.") from exc


def _write_policy(path: Path, enable: bool) -> bool:
    with _LOCK:
        with _installer_update_lock():
            _validate_policy_path(path)
            document, existed, bom, original = _read_policy(path)
            providers = document["providers"]
            claude = providers.get("Claude")
            if not isinstance(claude, dict):
                claude = {}
            updated = copy.deepcopy(document)
            updated_providers = updated["providers"]
            updated_claude = dict(claude)
            before = (updated_claude.get("collect"), updated_claude.get("show"), updated_claude.get("send"))
            if enable:
                updated_claude.update(collect=True, show=True)
            else:
                updated_claude.pop("collect", None)
                updated_claude.pop("show", None)
                updated_claude.pop("send", None)
            after = (updated_claude.get("collect"), updated_claude.get("show"), updated_claude.get("send"))
            if updated_claude:
                updated_providers["Claude"] = updated_claude
            else:
                updated_providers.pop("Claude", None)
            if before == after and existed:
                return False

            data = ((b"\xef\xbb\xbf" if bom else b"")
                    + (json.dumps(updated, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
            _validate_policy_bytes(data)
            _validate_policy_path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            _validate_policy_path(path)
            temp = _exclusive_temp(path, data)
            try:
                # Compare the exact bytes immediately before publication so a
                # concurrent writer's provider grant is never overwritten.
                _validate_policy_path(path)
                current = _read_policy_bytes(path)
                if current != original:
                    raise ClaudeCollectionError("Policy file changed while editing; no policy change was made.")
                os.replace(temp, path)
            finally:
                try:
                    temp.unlink()
                except OSError:
                    pass
            return True


def _consent_present() -> bool:
    try:
        return product.claude_consent_marker_path().is_file()
    except OSError:
        return False


def _runtime_restart_required(path: Path, consent: bool) -> tuple[bool, Any]:
    """Policies are immutable after import; generation changes only on restart."""
    try:
        current = provider_policy.public_policies(provider_policy.POLICIES)["generation"]
        candidate = provider_policy.load_policies_resilient(policy_path=path, claude_consent=consent)
        expected = provider_policy.public_policies(candidate)["generation"]
        effective = provider_policy.policy_for("Claude", candidate)
        return current != expected, effective
    except Exception:
        return True, None


def status() -> dict[str, Any]:
    with _LOCK:
        path = product.policy_file_path()
        consent = _consent_present()
        hooks_raw: dict[str, Any]
        try:
            hooks_raw = claude_hook_install.status()
        except Exception:
            hooks_raw = {"state": "blocked", "installed": []}
        raw_hook_state = hooks_raw.get("state")
        hook_state = (raw_hook_state if isinstance(raw_hook_state, str)
                      and raw_hook_state in {"blocked", "partial", "installed", "missing"} else "unavailable")
        installed_count = len(hooks_raw.get("installed", [])) if isinstance(hooks_raw.get("installed"), list) else 0
        hooks = {"state": hook_state, "installed": installed_count}
        grant = False
        reasons = {
            "blocked": "Claude Code hooks are blocked by settings or build requirements.",
            "partial": "Some Claude Code hooks are missing or could not be removed.",
            "unavailable": "Claude Code hook status is unavailable.",
        }
        reason = reasons.get(hook_state)
        if path is None:
            return {"state": "unavailable", "reason": "development build has no policy file",
                    "hooks": hooks, "consent": consent, "grant": False, "restartRequired": False}
        try:
            document, _exists, _bom, _raw = _read_policy(path)
            flags = document["providers"].get("Claude", {})
            grant = flags.get("collect") is True and flags.get("show") is True
        except ClaudeCollectionError as exc:
            return {"state": "blocked", "reason": str(exc), "hooks": hooks, "consent": consent,
                    "grant": False, "restartRequired": False}
        restart_required, effective = _runtime_restart_required(path, consent)
        vetoed = bool(grant and consent and effective is not None
                      and not (effective.collect and effective.show))
        if hook_state == "blocked":
            state = "blocked"
            reason = "Claude Code hooks are blocked by settings or build requirements."
        elif vetoed:
            state, reason, restart_required = "vetoed", "turned off by an environment setting", False
        elif hook_state == "installed" and consent and grant and effective is not None and effective.collect and effective.show:
            state, reason = "on", None
        elif hook_state == "missing" and not consent and not grant and effective is not None and not effective.collect:
            state, reason = "off", None
        else:
            state = "partial"
            reason = reason or "Claude Code hooks, consent, and policy grant are not all enabled."
        return {"state": state, "reason": reason, "hooks": hooks, "consent": consent,
                "grant": grant, "restartRequired": restart_required}


def enable() -> dict[str, Any]:
    with _LOCK:
        path = product.policy_file_path()
        if path is None:
            return {**status(), "ok": False, "reason": "development build has no policy file"}
        try:
            with _installer_update_lock():
                _read_policy(path)  # refuse before touching Claude Code settings
                try:
                    result = claude_hook_install.install()
                    if isinstance(result, dict) and result.get("ok") is False:
                        raise ClaudeCollectionError("Claude Code hook installation failed.")
                except Exception:
                    current = status()
                    return {**current, "ok": False, "reason": "Claude Code hooks could not be installed."}
                try:
                    _write_policy(path, True)
                except Exception:
                    rollback_ok = False
                    try:
                        result = claude_hook_install.uninstall()
                        rollback_ok = result.get("ok") is True and not result.get("hooksLeft")
                    except Exception:
                        pass
                    current = status()
                    return {**current, "ok": False,
                            "reason": ("Policy grant could not be saved; hooks were rolled back." if rollback_ok
                                       else "Policy grant could not be saved; hook rollback is incomplete.")}
        except ClaudeCollectionError as exc:
            if str(exc) == "Branchglance is being updated; try again.":
                return {**status(), "ok": False, "reason": str(exc)}
            return {**status(), "ok": False, "reason": str(exc)}
        except Exception:
            return {**status(), "ok": False, "reason": "Claude Code collection could not be enabled."}
        return {**status(), "ok": True}


def disable() -> dict[str, Any]:
    with _LOCK:
        path = product.policy_file_path()
        if path is None:
            return {**status(), "ok": False, "reason": "development build has no policy file"}
        try:
            with _installer_update_lock():
                uninstall_error: str | None = None
                try:
                    result = claude_hook_install.uninstall()
                    if result.get("ok") is not True:
                        uninstall_error = "Claude Code hooks could not all be removed."
                except Exception:
                    uninstall_error = "Claude Code hooks could not all be removed."
                grant_error: str | None = None
                try:
                    _write_policy(path, False)
                except Exception:
                    grant_error = "Claude policy grant could not be removed."
                current = status()
                if uninstall_error or grant_error:
                    return {**current, "ok": False, "state": "partial",
                            "reason": "; ".join(x for x in (uninstall_error, grant_error) if x)}
        except ClaudeCollectionError as exc:
            if str(exc) == "Branchglance is being updated; try again.":
                return {**status(), "ok": False, "reason": str(exc)}
            return {**status(), "ok": False, "reason": "Claude Code settings could not be disabled safely."}
        except Exception:
            return {**status(), "ok": False, "reason": "Claude Code collection could not be disabled."}
        return {**status(), "ok": True}
