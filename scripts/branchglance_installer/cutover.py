from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from .core import SafetyError, atomic_json, is_reparse, read_json
from .integration import MARKER
from token_tracker.product import APP_USER_MODEL_ID

STATE_FILES = ("limit-reset-state.json", "limit-reset.jsonl", "window-state.json")
POLICY = {"schemaVersion": 1, "origin": "user_settings",
          "providers": {"Codex": {"collect": True, "show": True, "send": True},
                        "Claude": {"collect": True, "show": True, "send": False}}}
OLD_ARGUMENTS = "-m token_tracker.widget"


class CutoverAdapter(Protocol):
    def old_pythonw_running(self) -> bool: ...
    def port_is_free(self, port: int) -> bool: ...
    def snapshot_old_shortcut(self) -> dict: ...
    def rename_old_shortcut(self, snapshot: dict) -> None: ...
    def create_branchglance_shortcut(self, record: dict) -> dict: ...
    def remove_branchglance_shortcut(self, record: dict | None = None) -> None: ...
    def start_branchglance(self, handoff_token: str) -> int: ...
    def identity(self) -> dict: ...
    def branchglance_running(self) -> bool: ...
    def quit_branchglance(self) -> bool: ...
    def restore_old_shortcut(self, snapshot: dict) -> None: ...
    def start_old_app(self, snapshot: dict) -> None: ...


