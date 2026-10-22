"""Read account-scoped quota deadlines from Claude Desktop's local HTTP cache.

No requests or credentials are used. Chromium may evict or rewrite this cache
at any time; missing, inconsistent and expired records must stay unknown.
"""
from __future__ import annotations

import io
import json
import math
import os
import re
import struct
import time
import zlib
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

HEADER_SIZE = 8192
MAX_BODY = 262144
MAX_SCAN = 32 * 1024 * 1024
BLOCK_SIZES = {2: 256, 3: 1024, 4: 4096}
MAX_USAGE_AGE_SECONDS = 90


def _read(path: Path, offset: int, size: int) -> bytes:
    if not 0 < size <= MAX_SCAN or offset < 0:
        return b""
    try:
        if os.name != "nt":
            with path.open("rb") as handle:
                handle.seek(offset)
                return handle.read(size)
        # Electron holds cache files open. Explicit sharing avoids taking locks
        # and allows it to replace/delete the file while this reader is active.
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
                                      ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.SetFilePointerEx.argtypes = [wintypes.HANDLE, ctypes.c_longlong, ctypes.c_void_p, wintypes.DWORD]
        kernel.SetFilePointerEx.restype = wintypes.BOOL
        kernel.ReadFile.argtypes = [wintypes.HANDLE, ctypes.c_void_p, wintypes.DWORD,
                                   ctypes.POINTER(wintypes.DWORD), ctypes.c_void_p]
        kernel.ReadFile.restype = wintypes.BOOL
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.CloseHandle.restype = wintypes.BOOL
        handle = kernel.CreateFileW(str(path), 0x80000000, 7, None, 3, 0, None)
        if handle in (None, wintypes.HANDLE(-1).value):
            return b""
        try:
            if not kernel.SetFilePointerEx(handle, offset, None, 0):
                return b""
            buffer = ctypes.create_string_buffer(size)
            count = wintypes.DWORD()
            if not kernel.ReadFile(handle, buffer, size, ctypes.byref(count), None):
                return b""
            return buffer.raw[:count.value]
        finally:
            kernel.CloseHandle(handle)
    except OSError:
        return b""


def _stream(cache: Path, address: int, size: int) -> bytes:
    if not address & 0x80000000 or not 0 < size <= MAX_BODY:
        return b""
    kind = (address >> 28) & 7
    if kind == 0:
        data = _read(cache / f"f_{address & 0x0fffffff:06x}", 0, size)
    elif kind in BLOCK_SIZES:
        capacity = (((address >> 24) & 3) + 1) * BLOCK_SIZES[kind]
        if size > capacity:
            return b""
        data = _read(cache / f"data_{(address >> 16) & 255}",
                     HEADER_SIZE + (address & 65535) * BLOCK_SIZES[kind], size)
    else:
        return b""
    return data if len(data) == size else b""


def _body(raw: bytes, encoding: bytes) -> dict[str, Any] | None:
    try:
        if encoding == b"zstd" or raw.startswith(b"\x28\xb5\x2f\xfd"):
            import zstandard
            with zstandard.ZstdDecompressor(max_window_size=8 * 1024 * 1024).stream_reader(io.BytesIO(raw)) as reader:
                raw = reader.read(MAX_BODY + 1)
        elif encoding in (b"gzip", b"deflate"):
            decoder = zlib.decompressobj(31 if encoding == b"gzip" else 15)
            raw = decoder.decompress(raw, MAX_BODY + 1)
            if not decoder.eof:
                return None
        elif encoding not in (b"", b"identity"):
            return None
        if len(raw) > MAX_BODY:
            return None
        value = json.loads(raw)
        return value if isinstance(value, dict) else None
    except (ImportError, ValueError, OSError, zlib.error):
        return None
    except Exception:
        # zstandard's optional native extension has its own exception type.
        return None


