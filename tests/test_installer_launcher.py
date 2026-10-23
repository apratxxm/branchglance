"""Native launcher (scripts/launcher/Branchglance.cs) compiled with the in-box csc.exe into a temp dir."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
CS = ROOT / "scripts" / "launcher" / "Branchglance.cs"
CSC = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Microsoft.NET" / "Framework64" / "v4.0.30319" / "csc.exe"
REFERENCES = ["/reference:System.Web.Extensions.dll", "/reference:System.Windows.Forms.dll"]

HARNESS = r'''using System; using System.IO; using System.Linq; class Harness { static int Main(string[] a) { try {
    if (a[0] == "appdata") { long ms; string status = BranchglanceLauncher.ProbeAppData(a[1], out ms);
        Console.Write(status + "|" + ms.ToString(System.Globalization.CultureInfo.InvariantCulture));
        return status == "redirected" ? 10 : status == "unknown" ? 11 : 0; }
    if (a[0] == "appdata-simulate") { long ms; string status = BranchglanceLauncher.ProbeAppData(a[1], out ms, a[2]);
        Console.Write(status + "|" + ms.ToString(System.Globalization.CultureInfo.InvariantCulture));
        return status == "redirected" ? 10 : status == "unknown" ? 11 : 0; }
    if (a[0] == "request-gate") {
        Directory.CreateDirectory(a[1]); string id = Guid.NewGuid().ToString("N"); string file = Path.Combine(a[1], ".relaunch-" + id + ".json");
        long expires = (long)(DateTime.UtcNow - new DateTime(1970,1,1)).TotalSeconds + 30;
        File.WriteAllText(file, "{\"schemaVersion\":1,\"id\":\"" + id + "\",\"argv\":[\"--background\"],\"hops\":1,\"created\":" + DateTime.UtcNow.Ticks + ",\"expires\":" + expires + ",\"guard\":true}");
        var request = BranchglanceLauncher.TryConsumeRelaunchRequest(a[1], a[2]);
        Console.Write((request == null ? "preserve" : "consume") + "|" + File.Exists(file) + "|" + (request == null ? "no-appdata-ok" : "appdata-ok"));
        return 0; }
    if (a[0] == "mac") { Console.Write(BranchglanceLauncher.ControlMac(a[1], a[2], a.Skip(3).ToArray())); return 0; }
    if (a[0] == "ask") { Console.Write(BranchglanceLauncher.AskMaintenanceExit(a[1], int.Parse(a[2])) ? "true" : "false"); return 0; }
    if (a[0] == "hook") { BranchglanceLauncher.HookDeadlineMs = int.Parse(a[1]);
        return BranchglanceLauncher.RunHook(a[2], new[] { "--claude-hook" }.Concat(a.Skip(3)).ToArray()); }
    if (a[0] == "hookdiag") { BranchglanceLauncher.HookDeadlineMs = int.Parse(a[1]);
        BranchglanceLauncher.RunHook(a[2], new[] { "--claude-hook" }.Concat(a.Skip(4)).ToArray());
        File.WriteAllText(a[3], BranchglanceLauncher.HookDiagnosticCategory); return 0; }
    if (a[0] == "hookresolve") {
        BranchglanceLauncher.ResolveHookRelease(a[1], a[2]);  // warm-up (JIT)
        var watch = System.Diagnostics.Stopwatch.StartNew();
        string release = BranchglanceLauncher.ResolveHookRelease(a[1], a[2]);
        Console.Write(release + "|" + watch.Elapsed.TotalMilliseconds.ToString(System.Globalization.CultureInfo.InvariantCulture));
        return 0; }
    Console.Write(BranchglanceLauncher.ResolveRelease(a[0], a[1])); return 0;
    } catch(Exception e) { Console.Error.WriteLine(e.ToString()); return 1; } } }'''

# A stand-in hook entry that echoes raw bytes: args as UTF-8, then '|', then stdin verbatim.
ECHO = r'''using System; using System.IO; using System.Text; using System.Diagnostics; class Echo { static int Main(string[] a) {
    if (Array.IndexOf(a, "--sleep") >= 0) System.Threading.Thread.Sleep(20000);
    int spawn = Array.IndexOf(a, "--spawn-blocker");
    if (spawn >= 0) { Process.Start(new ProcessStartInfo(a[spawn+1]) { UseShellExecute=false, CreateNoWindow=true }); System.Threading.Thread.Sleep(20000); return 0; }
    var input = new MemoryStream(); Console.OpenStandardInput().CopyTo(input);
    var output = Console.OpenStandardOutput();
    byte[] head = new UTF8Encoding(false).GetBytes(String.Join(",", a) + "|");
    output.Write(head, 0, head.Length); input.WriteTo(output); output.Flush();
    byte[] err = new byte[] { 0x68, 0x6F, 0x6F, 0x6B, 0xC3, 0xA9, 0xFF };  // "hook" + U+00E9 + invalid byte
    var error = Console.OpenStandardError(); error.Write(err, 0, err.Length); error.Flush();
    if (Array.IndexOf(a, "--large") >= 0) { var block = new byte[65536]; for (int i=0;i<256;i++) output.Write(block,0,block.Length); output.Flush(); }
    return 3; } }'''
BLOCKER = r'''using System.Threading; class Blocker { static void Main() { Thread.Sleep(30000); } }'''


def _csc(*args):
    subprocess.run([str(CSC), "/nologo", *args], check=True, capture_output=True)


@pytest.fixture(scope="module")
def launcher(tmp_path_factory):
    if not CSC.is_file():
        pytest.skip("in-box .NET Framework csc.exe is unavailable")
    folder = tmp_path_factory.mktemp("branchglance-launcher")
    exe = folder / "Branchglance.exe"
    _csc("/target:exe", "/out:" + str(exe), *REFERENCES, str(CS))
    (folder / "Harness.cs").write_text(HARNESS, encoding="utf-8")
    harness_exe = folder / "Harness.exe"
    _csc("/target:exe", "/out:" + str(harness_exe), *REFERENCES, "/reference:" + str(exe), str(folder / "Harness.cs"))
    (folder / "Echo.cs").write_text(ECHO, encoding="utf-8")
    _csc("/target:exe", "/out:" + str(folder / "Echo.exe"), str(folder / "Echo.cs"))
    (folder / "Blocker.cs").write_text(BLOCKER, encoding="utf-8")
    _csc("/target:exe", "/out:" + str(folder / "Blocker.exe"), str(folder / "Blocker.cs"))
    return folder, harness_exe


def _release(root, version, content):
    folder = root / "releases" / version
    folder.mkdir(parents=True)
    app = folder / "BranchglanceApp.exe"
    app.write_bytes(content)
    manifest = {"schemaVersion": 1, "product": "Branchglance", "version": version,
                "buildId": version.split("+")[-1], "files": {"BranchglanceApp.exe": hashlib.sha256(content).hexdigest()}}
    (folder / "release-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return folder


def _pointer(path, current, previous=None):
    path.write_text(json.dumps({"schemaVersion": 1,
        "current": {"version": current, "path": "C:\\evil\\absolute paths are ignored"},
        "previous": {"version": previous} if previous else None}), encoding="utf-8")


def _probe(harness, args):
    return subprocess.run([str(harness), *args], capture_output=True, text=True, timeout=20)


def test_csharp_appdata_probe_scans_more_than_128_packages(launcher, tmp_path):
    _folder, harness = launcher
    local = tmp_path / "Local"
    for index in range(150):
        (local / "Packages" / f"Package{index:03d}" / "LocalCache" / "Local").mkdir(parents=True)
    result = _probe(harness, ["appdata", str(local)])
    assert result.returncode == 0 and result.stdout.startswith("not_redirected|")


def test_csharp_appdata_probe_keeps_unrelated_junction_untouched(launcher, tmp_path):
    _folder, harness = launcher
    local = tmp_path / "Local"
    package = local / "Packages" / "Junctioned"
    package.mkdir(parents=True)
    target = tmp_path / "external-cache"
    branch_root = target / "Local" / "Branchglance"
    branch_root.mkdir(parents=True)
    sentinel = branch_root / "keep.txt"
    sentinel.write_text("untouched", encoding="utf-8")
    link = package / "LocalCache"
    made = subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, text=True)
    if made.returncode:
        pytest.skip("junction creation unavailable")
    result = _probe(harness, ["appdata", str(local)])
    assert result.returncode == 0 and result.stdout.startswith("not_redirected|")
    assert sentinel.read_text(encoding="utf-8") == "untouched"
    assert not list(branch_root.glob(".branchglance-probe-*"))


def test_csharp_appdata_probe_detects_and_cleans_its_marker_through_junction(launcher, tmp_path):
    _folder, harness = launcher
    local = tmp_path / "Local"
    package = local / "Packages" / "Junctioned"
    package.mkdir(parents=True)
    target = tmp_path / "external-cache"
    branch_root = target / "Local" / "Branchglance"
    branch_root.mkdir(parents=True)
    sentinel = branch_root / "keep.txt"
    sentinel.write_text("untouched", encoding="utf-8")
    link = package / "LocalCache"
    made = subprocess.run(["cmd.exe", "/c", "mklink", "/J", str(link), str(target)], capture_output=True, text=True)
    if made.returncode:
        pytest.skip("junction creation unavailable")
    redirected_alias = package / "LocalCache" / "Local" / "Branchglance"
    result = _probe(harness, ["appdata-simulate", str(local), str(redirected_alias)])
    assert result.returncode == 10 and result.stdout.startswith("redirected|")
    assert sentinel.read_text(encoding="utf-8") == "untouched"
    assert not list(branch_root.glob(".branchglance-probe-*"))


def test_redirected_bare_launcher_preserves_concurrent_relaunch_request(launcher, tmp_path):
    _folder, harness = launcher
    redirected_root = tmp_path / "redirected"
    result = _probe(harness, ["request-gate", str(redirected_root), "redirected"])
    assert result.returncode == 0 and result.stdout == "preserve|True|no-appdata-ok"
    unknown_root = tmp_path / "unknown"
    unknown = _probe(harness, ["request-gate", str(unknown_root), "unknown"])
    assert unknown.returncode == 0 and unknown.stdout == "preserve|True|no-appdata-ok"
    clean_root = tmp_path / "clean"
    clean = _probe(harness, ["request-gate", str(clean_root), "not_redirected"])
    assert clean.returncode == 0 and clean.stdout == "consume|False|appdata-ok"


def test_csharp_appdata_probe_detects_redirected_probe_in_plain_package(launcher, tmp_path):
    _folder, harness = launcher
    local = tmp_path / "Local"
    package_root = local / "Packages" / "Plain" / "LocalCache" / "Local" / "Branchglance"
    package_root.mkdir(parents=True)
    result = _probe(harness, ["appdata-simulate", str(local), str(package_root)])
    assert result.returncode == 10 and result.stdout.startswith("redirected|")
    assert not list(package_root.glob(".branchglance-probe-*"))


def test_launcher_resolves_current_release_by_manifest_and_version(launcher, tmp_path):
    folder, harness = launcher
    current = "1.0.0+abc"
    release = _release(tmp_path, current, b"fake executable payload")
    pointer = tmp_path / "current.json"
    _pointer(pointer, current)
    result = subprocess.run([str(harness), str(tmp_path), str(pointer)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert str(release) in result.stdout


def test_launcher_falls_back_when_current_manifest_is_missing(launcher, tmp_path):
    _folder, harness = launcher
    previous = "1.0.0+old"
    expected = _release(tmp_path, previous, b"previous app")
    pointer = tmp_path / "current.json"
    _pointer(pointer, "1.0.1+missing", previous)
    result = subprocess.run([str(harness), str(tmp_path), str(pointer)], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert Path(result.stdout) == expected


def test_launcher_rejects_tampered_release_and_missing_previous(launcher, tmp_path):
    _folder, harness = launcher
    version = "1.2.3+tampered"
    _release(tmp_path, version, b"clean")
    (tmp_path / "releases" / version / "BranchglanceApp.exe").write_bytes(b"foreign")
    pointer = tmp_path / "current.json"
    _pointer(pointer, version)
    result = subprocess.run([str(harness), str(tmp_path), str(pointer)], capture_output=True, text=True, timeout=10)
    assert result.returncode != 0
    assert "Current release is invalid" in result.stderr


@pytest.mark.parametrize("version", ["1.0.0+..", "1.0.0+a..b", "..\\..\\x"])
def test_launcher_rejects_versions_that_could_escape(launcher, tmp_path, version):
    _folder, harness = launcher
    pointer = tmp_path / "current.json"
    _pointer(pointer, version)
    result = subprocess.run([str(harness), str(tmp_path), str(pointer)], capture_output=True, text=True, timeout=10)
    assert result.returncode != 0


# --- hook mode -----------------------------------------------------------------------------------

def _hook_release(root, echo_exe, version="1.0.0+hook", extra_files=0, blocker_exe=None):
    """A release as the installer leaves it: build manifest plus install stamps (size, FILETIME)."""
    from branchglance_installer.core import write_manifest, write_stamps
    folder = root / "releases" / version
    folder.mkdir(parents=True)
    (folder / "BranchglanceApp.exe").write_bytes(b"app")
    shutil.copyfile(echo_exe, folder / "BranchglanceHook.exe")
    if blocker_exe:
        shutil.copyfile(blocker_exe, folder / "BranchglanceBlocker.exe")
    for index in range(extra_files):
        (folder / "_internal").mkdir(exist_ok=True)
        (folder / "_internal" / f"lib{index}.pyd").write_bytes(os.urandom(64))
    write_manifest(folder, *version.split("+"))
    write_stamps(folder)
    (root / "current.json").write_text(json.dumps({"schemaVersion": 1, "current": {"version": version}}),
                                       encoding="utf-8")
    return folder


EVENT = ('{"tool":"Edit","text":"h\u00e9llo \u2713 \u65e5\u672c \U0001F600"}'.encode("utf-8")
         + b"\n\xff\xfe\x80 invalid utf-8 \xc3(")


def test_hook_mode_relays_stdio_bytes_exactly_and_always_exits_zero(launcher, tmp_path):
    folder, harness = launcher
    _hook_release(tmp_path, folder / "Echo.exe")
    result = subprocess.run([str(harness), "hook", "9000", str(tmp_path), "PostToolUse", "x y", "\u00fcn\u00ef"],
                            input=EVENT, capture_output=True, timeout=30)  # bytes: no code page involved
    assert result.returncode == 0  # the hook entry exited 3; Claude Code still sees success
    assert result.stdout == "--claude-hook,PostToolUse,x y,\u00fcn\u00ef|".encode("utf-8") + EVENT
    assert result.stderr == b"hook\xc3\xa9\xff"


def test_hook_mode_stops_waiting_at_its_deadline(launcher, tmp_path):
    folder, harness = launcher
    _hook_release(tmp_path, folder / "Echo.exe")
    started = time.monotonic()
    result = subprocess.run([str(harness), "hook", "500", str(tmp_path), "--sleep"],
                            input=b"", capture_output=True, timeout=30)
    assert result.returncode == 0 and time.monotonic() - started < 10
    assert not result.stdout and b"diagnostic: timeout" in result.stderr


def test_hook_mode_without_a_valid_release_exits_zero_quietly(launcher, tmp_path):
    _folder, harness = launcher
    diagnostic = tmp_path / "diagnostic.txt"
    result = subprocess.run([str(harness), "hookdiag", "9000", str(tmp_path), str(diagnostic)], input=b"{}",
                            capture_output=True, timeout=30)
    assert result.returncode == 0 and result.stdout == b""
    assert diagnostic.read_text() == "missing-release"


@pytest.mark.parametrize("change", ["size", "mtime", "no-stamps", "hook-unstamped", "missing-file"])
def test_hook_mode_light_check_refuses_changed_release(launcher, tmp_path, change):
    folder, harness = launcher
    release = _hook_release(tmp_path, folder / "Echo.exe", extra_files=2)
    target = release / "_internal" / "lib0.pyd"
    manifest_path = release / "release-manifest.json"
    manifest = json.loads(manifest_path.read_text())
    if change == "size":
        target.write_bytes(target.read_bytes() + b"x")
        os.utime(target, ns=(target.stat().st_atime_ns, (manifest["stamps"]["_internal/lib0.pyd"][1]
                                                          - 116444736000000000) * 100))
    elif change == "mtime":
        info = target.stat()
        os.utime(target, ns=(info.st_atime_ns, info.st_mtime_ns + 10_000_000_000))
    elif change == "no-stamps":
        del manifest["stamps"]
        manifest_path.write_text(json.dumps(manifest))
    elif change == "hook-unstamped":
        del manifest["stamps"]["BranchglanceHook.exe"]
        manifest_path.write_text(json.dumps(manifest))
    else:
        target.unlink()
    diagnostic = tmp_path / "diagnostic.txt"
    result = subprocess.run([str(harness), "hookdiag", "9000", str(tmp_path), str(diagnostic)], input=b"{}",
                            capture_output=True, timeout=30)
    assert result.returncode == 0 and result.stdout == b""
    assert diagnostic.read_text() == "stamp-refusal"


def test_hook_mode_rejects_reparse_point_in_a_stamped_directory(launcher, tmp_path):
    folder, harness = launcher
    release = _hook_release(tmp_path, folder / "Echo.exe", extra_files=1)
    internal = release / "_internal"
    outside = tmp_path / "outside"
    internal.rename(outside)
    try:
        os.symlink(outside, internal, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:
        pytest.skip(f"Directory symlinks are unavailable: {exc}")
    result = subprocess.run([str(harness), "hook", "9000", str(tmp_path)], input=b"{}",
                            capture_output=True, timeout=30)
    assert result.returncode == 0 and result.stdout == b""


def test_hook_mode_drains_large_output_with_a_slow_consumer(launcher, tmp_path):
    folder, harness = launcher
    _hook_release(tmp_path, folder / "Echo.exe")
    process = subprocess.Popen([str(harness), "hook", "9000", str(tmp_path), "--large"],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    process.stdin.close()
    time.sleep(0.2)
    collected = bytearray()
    while True:
        block = process.stdout.read(65536)
        if not block:
            break
        collected.extend(block)
        time.sleep(0.001)
    stderr = process.stderr.read()
    assert process.wait(timeout=10) == 0
    assert len(collected) == len(b"--claude-hook,--large|") + 16 * 1024 * 1024
    assert stderr == b"hook\xc3\xa9\xff"


def test_hook_job_timeout_unblocks_large_stdin_producer_when_descendant_holds_pipe(launcher, tmp_path):
    folder, harness = launcher
    release = _hook_release(tmp_path, folder / "Echo.exe", blocker_exe=folder / "Blocker.exe")
    started = time.monotonic()
    process = subprocess.Popen([str(harness), "hook", "600", str(tmp_path), "--spawn-blocker",
                                str(release / "BranchglanceBlocker.exe")], stdin=subprocess.PIPE,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    outcome = []
    def produce():
        try:
            block = b"x" * (1024 * 1024)
            for _ in range(128):
                process.stdin.write(block)
                process.stdin.flush()
        except OSError as exc:
            outcome.append(exc)
        finally:
            try: process.stdin.close()
            except OSError: pass
    writer = threading.Thread(target=produce, daemon=True)
    writer.start()
    assert process.wait(timeout=6) == 0
    writer.join(timeout=3)
    assert not writer.is_alive(), "producer remained blocked after the launcher job closed"
    assert time.monotonic() - started < 6


def test_hook_mode_light_check_is_fast_and_does_not_hash(launcher, tmp_path, record_property):
    folder, harness = launcher
    release = _hook_release(tmp_path, folder / "Echo.exe", extra_files=300)
    # Same size and mtime but different bytes: hook mode accepts it (stamps only, by decision);
    # full launch verification still rejects it.
    target = release / "_internal" / "lib0.pyd"
    info = target.stat()
    target.write_bytes(bytes(len(target.read_bytes())))
    os.utime(target, ns=(info.st_atime_ns, info.st_mtime_ns))
    result = subprocess.run([str(harness), "hookresolve", str(tmp_path), str(tmp_path / "current.json")],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    resolved, elapsed = result.stdout.rsplit("|", 1)
    assert Path(resolved) == release and float(elapsed) < 150
    record_property("hook_check_ms", float(elapsed))
    full = subprocess.run([str(harness), str(tmp_path), str(tmp_path / "current.json")], capture_output=True, text=True, timeout=10)
    assert full.returncode != 0 and "hash mismatch" in full.stderr


# --- control protocol ----------------------------------------------------------------------------

def test_launcher_control_mac_matches_python(launcher):
    from token_tracker import product
    _folder, harness = launcher
    nonce, cc, sc = "a1" * 32, "b2" * 32, "c3" * 32
    for role, parts in (("server", [cc, sc]), ("client", [cc, sc, "maintenance-exit"])):
        result = subprocess.run([str(harness), "mac", nonce, role, *parts], capture_output=True, text=True, timeout=10)
        assert result.stdout == product.control_mac(nonce, role, *parts)


def test_launcher_maintenance_exit_authenticates_against_live_app(launcher, monkeypatch):
    from token_tracker import widget
    _folder, harness = launcher
    monkeypatch.setattr(widget, "CONTROL_PORT", 0)
    app = widget.WidgetApp()
    quit_called = threading.Event()
    app.quit = lambda *_: quit_called.set()
    app.start_control()
    try:
        port = app.control.getsockname()[1]
        result = subprocess.run([str(harness), "ask", str(app.nonce_path), str(port)],
                                capture_output=True, text=True, timeout=10)
        assert result.stdout == "true" and quit_called.wait(3) and app.maintenance_exit_requested
    finally:
        app.quitting = True
        app.control.close()


def test_launcher_maintenance_exit_never_sends_nonce_to_forged_listener(launcher, tmp_path):
    _folder, harness = launcher
    nonce = "d4" * 32
    nonce_path = tmp_path / "control.nonce"
    nonce_path.write_text(nonce)
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0)); listener.listen(1)
    received = []

    def serve():
        conn, _ = listener.accept()
        with conn:
            conn.settimeout(3)
            data = b""
            try:
                while True:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                    if data.count(b"\n") == 1 and not received:
                        received.append(data)
                        conn.sendall(json.dumps({"product": "Branchglance", "build": "b", "pid": 7,
                                                 "challenge": "e5" * 32, "proof": "0" * 64}).encode() + b"\n")
            except OSError:
                pass
            received.append(data)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        result = subprocess.run([str(harness), "ask", str(nonce_path), str(listener.getsockname()[1])],
                                capture_output=True, text=True, timeout=10)
        thread.join(5)
        assert result.stdout == "false"
        everything = received[-1].decode()
        assert everything.startswith("hello ") and everything.count("\n") == 1 and nonce not in everything
    finally:
        listener.close()


def test_launcher_has_no_install_or_uninstall_code_paths():
    source = CS.read_text(encoding="utf-8")
    for retired in ("Install(", "UninstallWorker", "Recover(", "Registry.", "payload.zip", "MoveFileEx"):
        assert retired not in source