@dataclass
class Cutover:
    adapter: CutoverAdapter
    legacy_state: Path
    product_root: Path
    expected_old_target: Path
    expected_old_workdir: Path
    migrate: Callable[[Path], None]
    after_mutation: Callable[[str], None] = lambda _name: None
    replace: Callable[[Path, Path], None] = os.replace

    def _preconditions(self) -> dict:
        self._validate_state_paths(allow_missing_product=True)
        if self.adapter.old_pythonw_running():
            raise SafetyError("Quit Token Tracker from its tray before cutover; no process will be killed")
        if not self.adapter.port_is_free(8765) or not self.adapter.port_is_free(8766):
            raise SafetyError("Token Tracker ports 8765/8766 are not both free")
        shortcut = self.adapter.snapshot_old_shortcut()
        if (Path(shortcut.get("target", "")).resolve() != self.expected_old_target.resolve()
                or Path(shortcut.get("workdir", "")).resolve() != self.expected_old_workdir.resolve()
                or shortcut.get("arguments") != OLD_ARGUMENTS):
            raise SafetyError("Old Desktop shortcut does not match the inventoried Token Tracker target")
        return shortcut

    def plan(self) -> dict:
        shortcut = self._preconditions()
        files = {}
        if not self.legacy_state.is_dir() or is_reparse(self.legacy_state):
            raise SafetyError("Legacy state folder is missing or unsafe")
        for name in STATE_FILES:
            source = self.legacy_state / name
            if source.exists():
                if is_reparse(source) or not source.is_file():
                    raise SafetyError(f"Unsafe legacy state item: {name}")
                files[name] = _hash(source)
        return {"writes": 0, "state_files": files, "old_shortcut": shortcut,
                "policy": POLICY, "actions": ["copy and verify state", "migrate copy", "write policy",
                "rename inventoried shortcut", "create Branchglance shortcut", "start and verify identity"]}

    def run(self, *, release_instance: Callable[[], None] = lambda: None) -> dict:
        plan = self.plan()
        journal_path = self.product_root / "cutover-journal.json"
        if journal_path.exists():
            raise SafetyError("An unfinished cutover journal requires recovery first")
        target = self.product_root / "state"
        if self.product_root.exists() and is_reparse(self.product_root):
            raise SafetyError("Branchglance data root is a reparse point")
        if target.exists() or (self.product_root / "policy.json").exists():
            raise SafetyError("Branchglance state already exists; refusing to overwrite it")
        target.parent.mkdir(parents=True, exist_ok=True)
        txid = uuid.uuid4().hex
        staging = target.with_name(f"state.cutover-{txid}.partial")
        snapshot = plan["old_shortcut"]
        mutated_shortcut = False
        launcher = str(self.product_root.parent / "Programs" / "Branchglance" / "Branchglance.exe")
        handoff_path = Path(launcher).parent / ".cutover-handoff.json"
        handoff_token = secrets.token_hex(32)
        handoff_written = False
        handoff_record = {"file": handoff_path.name, "nonce": handoff_token, "expires": time.time() + 30.0}
        new_shortcut = {"path": str(Path(os.environ.get("USERPROFILE", Path.home())) / "Desktop" / "Branchglance.lnk"),
                        "target": launcher, "arguments": "", "workdir": str(Path(launcher).parent),
                        "app_id": APP_USER_MODEL_ID, "description": MARKER}
        journal = {"schemaVersion": 1, "id": txid, "phase": "planned", "staging": staging.name,
                   "shortcut": snapshot, "beforeHashes": plan["state_files"], "beforePolicy": None,
                   "newShortcut": new_shortcut, "newShortcutCreated": False}
        atomic_json(journal_path, journal, replace=self.replace)
        self.after_mutation("journal-created")

        def phase(name: str, value: str) -> None:
            journal["phase"] = value
            atomic_json(journal_path, journal, replace=self.replace)
            self.after_mutation(name)

        def intent(value: str) -> None:
            journal["phase"] = value
            atomic_json(journal_path, journal)

        try:
            staging.mkdir()
            for name, before in plan["state_files"].items():
                shutil.copyfile(self.legacy_state / name, staging / name)
                if _hash(self.legacy_state / name) != before or _hash(staging / name) != before:
                    raise SafetyError(f"State changed while copying: {name}")
            phase("state-copied", "copied")
            self.migrate(staging)
            phase("state-migrated", "migrated")
            atomic_json(self.product_root / "policy.json", POLICY, replace=self.replace)
            phase("policy-written", "policy-written")
            journal["promotedStateHash"] = _tree_hash(staging)
            atomic_json(journal_path, journal, replace=self.replace)
            self.replace(staging, target)
            phase("state-promoted", "state-promoted")
            intent("renaming-old-shortcut")
            self.adapter.rename_old_shortcut(snapshot)
            mutated_shortcut = True
            phase("old-shortcut-renamed", "old-shortcut-renamed")
            intent("creating-new-shortcut")
            journal["creationAttempted"] = True
            atomic_json(journal_path, journal, replace=self.replace)
            self.adapter.create_branchglance_shortcut(new_shortcut)
            phase("new-shortcut-created", "new-shortcut-created")
            journal["newShortcutCreated"] = True
            atomic_json(journal_path, journal, replace=self.replace)
            intent("starting-branchglance")
            if os.path.lexists(handoff_path):
                raise SafetyError("A cutover startup handoff is already pending")
            journal["handoff"] = dict(handoff_record)
            atomic_json(journal_path, journal, replace=self.replace)
            self.after_mutation("handoff-intent")
            atomic_json(handoff_path, {"schemaVersion": 1, "nonce": handoff_token,
                                       "expires": handoff_record["expires"]})
            handoff_written = True
            self.after_mutation("handoff-written")
            release_instance()
            pid = self.adapter.start_branchglance(handoff_token)
            phase("branchglance-started", "started")
            who = self.adapter.identity()
            if (who.get("product") != "Branchglance" or type(who.get("pid")) is not int
                    or who.get("stateRoot") != str(target) or not who.get("build")):
                raise SafetyError("Branchglance health identity did not match the launched release")
            phase("committed", "committed")
            if handoff_written:
                self._remove_owned_handoff(handoff_record, require_expired=False)
            journal_path.unlink(missing_ok=True)
            return {"pid": who["pid"], "identity": who, "state": str(target)}
        except Exception:
            if self.adapter.branchglance_running() and not self.adapter.quit_branchglance():
                raise SafetyError("Cutover failed and Branchglance did not confirm shutdown; journal retained")
            if mutated_shortcut:
                self.adapter.restore_old_shortcut(snapshot)
            if journal.get("newShortcutCreated") or journal.get("creationAttempted"):
                self.adapter.remove_branchglance_shortcut(new_shortcut)
            if handoff_written:
                self._remove_owned_handoff(handoff_record, require_expired=False)
            if staging.exists():
                shutil.rmtree(staging)
            # Once promoted, retain policy and journal so recover can safely undo it.
            if journal.get("phase") not in {"state-promoted", "renaming-old-shortcut", "old-shortcut-renamed",
                                             "creating-new-shortcut", "new-shortcut-created", "starting-branchglance",
                                             "started"}:
                (self.product_root / "policy.json").unlink(missing_ok=True)
                journal_path.unlink(missing_ok=True)
            raise

    def recover(self) -> str:
        journal_path = self.product_root / "cutover-journal.json"
        if not journal_path.exists():
            return "clean"
        journal = read_json(journal_path)
        if journal.get("schemaVersion") != 1 or journal.get("phase") not in {
                "planned", "copied", "migrated", "policy-written", "state-promoted",
                "renaming-old-shortcut", "old-shortcut-renamed", "creating-new-shortcut", "rollback-merging",
                "new-shortcut-created", "starting-branchglance", "started", "committed"}:
            raise SafetyError("Cutover journal is corrupt or unsupported")
        handoff = journal.get("handoff")
        if isinstance(handoff, dict):
            self._remove_owned_handoff(handoff, require_expired=True)
        if journal["phase"] == "rollback-merging":
            merge = journal.get("rollbackMerge")
            if not isinstance(merge, dict) or merge.get("path") != "limit-reset-state.json":
                raise SafetyError("Rollback merge journal is invalid")
            target = self.legacy_state / merge["path"]
            if is_reparse(target) or not target.is_file():
                raise SafetyError("Legacy merge target is missing or unsafe")
            current_hash = _hash(target)
            if current_hash not in (merge.get("beforeHash"), merge.get("afterHash")):
                raise SafetyError("Legacy state changed during interrupted rollback; refusing recovery")
            snapshot = self.adapter.snapshot_old_shortcut()
            self.adapter.restore_old_shortcut(snapshot)
            journal_path.unlink()
            return "rollback-merged" if current_hash == merge.get("afterHash") else "rollback-not-applied"
        if journal["phase"] != "committed":
            if self.adapter.branchglance_running() and not self.adapter.quit_branchglance():
                raise SafetyError("Quit Branchglance from the tray before cutover recovery")
            stage = self.product_root / journal.get("staging", "")
            if (not isinstance(journal.get("id"), str) or not re.fullmatch(r"[0-9a-f]{32}", journal["id"])
                    or stage.parent != self.product_root or stage.name != f"state.cutover-{journal['id']}.partial"):
                raise SafetyError("Cutover journal has an unsafe staging path")
            if stage.exists():
                if is_reparse(stage):
                    raise SafetyError("Cutover staging path became a reparse point")
                shutil.rmtree(stage)
            if journal.get("newShortcutCreated") or journal.get("creationAttempted"):
                self.adapter.remove_branchglance_shortcut(journal.get("newShortcut"))
            if journal["phase"] in {"renaming-old-shortcut", "old-shortcut-renamed", "creating-new-shortcut",
                                     "new-shortcut-created", "starting-branchglance", "started", "state-promoted"}:
                self.adapter.restore_old_shortcut(journal["shortcut"])
            target = self.product_root / "state"
            if journal["phase"] in {"policy-written", "state-promoted", "renaming-old-shortcut", "old-shortcut-renamed",
                                     "creating-new-shortcut", "new-shortcut-created", "starting-branchglance", "started"}:
                if target.exists():
                    if is_reparse(target):
                        raise SafetyError("Promoted state became a reparse point")
                    expected_hash = journal.get("promotedStateHash")
                    if not expected_hash or _tree_hash(target) != expected_hash:
                        raise SafetyError("Promoted state no longer matches this cutover; retained for review")
                    shutil.rmtree(target)
            policy = self.product_root / "policy.json"
            if policy.exists() and journal.get("beforePolicy") is None:
                policy.unlink()
        journal_path.unlink()
        return "completed" if journal["phase"] == "committed" else "rolled-back"

    def _remove_owned_handoff(self, record: dict, *, require_expired: bool) -> bool:
        if (not isinstance(record, dict) or record.get("file") != ".cutover-handoff.json"
                or not isinstance(record.get("nonce"), str)
                or not re.fullmatch(r"[0-9a-f]{64}", record["nonce"])
                or not isinstance(record.get("expires"), (int, float))):
            raise SafetyError("Cutover handoff ownership record is invalid; marker preserved")
        path = self.product_root.parent / "Programs" / "Branchglance" / record["file"]
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            return False
        if is_reparse(path) or not path.is_file() or info.st_size > 1024:
            raise SafetyError("Cutover handoff marker is foreign or unsafe; marker preserved")
        marker = read_json(path)
        owned = (marker.get("schemaVersion") == 1 and marker.get("nonce") == record["nonce"]
                 and marker.get("expires") == record["expires"])
        if not owned:
            raise SafetyError("Cutover handoff marker does not match its journal; marker preserved")
        if require_expired and time.time() < float(record["expires"]):
            raise SafetyError("Cutover handoff is still unexpired; journal and marker retained")
        path.unlink()
        return True

    def rollback(self) -> dict:
        journal_path = self.product_root / "cutover-journal.json"
        if journal_path.exists():
            raise SafetyError("An interrupted cutover must be recovered before rollback; journal left untouched")
        self._validate_state_paths()
        if self.adapter.old_pythonw_running():
            raise SafetyError("Quit Token Tracker from its tray before rollback")
        snapshot = self.adapter.snapshot_old_shortcut()
        if self.adapter.branchglance_running() and not self.adapter.quit_branchglance():
            raise SafetyError("Quit Branchglance from its tray before rollback")
        old_file = self.legacy_state / "limit-reset-state.json"
        new_file = self.product_root / "state" / "limit-reset-state.json"
        merged = None
        if old_file.exists() and new_file.exists():
            before_hash = _hash(old_file)
            old_data, new_data = read_json(old_file), read_json(new_file)
            # Copy only compatible provider greeting state; preserve legacy resetAction verbatim.
            old_providers = old_data.get("providers", {})
            new_providers = new_data.get("providers", {})
            for provider in ("Codex", "Claude"):
                previous, current = old_providers.get(provider), new_providers.get(provider)
                if isinstance(previous, dict) and isinstance(current, dict):
                    for key in ("lastTriggerAt", "nextGreetingAt", "lastGreetingAt", "lastGreetingId"):
                        if key in current:
                            previous[key] = current[key]
            journal_path = self.product_root / "cutover-journal.json"
            payload = json.dumps(old_data, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
            after_hash = hashlib.sha256(payload).hexdigest()
            journal = {"schemaVersion": 1, "phase": "rollback-merging", "rollbackMerge": {
                "path": old_file.name, "beforeHash": before_hash, "afterHash": after_hash}}
            atomic_json(journal_path, journal, replace=self.replace)
            self.after_mutation("rollback-merge-journaled")
            self._validate_state_paths()
            if _hash(old_file) != before_hash or is_reparse(old_file):
                raise SafetyError("Legacy state changed during rollback; merge refused")
            atomic_json(old_file, old_data, replace=self.replace)
            self.after_mutation("rollback-merge-written")
            merged = old_data
        self.adapter.restore_old_shortcut(snapshot)
        self.adapter.start_old_app(snapshot)
        (self.product_root / "cutover-journal.json").unlink(missing_ok=True)
        return {"restored": True, "merged_state": merged}

    def _validate_state_paths(self, *, allow_missing_product: bool = False) -> None:
        if (self.legacy_state.name != "TokenTracker" or self.product_root.name != "Branchglance"
                or self.legacy_state.parent.resolve() != self.product_root.parent.resolve()):
            raise SafetyError("State paths are not the expected per-user TokenTracker and Branchglance roots")
        for path in (self.legacy_state, self.product_root):
            if not path.is_dir() and not (allow_missing_product and path == self.product_root and not path.exists()):
                raise SafetyError(f"Expected state root is missing: {path}")
            current = path
            while current != current.parent:
                if current.exists() and is_reparse(current):
                    raise SafetyError(f"State path contains a reparse point: {current}")
                current = current.parent


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    paths = []
    for directory, dirs, files in os.walk(root, topdown=True, followlinks=False):
        base = Path(directory)
        for name in list(dirs):
            item = base / name
            if is_reparse(item):
                raise SafetyError(f"Reparse point in state tree: {item}")
        paths.extend(base / name for name in files)
    for path in sorted(paths, key=lambda item: item.relative_to(root).as_posix()):
        if is_reparse(path) or not path.is_file():
            raise SafetyError(f"Unsafe item in state tree: {path}")
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big")); digest.update(relative); digest.update(bytes.fromhex(_hash(path)))
    return digest.hexdigest()
