from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

SCHEMA = 1
# Dot-separated identifiers only: no empty segment, so ".." and path separators never match.
VERSION_RE = re.compile(r"\A\d+\.\d+\.\d+(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?\+[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*\Z")
OWNER_FILE = ".branchglance-owner.json"
OWNED_LEDGER = "install-owned.json"
MANIFEST = "release-manifest.json"
STAGE_MARKER = ".branchglance-stage-owner.json"
JOURNAL = "install-journal.json"
POINTER = "current.json"
LAUNCHER = "Branchglance.exe"
UNINSTALLER = "BranchglanceUninstall.exe"
TOOLS = (LAUNCHER, UNINSTALLER)
# FILETIME (100 ns ticks since 1601) of the Unix epoch; the launcher compares
# FileInfo.LastWriteTimeUtc.ToFileTimeUtc() against these stamps.
_EPOCH_AS_FILETIME = 116444736000000000


class SafetyError(RuntimeError):
    pass


class FileSystem(Protocol):
    def exists(self, path: Path) -> bool: ...
    def replace(self, source: Path, target: Path, *, mutation: str = "rename",
                after: Callable[[str], None] | None = None) -> None: ...
    def remove_tree(self, path: Path) -> None: ...


class LocalFileSystem:
    def exists(self, path: Path) -> bool:
        return path.exists()

    def replace(self, source: Path, target: Path, *, mutation: str = "rename",
                after: Callable[[str], None] | None = None) -> None:
        os.replace(source, target)
        if after is not None:
            after(mutation)

    def remove_tree(self, path: Path) -> None:
        remove_plain_tree(path)


def atomic_json(path: Path, value: Any, *, replace=os.replace) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    try:
        with temp.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        replace(temp, path)
    finally:
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SafetyError(f"Cannot read valid JSON at {path}") from exc
    if not isinstance(value, dict):
        raise SafetyError(f"Expected a JSON object at {path}")
    return value


def is_reparse(path: Path) -> bool:
    try:
        info = os.lstat(path)
    except OSError:
        return True
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & 0x400)


def _walk_plain(root: Path):
    if is_reparse(root):
        raise SafetyError(f"Reparse point refused: {root}")
    for directory, dirs, files in os.walk(root, topdown=True, followlinks=False):
        base = Path(directory)
        dirs.sort()
        files.sort()
        for name in list(dirs):
            path = base / name
            if is_reparse(path):
                raise SafetyError(f"Reparse point refused: {path}")
        for name in files:
            path = base / name
            if is_reparse(path):
                raise SafetyError(f"Reparse point refused: {path}")
            yield path


def remove_plain_tree(path: Path) -> None:
    """Delete only ordinary descendants; a reparse point anywhere aborts deletion."""
    if not path.exists():
        return
    files = list(_walk_plain(path))
    for item in files:
        item.unlink()
    for directory, dirs, _ in os.walk(path, topdown=False, followlinks=False):
        for name in dirs:
            (Path(directory) / name).rmdir()
    path.rmdir()


def _remove_known_file(path: Path) -> bool:
    if not os.path.lexists(path):
        return False
    if is_reparse(path) or not path.is_file():
        raise SafetyError(f"Unsafe owned install file: {path}")
    path.unlink()
    return True


def _finish_uninstall(root: Path, record: dict, journal: Journal, fs: FileSystem,
                      after_mutation: Callable[[str], None]) -> None:
    allowed = {JOURNAL, OWNER_FILE, OWNED_LEDGER}
    remaining = [item for item in root.iterdir() if item.name not in allowed]
    if remaining:
        raise SafetyError("Uninstall kept unowned items in the install root: " + ", ".join(p.name for p in remaining))
    if record.get("phase") != "finalizing":
        record["phase"] = "finalizing"
        journal.save(record)
        after_mutation("uninstall-finalizing")
    ledger_path = root / OWNED_LEDGER
    if ledger_path.exists():
        ledger = read_owned_ledger(root)
        if any(ledger.get(name) for name in ("releases", "trash", "tools", "files", "directories")):
            raise SafetyError("Uninstall finalization found owned items still recorded")
        _remove_known_file(ledger_path)
        after_mutation("ledger-deleted")
    marker = root / OWNER_FILE
    if marker.exists():
        require_owned_root(root)
        _remove_known_file(marker)
        after_mutation("owner-marker-deleted")
    journal.path.unlink(missing_ok=True)
    after_mutation("journal-cleared")
    if root.exists() and not any(root.iterdir()):
        root.rmdir()
        after_mutation("root-removed")


def uninstall_preflight(root: Path, *, verify_releases: bool = False):
    """Read-only checks that decide whether uninstall_owned_root may proceed.

    Returns None when the root is absent, else (journal record, owner marker
    present, resuming an uninstall, ledger or None). Raises SafetyError exactly
    where uninstall would refuse, so callers can check before external changes.
    """
    root = Path(root).absolute()
    if not os.path.lexists(root):
        return None
    if is_reparse(root) or not root.is_dir():
        raise SafetyError("Install root is absent or is a reparse point")
    record = Journal(root).load()
    owner_exists = os.path.lexists(root / OWNER_FILE)
    resuming = record is not None and record.get("operation") == "uninstall"
    if resuming:
        if record.get("target") != "uninstall" or record.get("phase") not in {"staging", "switching", "finalizing", "committed"}:
            raise SafetyError("Uninstall journal is corrupt or unsupported")
        if owner_exists:
            require_owned_root(root)
            ledger = read_owned_ledger(root, allow_missing=record.get("phase") == "finalizing")
        else:
            ledger = None
    else:
        require_owned_root(root)
        if record is not None and record.get("phase") not in {"committed", "rolled-back"}:
            raise SafetyError("Unfinished installer transaction prevents uninstall")
        ledger = read_owned_ledger(root)
    # A resumed trash intent is reconciled by uninstall itself before verification.
    if verify_releases and ledger is not None and not (resuming and record.get("trashIntent")):
        for version in ledger["releases"]:
            path = safe_child(root, f"releases/{version}")
            if path.exists():
                try:
                    verify_manifest(path, expected_version=version)
                except SafetyError as exc:
                    raise SafetyError(f"Owned release {version} is unverifiable and was retained") from exc
    return record, owner_exists, resuming, ledger


