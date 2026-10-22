# Offline draft: CPython 3.10.11 x64 / PyInstaller 6.22.3.
# Place a source copy and both entry wrappers in ./source.
from pathlib import Path
import json
import hashlib
import time
ROOT = Path(SPECPATH)
SOURCE = ROOT / "source"
STATIC = SOURCE / "token_tracker" / "static"
manifest = {str(p.relative_to(STATIC)).replace("\\", "/"): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in STATIC.rglob("*") if p.is_file()}
(ROOT / "probe-assets.json").write_text(json.dumps(manifest), encoding="utf-8")
metrics = {}
HIDDEN = ["token_tracker.widget", "webview.platforms.winforms", "webview.platforms.edgechromium",
          "pystray._win32", "clr", "clr_loader", "zstandard"]
# Installed hooks collect webview/lib, pythonnet/runtime and clr_loader DLLs.
def bundle(name, entry, console, probe=False):
    started = time.perf_counter()
    data = [(str(STATIC), "token_tracker/static")]
    if probe:
        data.append((str(ROOT / "probe-assets.json"), "."))
    a = Analysis([str(SOURCE / entry)], pathex=[str(SOURCE)], binaries=[], datas=data,
                 hiddenimports=HIDDEN, hookspath=[], hooksconfig={}, runtime_hooks=[],
                 excludes=[], noarchive=False)
    pyz = PYZ(a.pure)
    exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name=name, debug=False,
              bootloader_ignore_signals=False, strip=False, upx=False, console=console)
    COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name=name)
    metrics[name] = {"build_seconds": round(time.perf_counter() - started, 3)}
    (ROOT / "build-metrics.json").write_text(json.dumps(metrics, indent=2), encoding="utf-8")
bundle("OrchestratorProbe", "frozen_probe.py", console=True, probe=True)
bundle("OrchestratorWidget", "widget_entry.py", console=False)
