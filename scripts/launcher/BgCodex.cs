using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Threading;
using System.Web.Script.Serialization;

public static class BgCodex
{
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool GetProcessTimes(IntPtr process, out long creation, out long exit, out long kernel, out long user);
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)] static extern IntPtr CreateFileW(string path, uint access, uint share, IntPtr security, uint disposition, uint flags, IntPtr template);
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)] static extern uint GetFinalPathNameByHandleW(IntPtr handle, StringBuilder path, uint size, uint flags);
    [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
    const int MaxLine = 65536;
    static string Quote(string value) {
        var b = new StringBuilder("\""); int slashes = 0;
        foreach (char c in value) { if (c == '\\') { slashes++; continue; } if (c == '"') { b.Append('\\', slashes * 2 + 1).Append(c); slashes = 0; continue; } b.Append('\\', slashes).Append(c); slashes = 0; }
        b.Append('\\', slashes * 2).Append('"'); return b.ToString();
    }
    static string Canonical(string path) {
        IntPtr h = CreateFileW(path, 0, 7, IntPtr.Zero, 3, 0x02000000, IntPtr.Zero);
        if (h == new IntPtr(-1)) return null;
        try { var b = new StringBuilder(32768); uint n = GetFinalPathNameByHandleW(h, b, (uint)b.Capacity, 0); if (n == 0 || n >= b.Capacity) return null; string s = b.ToString(); return s.StartsWith(@"\\?\UNC\") ? @"\\" + s.Substring(8) : s.StartsWith(@"\\?\") ? s.Substring(4) : s; }
        finally { CloseHandle(h); }
    }
    static bool Safe(string path, string cwd) {
        string actual = Canonical(path), current = Canonical(cwd);
        if (actual == null || current == null) return false;
        current = current.TrimEnd('\\');
        return !actual.Equals(current, StringComparison.OrdinalIgnoreCase) && !actual.StartsWith(current + "\\", StringComparison.OrdinalIgnoreCase);
    }
    static bool Absolute(string path) { return !String.IsNullOrEmpty(path) && System.Text.RegularExpressions.Regex.IsMatch(path, @"\A(?:[A-Za-z]:[\\/]|\\\\[^\\/]+[\\/][^\\/]+)"); }
    public sealed class Target { public string Executable; public string PackageRoot; public string PackageEntry; }
    public static Target Resolve(string cwd, string path, string local, string home) {
        var candidates = new List<string>();
        if (Absolute(local)) { candidates.Add(Path.Combine(local, @"Programs\OpenAI\Codex\bin\codex.exe")); candidates.Add(Path.Combine(local, @"Programs\codex\bin\codex.exe")); }
        if (Absolute(home)) candidates.Add(Path.Combine(home, @"AppData\Local\Programs\OpenAI\Codex\bin\codex.exe"));
        foreach (string candidate in candidates) if (File.Exists(candidate) && Safe(candidate, cwd)) return new Target { Executable = Path.GetFullPath(candidate) };
        foreach (string entry0 in (path ?? "").Split(';')) {
            string entry = entry0.Trim().Trim('"');
            if (!Absolute(entry) || !Directory.Exists(entry) || !Safe(entry, cwd)) continue;
            string exe = Path.Combine(entry, "codex.exe");
            if (File.Exists(exe) && Safe(exe, cwd)) return new Target { Executable = Path.GetFullPath(exe) };
            if (!Array.Exists(new[] { "codex.cmd", "codex.ps1" }, name => File.Exists(Path.Combine(entry, name)) && Safe(Path.Combine(entry, name), cwd))) continue;
            string root = Path.Combine(entry, @"node_modules\@openai\codex");
            string js = Path.Combine(root, @"bin\codex.js");
            if (!File.Exists(js) || !Safe(js, cwd) || !Safe(root, cwd)) continue;
            string arch = Environment.GetEnvironmentVariable("PROCESSOR_ARCHITECTURE") ?? "";
            if (!arch.Equals("ARM64", StringComparison.OrdinalIgnoreCase) && !arch.Equals("AMD64", StringComparison.OrdinalIgnoreCase)) continue;
            string triple = arch.Equals("ARM64", StringComparison.OrdinalIgnoreCase) ? "aarch64-pc-windows-msvc" : "x86_64-pc-windows-msvc";
            string package = triple.StartsWith("aarch64") ? "codex-win32-arm64" : "codex-win32-x64";
            string vendor = null; bool foundPackage = false;
            for (var dir = new DirectoryInfo(Path.GetDirectoryName(Canonical(js))); dir != null; dir = dir.Parent) {
                string pkg = Path.Combine(dir.FullName, "node_modules", "@openai", package);
                string metadata = Path.Combine(pkg, "package.json");
                if (!File.Exists(metadata)) continue;
                foundPackage = true;
                if (Safe(metadata, cwd)) vendor = Path.Combine(pkg, "vendor", triple, "bin", "codex.exe");
                break;
            }
            if (!foundPackage) vendor = Path.Combine(root, "vendor", triple, "bin", "codex.exe");
            if (vendor == null) continue;
            if (File.Exists(vendor) && Safe(vendor, cwd)) return new Target { Executable = Path.GetFullPath(vendor), PackageRoot = Canonical(root), PackageEntry = root };
        }
        return null;
    }
    static string Uuid(string value) { Guid id; return value != null && Guid.TryParseExact(value, "D", out id) ? id.ToString() : null; }
    static string Resume(string[] args) {
        if (args.Length < 3 || args[0] != "exec" || args[1] != "resume") return null;
        return Uuid(args[2]);
    }
    static bool JsonExec(string[] args) { return args.Length > 0 && args[0] == "exec" && Array.IndexOf(args, "--json") >= 0; }
    sealed class ReceiptAttempt {
        readonly object gate = new object();
        Process helper;
        bool cancelled;
        public void Cancel() {
            Process running;
            lock (gate) { cancelled = true; running = helper; }
            if (running != null) try { running.Kill(); } catch { }
        }
        public void Run(int pid, string thread, long created) {
            try {
                string launcher = Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "Branchglance.exe");
                if (!File.Exists(launcher)) return;
                var start = new ProcessStartInfo(launcher) { UseShellExecute = false, CreateNoWindow = true, RedirectStandardInput = true, RedirectStandardOutput = true, RedirectStandardError = true, Arguments = "--link-codex " + Quote(thread) + " --codex-pid " + pid.ToString() + " --codex-created " + created.ToString(System.Globalization.CultureInfo.InvariantCulture) };
                using (var launched = Process.Start(start)) {
                    if (launched == null) return;
                    bool stop;
                    lock (gate) { helper = launched; stop = cancelled; }
                    if (stop) { try { launched.Kill(); } catch { } return; }
                    launched.StandardInput.Close(); launched.BeginOutputReadLine(); launched.BeginErrorReadLine();
                    if (!launched.WaitForExit(1800)) { try { launched.Kill(); } catch { } }
                }
            } catch { }
            finally { lock (gate) { helper = null; } }
        }
    }
    static string PackageManager(string root, string entry) {
        foreach (string origin in new[] { root, entry }) {
            for (var dir = new DirectoryInfo(origin); dir != null; dir = dir.Parent) {
                if (dir.Name.Equals("packages", StringComparison.Ordinal)) try {
                    string meta = Path.Combine(dir.FullName, "@openai", "codex.json");
                    var json = new JavaScriptSerializer().DeserializeObject(File.ReadAllText(meta)) as Dictionary<string, object>;
                    object name, id;
                    if (json == null || !json.TryGetValue("name", out name) || (name as string) != "@openai/codex") continue;
                    string installId = json.TryGetValue("installId", out id) ? id as string ?? "" : "";
                    string install = installId.StartsWith("#") ? Path.Combine(dir.FullName, "@openai", "codex" + installId) : Path.Combine(dir.FullName, "@openai", "codex", installId);
                    foreach (string node in new[] { Path.Combine(install, "lib", "node_modules"), Path.Combine(install, "node_modules") })
                        if (String.Equals(Canonical(Path.Combine(node, "@openai", "codex")), root, StringComparison.OrdinalIgnoreCase)) return "VITE_PLUS";
                } catch { }
                string modules = Path.Combine(dir.FullName, "node_modules");
                if (File.Exists(Path.Combine(modules, ".modules.yaml")) && String.Equals(Canonical(Path.Combine(modules, "@openai", "codex")), root, StringComparison.OrdinalIgnoreCase)) return "PNPM";
            }
        }
        string userAgent = Environment.GetEnvironmentVariable("npm_config_user_agent") ?? "";
        string execPath = Environment.GetEnvironmentVariable("npm_execpath") ?? "";
        return System.Text.RegularExpressions.Regex.IsMatch(userAgent, @"\bbun/") || execPath.Contains("bun") || root.IndexOf(@".bun\install\global", StringComparison.OrdinalIgnoreCase) >= 0 ? "BUN" : "NPM";
    }
    static void SetPackageEnvironment(ProcessStartInfo start, string root, string entry) {
        start.EnvironmentVariables["CODEX_MANAGED_PACKAGE_ROOT"] = root;
        foreach (string key in new[] {"CODEX_MANAGED_BY_NPM", "CODEX_MANAGED_BY_BUN", "CODEX_MANAGED_BY_PNPM", "CODEX_MANAGED_BY_VITE_PLUS"}) start.EnvironmentVariables.Remove(key);
        start.EnvironmentVariables["CODEX_MANAGED_BY_" + PackageManager(root, entry)] = "1";
    }
    public static int Main(string[] args) {
        Target target = Resolve(Environment.CurrentDirectory, Environment.GetEnvironmentVariable("PATH"), Environment.GetEnvironmentVariable("LOCALAPPDATA"), Environment.GetFolderPath(Environment.SpecialFolder.UserProfile));
        return Run(args, target);
    }
    public static int Run(string[] args, Target target) {
        try {
            if (target == null) { Console.Error.WriteLine("bg-codex: Codex executable not found outside the current directory"); return 1; }
            bool observe = JsonExec(args);
            var start = new ProcessStartInfo(target.Executable) { UseShellExecute = false, Arguments = String.Join(" ", Array.ConvertAll(args, Quote)), RedirectStandardOutput = observe };
            if (target.PackageRoot != null) SetPackageEnvironment(start, target.PackageRoot, target.PackageEntry);
            Console.CancelKeyPress += delegate(object sender, ConsoleCancelEventArgs e) { e.Cancel = true; };
            using (var child = Process.Start(start)) {
                if (child == null) return 1;
                long created, exited, kernel, user; bool hasTime = GetProcessTimes(child.Handle, out created, out exited, out kernel, out user);
                Thread receipt = null; ReceiptAttempt attempt = null; object sync = new object();
                Action<string> enqueue = delegate(string id) { lock(sync) { if (receipt != null || !hasTime || id == null) return; attempt = new ReceiptAttempt(); int pid = child.Id; receipt = new Thread(() => attempt.Run(pid, id, created)); receipt.IsBackground = true; receipt.Start(); } };
                enqueue(Resume(args));
                if (observe) {
                    var source = child.StandardOutput.BaseStream; var dest = Console.OpenStandardOutput(); byte[] chunk = new byte[8192]; var line = new MemoryStream(); bool discard = false; int n;
                    bool outputOpen = true;
                    while ((n = source.Read(chunk, 0, chunk.Length)) > 0) {
                        if (outputOpen) { try { dest.Write(chunk, 0, n); dest.Flush(); } catch (IOException) { outputOpen = false; } }
                        for (int i = 0; i < n; i++) { byte b = chunk[i]; if (b == 10) { if (!discard && line.Length > 0) { try { var json = new JavaScriptSerializer().DeserializeObject(new UTF8Encoding(false, true).GetString(line.ToArray())) as Dictionary<string, object>; object type, id; if (json != null && json.TryGetValue("type", out type) && (type as string) == "thread.started" && json.TryGetValue("thread_id", out id)) enqueue(Uuid(id as string)); } catch {} } line.SetLength(0); discard = false; } else if (!discard) { if (line.Length >= MaxLine) { discard = true; line.SetLength(0); } else line.WriteByte(b); } }
                    }
                }
                child.WaitForExit(); int code = child.ExitCode; Thread pending; lock(sync) pending = receipt; if (pending != null && pending.IsAlive && !pending.Join(2000)) attempt.Cancel(); return code;
            }
        } catch (Exception e) { Console.Error.WriteLine("bg-codex: " + e.Message); return 1; }
    }
}