def uninstall_owned_root(root: Path, *, integration_remove: Callable[[], None] = lambda: None,
                        after_mutation: Callable[[str], None] = lambda _name: None,
                        integration_snapshot: Callable[[], dict] = lambda: {},
                        fs: FileSystem | None = None) -> None:
    """Remove only the owned program tree; leave sibling state and unknown root content intact.

    Only release versions and tool names recorded in install-owned.json are
    removed. Foreign/unverifiable content is retained and leaves the root.
    """
    root = Path(root).absolute()
    state = uninstall_preflight(root)
    if state is None:
        return  # repeated uninstall after successful removal
    record, owner_exists, resuming, ledger = state
    fs = fs or LocalFileSystem()
    journal = root / JOURNAL
    journal_store = Journal(root, replace=fs.replace)
    if not resuming:
        if record is not None:
            journal.unlink()
            after_mutation("install-journal-removed")
        ledger = read_owned_ledger(root)
        record = journal_store.begin("uninstall", "uninstall", {"pointer": None,
                                                                     "integration": integration_snapshot()})
        record["phase"] = "switching"
        journal_store.save(record)
        after_mutation("uninstall-journaled")
    integration_remove()
    after_mutation("integrations-removed")
    if resuming and record.get("phase") == "finalizing":
        _finish_uninstall(root, record, journal_store, fs, after_mutation)
        return
    if resuming and record.get("trashIntent"):
        ReleaseOps(root, fs=fs, after_mutation=after_mutation)._reconcile_trash_intent(record)
        ledger = read_owned_ledger(root, allow_missing=not owner_exists)
    if ledger is not None:
        for version in list(ledger["releases"]):
            path = safe_child(root, f"releases/{version}")
            if not path.exists():
                ledger["releases"].remove(version)
                write_owned_ledger(root, ledger, replace=fs.replace)
                after_mutation("ledger-release-cleared")
                continue
            try:
                verify_manifest(path, expected_version=version)
            except SafetyError as exc:
                raise SafetyError(f"Owned release {version} is unverifiable and was retained") from exc
            ops = ReleaseOps(root, fs=fs, after_mutation=after_mutation)
            pending = next((item for item in record.get("trashEntries", [])
                            if item.get("version") == version
                            and not safe_child(root, f"releases/{item['name']}").exists()), None)
            if pending:
                entry = pending
                if entry.get("files") != trash_hash_snapshot(path, version):
                    raise SafetyError("Reserved uninstall trash source changed")
            else:
                index = len(record.get("trashEntries", []))
                name = f".trash-{record['id']}-{index}"
                entry = {"name": name, "version": version, "files": trash_hash_snapshot(path, version)}
                ops._reserve_trash(entry, ledger, record)
            after_mutation("uninstall-trash-journaled")
            trash_path = safe_child(root, f"releases/{entry['name']}")
            if path.exists() and not trash_path.exists():
                fs.replace(path, trash_path, mutation="uninstall-release-trashed", after=after_mutation)
            elif path.exists() and trash_path.exists():
                raise SafetyError("Uninstall source and reserved trash both exist")
            ops._delete_recorded_trash(entry)
            after_mutation("uninstall-release-deleted")
            ledger = read_owned_ledger(root)
            ledger["releases"].remove(version)
            write_owned_ledger(root, ledger, replace=fs.replace)
            after_mutation("uninstall-ledger-updated")
        for entry in list(ledger["trash"]):
            path = safe_child(root, f"releases/{entry['name']}")
            if not path.exists():
                ledger["trash"].remove(entry)
                write_owned_ledger(root, ledger, replace=fs.replace)
                continue
            ReleaseOps(root, fs=fs, after_mutation=after_mutation)._delete_recorded_trash(entry)
            ledger = read_owned_ledger(root)
            after_mutation("uninstall-recorded-trash-deleted")
        for name in list(ledger["tools"]):
            if _remove_known_file(root / name):
                after_mutation("known-file-removed")
            ledger["tools"].remove(name)
            write_owned_ledger(root, ledger, replace=fs.replace)
        for name in list(ledger["files"]):
            if _remove_known_file(safe_child(root, name)):
                if name == POINTER:
                    after_mutation("pointer-removed")
                after_mutation("known-file-removed")
            ledger["files"].remove(name)
            write_owned_ledger(root, ledger, replace=fs.replace)
        if "releases" in ledger["directories"]:
            release_root = root / "releases"
            if release_root.exists() and release_root.is_dir() and not is_reparse(release_root) and not any(release_root.iterdir()):
                release_root.rmdir()
                ledger["directories"].remove("releases")
                write_owned_ledger(root, ledger, replace=fs.replace)
                after_mutation("owned-release-directory-removed")
        _finish_uninstall(root, record, journal_store, fs, after_mutation)
    elif owner_exists:
        raise SafetyError("Install ownership ledger is missing")
    elif resuming:
        _finish_uninstall(root, record, journal_store, fs, after_mutation)


def create_owner(root: Path, *, product: str = "Branchglance") -> None:
    root.mkdir(parents=True, exist_ok=True)
    atomic_json(root / OWNER_FILE, {"schemaVersion": SCHEMA, "product": product})
    atomic_json(root / OWNED_LEDGER, {"schemaVersion": SCHEMA, "product": product,
                                      "releases": [], "trash": [], "tools": [], "files": [], "directories": []})


