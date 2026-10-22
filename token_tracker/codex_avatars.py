"""Subagent avatars exactly as the Codex desktop app draws them.

Codex picks one of a fixed list of SVG avatars per subagent thread:
``avatars[hash(thread_id) % len(avatars)]`` with a Java-style string hash.
The artwork belongs to the locally installed Codex app, so it is read from that
install at runtime (and cached by app version) rather than shipped here.
Missing app or an unrecognised bundle layout yields an empty list, and the UI
falls back to its own monochrome mark.
"""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import threading
from pathlib import Path

HASH_MODULUS = 2147483647
_ANCHOR = re.compile(rb"=\[\{dark:([A-Za-z_$][\w$]*),light:([A-Za-z_$][\w$]*)\}(?:,\{dark:[A-Za-z_$][\w$]*,light:[A-Za-z_$][\w$]*\}){7,}\]")
_PAIR = re.compile(rb"\{dark:([A-Za-z_$][\w$]*),light:([A-Za-z_$][\w$]*)\}")
_LOCK = threading.Lock()
_MEMO: dict[str, list[str]] = {}


def avatar_index(seed: str, count: int) -> int:
    """Same arithmetic as Codex: t = (t*31 + code) mod 2^31-1, then mod count."""
    value = 0
    for char in seed:
        value = (value * 31 + ord(char)) % HASH_MODULUS
    return value % count if count else 0


def _codex_asar() -> Path | None:
    """WindowsApps cannot be listed, so ask Windows for the MSIX install location."""
    if os.name != "nt":
        return None
    try:
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        location = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", "(Get-AppxPackage -Name OpenAI.Codex).InstallLocation"],
            capture_output=True, text=True, timeout=20, creationflags=flags,
        ).stdout.strip().splitlines()
    except (OSError, subprocess.SubprocessError):
        return None
    for line in reversed(location):
        asar = Path(line.strip()) / "app" / "resources" / "app.asar"
        if asar.is_file():
            return asar
    return None


def _asar_files(data: bytes) -> tuple[dict, int]:
    """ASAR index: a pickled JSON header, then file bodies addressed by offset."""
    header_size = int.from_bytes(data[4:8], "little")
    json_size = int.from_bytes(data[12:16], "little")
    return json.loads(data[16:16 + json_size]), 8 + header_size


def _find_file(node: dict, name: str) -> dict | None:
    for key, child in (node.get("files") or {}).items():
        if key == name and "offset" in child:
            return child
        if "files" in child:
            found = _find_file(child, name)
            if found:
                return found
    return None


def _extract(asar: Path) -> list[str]:
    """Dark-theme avatar SVGs (as data URIs) in Codex's own order."""
    data = asar.read_bytes()
    anchor = _ANCHOR.search(data)
    if not anchor:
        return []
    pairs = _PAIR.findall(anchor.group(0))
    # Each avatar module sits just before the table and is either an asset reference
    # `NAME=``+new URL(`bloom-dark-hash.svg`,import.meta.url).href` or, for small SVGs the
    # bundler inlined, `NAME=`data:image/svg+xml,...``. Minified names repeat across the bundle,
    # so only definitions within this window count and the nearest one wins.
    lo = max(0, anchor.start() - 300_000)
    window = data[lo:anchor.start()]
    found: dict[bytes, tuple[str, str]] = {}
    for match in re.finditer(rb"([A-Za-z_$][\w$]*)=``\+new URL\(`([\w.-]+\.svg)`", window):
        found[match.group(1)] = ("file", match.group(2).decode())
    for match in re.finditer(rb"([A-Za-z_$][\w$]*)=`(data:image/svg\+xml,[^`]{40,300000})`", window):
        found[match.group(1)] = ("uri", match.group(2).decode("utf-8", "replace"))
    index, base = _asar_files(data)
    avatars = []
    for dark, _light in pairs:
        kind, value = found.get(dark, ("", ""))
        if kind == "uri":
            avatars.append(value)
            continue
        entry = _find_file(index, value) if kind == "file" else None
        if not entry:
            return []
        start = base + int(entry["offset"])
        svg = data[start:start + int(entry["size"])]
        avatars.append("data:image/svg+xml;base64," + base64.b64encode(svg).decode())
    return avatars


def codex_avatars(cache_dir: Path) -> list[str]:
    """Cached by the installed app's folder name (its version)."""
    asar = _codex_asar()
    if asar is None:
        return []
    version = asar.parent.parent.parent.name
    with _LOCK:
        if version in _MEMO:
            return _MEMO[version]
        cache = cache_dir / "codex-avatars.json"
        try:
            cached = json.loads(cache.read_text(encoding="utf-8"))
            if cached.get("version") == version and isinstance(cached.get("avatars"), list):
                _MEMO[version] = cached["avatars"]
                return _MEMO[version]
        except (OSError, ValueError):
            pass
        try:
            avatars = _extract(asar)
        except OSError:
            avatars = []
        if avatars:
            try:
                cache_dir.mkdir(parents=True, exist_ok=True)
                cache.write_text(json.dumps({"version": version, "avatars": avatars}), encoding="utf-8")
            except OSError:
                pass
        _MEMO[version] = avatars
        return avatars
