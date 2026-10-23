"""PyInstaller onedir spec; source/output are isolated by packaging/build.py."""
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

SOURCE = Path(os.environ["BRANCHGLANCE_SOURCE"]).resolve()
STATIC = SOURCE / "token_tracker" / "static"
ICON = Path(os.environ["BRANCHGLANCE_ICON"]).resolve()
ENTRY = SOURCE / "branchglance_entry.py"
hidden = ["token_tracker.widget", "webview.platforms.winforms", "webview.platforms.edgechromium",
          "pystray._win32", "clr", "clr_loader", "zstandard"]
if (SOURCE / "token_tracker" / "claude_hook.py").is_file():
    hidden.append("token_tracker.claude_hook")

a = Analysis([str(ENTRY)], pathex=[str(SOURCE)], binaries=[],
             datas=[(str(STATIC), "token_tracker/static"), (str(ICON), "assets/brand")], hiddenimports=hidden,
             hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=["setuptools", "pkg_resources"],
             noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="BranchglanceApp", debug=False,
          bootloader_ignore_signals=False, strip=False, upx=False, console=False, icon=str(ICON))
# Console entry for Claude Code hooks (stdin/stdout relayed by the launcher); same
# script and shared libraries, so it adds only a small bootloader exe.
hook_exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="BranchglanceHook", debug=False,
               bootloader_ignore_signals=False, strip=False, upx=False, console=True, icon=str(ICON))
COLLECT(exe, hook_exe, a.binaries, a.datas, strip=False, upx=False, name="BranchglanceApp")