def read_owned_ledger(root: Path, *, allow_missing: bool = False) -> dict:
    path = Path(root) / OWNED_LEDGER
    if not path.exists() and allow_missing:
        return {"schemaVersion": SCHEMA, "product": "Branchglance", "releases": [], "trash": [], "tools": [],
                "files": [], "directories": []}
    value = read_json(path)
    return validate_owned_ledger(value)


def validate_owned_ledger(value: dict) -> dict:
    if (not isinstance(value, dict) or value.get("schemaVersion") != SCHEMA or value.get("product") != "Branchglance"
            or any(not isinstance(value.get(name), list) for name in ("releases", "trash", "tools", "files", "directories"))):
        raise SafetyError("Install ownership ledger is corrupt or unsupported")
    for version in value["releases"]:
        validate_version(version)
    if any(name not in TOOLS and not re.fullmatch(r"(?:Branchglance|BranchglanceUninstall)\.exe\.old-[0-9a-f]{32}", name)
           for name in value["tools"]):
        raise SafetyError("Install ownership ledger contains an unknown tool")
    if any(name != "releases" for name in value["directories"]):
        raise SafetyError("Install ownership ledger contains an unknown directory")
    if any(name != POINTER for name in value["files"]):
        raise SafetyError("Install ownership ledger contains an unknown file")
    for entry in value["trash"]:
        if (not isinstance(entry, dict) or not re.fullmatch(r"\.trash-[0-9a-f]{32}-[0-9]+", str(entry.get("name", "")))):
            raise SafetyError("Install ownership ledger contains an invalid trash name")
        validate_version(entry.get("version"))
        files = entry.get("files")
        if files is not None and (not isinstance(files, dict) or not files or any(
                not isinstance(name, str) or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
                for name, digest in files.items())):
            raise SafetyError("Install ownership ledger contains invalid trash hashes")
        if "deleting" in entry and type(entry["deleting"]) is not bool:
            raise SafetyError("Install ownership ledger contains an invalid deletion intent")
    return value


def write_owned_ledger(root: Path, ledger: dict, *, replace=os.replace) -> None:
    probe = {"schemaVersion": ledger.get("schemaVersion"), "product": ledger.get("product"),
             "releases": ledger.get("releases"), "trash": ledger.get("trash"), "tools": ledger.get("tools"),
             "files": ledger.get("files", []), "directories": ledger.get("directories", [])}
    validate_owned_ledger(probe)
    atomic_json(Path(root) / OWNED_LEDGER, probe, replace=replace)


def require_owned_root(root: Path) -> Path:
    root = root.absolute()
    if is_reparse(root) or not root.is_dir():
        raise SafetyError("Install root is absent or is a reparse point")
    marker = root / OWNER_FILE
    if is_reparse(marker) or not marker.is_file():
        raise SafetyError("Install root ownership marker is missing")
    try:
        owner = read_json(marker)
    except SafetyError as exc:
        raise SafetyError("Install root ownership marker is unreadable") from exc
    if owner != {"schemaVersion": SCHEMA, "product": "Branchglance"}:
        raise SafetyError("Install root ownership marker is missing or invalid")
    return root


def safe_child(root: Path, relative: str) -> Path:
    root = root.absolute()
    raw = Path(relative)
    if raw.is_absolute() or not relative or any(part in (".", "..") for part in raw.parts):
        raise SafetyError("Path must be a normalized relative path inside the install root")
    candidate = root.joinpath(raw)
    if os.path.commonpath((str(root), str(candidate.absolute()))) != str(root):
        raise SafetyError("Path escapes the install root")
    current = root
    for part in raw.parts:
        current = current / part
        if current.exists() and is_reparse(current):
            raise SafetyError(f"Reparse point refused: {current}")
    return candidate


def validate_version(value: str) -> str:
    if not isinstance(value, str) or not VERSION_RE.fullmatch(value):
        raise SafetyError("Invalid release version/build identifier")
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def parse_pointer(value: dict) -> dict:
    """Validate pointer structure without requiring the selected release to be healthy."""
    if not isinstance(value, dict) or value.get("schemaVersion") != SCHEMA:
        raise SafetyError("Current pointer schema is invalid")
    current = value.get("current")
    previous = value.get("previous")
    if not isinstance(current, dict):
        raise SafetyError("Current pointer current reference is invalid")
    for label, ref in (("current", current), ("previous", previous)):
        if ref is None and label == "previous":
            continue
        if not isinstance(ref, dict):
            raise SafetyError(f"Current pointer {label} reference is invalid")
        version = validate_version(ref.get("version"))
        if ref.get("path") != f"releases/{version}":
            raise SafetyError(f"Current pointer {label} path does not match its version")
    return value


def pointer_versions(pointer: dict | None) -> set[str]:
    pointer = parse_pointer(pointer) if pointer is not None else None
    if pointer is None:
        return set()
    return {ref["version"] for ref in (pointer.get("current"), pointer.get("previous")) if ref is not None}


def manifest_for(release: Path, version: str, build_id: str) -> dict:
    version = validate_version(f"{version}+{build_id}")
    entries = {}
    for path in _walk_plain(release):
        name = path.relative_to(release).as_posix()
        if name != MANIFEST:
            entries[name] = sha256_file(path)
    return {"schemaVersion": SCHEMA, "product": "Branchglance", "version": version,
            "buildId": build_id, "files": entries}


def write_manifest(release: Path, version: str, build_id: str) -> dict:
    payload = manifest_for(release, version, build_id)
    atomic_json(release / MANIFEST, payload)
    return payload


