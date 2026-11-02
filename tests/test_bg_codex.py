"""Black-box checks for the native console wrapper, using only temporary executables."""
import json
import os
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
CSC = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
pytestmark = pytest.mark.skipif(os.name != "nt" or not CSC.is_file(), reason="Windows .NET compiler required")


def compile_cs(source, output, *args):
    subprocess.run([str(CSC), "/nologo", "/target:exe", "/platform:x64", f"/out:{output}", *args, str(source)], check=True)


@pytest.fixture
def executables(tmp_path):
    wrapper_dir = tmp_path / "wrapper"
    bin_dir = tmp_path / "bin"
    cwd = tmp_path / "cwd"
    for path in (wrapper_dir, bin_dir, cwd):
        path.mkdir()
    wrapper = wrapper_dir / "bg-codex.exe"
    harness = tmp_path / "harness.cs"
    harness.write_text('using System; class Harness { static int Main(string[] a) { return BgCodex.Run(a, new BgCodex.Target { Executable = Environment.GetEnvironmentVariable("FAKE_CODEX") }); } }', encoding="utf-8")
    compile_cs(ROOT / "scripts/launcher/BgCodex.cs", wrapper, "/reference:System.Web.Extensions.dll", "/main:Harness", str(harness))
    source = tmp_path / "fake.cs"
    source.write_text(r'''using System; using System.IO; using System.Text; using System.Threading;
class Fake { static int Main(string[] a) {
 File.WriteAllText(Environment.GetEnvironmentVariable("ARGS_PATH"), String.Join("\n", Array.ConvertAll(a, x => Convert.ToBase64String(Encoding.UTF8.GetBytes(x)))));
 var o=Console.OpenStandardOutput(); var e=Console.OpenStandardError();
 byte[] b=Convert.FromBase64String(Environment.GetEnvironmentVariable("OUT_BYTES"));
 foreach(byte v in b) { o.WriteByte(v); o.Flush(); }
 b=Convert.FromBase64String(Environment.GetEnvironmentVariable("ERR_BYTES")); e.Write(b,0,b.Length); e.Flush();
 Thread.Sleep(700); return 37;
} }''', encoding="utf-8")
    compile_cs(source, bin_dir / "codex.exe")
    helper = tmp_path / "helper.cs"
    helper.write_text(r'''using System; using System.IO;
class Helper { static int Main(string[] a) { if (Environment.GetEnvironmentVariable("RECEIPT_DELAY") == "1") System.Threading.Thread.Sleep(5000); File.WriteAllLines(Environment.GetEnvironmentVariable("RECEIPT_PATH"), a); return 4; } }''', encoding="utf-8")
    compile_cs(helper, wrapper_dir / "Branchglance.exe")
    return wrapper, bin_dir, cwd


def invoke(executables, tmp_path, args, stdout):
    import base64
    wrapper, bin_dir, cwd = executables
    env = os.environ.copy()
    env.update(PATH=str(bin_dir), LOCALAPPDATA=str(tmp_path / "missing"), USERPROFILE=str(tmp_path / "missing"),
               FAKE_CODEX=str(bin_dir / "codex.exe"),
               ARGS_PATH=str(tmp_path / "args"), RECEIPT_PATH=str(tmp_path / "receipt"),
               OUT_BYTES=base64.b64encode(stdout).decode(), ERR_BYTES=base64.b64encode(b"err\x00\xff").decode())
    return subprocess.run([str(wrapper), *args], cwd=cwd, env=env, capture_output=True, timeout=15)


def test_arguments_and_raw_streams_are_preserved(executables, tmp_path):
    import base64
    args = ["exec", "--json", "", 'a "quoted" argument', "unicode \u2603", "&|<>^", "backslash\\"]
    stdout = b'plain\x00\xff\r\n'
    result = invoke(executables, tmp_path, args, stdout)
    assert result.returncode == 37
    assert result.stdout == stdout and result.stderr == b"err\x00\xff"
    assert [base64.b64decode(line).decode() for line in (tmp_path / "args").read_text().splitlines()] == args
    assert not (tmp_path / "receipt").exists()


def test_fragmented_json_ignores_fake_and_overlong_events(executables, tmp_path):
    thread = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    false = json.dumps({"nested": {"type": "thread.started", "thread_id": thread}}).encode() + b"\n"
    long_line = b"x" * 70000 + b"\n"
    event = json.dumps({"type": "thread.started", "thread_id": thread}).encode() + b"\n"
    data = false + long_line + event
    result = invoke(executables, tmp_path, ["exec", "--json"], data)
    assert result.returncode == 37 and result.stdout == data
    receipt = (tmp_path / "receipt").read_text().splitlines()
    assert receipt[:2] == ["--link-codex", thread]
    assert receipt[2] == "--codex-pid" and int(receipt[3]) > 0
    assert receipt[4] == "--codex-created" and int(receipt[5]) > 0


def test_resume_receipt_failure_does_not_change_child_result(executables, tmp_path):
    thread = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    result = invoke(executables, tmp_path, ["exec", "resume", thread], b"output")
    assert result.returncode == 37 and result.stdout == b"output"
    assert (tmp_path / "receipt").read_text().splitlines()[1] == thread