def _number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def usage_info(root: Path, samples: list[dict[str, Any]], now: float | None = None) -> dict[str, Any]:
    """Return usage and deadlines only from a recent authenticated Desktop response.

    ``plan-usage-history.json`` is a convenient historical cache but may stop
    updating when Claude Desktop is closed. This reads the timestamped response
    cache directly and leaves quota percentages unknown once it is stale.
    """
    result: dict[str, Any] = {
        "fiveHour": None, "weekly": None, "observedAt": None,
        "fiveHourPercent": None, "weeklyPercent": None,
    }
    now = time.time() if now is None else now
    org = samples[-1].get("org") if samples else None
    if not isinstance(org, str) or not re.fullmatch(r"[0-9a-fA-F-]{36}", org):
        return result
    cache = root / "Cache" / "Cache_Data"
    endpoint = ("https://claude.ai/api/organizations/" + org + "/usage").encode()
    key_pattern = re.compile(rb"(?:[0-9/ ]{0,64})" + re.escape(endpoint) + rb"(?:\?[A-Za-z0-9_=&.%-]*)?")
    try:
        files = sorted(cache.glob("data_*"), key=lambda p: p.stat().st_mtime, reverse=True)[:16]
    except OSError:
        return result
    records = []
    budget = MAX_SCAN
    for path in files:
        header = _read(path, 0, 24)
        # Chromium block-file header: magic, version, file id, next id, block size.
        if len(header) < 24 or struct.unpack_from("<I", header)[0] != 0xC104CAC3:
            continue
        if struct.unpack_from("<i", header, 12)[0] != 256:
            continue
        data = _read(path, 0, budget)
        budget -= len(data)
        for match in re.finditer(re.escape(endpoint), data):
            block = HEADER_SIZE + (match.start() - HEADER_SIZE) // 256 * 256
            for base in (block, block - 256, block - 512, block - 768):
                if base < HEADER_SIZE or base + 96 > len(data):
                    continue
                key_len, long_key = struct.unpack_from("<iI", data, base + 32)
                if not 0 < key_len <= 928 or long_key or struct.unpack_from("<i", data, base + 20)[0] != 0:
                    continue
                key = data[base + 96:base + 96 + key_len]
                if not key_pattern.fullmatch(key):
                    continue
                sizes = struct.unpack_from("<4i", data, base + 40)
                addresses = struct.unpack_from("<4I", data, base + 56)
                headers, body = [_stream(cache, a, s) for a, s in zip(addresses[:2], sizes[:2])]
                if not re.search(rb"HTTP/[\d.]+ 200(?: |\x00)", headers) or not body:
                    break
                date = re.search(rb"\x00date:\s*([^\x00]{10,50})\x00", headers, re.I)
                encoding = re.search(rb"\x00content-encoding:\s*([^\x00]{1,20})\x00", headers, re.I)
                try:
                    observed = parsedate_to_datetime(date.group(1).decode("ascii")).timestamp() if date else None
                except (ValueError, UnicodeError, TypeError, OverflowError):
                    observed = None
                value = _body(body, encoding.group(1).strip().lower() if encoding else b"")
                if observed is not None and observed <= now + 90 and value is not None:
                    records.append((observed, value))
                break
        if budget <= 0:
            break
    if not records:
        return result
    observed, value = max(records, key=lambda record: record[0])
    result["observedAt"] = observed
    for name, field, usage_key, duration in (("fiveHour", "five_hour", "fh", 18000),
                                             ("weekly", "seven_day", "sd", 604800)):
        window = value.get(field)
        if not isinstance(window, dict):
            continue
        utilization = window.get("utilization")
        if _number(utilization) and 0 <= utilization <= 100 and now - observed <= MAX_USAGE_AGE_SECONDS:
            invalid_usage = False
            previous_usage = utilization
            for sample in sorted(samples, key=lambda s: s.get("t", 0)):
                if sample.get("org") != org or not _number(sample.get("t")):
                    continue
                at = sample["t"] / 1000
                used = (sample.get("u") or {}).get(usage_key)
                if not _number(used) or at < observed - 90:
                    continue
                if abs(at - observed) <= 90 and abs(used - utilization) > 2:
                    invalid_usage = True
                if at > observed + 90:
                    if used < previous_usage - 2:
                        invalid_usage = True
                    previous_usage = used
            if not invalid_usage:
                result[name + "Percent"] = utilization

        if not isinstance(window.get("resets_at"), str):
            continue
        try:
            date = datetime.fromisoformat(window["resets_at"].replace("Z", "+00:00"))
            if date.tzinfo is None:
                continue
            reset = math.ceil(date.timestamp())
        except (ValueError, OverflowError, OSError):
            continue
        if not observed < reset <= observed + duration + 300:
            continue
        if not _number(utilization) or not 0 <= utilization <= 100:
            continue
        if reset <= now:
            # This is an old scheduled deadline, not evidence that quota reset.
            # Keep it separately so the UI can explain a stale source precisely.
            result["lastKnown" + name[0].upper() + name[1:]] = reset
            continue
        if now - observed > duration:
            continue
        invalid = False
        previous = utilization
        for sample in sorted(samples, key=lambda s: s.get("t", 0)):
            if sample.get("org") != org or not _number(sample.get("t")):
                continue
            at, used = sample["t"] / 1000, (sample.get("u") or {}).get(usage_key)
            if not _number(used) or at < observed - 90:
                continue
            if abs(at - observed) <= 90 and abs(used - utilization) > 2:
                invalid = True
            if at > observed + 90:
                if used < previous - 2:
                    invalid = True
                previous = used
        if not invalid:
            result[name] = reset
    return result


def reset_info(root: Path, samples: list[dict[str, Any]], now: float | None = None) -> dict[str, Any]:
    """Compatibility view containing only scheduled reset deadline metadata."""
    result = usage_info(root, samples, now)
    return {key: value for key, value in result.items() if key in {"fiveHour", "weekly", "observedAt", "lastKnownFiveHour", "lastKnownWeekly"}}