def file_stamp(path: Path) -> list[int]:
    """[size, last-write FILETIME] as the launcher's hook-mode check reads them."""
    info = os.stat(path)
    return [info.st_size, info.st_mtime_ns // 100 + _EPOCH_AS_FILETIME]


def write_stamps(release: Path) -> dict:
    """Record size+mtime of every installed file in the manifest (no hashing in hook mode)."""
    payload = read_json(release / MANIFEST)
    payload["stamps"] = {name: file_stamp(release / name) for name in sorted(payload["files"])}
    atomic_json(release / MANIFEST, payload)
    return payload


def verify_manifest(release: Path, *, expected_version: str | None = None) -> dict:
    payload = read_json(release / MANIFEST)
    if payload.get("schemaVersion") != SCHEMA or payload.get("product") != "Branchglance":
        raise SafetyError("Release manifest schema or product mismatch")
    validate_version(payload.get("version"))
    if expected_version and payload["version"] != expected_version:
        raise SafetyError("Release manifest version mismatch")
    files = payload.get("files")
    if not isinstance(files, dict) or not files:
        raise SafetyError("Release manifest file list is missing")
    actual = {path.relative_to(release).as_posix(): sha256_file(path)
              for path in _walk_plain(release) if path.relative_to(release).as_posix() != MANIFEST}
    if actual != files:
        raise SafetyError("Release files differ from the SHA-256 manifest")
    return payload


def trash_hash_snapshot(release: Path, version: str) -> dict:
    manifest = verify_manifest(release, expected_version=version)
    hashes = dict(manifest["files"])
    hashes[MANIFEST] = sha256_file(release / MANIFEST)
    return hashes


def verify_trash_contents(path: Path, entry: dict, *, allow_missing: bool) -> None:
    """Check recorded trash hashes; partial deletion may remove files but never add/change any."""
    hashes = entry.get("files")
    if not isinstance(hashes, dict) or not hashes:
        verify_manifest(path, expected_version=entry["version"])
        return
    if is_reparse(path) or not path.is_dir():
        raise SafetyError("Recorded trash is redirected or not a directory")
    actual_files: dict[str, Path] = {}
    actual_dirs = set()
    for directory, dirs, files in os.walk(path, topdown=True, followlinks=False):
        base = Path(directory)
        for name in list(dirs):
            item = base / name
            if is_reparse(item):
                raise SafetyError(f"Reparse point in recorded trash: {item}")
            actual_dirs.add(item.relative_to(path).as_posix())
        for name in files:
            item = base / name
            if is_reparse(item):
                raise SafetyError(f"Reparse point in recorded trash: {item}")
            actual_files[item.relative_to(path).as_posix()] = item
    if set(actual_files) - set(hashes):
        raise SafetyError("Recorded trash contains added files")
    for name, item in actual_files.items():
        if sha256_file(item) != hashes[name]:
            raise SafetyError(f"Recorded trash file changed: {name}")
    known_dirs = {str(Path(name).parent).replace("\\", "/") for name in hashes if "/" in name}
    known_dirs |= {ancestor for directory in list(known_dirs) for ancestor in _ancestors(directory)}
    if actual_dirs - known_dirs:
        raise SafetyError("Recorded trash contains added directories")
    if not allow_missing and set(actual_files) != set(hashes):
        raise SafetyError("Recorded trash is incomplete before deletion")


def _ancestors(relative: str) -> set[str]:
    result = set()
    current = relative
    while current and current != ".":
        result.add(current)
        current = str(Path(current).parent).replace("\\", "/")
    return result


class Journal:
    """Strict journal: malformed or future state is never silently discarded."""
    def __init__(self, root: Path, *, replace=os.replace):  # noqa: D107
        self.root = root
        self.path = root / JOURNAL
        self.replace = replace

    def load(self) -> dict | None:
        if not self.path.exists():
            return None
        value = read_json(self.path)
        if (value.get("schemaVersion") != SCHEMA or not isinstance(value.get("id"), str)
                or not re.fullmatch(r"[0-9a-f]{32}", value["id"])):
            raise SafetyError("Installer journal schema is corrupt or unsupported")
        operation = value.get("operation")
        phases = {"install": {"staging", "verified", "switching", "committed", "rolled-back"},
                  "rollback": {"staging", "switching", "committed", "rolled-back"},
                  "uninstall": {"staging", "switching", "finalizing", "committed"}}
        if operation not in phases or value.get("phase") not in phases[operation]:
            raise SafetyError("Installer journal phase is invalid")
        if operation == "uninstall":
            if value.get("target") != "uninstall":
                raise SafetyError("Uninstall journal target is invalid")
        else:
            validate_version(value.get("target"))
        before = value.get("before")
        if not isinstance(before, dict) or set(before) != {"pointer", "integration"} or not isinstance(before["integration"], dict):
            raise SafetyError("Installer journal snapshot is invalid")
        if before["pointer"] is not None:
            parse_pointer(before["pointer"])
        allowed = {"schemaVersion", "id", "operation", "target", "phase", "before", "created",
                   "tools", "promotionIntent", "partialIntent", "trashEntries", "damagedCurrent",
                   "trashIntent", "quarantineName", "collisionQuarantine", "restoreHandoff"}
        if set(value) - allowed:
            raise SafetyError("Installer journal contains unsupported fields")
        if operation != "install" and set(value) & {"tools", "promotionIntent", "partialIntent"}:
            raise SafetyError("Journal fields do not match the operation")
        if operation == "rollback" and set(value) & {"trashEntries", "trashIntent"}:
            raise SafetyError("Rollback journal cannot authorize trash deletion")
        if operation != "rollback" and set(value) & {"damagedCurrent", "quarantineName"}:
            raise SafetyError("Damaged-release quarantine is only valid for rollback")
        if operation == "uninstall" and set(value) & {"damagedCurrent", "quarantineName", "collisionQuarantine"}:
            raise SafetyError("Journal fields do not match the operation")
        if "promotionIntent" in value and (operation != "install" or type(value["promotionIntent"]) is not bool):
            raise SafetyError("Installer journal promotion intent is invalid")
        if "partialIntent" in value and (operation != "install" or type(value["partialIntent"]) is not bool):
            raise SafetyError("Installer journal partial intent is invalid")
        if "damagedCurrent" in value:
            validate_version(value["damagedCurrent"])
            if value.get("quarantineName") != f".quarantine-{value['id']}":
                raise SafetyError("Installer journal quarantine name is invalid")
        elif "quarantineName" in value:
            raise SafetyError("Installer journal quarantine has no damaged version")
        if "collisionQuarantine" in value and (operation != "install"
                or value["collisionQuarantine"] != f".quarantine-{value['id']}"):
            raise SafetyError("Installer journal collision quarantine name is invalid")
        if "restoreHandoff" in value:
            handoff = value["restoreHandoff"]
            if (operation != "install" or value["phase"] != "committed" or not isinstance(handoff, dict)
                    or set(handoff) != {"file", "nonce", "expires"}
                    or handoff.get("file") != ".cutover-handoff.json"
                    or not isinstance(handoff.get("nonce"), str)
                    or not re.fullmatch(r"[0-9a-f]{64}", handoff["nonce"])
                    or type(value.get("created")) not in (int, float)
                    or type(handoff.get("expires")) not in (int, float)
                    or not value.get("created") < handoff["expires"] <= value["created"] + 31):
                raise SafetyError("Installer restore-handoff journal record is invalid")
        tools = value.get("tools", {})
        if not isinstance(tools, dict):
            raise SafetyError("Installer journal tool list is invalid")
        for name, tool in tools.items():
            if name not in TOOLS or not isinstance(tool, dict):
                raise SafetyError("Installer journal tool record is invalid")
            expected = {"backup": f"{name}.old-{value['id']}", "staged": f"{name}.new-{value['id']}"}
            if tool.get("backup") not in (None, expected["backup"]) or tool.get("staged") != expected["staged"] or type(tool.get("created")) is not bool:
                raise SafetyError("Installer journal tool path is invalid")
        trash_entries = value.get("trashEntries", [])
        if not isinstance(trash_entries, list):
            raise SafetyError("Installer journal trash list is invalid")
        for index, entry in enumerate(trash_entries):
            if not isinstance(entry, dict):
                raise SafetyError("Installer journal trash record is invalid")
            if entry.get("name") != f".trash-{value['id']}-{index}":
                raise SafetyError("Installer journal trash name is invalid")
            validate_version(entry.get("version"))
            if set(entry) - {"name", "version", "files"}:
                raise SafetyError("Installer journal trash record has unsupported fields")
            if "files" in entry and (not isinstance(entry["files"], dict) or not entry["files"] or any(
                    not isinstance(name, str) or not isinstance(digest, str)
                    or not re.fullmatch(r"[0-9a-f]{64}", digest) for name, digest in entry["files"].items())):
                raise SafetyError("Installer journal trash hashes are invalid")
        intent = value.get("trashIntent")
        if intent is not None:
            if operation not in ("install", "uninstall") or not isinstance(intent, dict):
                raise SafetyError("Installer journal trash intent is invalid")
            if intent.get("name") != f".trash-{value['id']}-{len(trash_entries)}":
                raise SafetyError("Installer journal trash intent name is invalid")
            validate_version(intent.get("version"))
            if not isinstance(intent.get("files"), dict) or not intent["files"]:
                raise SafetyError("Installer journal trash hashes are missing")
            if set(intent) != {"name", "version", "files"}:
                raise SafetyError("Installer journal trash intent has unsupported fields")
            if any(not isinstance(name, str) or not isinstance(digest, str)
                   or not re.fullmatch(r"[0-9a-f]{64}", digest) for name, digest in intent["files"].items()):
                raise SafetyError("Installer journal trash hashes are invalid")
        return value

    def save(self, record: dict) -> None:
        atomic_json(self.path, record, replace=self.replace)

    def begin(self, operation: str, target: str, before: dict) -> dict:
        if self.load() is not None:
            raise SafetyError("An unfinished installer journal requires recovery first")
        record = {"schemaVersion": SCHEMA, "id": uuid.uuid4().hex, "operation": operation,
                  "target": target, "phase": "staging", "before": before, "created": time.time()}
        self.save(record)
        return record


@dataclass
class ReleaseOps:
    """Narrow injectable boundary for transactional filesystem and integration writes."""
    root: Path
    fs: FileSystem = LocalFileSystem()
    integration_snapshot: Callable[[], dict] = lambda: {}
    integration_restore: Callable[[dict], None] = lambda _state: None
    integration_apply: Callable[[Path], None] = lambda _release: None
    after_mutation: Callable[[str], None] = lambda _name: None
    # Installed name -> new file (launcher, uninstaller); replaced only when the bytes change.
    tools: dict = None
    # Headless check of the staged release before anything is promoted.
    self_test: Callable[[Path], None] = lambda _release: None

    def __post_init__(self) -> None:
        self.tools = dict(self.tools or {})
        unknown = set(self.tools) - set(TOOLS)
        if unknown:
            raise SafetyError(f"Unknown install tool: {sorted(unknown)}")

    @property
    def releases(self) -> Path:
        return self.root / "releases"

    @property
    def current_path(self) -> Path:
        return self.root / POINTER

    @property
    def journal(self) -> Journal:
        return Journal(self.root, replace=self._journal_replace)

    def _journal_replace(self, source: Path, target: Path) -> None:
        self.fs.replace(source, target, mutation="journal-write", after=self.after_mutation)

    def _replace(self, source: Path, target: Path, mutation: str) -> None:
        self.fs.replace(source, target, mutation=mutation, after=self.after_mutation)

    def current(self) -> dict | None:
        if not self.current_path.exists():
            return None
        return parse_pointer(read_json(self.current_path))

    def _pointer_for(self, path: Path, previous: dict | None) -> dict:
        manifest = verify_manifest(path)
        rel = path.relative_to(self.root).as_posix()
        if previous is not None:
            parse_pointer({"schemaVersion": SCHEMA, "current": previous, "previous": None})
        return {"schemaVersion": SCHEMA, "current": {"version": manifest["version"], "path": rel},
                "previous": previous}

    def _ledger(self) -> dict:
        return read_owned_ledger(self.root, allow_missing=True)

    def _save_ledger(self, ledger: dict) -> None:
        write_owned_ledger(self.root, ledger, replace=self._journal_replace)

    def _trash_valid_release(self, path: Path, version: str, *, record: dict | None = None) -> str:
        files = trash_hash_snapshot(path, version)
        ledger = self._ledger()
        transaction = record["id"] if record is not None else uuid.uuid4().hex
        pending = next((item for item in (record or {}).get("trashEntries", [])
                        if item.get("version") == version
                        and not (self.releases / item["name"]).exists()), None)
        if record is None and pending is None:
            pending = next((item for item in ledger["trash"] if item.get("version") == version
                            and item.get("files") == files
                            and not (self.releases / item["name"]).exists()), None)
        if pending:
            if pending.get("files") != files:
                raise SafetyError("Reserved trash source changed before promotion")
            entry = pending
        else:
            index = len(record.get("trashEntries", [])) if record is not None else 0
            name = f".trash-{transaction}-{index}"
            entry = {"name": name, "version": version, "files": files}
        name = entry["name"]
        self._reserve_trash(entry, ledger, record)
        target = self.releases / name
        self._replace(path, target, "release-trashed")
        return name

    def _reserve_trash(self, entry: dict, ledger: dict, record: dict | None) -> None:
        if record is not None:
            current = next((item for item in record.get("trashEntries", []) if item["name"] == entry["name"]), None)
            if current is not None:
                if current != entry:
                    raise SafetyError("Trash journal reservation changed")
            elif record.get("trashIntent") not in (None, entry):
                raise SafetyError("A different trash reservation is already pending")
            else:
                record["trashIntent"] = entry
                self.journal.save(record)
                self.after_mutation("trash-intent-journaled")
        existing = next((item for item in ledger["trash"] if item["name"] == entry["name"]), None)
        if existing is None:
            ledger["trash"].append(dict(entry))
            self._save_ledger(ledger)
            self.after_mutation("trash-intent-ledgered")
        elif any(existing.get(key) != entry.get(key) for key in ("version", "files")):
            raise SafetyError("Trash ledger reservation changed")
        if record is not None:
            if not any(item["name"] == entry["name"] for item in record.get("trashEntries", [])):
                record.setdefault("trashEntries", []).append(entry)
            record.pop("trashIntent", None)
            self.journal.save(record)
            self.after_mutation("trash-reserved")

    def _reconcile_trash_intent(self, record: dict) -> None:
        entry = record.get("trashIntent")
        if entry is None:
            return
        ledger = self._ledger()
        self._reserve_trash(entry, ledger, record)

    def _delete_recorded_trash(self, entry: dict) -> bool:
        ledger = self._ledger()
        path = safe_child(self.root, f"releases/{entry['name']}")
        owned = next((item for item in ledger["trash"] if item["name"] == entry["name"]), None)
        if owned is None:
            if not path.exists():
                return True  # an earlier recovery already completed this recorded deletion
            raise SafetyError("Trash deletion lacks an installer ownership record")
        if (owned.get("version") != entry.get("version")
                or (entry.get("files") is not None and owned.get("files") != entry.get("files"))):
            raise SafetyError("Trash deletion record does not match its ownership ledger")
        if path.exists():
            if is_reparse(path) or not path.is_dir():
                raise SafetyError("Recorded trash was redirected or replaced")
            if owned.get("files"):
                if not owned.get("deleting"):
                    verify_trash_contents(path, owned, allow_missing=False)
                    owned["deleting"] = True
                    self._save_ledger(ledger)
                    self.after_mutation("trash-deletion-intent")
                verify_trash_contents(path, owned, allow_missing=True)
            else:
                verify_manifest(path, expected_version=entry["version"])
            self.fs.remove_tree(path)
            self.after_mutation("recorded-trash-deleted")
        ledger["trash"].remove(owned)
        self._save_ledger(ledger)
        return True

    def recover(self) -> str:
        journal = self.journal.load()
        if journal is None:
            return "clean"
        if journal["operation"] == "uninstall":
            raise SafetyError("Interrupted uninstall must resume through app.main --uninstall-worker")
        self._reconcile_trash_intent(journal)
        phase = journal["phase"]
        if phase in ("committed", "rolled-back"):
            self.journal.path.unlink(missing_ok=True)
            self.after_mutation("journal-cleared")
            return "completed"
        before = journal["before"]
        pointer_before = before.get("pointer")
        if pointer_before is None:
            self.current_path.unlink(missing_ok=True)
        else:
            atomic_json(self.current_path, pointer_before, replace=self.fs.replace)
        self.after_mutation("pointer-restored")
        self.integration_restore(before.get("integration", {}))
        self.after_mutation("integration-restored")
        self._restore_tools(journal)
        damaged = journal.get("damagedCurrent")
        quarantine_name = journal.get("quarantineName")
        if damaged and quarantine_name:
            original = safe_child(self.root, f"releases/{damaged}")
            quarantine = safe_child(self.root, f"releases/{quarantine_name}")
            if quarantine.exists() and not original.exists():
                self._replace(quarantine, original, "damaged-current-restored")
        if journal["operation"] == "install":
            version = validate_version(journal["target"])
            protected = pointer_versions(pointer_before)
            partial = safe_child(self.root, f"releases/.partial-{version}-{journal['id']}")
            marker = partial / STAGE_MARKER
            if partial.exists() and version not in protected:
                marker_owned = False
                if not is_reparse(partial) and marker.is_file() and not is_reparse(marker):
                    try:
                        stage_owner = read_json(marker)
                        marker_owned = stage_owner == {"schemaVersion": SCHEMA, "product": "Branchglance",
                                                       "transaction": journal["id"], "version": version}
                    except SafetyError:
                        marker_owned = False
                if marker_owned:
                    self.fs.remove_tree(partial)
                    self.after_mutation("owned-partial-removed")
                else:
                    try:
                        verify_manifest(partial, expected_version=version)
                    except SafetyError:
                        pass  # malformed or foreign staging content is retained
                    else:
                        self._trash_valid_release(partial, version, record=journal)
            final = safe_child(self.root, f"releases/{version}")
            if journal.get("promotionIntent") is True and final.exists() and version not in protected:
                try:
                    verify_manifest(final, expected_version=version)
                except SafetyError:
                    pass  # don't delete an unverifiable replacement
                else:
                    self._trash_valid_release(final, version, record=journal)
        for entry in list(journal.get("trashEntries", [])):
            self._delete_recorded_trash(entry)
        if journal["operation"] == "install" and journal["target"] not in pointer_versions(pointer_before):
            ledger = self._ledger()
            final = self.releases / journal["target"]
            if not final.exists() and journal["target"] in ledger["releases"]:
                ledger["releases"].remove(journal["target"])
                self._save_ledger(ledger)
        journal["phase"] = "rolled-back"
        self.journal.save(journal)
        self.after_mutation("rollback-journaled")
        self.journal.path.unlink(missing_ok=True)
        self.after_mutation("journal-cleared")
        return "rolled-back"

    def _restore_tools(self, journal: dict) -> None:
        ledger = self._ledger()
        for name, entry in (journal.get("tools") or {}).items():
            if name not in TOOLS or not isinstance(entry, dict):
                raise SafetyError("Installer journal tool record is invalid")
            live = self.root / name
            backup = entry.get("backup")
            staged = entry.get("staged")
            if backup:
                backup_path = safe_child(self.root, backup)
                if backup_path.exists():
                    self.fs.replace(backup_path, live)  # the previous bytes win
                    self.after_mutation("tool-restored")
                if backup in ledger["tools"]:
                    ledger["tools"].remove(backup)
                    self._save_ledger(ledger)
            elif entry.get("created") and live.exists():
                _remove_known_file(live)
                self.after_mutation("tool-removed")
                if name in ledger["tools"]:
                    ledger["tools"].remove(name)
                    self._save_ledger(ledger)
            if staged:
                staged_path = safe_child(self.root, staged)
                if staged_path.exists():
                    _remove_known_file(staged_path)

    def _replace_tools(self, record: dict) -> None:
        """Write-new then rename: the live file is renamed aside (works while it runs)."""
        record["tools"] = {}
        for name, source in sorted(self.tools.items()):
            source = Path(source)
            if is_reparse(source) or not source.is_file():
                raise SafetyError(f"Install tool is missing or unsafe: {source}")
            live = self.root / name
            if os.path.lexists(live) and (is_reparse(live) or not live.is_file()):
                raise SafetyError(f"Unsafe owned install file: {live}")
            ledger = self._ledger()
            if name not in ledger["tools"]:
                ledger["tools"].append(name)
                self._save_ledger(ledger)
            if live.exists() and sha256_file(live) == sha256_file(source):
                continue
            entry = {"backup": f"{name}.old-{record['id']}" if live.exists() else None,
                     "created": not live.exists(), "staged": f"{name}.new-{record['id']}"}
            record["tools"][name] = entry
            self.journal.save(record)  # intent first
            if entry["backup"]:
                ledger["tools"].append(entry["backup"])
                self._save_ledger(ledger)
            staged = self.root / entry["staged"]
            shutil.copyfile(source, staged)
            self.after_mutation("tool-staged")
            if entry["backup"]:
                self._replace(live, self.root / entry["backup"], "tool-backed-up")
            self._replace(staged, live, "tool-replaced")

    def _cleanup_tool_backups(self, record: dict) -> None:
        ledger = self._ledger()
        for entry in (record.get("tools") or {}).values():
            backup = entry.get("backup")
            if not backup:
                continue
            path = safe_child(self.root, backup)
            try:
                _remove_known_file(path)
            except (OSError, SafetyError):
                continue  # still running or unsafe; the ownership ledger retains its exact name
            if backup in ledger["tools"]:
                ledger["tools"].remove(backup)
                self._save_ledger(ledger)

    def install(self, staged: Path, version: str, build_id: str) -> dict:
        require_owned_root(self.root)
        self.recover()
        version = validate_version(f"{version}+{build_id}")
        staged = Path(staged)
        if is_reparse(staged) or not staged.is_dir():
            raise SafetyError("Staged release is missing or redirected")
        verify_manifest(staged, expected_version=version)
        pointer = self.current()
        release_name = version
        final = safe_child(self.root, f"releases/{version}")
        integration = self.integration_snapshot()
        record = self.journal.begin("install", version, {"pointer": pointer, "integration": integration})
        record["promotionIntent"] = False
        partial_name = f".partial-{version}-{record['id']}"
        record["partialIntent"] = True
        self.journal.save(record)
        self.after_mutation("journal-created")
        if final.exists():
            try:
                verify_manifest(final, expected_version=version)
            except SafetyError:
                if version in pointer_versions(pointer):
                    raise SafetyError("Cannot replace a release still referenced by current.json")
                record["collisionQuarantine"] = f".quarantine-{record['id']}"
                self.journal.save(record)
                self._replace(final, self.releases / record["collisionQuarantine"], "foreign-release-quarantined")
            else:
                raise SafetyError("Immutable release already exists")
        ledger = self._ledger()
        if "releases" not in ledger["directories"]:
            ledger["directories"].append("releases")
            self._save_ledger(ledger)
        self.releases.mkdir(exist_ok=True)
        partial = safe_child(self.root, f"releases/{partial_name}")
        if partial.exists():
            raise SafetyError("Unrecovered partial release exists")
        partial.mkdir()
        atomic_json(partial / STAGE_MARKER, {"schemaVersion": SCHEMA, "product": "Branchglance",
                                             "transaction": record["id"], "version": version},
                    replace=self._journal_replace)
        shutil.copytree(staged, partial, symlinks=True, dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns(STAGE_MARKER))
        self.after_mutation("release-staged")
        marker = partial / STAGE_MARKER
        if marker.exists():
            marker.unlink()
        verify_manifest(partial, expected_version=version)
        self.self_test(partial)
        write_stamps(partial)
        record["phase"] = "verified"
        self.journal.save(record)
        self.after_mutation("release-verified")
        # Durable intent makes either location recoverable if power fails in MoveFileEx.
        record["promotionIntent"] = True
        self.journal.save(record)
        if final.exists():
            quarantine_name = record.get("collisionQuarantine") or f".quarantine-{record['id']}"
            record["collisionQuarantine"] = quarantine_name
            self.journal.save(record)
            self._replace(final, self.releases / quarantine_name, "foreign-release-quarantined")
        self._replace(partial, final, "release-promoted")
        record["phase"] = "switching"
        self.journal.save(record)
        self._replace_tools(record)
        previous = None
        if pointer:
            for candidate in (pointer.get("current"), pointer.get("previous")):
                if candidate is None:
                    continue
                try:
                    verify_manifest(safe_child(self.root, candidate["path"]), expected_version=candidate["version"])
                except (SafetyError, KeyError, TypeError):
                    continue
                previous = candidate
                break
        ledger = self._ledger()
        if version not in ledger["releases"]:
            ledger["releases"].append(version)
            self._save_ledger(ledger)
        if POINTER not in ledger["files"]:
            ledger["files"].append(POINTER)
            self._save_ledger(ledger)
        atomic_json(self.current_path, self._pointer_for(final, previous), replace=self.fs.replace)
        self.after_mutation("pointer-switched")
        self.integration_apply(final)
        self.after_mutation("integration-applied")
        record["phase"] = "committed"
        self.journal.save(record)
        self.after_mutation("commit-journaled")
        self.journal.path.unlink(missing_ok=True)
        self.after_mutation("journal-cleared")
        self._cleanup_tool_backups(record)
        return self.current() or {}

    def rollback(self) -> dict:
        require_owned_root(self.root)
        self.recover()
        pointer = self.current()
        if not pointer or not pointer.get("previous"):
            raise SafetyError("No previous release is available")
        previous = pointer["previous"]
        path = safe_child(self.root, f"releases/{validate_version(previous.get('version'))}")
        manifest = verify_manifest(path, expected_version=previous.get("version"))
        next_previous = pointer.get("current")
        damaged_current = None
        if next_previous is not None:
            try:
                current_path = safe_child(self.root, f"releases/{validate_version(next_previous.get('version'))}")
                verify_manifest(current_path, expected_version=next_previous.get("version"))
            except (SafetyError, TypeError, AttributeError):
                damaged_current = next_previous.get("version") if isinstance(next_previous, dict) else None
                next_previous = None
        if damaged_current == manifest["version"]:
            damaged_current = None
        record = self.journal.begin("rollback", manifest["version"],
                                    {"pointer": pointer, "integration": self.integration_snapshot()})
        if damaged_current:
            record["damagedCurrent"] = validate_version(damaged_current)
            record["quarantineName"] = f".quarantine-{record['id']}"
            self.journal.save(record)
        record["phase"] = "switching"
        self.journal.save(record)
        self.after_mutation("rollback-journaled")
        atomic_json(self.current_path, self._pointer_for(path, next_previous), replace=self.fs.replace)
        self.after_mutation("pointer-rolled-back")
        if damaged_current:
            damaged_path = self.releases / damaged_current
            quarantine = self.releases / record["quarantineName"]
            if os.path.lexists(damaged_path) and not os.path.lexists(quarantine):
                self._replace(damaged_path, quarantine, "damaged-current-quarantined")
        self.integration_apply(path)
        self.after_mutation("integration-rolled-back")
        record["phase"] = "committed"
        self.journal.save(record)
        self.after_mutation("rollback-committed")
        self.journal.path.unlink(missing_ok=True)
        return self.current() or {}

    def prune(self) -> list[str]:
        """Keep current + previous; delete only manifest-verified releases or ledger trash."""
        require_owned_root(self.root)
        pointer = self.current()  # structural parsing does not require current to verify
        keep = pointer_versions(pointer)
        removed = []
        if not self.releases.exists():
            return removed
        ledger = self._ledger()
        for entry in list(ledger["trash"]):
            if self._delete_recorded_trash(entry):
                removed.append(f"releases/{entry['name']}")
        for release in sorted(self.releases.iterdir()):
            if release.name.startswith("."):
                continue  # quarantine is retention, never deletion authority
            if not VERSION_RE.fullmatch(release.name) or release.name in keep:
                continue
            if is_reparse(release):
                raise SafetyError(f"Reparse point refused: {release}")
            if not release.is_dir():
                continue
            try:
                verify_manifest(release, expected_version=release.name)
            except SafetyError:
                # Preserve suspicious content in a dot-prefixed quarantine. That
                # name never authorizes later deletion.
                quarantine = self.releases / f".quarantine-{uuid.uuid4().hex}"
                self._replace(release, quarantine, "unverified-release-quarantined")
                continue
            name = self._trash_valid_release(release, release.name)
            entry = next(item for item in self._ledger()["trash"] if item["name"] == name)
            if self._delete_recorded_trash(entry):
                removed.append(f"releases/{release.name}")
        return removed