def test_native_resolver_rejects_cwd_and_finds_npm_payload(tmp_path):
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    (cwd / "codex.exe").touch()
    npm = tmp_path / "npm"
    npm.mkdir()
    (npm / "codex.cmd").touch()
    package = npm / "node_modules" / "@openai" / "codex"
    (package / "bin").mkdir(parents=True)
    (package / "bin" / "codex.js").touch()
    vendor = package.parent / "codex-win32-x64"
    exe = vendor / "vendor" / "x86_64-pc-windows-msvc" / "bin" / "codex.exe"
    exe.parent.mkdir(parents=True)
    exe.touch()
    (vendor / "package.json").write_text('{}', encoding="utf-8")
    harness = tmp_path / "resolve.cs"
    harness.write_text('using System; class Harness { static int Main(string[] a) { var t=BgCodex.Resolve(a[0], a[1], "", ""); Console.Write(t == null ? "none" : t.Executable); return 0; } }', encoding="utf-8")
    resolver = tmp_path / "resolver.exe"
    compile_cs(ROOT / "scripts/launcher/BgCodex.cs", resolver, "/reference:System.Web.Extensions.dll", "/main:Harness", str(harness))
    env = os.environ.copy()
    env["PROCESSOR_ARCHITECTURE"] = "AMD64"
    assert subprocess.check_output([str(resolver), str(cwd), f";.;{cwd}"], env=env, text=True) == "none"
    assert Path(subprocess.check_output([str(resolver), str(cwd), f";.;{cwd};{npm}"], env=env, text=True)) == exe


def test_slow_receipt_is_cancelled_without_changing_exit(executables, tmp_path, monkeypatch):
    import time
    monkeypatch.setenv("RECEIPT_DELAY", "1")
    started = time.monotonic()
    result = invoke(executables, tmp_path, ["exec", "resume", "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"], b"output")
    elapsed = time.monotonic() - started
    assert result.returncode == 37 and result.stdout == b"output"
    assert elapsed < 3.5
    assert not (tmp_path / "receipt").exists()


def test_native_resolver_prefers_path_and_supports_local_npm_bin(tmp_path):
    cwd = tmp_path / "work"
    cwd.mkdir()
    local = tmp_path / "local"
    fallback = local / "Programs" / "OpenAI" / "Codex" / "bin" / "codex.exe"
    fallback.parent.mkdir(parents=True)
    fallback.touch()
    modules = tmp_path / "npm" / "node_modules"
    bin_dir = modules / ".bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "codex.cmd").write_text("do not execute", encoding="utf-8")
    package = modules / "@openai" / "codex"
    (package / "bin").mkdir(parents=True)
    (package / "bin" / "codex.js").touch()
    vendor = modules / "@openai" / "codex-win32-x64"
    exe = vendor / "vendor" / "x86_64-pc-windows-msvc" / "bin" / "codex.exe"
    exe.parent.mkdir(parents=True)
    exe.touch()
    (vendor / "package.json").write_text('{}', encoding="utf-8")
    harness = tmp_path / "resolve.cs"
    harness.write_text('using System; class Harness { static int Main(string[] a) { var t=BgCodex.Resolve(a[0], a[1], a[2], ""); Console.Write(t == null ? "none" : t.Executable); return 0; } }', encoding="utf-8")
    resolver = tmp_path / "resolver.exe"
    compile_cs(ROOT / "scripts/launcher/BgCodex.cs", resolver, "/reference:System.Web.Extensions.dll", "/main:Harness", str(harness))
    env = os.environ.copy()
    env["PROCESSOR_ARCHITECTURE"] = "AMD64"
    selected = subprocess.check_output([str(resolver), str(cwd), str(bin_dir), str(local)], env=env, text=True)
    assert Path(selected) == exe
    selected = subprocess.check_output([str(resolver), str(cwd), "", str(local)], env=env, text=True)
    assert Path(selected) == fallback


def test_closed_consumer_closes_codex_stdout_and_preserves_exit(executables, tmp_path):
    wrapper, bin_dir, cwd = executables
    fake = tmp_path / "broken_pipe.cs"
    fake.write_text('using System; using System.Runtime.InteropServices; class Fake { [DllImport("kernel32.dll")] static extern IntPtr GetStdHandle(int n); [DllImport("kernel32.dll")] static extern bool WriteFile(IntPtr h, byte[] b, int n, out int written, IntPtr o); static int Main() { var h=GetStdHandle(-11); var b=new byte[8192]; int n; while (WriteFile(h,b,b.Length,out n,IntPtr.Zero)) {} return 42; } }', encoding="utf-8")
    compile_cs(fake, bin_dir / "codex.exe")
    env = os.environ.copy()
    env["FAKE_CODEX"] = str(bin_dir / "codex.exe")
    process = subprocess.Popen([str(wrapper), "exec", "--json"], cwd=cwd, env=env,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    process.stdout.close()
    try:
        assert process.wait(timeout=8) == 42
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        process.stderr.close()
