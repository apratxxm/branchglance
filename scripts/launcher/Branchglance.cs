// Branchglance.exe: the small native launcher in the install root.
//
// It only resolves current.json to a verified release and starts the app, relays
// Claude Code hooks to the release's console hook entry, and offers the
// maintenance-exit client. Installing, recovery and uninstall live in the Python
// installer core (scripts/branchglance_installer), frozen as the setup and
// uninstall executables.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Net.Sockets;
using System.Security.Cryptography;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Web.Script.Serialization;
using System.Runtime.InteropServices;

public static class BranchglanceLauncher
{
    const uint JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9;
    const uint JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x2000;
    [StructLayout(LayoutKind.Sequential)] struct IO_COUNTERS
    { public ulong ReadOperationCount, WriteOperationCount, OtherOperationCount, ReadTransferCount, WriteTransferCount, OtherTransferCount; }
    [StructLayout(LayoutKind.Sequential)] struct JOBOBJECT_BASIC_LIMIT_INFORMATION
    {
        public long PerProcessUserTimeLimit, PerJobUserTimeLimit;
        public uint LimitFlags;
        public UIntPtr MinimumWorkingSetSize, MaximumWorkingSetSize;
        public uint ActiveProcessLimit;
        public UIntPtr Affinity;
        public uint PriorityClass, SchedulingClass;
    }
    [StructLayout(LayoutKind.Sequential)] struct JOBOBJECT_EXTENDED_LIMIT_INFORMATION
    {
        public JOBOBJECT_BASIC_LIMIT_INFORMATION BasicLimitInformation;
        public IO_COUNTERS IoInfo;
        public UIntPtr ProcessMemoryLimit, JobMemoryLimit, PeakProcessMemoryUsed, PeakJobMemoryUsed;
    }
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)]
    static extern IntPtr CreateJobObjectW(IntPtr attributes, string name);
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool SetInformationJobObject(IntPtr job, uint infoClass, IntPtr info, uint infoLength);
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);
    [DllImport("kernel32.dll")]
    static extern IntPtr GetCurrentProcess();
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool TerminateJobObject(IntPtr job, uint exitCode);
    [DllImport("kernel32.dll", SetLastError=true)]
    static extern bool CloseHandle(IntPtr handle);
    static readonly JavaScriptSerializer Json = new JavaScriptSerializer { MaxJsonLength = 64 * 1024 * 1024 };
    // Kept identical to scripts/branchglance_installer/core.py VERSION_RE: no empty dot segment.
    static readonly string VersionPattern = @"\A\d+\.\d+\.\d+(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?\+[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*\z";
    const string Manifest = "release-manifest.json";
    const string Product = "Branchglance";
    const int MaxProbePackages = 4096; // one existence check per package; real machines exceed 128
    const int RelaunchRequestLimit = 64 * 1024;
    static Stopwatch ActiveHookClock;

    public sealed class RelaunchRequest
    {
        public string Id;
        public string[] Arguments;
        public bool Guard;
        public string TestReport;
    }

    static void LauncherLog(string message)
    {
        try { File.AppendAllText(Path.Combine(Path.GetTempPath(), "Branchglance-launcher.log"),
            DateTime.UtcNow.ToString("o") + " " + message + Environment.NewLine, Encoding.UTF8); } catch { }
    }

    public static string ProbeAppData(string localAppData, out long elapsedMs, string simulateRedirectRoot = null)
    {
        var clock = Stopwatch.StartNew();
        string root = Path.Combine(localAppData, Product);
        string id = Guid.NewGuid().ToString("N");
        string name = ".branchglance-probe-" + id;
        string probeDir = Path.Combine(root, name);
        string probeFile = Path.Combine(probeDir, "probe.bin");
        bool rootCreated = false;
        var packageRoots = new List<string>();
        var packageRootsBefore = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        var packageCacheDirs = new List<string>();
        var packageCacheDirsBefore = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        var packageDirs = new List<string>();
        string result = "unknown";
        string reason = "probe did not complete";
        try
        {
            if (!DirectoryExistsStrict(root))
            {
                try { Directory.CreateDirectory(root); rootCreated = true; }
                catch (IOException) { if (!DirectoryExistsStrict(root)) throw; }
            }
            string packages = Path.Combine(localAppData, "Packages");
            if (DirectoryExistsStrict(packages))
            {
                foreach (string package in Directory.EnumerateDirectories(packages))
                {
                    if (packageDirs.Count >= MaxProbePackages)
                        throw new InvalidDataException("Package scan exceeded its bounded limit");
                    string localCache = Path.Combine(package, "LocalCache");
                    string local = Path.Combine(localCache, "Local");
                    string packageRoot = Path.Combine(local, Product);
                    bool cacheBefore = DirectoryExistsFollow(localCache);
                    bool localBefore = DirectoryExistsFollow(local);
                    bool rootBefore = DirectoryExistsFollow(packageRoot);
                    packageDirs.Add(package);
                    packageCacheDirs.Add(localCache);
                    packageCacheDirs.Add(local);
                    if (cacheBefore) packageCacheDirsBefore.Add(localCache);
                    if (localBefore) packageCacheDirsBefore.Add(local);
                    packageRoots.Add(packageRoot);
                    if (rootBefore) packageRootsBefore.Add(packageRoot);
                }
            }
            Directory.CreateDirectory(probeDir);
            using (var stream = new FileStream(probeFile, FileMode.CreateNew, FileAccess.Write, FileShare.None))
            {
                byte[] marker = Encoding.ASCII.GetBytes(id);
                stream.Write(marker, 0, marker.Length);
                stream.Flush(true);
            }
            if (simulateRedirectRoot != null)
            {
                bool allowed = false;
                foreach (string branchRoot in packageRoots)
                    if (String.Equals(Path.GetFullPath(branchRoot), Path.GetFullPath(simulateRedirectRoot), StringComparison.OrdinalIgnoreCase))
                        allowed = true;
                if (!allowed) throw new InvalidDataException("Redirect simulation must target a plain discovered package root");
                string simulated = Path.Combine(simulateRedirectRoot, name);
                Directory.CreateDirectory(simulated);
                File.Copy(probeFile, Path.Combine(simulated, "probe.bin"), false);
            }
            if (packageDirs.Count == 0)
            {
                result = "not_redirected";
                reason = null;
            }
            else
            {
                foreach (string localCache in packageRoots)
                {
                    if (!DirectoryExistsFollow(localCache)) continue;
                    string candidate = Path.Combine(localCache, name);
                    if (Directory.Exists(candidate) && File.Exists(Path.Combine(candidate, "probe.bin")))
                    {
                        result = "redirected";
                        reason = "probe subfolder appeared under a package LocalCache";
                        break;
                    }
                }
                if (result == "unknown") { result = "not_redirected"; reason = null; }
            }
        }
        catch (Exception e) { result = "unknown"; reason = e.GetType().Name + ": " + e.Message; }
        finally
        {
            try { if (FileExistsStrict(probeFile)) File.Delete(probeFile); } catch { result = "unknown"; reason = "probe file cleanup failed"; }
            foreach (string packageRoot in packageRoots)
            {
                string candidate = Path.Combine(packageRoot, name);
                try { if (File.Exists(Path.Combine(candidate, "probe.bin"))) File.Delete(Path.Combine(candidate, "probe.bin")); } catch { result = "unknown"; reason = "redirected probe cleanup failed"; }
                try { if (Directory.Exists(candidate) && Directory.GetFileSystemEntries(candidate).Length == 0) Directory.Delete(candidate, false); } catch { result = "unknown"; reason = "redirected probe folder cleanup failed"; }
            }
            try { if (DirectoryExistsStrict(probeDir)) Directory.Delete(probeDir, false); } catch { result = "unknown"; reason = "probe folder cleanup failed"; }
            foreach (string packageRoot in packageRoots)
            {
                if (PathHasReparseAncestor(packageRoot)) continue;
                if (!packageRootsBefore.Contains(packageRoot))
                { try { if (DirectoryExistsStrict(packageRoot) && Directory.GetFileSystemEntries(packageRoot).Length == 0) Directory.Delete(packageRoot, false); } catch { } }
            }
            foreach (string cacheDir in packageCacheDirs)
            {
                if (PathHasReparseAncestor(cacheDir)) continue;
                if (!packageCacheDirsBefore.Contains(cacheDir))
                { try { if (DirectoryExistsStrict(cacheDir) && Directory.GetFileSystemEntries(cacheDir).Length == 0) Directory.Delete(cacheDir, false); } catch { } }
            }
            if (rootCreated) { try { if (DirectoryExistsStrict(root) && Directory.GetFileSystemEntries(root).Length == 0) Directory.Delete(root, false); } catch { } }
        }
        clock.Stop();
        elapsedMs = clock.ElapsedMilliseconds;
        if (result == "unknown") LauncherLog("AppData probe unknown: " + reason);
        return result;
    }

    static bool DirectoryExistsStrict(string path)
    {
        try
        {
            FileAttributes attrs = File.GetAttributes(path);
            if ((attrs & FileAttributes.ReparsePoint) != 0) throw new IOException("reparse point in package cache");
            return (attrs & FileAttributes.Directory) != 0;
        }
        catch (FileNotFoundException) { return false; }
        catch (DirectoryNotFoundException) { return false; }
    }

    static bool DirectoryExistsFollow(string path)
    {
        try { return (File.GetAttributes(path) & FileAttributes.Directory) != 0; }
        catch (FileNotFoundException) { return false; }
        catch (DirectoryNotFoundException) { return false; }
    }

    static bool PathHasReparseAncestor(string path)
    {
        string current = Path.GetFullPath(path);
        while (!String.IsNullOrEmpty(current))
        {
            try { if ((File.GetAttributes(current) & FileAttributes.ReparsePoint) != 0) return true; } catch { }
            string parent = Path.GetDirectoryName(current);
            if (parent == current) break;
            current = parent;
        }
        return false;
    }

    static bool FileExistsStrict(string path)
    {
        try
        {
            FileAttributes attrs = File.GetAttributes(path);
            if ((attrs & FileAttributes.ReparsePoint) != 0) throw new IOException("reparse point in package cache");
            return (attrs & FileAttributes.Directory) == 0;
        }
        catch (FileNotFoundException) { return false; }
        catch (DirectoryNotFoundException) { return false; }
    }

    static string RequestMutexName(string root)
    {
        using (var sha = SHA256.Create())
            return "Local\\Branchglance-Relaunch-" + Hex(sha.ComputeHash(Encoding.UTF8.GetBytes(Path.GetFullPath(root).ToUpperInvariant()))).Substring(0, 32);
    }

    static string WriteRelaunchRequest(string root, string[] argv, string testReport = null)
    {
        string id = Guid.NewGuid().ToString("N");
        string path = Path.Combine(root, ".relaunch-" + id + ".json");
        var record = new Dictionary<string, object> {
            { "schemaVersion", 1 }, { "id", id }, { "argv", argv }, { "hops", 1 },
            { "created", DateTime.UtcNow.Ticks }, { "expires", (long)(DateTime.UtcNow - new DateTime(1970, 1, 1)).TotalSeconds + 30 },
            { "guard", true }
        };
        if (testReport != null) record["testReport"] = testReport;
        byte[] bytes = Encoding.UTF8.GetBytes(Json.Serialize(record));
        if (bytes.Length > RelaunchRequestLimit) throw new InvalidDataException("Relaunch arguments exceed the request limit");
        using (var stream = new FileStream(path, FileMode.CreateNew, FileAccess.Write, FileShare.None))
        { stream.Write(bytes, 0, bytes.Length); stream.Flush(true); }
        return path;
    }

    static RelaunchRequest ConsumeRelaunchRequest(string root)
    {
        string mutexName = RequestMutexName(root);
        using (var mutex = new Mutex(false, mutexName))
        {
            bool held = false;
            try { try { held = mutex.WaitOne(750); } catch (AbandonedMutexException) { held = true; }
                if (!held) throw new TimeoutException("Relaunch request lock is busy");
                var valid = new List<Tuple<long, string, RelaunchRequest>>();
                foreach (string path in Directory.GetFiles(root, ".relaunch-*.json"))
                {
                    string file = Path.GetFileName(path);
                    Match match = Regex.Match(file, @"\A\.relaunch-([0-9a-f]{32})\.json\z");
                    if (!match.Success) continue;
                    try
                    {
                        var attrs = File.GetAttributes(path);
                        var info = new FileInfo(path);
                        if ((attrs & (FileAttributes.Directory | FileAttributes.ReparsePoint)) != 0 || info.Length > RelaunchRequestLimit) continue;
                        var record = Object(Json.DeserializeObject(File.ReadAllText(path, Encoding.UTF8)), "relaunch request");
                        string id = GetString(record, "id");
                        if (!String.Equals(id, match.Groups[1].Value, StringComparison.Ordinal)) continue;
                        object schema, expiry, created, guard, rawArgs, hops;
                        if (!record.TryGetValue("schemaVersion", out schema) || !(schema is int) || (int)schema != 1
                            || !record.TryGetValue("expires", out expiry) || !(expiry is long || expiry is int)
                            || !record.TryGetValue("created", out created) || !(created is long || created is int)
                            || !record.TryGetValue("guard", out guard) || !(guard is bool) || !(bool)guard
                            || !record.TryGetValue("hops", out hops) || !(hops is int) || (int)hops != 1
                            || !record.TryGetValue("argv", out rawArgs) || !(rawArgs is object[])) continue;
                        long expiryValue = Convert.ToInt64(expiry);
                        object[] items = (object[])rawArgs;
                        if (items.Length > 64) continue;
                        var args = new List<string>();
                        foreach (object item in items) { if (!(item is string)) { args.Clear(); break; } args.Add((string)item); }
                        if (args.Count != items.Length || String.Join("", args.ToArray()).Length > 8192) continue;
                        if (expiryValue <= (long)(DateTime.UtcNow - new DateTime(1970, 1, 1)).TotalSeconds) { File.Delete(path); continue; }
                        string report = record.ContainsKey("testReport") && record["testReport"] is string ? (string)record["testReport"] : null;
                        valid.Add(Tuple.Create(Convert.ToInt64(created), path, new RelaunchRequest { Id=id, Arguments=args.ToArray(), Guard=true, TestReport=report }));
                    }
                    catch (FileNotFoundException) { }
                    catch (DirectoryNotFoundException) { }
                    catch (Exception e) { LauncherLog("Ignoring foreign relaunch request " + file + ": " + e.Message); }
                }
                if (valid.Count == 0) return null;
                // Explorer cannot carry an ID. Consume the oldest valid request under this short mutex.
                valid.Sort((left, right) => left.Item1.CompareTo(right.Item1));
                var selected = valid[0];
                File.Delete(selected.Item2); // one-use: delete before forwarding argv
                return selected.Item3;
            }
            finally { if (held) mutex.ReleaseMutex(); }
        }
    }

    // Shared by Main and the harness: redirected/unknown probes never consume
    // another process's request and therefore never receive an appdata-ok token.
    public static bool MayConsumeRelaunchRequest(string appDataState)
    { return String.Equals(appDataState, "not_redirected", StringComparison.Ordinal); }

    public static RelaunchRequest TryConsumeRelaunchRequest(string root, string appDataState)
    { return MayConsumeRelaunchRequest(appDataState) ? ConsumeRelaunchRequest(root) : null; }

    static Dictionary<string, object> Object(object value, string label)
    {
        var result = value as Dictionary<string, object>;
        if (result == null) throw new InvalidDataException("Invalid " + label + " object");
        return result;
    }

    static object ReadJsonBounded(string path)
    {
        const int limit = 4 * 1024 * 1024;
        using (var stream = File.OpenRead(path))
        {
            if (stream.Length > limit) throw new InvalidDataException("Metadata exceeds 4 MiB");
            var bytes = new byte[(int)stream.Length];
            int offset = 0, read;
            while (offset < bytes.Length && (read = stream.Read(bytes, offset, bytes.Length - offset)) > 0) offset += read;
            if (offset != bytes.Length) throw new EndOfStreamException("Metadata read was incomplete");
            return Json.DeserializeObject(Encoding.UTF8.GetString(bytes));
        }
    }

    static void CheckDirectories(string release, string file)
    {
        string current = release;
        if (IsReparse(current)) throw new InvalidDataException("Release is a reparse point");
        string relative = file.Substring(release.Length + 1);
        string parent = Path.GetDirectoryName(relative);
        if (parent == null) return;
        foreach (string part in parent.Split(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar))
        {
            current = Path.Combine(current, part);
            if (!Directory.Exists(current) || IsReparse(current))
                throw new InvalidDataException("Release directory is missing or redirected: " + current);
        }
    }

    static string GetString(Dictionary<string, object> value, string key)
    {
        object found;
        if (!value.TryGetValue(key, out found) || !(found is string)) throw new InvalidDataException("Invalid " + key);
        return (string)found;
    }

    static void RequireSchema(Dictionary<string, object> value)
    {
        object found;
        if (!value.TryGetValue("schemaVersion", out found) || !(found is int) || (int)found != 1)
            throw new InvalidDataException("Unsupported schemaVersion");
    }

    static string Hex(byte[] bytes)
    {
        var text = new StringBuilder(bytes.Length * 2);
        foreach (byte b in bytes) text.Append(b.ToString("x2"));
        return text.ToString();
    }

    static string Hash(string path)
    {
        using (var sha = SHA256.Create())
        using (var stream = File.OpenRead(path))
            return Hex(sha.ComputeHash(stream));
    }

    static bool IsReparse(string path)
    {
        return (File.GetAttributes(path) & FileAttributes.ReparsePoint) != 0;
    }

    static string SafeEntry(string release, string relative)
    {
        if (relative.Length == 0 || Path.IsPathRooted(relative) || relative.Contains("\\") || relative.Contains(":"))
            throw new InvalidDataException("Unsafe manifest path");
        foreach (string part in relative.Split('/'))
            if (part.Length == 0 || part == "." || part == "..") throw new InvalidDataException("Unsafe manifest path");
        string file = Path.GetFullPath(Path.Combine(release, relative.Replace('/', Path.DirectorySeparatorChar)));
        if (!file.StartsWith(release + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase))
            throw new InvalidDataException("Manifest path escaped the release");
        return file;
    }

    /// The release directory for a pointer entry, with its manifest, after checking
    /// that the version is well formed and nothing on the way is a reparse point.
    /// Absolute or relative paths stored in the pointer are ignored.
    static string ReleaseDirectory(Dictionary<string, object> item, string root, out Dictionary<string, object> manifest)
    {
        var version = GetString(item, "version");
        if (!Regex.IsMatch(version, VersionPattern, RegexOptions.CultureInvariant))
            throw new InvalidDataException("Invalid release version");
        root = Path.GetFullPath(root).TrimEnd(Path.DirectorySeparatorChar);
        string releases = Path.Combine(root, "releases");
        string release = Path.GetFullPath(Path.Combine(releases, version));
        if (!release.StartsWith(releases + Path.DirectorySeparatorChar, StringComparison.OrdinalIgnoreCase))
            throw new InvalidDataException("Release escaped install root");
        foreach (string directory in new[] { root, releases, release })
        {
            if (!Directory.Exists(directory)) throw new DirectoryNotFoundException("Release folder is missing: " + directory);
            if (IsReparse(directory)) throw new InvalidDataException("Install path is a reparse point: " + directory);
        }
        string manifestPath = Path.Combine(release, Manifest);
        if (!File.Exists(manifestPath)) throw new FileNotFoundException("Release manifest missing", manifestPath);
        manifest = Object(ReadJsonBounded(manifestPath), "manifest");
        RequireSchema(manifest);
        if (GetString(manifest, "product") != "Branchglance" || GetString(manifest, "version") != version)
            throw new InvalidDataException("Release manifest identity mismatch");
        return release;
    }

    /// Full verification (app launch): every file hashed, nothing missing or extra.
    static string ReleaseFor(Dictionary<string, object> item, string root)
    {
        Dictionary<string, object> manifest;
        string release = ReleaseDirectory(item, root, out manifest);
        var files = Object(manifest["files"], "files");
        if (files.Count == 0) throw new InvalidDataException("Release manifest is empty");
        var actual = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        var pending = new Stack<string>();
        pending.Push(release);
        string manifestPath = Path.Combine(release, Manifest);
        while (pending.Count > 0)
        {
            string directory = pending.Pop();
            foreach (string child in Directory.GetFileSystemEntries(directory))
            {
                var attributes = File.GetAttributes(child);
                if ((attributes & FileAttributes.ReparsePoint) != 0) throw new InvalidDataException("Release contains a reparse point");
                if ((attributes & FileAttributes.Directory) != 0) pending.Push(child);
                else if (!String.Equals(child, manifestPath, StringComparison.OrdinalIgnoreCase))
                    actual.Add(child.Substring(release.Length + 1).Replace('\\', '/'));
            }
        }
        if (actual.Count != files.Count) throw new InvalidDataException("Release contains missing or unmanifested files");
        foreach (var entry in files)
        {
            string file = SafeEntry(release, entry.Key);
            if (!actual.Contains(entry.Key) || !File.Exists(file)) throw new FileNotFoundException("Manifest file missing", file);
            if (!(entry.Value is string) || !String.Equals(Hash(file), (string)entry.Value, StringComparison.OrdinalIgnoreCase))
                throw new InvalidDataException("Release hash mismatch: " + entry.Key);
        }
        if (!File.Exists(Path.Combine(release, "BranchglanceApp.exe")))
            throw new FileNotFoundException("Branchglance application is missing");
        return release;
    }

    /// Light verification (hook mode, every Claude Code event): the manifest exists
    /// and every installed file still has the size and modification time recorded
    /// at install ("stamps"). No hashing; full hashing happens at launch and install.
    static string ReleaseForHook(Dictionary<string, object> item, string root)
    {
        Dictionary<string, object> manifest;
        string release = ReleaseDirectory(item, root, out manifest);
        object raw;
        if (!manifest.TryGetValue("stamps", out raw)) throw new InvalidDataException("Release has no install stamps");
        var stamps = Object(raw, "stamps");
        var files = Object(manifest["files"], "files");
        if (stamps.Count != files.Count)
            throw new InvalidDataException("Release stamp list is incomplete");
        foreach(string name in files.Keys)
            if(!stamps.ContainsKey(name)) throw new InvalidDataException("Release stamp is missing: " + name);
        if (!stamps.ContainsKey("BranchglanceHook.exe")) throw new InvalidDataException("Hook entry is not part of the release");
        foreach (var entry in stamps)
        {
            if (ActiveHookClock != null && ActiveHookClock.ElapsedMilliseconds >= HookDeadlineMs)
                throw new TimeoutException("Hook stamp verification exceeded its deadline");
            string file = SafeEntry(release, entry.Key);
            CheckDirectories(release, file);
            var pair = entry.Value as object[];
            var info = new FileInfo(file);
            if (pair == null || pair.Length != 2 || !info.Exists || (info.Attributes & FileAttributes.ReparsePoint) != 0
                || info.Length != Convert.ToInt64(pair[0]) || info.LastWriteTimeUtc.ToFileTimeUtc() != Convert.ToInt64(pair[1]))
                throw new InvalidDataException("Release file changed since install: " + entry.Key);
        }
        return release;
    }

    static string Resolve(string root, string pointerPath, Func<Dictionary<string, object>, string, string> check)
    {
        var pointer = Object(ReadJsonBounded(pointerPath), "pointer");
        RequireSchema(pointer);
        object current, previous;
        pointer.TryGetValue("current", out current);
        pointer.TryGetValue("previous", out previous);
        try { return check(Object(current, "current release"), root); }
        catch (Exception currentError)
        {
            if (previous == null) throw new InvalidDataException("Current release is invalid and no previous release is available", currentError);
            try { return check(Object(previous, "previous release"), root); }
            catch (Exception previousError) { throw new InvalidDataException("Current and previous releases are invalid", previousError); }
        }
    }

    public static string ResolveRelease(string root, string pointerPath) { return Resolve(root, pointerPath, ReleaseFor); }
    public static string ResolveHookRelease(string root, string pointerPath) { return Resolve(root, pointerPath, ReleaseForHook); }

    static string Quote(string value)
    {
        if (value == null) return "\"\"";
        var b = new StringBuilder("\"");
        int slashes = 0;
        foreach (char c in value)
        {
            if (c == '\\') { slashes++; continue; }
            if (c == '"') { b.Append('\\', slashes * 2 + 1).Append(c); slashes = 0; continue; }
            b.Append('\\', slashes).Append(c); slashes = 0;
        }
        b.Append('\\', slashes * 2).Append('"');
        return b.ToString();
    }

    public static int Main(string[] args)
    {
        string root = AppDomain.CurrentDomain.BaseDirectory;
        // Hook mode never shows UI and never fails Claude Code.
        if (args.Length > 0 && args[0] == "--claude-hook") return RunHook(root, args);
        if (args.Length > 0 && args[0] == "--appdata-probe" && args.Length == 2)
        {
            long elapsed;
            string state = ProbeAppData(args[1], out elapsed);
            Console.WriteLine(state + " " + elapsed.ToString() + "ms");
            return state == "redirected" ? 10 : state == "unknown" ? 11 : 0;
        }
        if (args.Length > 0 && args[0] == "--appdata-probe-simulate" && args.Length == 3)
        {
            long elapsed;
            string state = ProbeAppData(args[1], out elapsed, args[2]);
            Console.WriteLine(state + " " + elapsed.ToString() + "ms");
            return state == "redirected" ? 10 : state == "unknown" ? 11 : 0;
        }
        if (args.Length == 2 && args[0] == "--explorer-roundtrip-test")
        {
            try
            {
                string[] expected = { "--install-handoff", "a1b2c3d4e5f6", "--background", "--recenter" };
                WriteRelaunchRequest(root, expected, args[1]);
                string own = Process.GetCurrentProcess().MainModule.FileName;
                var explorerTest = Process.Start(new ProcessStartInfo("explorer.exe", Quote(own)) { UseShellExecute = true });
                return explorerTest == null ? 1 : 0;
            }
            catch (Exception e) { LauncherLog("Explorer round-trip test failed to start: " + e.Message); return 1; }
        }
        try
        {
            string ownPath = Process.GetCurrentProcess().MainModule.FileName;
            RelaunchRequest request = null;
            string[] launchArgs = args;
            string local = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
            long probeMilliseconds;
            string appDataState = ProbeAppData(local, out probeMilliseconds);
            if (appDataState == "redirected")
            {
                WriteRelaunchRequest(root, launchArgs);
                Process explorer = Process.Start(new ProcessStartInfo("explorer.exe", Quote(ownPath)) { UseShellExecute = true });
                if (explorer == null) throw new InvalidOperationException("Explorer did not accept the launcher request");
                return 0;
            }
            if (args.Length == 0 && MayConsumeRelaunchRequest(appDataState))
            {
                request = TryConsumeRelaunchRequest(root, appDataState);
                if (request != null)
                {
                    if (request.TestReport != null)
                    {
                        File.WriteAllText(request.TestReport, Json.Serialize(new Dictionary<string, object> {
                            { "id", request.Id }, { "argv", request.Arguments } }), new UTF8Encoding(false));
                        return 0;
                    }
                    launchArgs = request.Arguments;
                }
            }
            string release = ResolveRelease(root, Path.Combine(root, "current.json"));
            string app = Path.Combine(release, "BranchglanceApp.exe");
            var start = new ProcessStartInfo(app);
            start.WorkingDirectory = release;
            start.UseShellExecute = false;
            var forwardedArgs = new List<string>(launchArgs);
            forwardedArgs.RemoveAll(a => a == "--launcher-appdata-ok" || a == "--launcher-appdata-unknown");
            if (appDataState == "not_redirected") forwardedArgs.Add("--launcher-appdata-ok");
            else if (appDataState == "unknown") forwardedArgs.Add("--launcher-appdata-unknown");
            start.Arguments = String.Join(" ", forwardedArgs.ConvertAll(Quote).ToArray());
            Process.Start(start);
            return 0;
        }
        catch (Exception e)
        {
            Console.Error.WriteLine("Branchglance could not start: " + e.Message);
            try { System.Windows.Forms.MessageBox.Show("Branchglance could not start:\n\n" + e.Message,
                "Branchglance", System.Windows.Forms.MessageBoxButtons.OK, System.Windows.Forms.MessageBoxIcon.Error); } catch { }
            return 1;
        }
    }

    // Claude Code gives the hook a 10 s timeout and the hook entry (claude_hook.run)
    // ends itself after 5 s; the relay stops waiting before Claude Code would.
    public static int HookDeadlineMs = 9000;

    /// Raw byte copy: no encoding or code page anywhere in the hook path.
    static Thread Pump(Stream from, Stream to, bool closeTarget)
    {
        var thread = new Thread(() => {
            var buffer = new byte[65536];
            try { int count; while ((count = from.Read(buffer, 0, buffer.Length)) > 0) { to.Write(buffer, 0, count); to.Flush(); } } catch { }
            if (closeTarget) { try { to.Close(); } catch { } }
        });
        thread.IsBackground = true;
        thread.Start();
        return thread;
    }

    static void Note(string text)
    {
        try { using (var error = Console.OpenStandardError()) { var bytes = Encoding.UTF8.GetBytes(text + "\n"); error.Write(bytes, 0, bytes.Length); } } catch { }
    }

    /// Runs the release's console hook entry with this process's stdin/stdout/stderr
    /// relayed as bytes. Always returns 0: a missing release or a slow hook must
    /// never block or fail Claude Code.
    public static int RunHook(string root, string[] args)
    {
        HookDiagnosticCategory = "";
        var clock = Stopwatch.StartNew();
        ActiveHookClock = clock;
        int deadline = Math.Max(1, HookDeadlineMs);
        try
        {
            string release = ResolveHookRelease(root, Path.Combine(root, "current.json"));
            if (clock.ElapsedMilliseconds >= deadline) throw new TimeoutException("Hook verification exceeded its deadline");
            string hook = Path.Combine(release, "BranchglanceHook.exe");
            var start = new ProcessStartInfo(hook) {
                WorkingDirectory = release, UseShellExecute = false, CreateNoWindow = true,
                // stdin is inherited, not relayed: .NET Framework's redirected StandardInput writer
                // emits the console encoding's preamble (a UTF-8 BOM under code page 65001).
                RedirectStandardInput = false, RedirectStandardOutput = true, RedirectStandardError = true };
            start.Arguments = String.Join(" ", new List<string>(args).ConvertAll(Quote).ToArray());
            IntPtr job = CreateJobObjectW(IntPtr.Zero, null);
            if (job == IntPtr.Zero) throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error(), "CreateJobObjectW failed");
            bool launcherInJob = false;
            try
            {
                var limits = new JOBOBJECT_EXTENDED_LIMIT_INFORMATION();
                limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
                IntPtr buffer = Marshal.AllocHGlobal(Marshal.SizeOf(typeof(JOBOBJECT_EXTENDED_LIMIT_INFORMATION)));
                try
                {
                    Marshal.StructureToPtr(limits, buffer, false);
                    if (!SetInformationJobObject(job, JOB_OBJECT_EXTENDED_LIMIT_INFORMATION, buffer,
                            (uint)Marshal.SizeOf(typeof(JOBOBJECT_EXTENDED_LIMIT_INFORMATION))))
                        throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error(), "SetInformationJobObject failed");
                }
                finally { Marshal.FreeHGlobal(buffer); }
                launcherInJob = AssignProcessToJobObject(job, GetCurrentProcess());
                if (!launcherInJob) NoteAsync("Branchglance hook launcher self-assignment failed; using child-assignment fallback");
                using (var child = Process.Start(start))
                {
                    if (child == null) throw new InvalidOperationException("Hook process did not start");
                    if (!launcherInJob && !AssignProcessToJobObject(job, child.Handle))
                    {
                        try { child.Kill(); } catch { }
                        throw new System.ComponentModel.Win32Exception(Marshal.GetLastWin32Error(), "AssignProcessToJobObject failed");
                    }
                    var output = Pump(child.StandardOutput.BaseStream, Console.OpenStandardOutput(), false);
                    var error = Pump(child.StandardError.BaseStream, Console.OpenStandardError(), false);
                    int remaining = Math.Max(1, deadline - (int)clock.ElapsedMilliseconds);
                    if (child.WaitForExit(remaining))
                    {
                        remaining = Math.Max(0, deadline - (int)clock.ElapsedMilliseconds);
                        bool outDrained = output.Join(remaining);
                        remaining = Math.Max(0, deadline - (int)clock.ElapsedMilliseconds);
                        bool errDrained = error.Join(remaining);
                        if (!outDrained || !errDrained) NoteAsync("Branchglance hook output may be incomplete");
                    }
                    else
                    {
                        Note("Branchglance hook diagnostic: timeout");
                        try { Console.Out.Flush(); } catch { }
                        try { Console.Error.Flush(); } catch { }
                        HookDiagnosticCategory = "timeout";
                        try { TerminateJobObject(job, 0); } catch { }
                        return 0;
                    }
                }
            }
            finally
            {
                // A launcher assigned to its own kill-on-close job leaves the handle
                // open until process exit; the OS then closes it and kills descendants.
                if (!launcherInJob) CloseHandle(job);
            }
        }
        catch (Exception e)
        {
            string message = (e.Message + " " + (e.InnerException == null ? "" : e.InnerException.Message)).ToLowerInvariant();
            if (e is TimeoutException) HookDiagnosticCategory = "timeout";
            else if (e is FileNotFoundException || e is DirectoryNotFoundException
                    || e.InnerException is FileNotFoundException || e.InnerException is DirectoryNotFoundException)
                HookDiagnosticCategory = "missing-release";
            else if (message.Contains("stamp") || message.Contains("release file changed")
                    || message.Contains("reparse") || message.Contains("redirected"))
                HookDiagnosticCategory = "stamp-refusal";
            else if (HookDiagnosticCategory.Length == 0) HookDiagnosticCategory = "hook-error";
            NoteAsync("Branchglance hook skipped: " + e.Message);
        }
        return 0;
    }

    public static string HookDiagnosticCategory = "";

    static void NoteAsync(string text)
    {
        try
        {
            var thread = new Thread(() => Note(text));
            thread.IsBackground = true;
            thread.Start();
        }
        catch { }
    }

    /// Same construction as token_tracker.product.control_mac (protocol 2).
    public static string ControlMac(string nonce, string role, params string[] parts)
    {
        var all = new List<string> { "branchglance-control", "2", role };
        all.AddRange(parts);
        using (var mac = new HMACSHA256(Encoding.ASCII.GetBytes(nonce)))
            return Hex(mac.ComputeHash(Encoding.UTF8.GetBytes(String.Join("\n", all.ToArray()))));
    }

    static bool FixedTimeEquals(string a, string b)
    {
        if (a == null || b == null || a.Length != b.Length) return false;
        int diff = 0;
        for (int i = 0; i < a.Length; i++) diff |= a[i] ^ b[i];
        return diff == 0;
    }

    static string ReadLine(Stream stream, int limit)
    {
        var bytes = new List<byte>();
        while (bytes.Count < limit)
        {
            int value = stream.ReadByte();
            if (value < 0) return null;
            if (value == '\n') return Encoding.UTF8.GetString(bytes.ToArray()).Trim();
            bytes.Add((byte)value);
        }
        return null;
    }

    /// Asks a running Branchglance to exit for maintenance. The listener must first
    /// prove it holds the nonce for this exchange; an unverified listener gets the
    /// hello line only, never the nonce or a command.
    public static bool AskMaintenanceExit(string noncePath, int port)
    {
        if (!File.Exists(noncePath)) return false;
        string nonce = File.ReadAllText(noncePath).Trim();
        if (nonce.Length < 32 || nonce.Length > 128 || !Regex.IsMatch(nonce, @"\A[0-9A-Za-z]+\z")) return false;
        try
        {
            using (var client = new TcpClient())
            {
                var pending = client.BeginConnect("127.0.0.1", port, null, null);
                if (!pending.AsyncWaitHandle.WaitOne(2000)) return false;
                client.EndConnect(pending); client.ReceiveTimeout = 2000; client.SendTimeout = 2000;
                using (var stream = client.GetStream())
                {
                    byte[] random = new byte[32];
                    using (var rng = RandomNumberGenerator.Create()) rng.GetBytes(random);
                    string clientChallenge = Hex(random);
                    byte[] hello = Encoding.ASCII.GetBytes("hello " + clientChallenge + "\n");
                    stream.Write(hello, 0, hello.Length);
                    var who = Json.DeserializeObject(ReadLine(stream, 16384) ?? "null") as Dictionary<string, object>;
                    if (who == null) return false;
                    object product, challenge, proof;
                    if (!who.TryGetValue("product", out product) || !"Branchglance".Equals(product)) return false;
                    if (!who.TryGetValue("challenge", out challenge) || !(challenge is string)
                        || !Regex.IsMatch((string)challenge, @"\A[0-9a-f]{32,128}\z")) return false;
                    if (!who.TryGetValue("proof", out proof) || !(proof is string)) return false;
                    string serverChallenge = (string)challenge;
                    if (!FixedTimeEquals(ControlMac(nonce, "server", clientChallenge, serverChallenge), (string)proof)) return false;
                    string command = "maintenance-exit";
                    byte[] request = Encoding.ASCII.GetBytes(command + " " + ControlMac(nonce, "client", clientChallenge, serverChallenge, command) + "\n");
                    stream.Write(request, 0, request.Length);
                    return ReadLine(stream, 128) == "ok";
                }
            }
        }
        catch { return false; }
    }
}
