"""PyInstaller onefile spec for BranchglanceSetup.exe / BranchglanceUninstall.exe.

Both are the same Python installer core (scripts/branchglance_installer); the
setup role also embeds the payload folder (launcher, uninstaller, release).
Driven by packaging/build.py through environment variables.
"""
import os
from pathlib import Path

SOURCE = Path(os.environ["BRANCHGLANCE_SOURCE"]).resolve()
ICON = Path(os.environ["BRANCHGLANCE_ICON"]).resolve()
VERSION_INFO = Path(os.environ["BRANCHGLANCE_VERSION_INFO"]).resolve()
ROLE = os.environ["BRANCHGLANCE_INSTALLER_ROLE"]  # "setup" | "uninstall"
PAYLOAD = os.environ.get("BRANCHGLANCE_PAYLOAD")
NAME = {"setup": "BranchglanceSetup", "uninstall": "BranchglanceUninstall"}[ROLE]
ENTRY = SOURCE / f"branchglance_{ROLE}_entry.py"
datas = [(PAYLOAD, "payload")] if ROLE == "setup" else []

a = Analysis([str(ENTRY)], pathex=[str(SOURCE), str(SOURCE / "scripts")], binaries=[], datas=datas,
             hiddenimports=["branchglance_installer.windows", "winreg"], hookspath=[], hooksconfig={},
             runtime_hooks=[],
             # The installer never needs the app's GUI or .NET stacks.
             excludes=["setuptools", "pkg_resources", "webview", "pystray", "PIL", "clr", "clr_loader", "pythonnet",
                       "tkinter", "zstandard", "token_tracker.widget", "token_tracker.server"],
             noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, a.binaries, a.datas, [], name=NAME, debug=False, bootloader_ignore_signals=False,
          strip=False, upx=False, runtime_tmpdir=None, console=False, icon=str(ICON),
          version=str(VERSION_INFO))
