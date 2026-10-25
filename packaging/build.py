"""Build an immutable Branchglance onedir release, the launcher, and the unsigned setup/uninstall programs.

Outputs (in a fresh folder outside the checkout):
  releases/<version>+<build>/   onedir app (BranchglanceApp.exe, BranchglanceHook.exe) + release-manifest.json
  Branchglance.exe              native launcher (in-box csc.exe)
  BranchglanceUninstall.exe     Python installer core, uninstall role (PyInstaller onefile)
  BranchglanceSetup.exe         Python installer core, setup role, payload embedded (onefile)
  BranchglanceSetup.exe.sha256
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.branchglance_installer.core import (read_json, validate_version, verify_manifest,
                                                write_manifest, write_stamps)  # noqa: E402
RELEASE_VERSION = "1.0.0"
SPEC = ROOT / "packaging" / "branchglance.draft.spec"
INSTALLER_SPEC = ROOT / "packaging" / "branchglance_installer.spec"
ICON = ROOT / "assets" / "brand" / "branchglance.ico"
LAUNCHER = ROOT / "scripts" / "launcher" / "Branchglance.cs"
# Signing hook point: SIGNING_HOOK(path) Authenticode-signs one binary in place. It is
# applied to every shipped executable before it is hashed into a manifest or embedded,
# and to the setup last. Unsigned (None) until a certificate is supplied.
SIGNING_HOOK = None
LAUNCHER_REFERENCES = ["/reference:System.Web.Extensions.dll", "/reference:System.Windows.Forms.dll"]


def file_hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _version_parts(version: str) -> tuple[int, int, int, int]:
    core = version.split("+", 1)[0].split("-", 1)[0]
    parts = [int(part) for part in core.split(".")]
    return (*parts, 0)


def _pyinstaller_version_info(version: str) -> str:
    """Generate PyInstaller's native VERSIONINFO resource for every Python exe."""
    file_version = _version_parts(version)
    quoted = json.dumps(version)
    quoted_file_version = json.dumps(".".join(map(str, file_version)))
    return f'''VSVersionInfo(
  ffi=FixedFileInfo(filevers={file_version!r}, prodvers={file_version!r}, mask=0x3f,
                    flags=0x0, OS=0x4, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'Branchglance'),
      StringStruct('FileDescription', 'Branchglance'),
      StringStruct('FileVersion', {quoted_file_version}),
      StringStruct('ProductName', 'Branchglance'),
      StringStruct('ProductVersion', {quoted}),
    ])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)\n'''


def _launcher_assembly_info(version: str) -> str:
    file_version = ".".join(map(str, _version_parts(version)))
    quoted = json.dumps(version)
    return f'''using System.Reflection;
[assembly: AssemblyTitle("Branchglance")]
[assembly: AssemblyProduct("Branchglance")]
[assembly: AssemblyCompany("Branchglance")]
[assembly: AssemblyDescription("Branchglance")]
[assembly: AssemblyVersion("{file_version}")]
[assembly: AssemblyFileVersion("{file_version}")]
[assembly: AssemblyInformationalVersion({quoted})]
'''


def csc_path() -> Path:
    return Path(os.environ.get("WINDIR", r"C:\Windows")) / "Microsoft.NET" / "Framework64" / "v4.0.30319" / "csc.exe"


def _sign(path: Path) -> None:
    if SIGNING_HOOK is not None:
        SIGNING_HOOK(path)


def _pyinstaller(py: Path, spec: Path, work: Path, name: str, env: dict) -> Path:
    dist = work / f"dist-{name}"
    subprocess.run([str(py), "-B", "-m", "PyInstaller", "--noconfirm", "--clean", "--distpath", str(dist),
                    "--workpath", str(work / f"pyinstaller-{name}"), str(spec)], cwd=ROOT, env=env, check=True)
    return dist


