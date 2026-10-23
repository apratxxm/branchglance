"""User-attended Branchglance cutover commands. Never terminates an existing process."""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT))

from branchglance_installer.app import Native, Paths
from branchglance_installer.core import SafetyError
from branchglance_installer.cutover import Cutover
from branchglance_installer.lifecycle import Lifecycle
from branchglance_installer.windows_cutover import WindowsCutoverAdapter, migrate_state_copy
from token_tracker import product
from token_tracker.product import local_app_data


def main(argv=None, *, native_factory=None, adapter_factory=None) -> int:
    parser = argparse.ArgumentParser(description="Plan, run, or roll back the user-attended Branchglance cutover")
    parser.add_argument("command", choices=("plan", "run", "recover", "rollback"))
    parser.add_argument("--old-pid", type=int, required=True,
                        help="inventoried pythonw PID; inspected read-only and never terminated")
    parser.add_argument("--old-target", type=Path, required=True)
    parser.add_argument("--old-workdir", type=Path, required=True)
    parser.add_argument("--desktop-shortcut", type=Path)
    args = parser.parse_args(argv)
    if args.command != "plan":
        probe_status, reason = product.appdata_probe()
        if probe_status != "not_redirected":
            raise SafetyError("Run Branchglance cutover from File Explorer or the Start menu, not from inside another app's terminal. "
                              f"AppData probe: {probe_status}; {reason or 'destination could not be verified'}")
    paths = Paths.for_user()
    desktop = paths.desktop or Path(os.environ.get("USERPROFILE", Path.home())) / "Desktop"
    old_link = (args.desktop_shortcut or desktop / "Token Tracker.lnk").resolve()
    renamed = old_link.with_name("Token Tracker (old).lnk")
    appdata = local_app_data()
    root = appdata / "Branchglance"
    adapter = (adapter_factory or WindowsCutoverAdapter)(args.old_pid, old_link, renamed, args.old_target.resolve(),
                                                         args.old_workdir.resolve(), root)
    operation = Cutover(adapter, appdata / "TokenTracker", root, args.old_target.resolve(),
                        args.old_workdir.resolve(), migrate_state_copy,
                        replace=getattr(getattr(adapter, "file_system", None), "replace", None) or os.replace,
                        desktop_path=paths.desktop)
    if args.command == "plan":
        result = operation.plan()
    else:
        native = (native_factory or Native.real)(paths)
        def recover_while_locked():
            adapter.instance_mutex_held = True
            return operation.recover()
        with Lifecycle(native.mutex, native.control, native.processes, recover_while_locked) as lifecycle:
            adapter.instance_mutex_held = True
            if args.command == "run":
                def release_instance_for_launch():
                    lifecycle.release_instance()
                    adapter.instance_mutex_held = False
                result = operation.run(release_instance=release_instance_for_launch)
            elif args.command == "recover":
                result = operation.recover()
            else:
                result = operation.rollback()
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
