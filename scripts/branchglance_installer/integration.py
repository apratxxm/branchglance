from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .core import SafetyError
from token_tracker.product import APP_USER_MODEL_ID

MARKER = "Branchglance managed shortcut"
_LEGACY_MARKER = re.compile(re.escape(MARKER) + r" [0-9a-f]{32}")
UNINSTALL_KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\Branchglance"


class IntegrationAdapter(Protocol):
    def read_shortcut(self, path: Path) -> dict | None: ...
    def replace_shortcut(self, path: Path, *, target: str, arguments: str, workdir: str,
                         app_id: str, description: str) -> None: ...
    def remove_shortcut(self, path: Path) -> None: ...
    def read_uninstall(self) -> dict | None: ...
    def write_uninstall(self, values: dict, *, types: dict | None = None) -> None: ...
    def remove_uninstall(self, *, expected: dict | None = None) -> None: ...


@dataclass
class Integrations:
    adapter: IntegrationAdapter
    root: Path
    launcher: Path
    version: str
    start_menu: Path
    startup: Path | None = None
    uninstaller: Path | None = None
    desktop: Path | None = None

    @property
    def shortcut_paths(self) -> tuple[Path, ...]:
        paths = [self.start_menu / "Branchglance.lnk"]
        if self.desktop is not None:
            paths.append(self.desktop / "Branchglance.lnk")
        if self.startup is not None:
            paths.append(self.startup / "Branchglance.lnk")
        return tuple(paths)

    def _owned_shortcut(self, path: Path) -> dict | None:
        found = self.adapter.read_shortcut(path)
        if found is None:
            return None
        description = found.get("description")
        # Pre-1.0 cutovers stamped "<MARKER> <32-hex id>" and set no AppUserModelID;
        # adopt only that exact form, still bound to this launcher and install root.
        legacy = isinstance(description, str) and _LEGACY_MARKER.fullmatch(description) is not None
        if ((description != MARKER and not legacy)
                or Path(found.get("target", "")).resolve() != self.launcher.resolve()
                or found.get("app_id") not in ((APP_USER_MODEL_ID, "", None) if legacy else (APP_USER_MODEL_ID,))
                or found.get("arguments", "") not in ("", "--background")
                or Path(found.get("workdir", "")).resolve() != self.root.resolve()):
            raise SafetyError(f"Foreign shortcut collision: {path}")
        return found

    def _uninstall_values(self) -> dict:
        launcher = str(self.launcher)
        uninstaller = str(self.uninstaller or self.root / "BranchglanceUninstall.exe")
        return {"DisplayName": "Branchglance", "DisplayVersion": self.version,
            "InstallLocation": str(self.root), "UninstallString": f'"{uninstaller}" --uninstall',
            "DisplayIcon": launcher, "Publisher": "Branchglance", "NoModify": 1, "NoRepair": 1}

    def snapshot(self) -> dict:
        shortcuts = {}
        for path in self.shortcut_paths:
            shortcuts[str(path)] = self._owned_shortcut(path)
        registry = self.adapter.read_uninstall()
        typed = getattr(self.adapter, "read_uninstall_typed", lambda: None)()
        registry_types = typed["types"] if typed else {}
        if registry is not None and registry.get("InstallLocation") != str(self.root):
            raise SafetyError("Foreign uninstall registry key collision")
        return {"shortcuts": shortcuts, "uninstall": registry, "uninstallTypes": registry_types,
                "uninstallManaged": self._uninstall_values()}

    def restore(self, snapshot: dict) -> None:
        for path in self.shortcut_paths:
            previous = snapshot.get("shortcuts", {}).get(str(path))
            current = self._owned_shortcut(path)
            if previous is None:
                if current is not None:
                    self.adapter.remove_shortcut(path)
            else:
                self.adapter.replace_shortcut(path, target=previous["target"],
                    arguments=previous["arguments"], workdir=previous["workdir"],
                    app_id=previous["app_id"], description=previous["description"])
        registry = snapshot.get("uninstall")
        managed = snapshot.get("uninstallManaged", self._uninstall_values())
        current = self.adapter.read_uninstall()
        if registry is None:
            if current is not None:
                self.adapter.remove_uninstall(expected=managed)
        else:
            # Restore only managed values that still equal the value this transaction wrote.
            if current:
                restore = {}
                for name, expected in managed.items():
                    if current.get(name) == expected:
                        if name in registry:
                            restore[name] = registry[name]
                        else:
                            self.adapter.remove_uninstall(expected={name: expected})
                if restore:
                    types = snapshot.get("uninstallTypes", {})
                    if types and hasattr(self.adapter, "read_uninstall_typed"):
                        self.adapter.write_uninstall(restore, types={name: types[name] for name in restore if name in types})
                    else:
                        self.adapter.write_uninstall(restore)

    def apply(self, *, startup_enabled: bool = False) -> None:
        self.snapshot()  # recheck all collision state immediately before writes
        launcher = str(self.launcher)
        uninstaller = str(self.uninstaller or self.root / "BranchglanceUninstall.exe")
        for index, path in enumerate(self.shortcut_paths):
            is_startup = path == (self.startup / "Branchglance.lnk" if self.startup is not None else None)
            if is_startup and not startup_enabled:
                continue
            self.adapter.replace_shortcut(path, target=launcher, arguments="--background" if is_startup else "", workdir=str(self.root),
                                          app_id=APP_USER_MODEL_ID, description=MARKER)
        self.adapter.write_uninstall(self._uninstall_values())

    def remove(self) -> None:
        for path in self.shortcut_paths:
            found = self.adapter.read_shortcut(path)
            if found is None:
                continue
            if (found.get("description") == MARKER
                    and Path(found.get("target", "")).resolve() == self.launcher.resolve()
                    and found.get("app_id") == APP_USER_MODEL_ID
                    and Path(found.get("workdir", "")).resolve() == self.root.resolve()):
                self.adapter.remove_shortcut(path)
        registry = self.adapter.read_uninstall()
        if registry is not None:
            managed = self._uninstall_values()
            self.adapter.remove_uninstall(expected={name: value for name, value in managed.items()
                                                     if registry.get(name) == value})