def build(output: Path, version: str, build_id: str, *, python: Path | None = None) -> dict:
    versioned = validate_version(f"{version}+{build_id}")
    output = output.resolve()
    if output == ROOT or ROOT in output.parents:
        raise RuntimeError("Build output must be outside the source checkout")
    output.mkdir(parents=True, exist_ok=True)
    releases = output / "releases"
    final = releases / versioned
    if final.exists():
        raise RuntimeError(f"Immutable release already exists: {final}")
    work = output / ".branchglance-build"
    if work.exists():
        raise RuntimeError(f"Build work directory already exists; choose a fresh output path: {work}")
    for name in ("Branchglance.exe", "BranchglanceUninstall.exe", "BranchglanceSetup.exe"):
        if (output / name).exists():
            raise RuntimeError(f"Output already contains {name}; choose a fresh output path")
    source = work / "source"
    source.mkdir(parents=True)
    shutil.copytree(ROOT / "token_tracker", source / "token_tracker",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copytree(ROOT / "scripts" / "branchglance_installer", source / "scripts" / "branchglance_installer",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (source / "token_tracker" / "_build.py").write_text(
        f'VERSION = {version!r}\nBUILD_ID = {build_id!r}\n', encoding="utf-8")
    version_info = work / "version_info.txt"
    version_info.write_text(_pyinstaller_version_info(version), encoding="utf-8")
    launcher_version_info = work / "BranchglanceVersion.cs"
    launcher_version_info.write_text(_launcher_assembly_info(version), encoding="utf-8")
    (source / "branchglance_entry.py").write_text(_entry_source(), encoding="utf-8")
    for role in ("setup", "uninstall"):
        (source / f"branchglance_{role}_entry.py").write_text(_installer_entry_source(role), encoding="utf-8")
    py = python or Path(sys.executable)
    env = os.environ.copy()
    env.update({"BRANCHGLANCE_SOURCE": str(source), "BRANCHGLANCE_ICON": str(ICON), "PYTHONDONTWRITEBYTECODE": "1",
                "PYINSTALLER_CONFIG_DIR": str(work / "pyinstaller-config"),
                "BRANCHGLANCE_VERSION_INFO": str(version_info)})
    env.pop("BRANCHGLANCE_BUILD", None)

    # 1. The app release (onedir), signed binaries, then the build manifest.
    started = time.perf_counter()
    dist = _pyinstaller(py, SPEC, work, "app", env)
    build_seconds = time.perf_counter() - started
    release_stage = work / "release-stage"
    shutil.copytree(dist / "BranchglanceApp", release_stage)
    for exe in ("BranchglanceApp.exe", "BranchglanceHook.exe"):
        _sign(release_stage / exe)
    release_manifest = write_manifest(release_stage, version, build_id)
    write_stamps(release_stage)
    releases.mkdir(parents=True, exist_ok=True)
    os.replace(release_stage, final)

    # 2. The native launcher.
    compiler = csc_path()
    if not compiler.is_file():
        raise FileNotFoundError(f"In-box C# compiler not found: {compiler}")
    launcher = output / "Branchglance.exe"
    subprocess.run([str(compiler), "/nologo", "/target:winexe", "/platform:x64", "/optimize+",
                    "/win32icon:" + str(ICON), "/out:" + str(launcher), *LAUNCHER_REFERENCES,
                    str(launcher_version_info), str(LAUNCHER)], check=True)
    _sign(launcher)

    # 3. The uninstaller (installer core, uninstall role).
    uninstall_dist = _pyinstaller(py, INSTALLER_SPEC, work, "uninstall",
                                  dict(env, BRANCHGLANCE_INSTALLER_ROLE="uninstall"))
    uninstaller = output / "BranchglanceUninstall.exe"
    shutil.copyfile(uninstall_dist / "BranchglanceUninstall.exe", uninstaller)
    _sign(uninstaller)

    # 4. The setup (installer core, setup role) with the payload folder embedded.
    payload = work / "payload"
    payload.mkdir()
    shutil.copyfile(launcher, payload / "Branchglance.exe")
    shutil.copyfile(uninstaller, payload / "BranchglanceUninstall.exe")
    shutil.copytree(final, payload / "release")
    setup_dist = _pyinstaller(py, INSTALLER_SPEC, work, "setup",
                              dict(env, BRANCHGLANCE_INSTALLER_ROLE="setup", BRANCHGLANCE_PAYLOAD=str(payload)))
    setup = output / "BranchglanceSetup.exe"
    shutil.copyfile(setup_dist / "BranchglanceSetup.exe", setup)
    _sign(setup)
    checksum = output / "BranchglanceSetup.exe.sha256"
    checksum.write_text(file_hash(setup) + "  BranchglanceSetup.exe\n", encoding="ascii")
    return {"version": versioned, "release": str(final), "manifestFiles": len(release_manifest["files"]),
            "bundleBytes": sum(p.stat().st_size for p in final.rglob("*") if p.is_file()),
            "buildSeconds": round(build_seconds, 3), "setup": str(setup), "setupBytes": setup.stat().st_size,
            "setupSha256": file_hash(setup), "uninstallerBytes": uninstaller.stat().st_size,
            "signed": SIGNING_HOOK is not None}


def build_setup_from_components(output: Path, components: Path, version: str, build_id: str,
                                *, python: Path | None = None) -> dict:
    """Package already-signed component binaries into a setup exe for CI signing.

    SignPath changes PE bytes. The release manifest must first be refreshed over
    those signatures, then this second pass embeds that verified tree. The final
    Setup executable is signed separately after this function returns.
    """
    versioned = validate_version(f"{version}+{build_id}")
    output = output.resolve()
    components = components.resolve()
    if output == ROOT or ROOT in output.parents:
        raise RuntimeError("Build output must be outside the source checkout")
    release = components / "releases" / versioned
    verify_manifest(release, expected_version=versioned)
    for name in ("Branchglance.exe", "BranchglanceUninstall.exe"):
        if not (components / name).is_file():
            raise RuntimeError(f"Signed component is missing: {name}")
    output.mkdir(parents=True, exist_ok=True)
    work = output / ".branchglance-build"
    if work.exists():
        raise RuntimeError(f"Build work directory already exists; choose a fresh output path: {work}")
    work.mkdir()
    source = work / "source"
    source.mkdir()
    shutil.copytree(ROOT / "token_tracker", source / "token_tracker",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copytree(ROOT / "scripts" / "branchglance_installer", source / "scripts" / "branchglance_installer",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (source / "token_tracker" / "_build.py").write_text(
        f"VERSION = {version!r}\nBUILD_ID = {build_id!r}\n", encoding="utf-8")
    (source / "branchglance_setup_entry.py").write_text(_installer_entry_source("setup"), encoding="utf-8")
    version_info = work / "version_info.txt"
    version_info.write_text(_pyinstaller_version_info(version), encoding="utf-8")
    payload = work / "payload"
    payload.mkdir()
    shutil.copyfile(components / "Branchglance.exe", payload / "Branchglance.exe")
    shutil.copyfile(components / "BranchglanceUninstall.exe", payload / "BranchglanceUninstall.exe")
    shutil.copytree(release, payload / "release")
    env = os.environ.copy()
    env.update({"BRANCHGLANCE_SOURCE": str(source), "BRANCHGLANCE_ICON": str(ICON),
                "BRANCHGLANCE_VERSION_INFO": str(version_info), "BRANCHGLANCE_INSTALLER_ROLE": "setup",
                "BRANCHGLANCE_PAYLOAD": str(payload), "PYTHONDONTWRITEBYTECODE": "1",
                "PYINSTALLER_CONFIG_DIR": str(work / "pyinstaller-config")})
    py = python or Path(sys.executable)
    setup_dist = _pyinstaller(py, INSTALLER_SPEC, work, "setup", env)
    setup = output / "BranchglanceSetup.exe"
    shutil.copyfile(setup_dist / "BranchglanceSetup.exe", setup)
    checksum = output / "BranchglanceSetup.exe.sha256"
    checksum.write_text(file_hash(setup) + "  BranchglanceSetup.exe\n", encoding="ascii")
    return {"setup": str(setup), "setupBytes": setup.stat().st_size,
            "setupSha256": file_hash(setup), "version": versioned, "signedComponents": str(components)}


def _installer_entry_source(role: str) -> str:
    return (f"import sys\nfrom branchglance_installer.app import main\n"
            f"raise SystemExit(main(sys.argv[1:], role={role!r}))\n")


def _entry_source() -> str:
    # Imports stay inside each mode: diagnostic and hook modes never import the
    # widget, so they open no ports or windows.
    return '''import importlib, json, os, sys

def self_test(report=None):
    result = {"ok": False}
    try:
        import pythonnet
        pythonnet.load("netfx")
        import clr
        from webview.util import interop_dll_path
        clr.AddReference("System.Windows.Forms")
        clr.AddReference(interop_dll_path("Microsoft.Web.WebView2.Core.dll"))
        clr.AddReference(interop_dll_path("Microsoft.Web.WebView2.WinForms.dll"))
        from Microsoft.Web.WebView2.Core import CoreWebView2Environment
        version = str(CoreWebView2Environment.GetAvailableBrowserVersionString(None) or "")
        result = {"ok": bool(version), "webview2": version}
    except Exception as exc:
        result = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
    text = json.dumps(result)
    if report:  # windowed builds have no usable stdout; the installer reads this file
        with open(report, "w", encoding="utf-8") as stream:
            stream.write(text)
    if sys.stdout is not None:
        print(text)
    return 0 if result["ok"] else 1

def hook():
    # token_tracker.claude_hook.run() owns the contract: 5 s deadline, always exits 0.
    try:
        module = importlib.import_module("token_tracker.claude_hook")
        module.run()
    except Exception as exc:
        if sys.stderr is not None:
            print(f"Branchglance hook unavailable: {exc}", file=sys.stderr)
    return 0

def link_codex():
    # Receipts only associate an execution with a Claude session; they never
    # influence execution status or provider policy.
    try:
        from token_tracker.agent_links import write_receipt
        args = sys.argv[2:]
        thread = args[0] if args else ""
        pid = int(args[args.index("--codex-pid") + 1]) if "--codex-pid" in args else 0
        write_receipt(thread, pid)
    except Exception:
        pass
    return 0

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--self-test":
        raise SystemExit(self_test(sys.argv[2] if len(sys.argv) > 2 else None))
    if len(sys.argv) > 1 and sys.argv[1] == "--claude-hook":
        raise SystemExit(hook())
    if len(sys.argv) > 1 and sys.argv[1] == "--link-codex":
        raise SystemExit(link_codex())
    if len(sys.argv) > 1 and sys.argv[1] == "--appdata-probe":
        from token_tracker.product import appdata_probe
        status, _reason = appdata_probe()
        raise SystemExit(10 if status == "redirected" else 11 if status == "unknown" else 0)
    from token_tracker.widget import main as widget_main
    widget_main()
'''


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--version", default=RELEASE_VERSION)
    parser.add_argument("--build-id", default=time.strftime("%Y%m%d%H%M%S"))
    parser.add_argument("--python", type=Path, help="prepared offline build interpreter")
    parser.add_argument("--setup-from-components", type=Path,
                        help="package finalized signed binaries into Setup.exe")
    args = parser.parse_args(argv)
    if args.setup_from_components is not None:
        print(json.dumps(build_setup_from_components(args.output, args.setup_from_components, args.version,
                                                     args.build_id, python=args.python), indent=2))
        return 0
    print(json.dumps(build(args.output, args.version, args.build_id, python=args.python), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
