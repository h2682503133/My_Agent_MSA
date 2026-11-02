using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Net.Sockets;
using System.Text;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;

namespace MyAgentAdminPanel.Services;

/// <summary>
/// 单体模式（无 Docker / 无 k8s）的本地控制：由面板调用 deploy/launcher，
/// 在 Windows 上按功能起一组本地子进程。
/// 约定：不写注册表、不做开机自启；所有状态落 &lt;DataDir&gt;/pids.json 与 runtime.json。
/// </summary>
public class StandaloneService
{
    public string RepoRoot { get; set; } = "";
    public string DataDir { get; set; } = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "MyAgent", "data");
    public string PythonPath { get; set; } = "";

    private Process? _launcher;

    public event Action<string>? Log;
    private void Say(string message)
    {
        Log?.Invoke(message);
        AppendLogLine($"[{DateTime.Now:HH:mm:ss}] {message}");
    }

    public string LogFile => Path.Combine(DataDir, "logs", "launcher.log");

    private void AppendLogLine(string line)
    {
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(LogFile)!);
            File.AppendAllText(LogFile, line + Environment.NewLine, Encoding.UTF8);
        }
        catch { }
    }

    // ── 环境探测 ────────────────────────────────────────────

    public static string FindRepoRoot()
    {
        var dir = AppContext.BaseDirectory;
        for (var i = 0; i < 5 && !string.IsNullOrEmpty(dir); i++)
        {
            if (File.Exists(Path.Combine(dir, "deploy", "features.json")))
                return dir;
            dir = Path.GetDirectoryName(dir.TrimEnd(Path.DirectorySeparatorChar)) ?? "";
        }
        return "";
    }

    public static string FindPython()
    {
        // 优先面板自带的便携运行时（发布包里放 publish/runtime/python.exe）
        var bundled = Path.Combine(AppContext.BaseDirectory, "runtime", "python.exe");
        if (File.Exists(bundled)) return bundled;

        foreach (var dir in (Environment.GetEnvironmentVariable("PATH") ?? "").Split(Path.PathSeparator))
        {
            if (string.IsNullOrWhiteSpace(dir)) continue;
            var candidate = Path.Combine(dir.Trim(), "python.exe");
            if (File.Exists(candidate)) return candidate;
        }
        return "";
    }

    public string LauncherScript => Path.Combine(RepoRoot, "deploy", "launcher", "run.py");
    public string ProbeScript => Path.Combine(RepoRoot, "services", "agent-orchestrator-service", "tools", "probe_env.py");

    public (bool ok, string message) CheckReady()
    {
        if (string.IsNullOrWhiteSpace(RepoRoot) || !Directory.Exists(RepoRoot))
            return (false, "找不到仓库根目录（需要 deploy/features.json），请在设置里指定");
        if (!File.Exists(LauncherScript))
            return (false, $"找不到 {LauncherScript}");
        var python = string.IsNullOrWhiteSpace(PythonPath) ? FindPython() : PythonPath;
        if (string.IsNullOrWhiteSpace(python) || !File.Exists(python))
            return (false, "找不到 python.exe（可安装 Python 3.11+，或把便携运行时放到 publish/runtime/）");
        return (true, "就绪");
    }

    // ── 数据目录 ────────────────────────────────────────────

    /// <summary>
    /// 初始化数据目录：直接调用部署脚本（deps.py --init），
    /// 不在面板里再实现一遍，避免和脚本出现两个事实源。
    /// </summary>
    public (bool ok, string message) InitDataDir(IEnumerable<string> features)
    {
        var python = string.IsNullOrWhiteSpace(PythonPath) ? FindPython() : PythonPath;
        if (string.IsNullOrWhiteSpace(python)) return (false, "找不到 python.exe");
        if (!File.Exists(DepsScript)) return (false, $"找不到 {DepsScript}");
        var featureList = string.Join(",", features);
        Say($"初始化数据目录（调用 deploy/launcher/deps.py --init）：{DataDir}");
        var (ok, output) = RunStreaming(
            python,
            $"-X utf8 \"{DepsScript}\" --data-dir \"{DataDir}\" --features \"{featureList}\" --init",
            180,
            RepoRoot);
        return (ok, ok ? $"数据目录已就绪：{DataDir}" : $"初始化失败：{Tail(output)}");
    }

    // ── 依赖（共享 venv）────────────────────────────────────

    public string DepsScript => Path.Combine(RepoRoot, "deploy", "launcher", "deps.py");

    /// <summary>检查依赖是否已装好（venv 存在且 stamp 匹配）。</summary>
    public (bool ready, string message) CheckDeps(IEnumerable<string> features)
    {
        var python = string.IsNullOrWhiteSpace(PythonPath) ? FindPython() : PythonPath;
        if (string.IsNullOrWhiteSpace(python)) return (false, "找不到 python.exe");
        if (!File.Exists(DepsScript)) return (false, $"找不到 {DepsScript}");

        var (ok, output) = Run(
            python,
            $"-X utf8 \"{DepsScript}\" --data-dir \"{DataDir}\" --features \"{string.Join(",", features)}\" --check",
            120,
            RepoRoot);
        return (ok, output);
    }

    public bool OpenvikingServerInstalled() =>
        File.Exists(Path.Combine(DataDir, "venvs", "openviking", "Scripts", "openviking-server.exe"));

    public bool OpenvikingApiKeyReady() =>
        File.Exists(Path.Combine(DataDir, "config", "openviking", "api_key"));

    /// <summary>安装/更新本机 openviking-server（独立 venv；首次约 8~10 分钟）。</summary>
    public (bool ok, string message) InstallOpenviking()
    {
        var python = string.IsNullOrWhiteSpace(PythonPath) ? FindPython() : PythonPath;
        if (string.IsNullOrWhiteSpace(python)) return (false, "找不到 python.exe");
        if (!File.Exists(DepsScript)) return (false, $"找不到 {DepsScript}");
        Say("开始安装 openviking-server（独立 venv，约 8~10 分钟，约 150 个包）…");
        var (ok, output) = RunStreaming(
            python,
            $"-X utf8 \"{DepsScript}\" --data-dir \"{DataDir}\" --features \"core,memory\" --openviking",
            1800,
            RepoRoot);
        return (ok, ok ? "openviking-server 已就绪" : $"安装失败：{Tail(output)}");
    }

    /// <summary>初始化本机 openviking：建 account + 服务用户 + 写 api_key（幂等）。</summary>
    public (bool ok, string message) InitOpenviking()
    {
        var python = string.IsNullOrWhiteSpace(PythonPath) ? FindPython() : PythonPath;
        if (string.IsNullOrWhiteSpace(python)) return (false, "找不到 python.exe");
        var script = Path.Combine(RepoRoot, "deploy", "launcher", "openviking_init.py");
        if (!File.Exists(script)) return (false, $"找不到 {script}");
        var url = "http://127.0.0.1:1933";
        var (ok, output) = Run(
            python, $"-X utf8 \"{script}\" --data-dir \"{DataDir}\" --url \"{url}\"", 120, RepoRoot);
        return (ok, ok ? "openviking 初始化完成（api_key 在 data/config/openviking/api_key）"
                       : $"初始化失败：{Tail(output)}");
    }

    private static string Tail(string text)
    {
        if (string.IsNullOrWhiteSpace(text)) return "";
        var lines = text.Trim().Split('\n');
        return lines.Length == 0 ? "" : lines[^1].Trim();
    }

    private (bool ok, string output) RunStreaming(string exe, string arguments, int timeoutSeconds, string workdir)
    {
        try
        {
            var psi = new ProcessStartInfo
            {
                FileName = exe,
                Arguments = arguments,
                WorkingDirectory = workdir,
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                StandardOutputEncoding = Encoding.UTF8,
                StandardErrorEncoding = Encoding.UTF8,
            };
            // 子进程输出一律 UTF-8：Windows 默认 locale 编码（cp1252/cp936）会让脚本里的中文 print 崩
            psi.Environment["PYTHONUTF8"] = "1";
            psi.Environment["PYTHONIOENCODING"] = "utf-8";
            using var proc = Process.Start(psi);
            if (proc is null) return (false, "进程未创建");
            var tail = new StringBuilder();
            proc.OutputDataReceived += (_, e) => { if (!string.IsNullOrWhiteSpace(e.Data)) { Say(e.Data); tail.AppendLine(e.Data); } };
            proc.ErrorDataReceived += (_, e) => { if (!string.IsNullOrWhiteSpace(e.Data)) { Say(e.Data); tail.AppendLine(e.Data); } };
            proc.BeginOutputReadLine();
            proc.BeginErrorReadLine();
            if (!proc.WaitForExit(timeoutSeconds * 1000))
            {
                try { proc.Kill(entireProcessTree: true); } catch { }
                return (false, "超时");
            }
            proc.WaitForExit();
            return (proc.ExitCode == 0, tail.ToString());
        }
        catch (Exception exc)
        {
            return (false, $"执行异常：{exc.Message}");
        }
    }

    /// <summary>安装共享 venv 依赖并生成 gRPC stubs（首次较慢，输出实时进日志）。</summary>
    public (bool ok, string message) InstallDeps(IEnumerable<string> features)
    {
        var python = string.IsNullOrWhiteSpace(PythonPath) ? FindPython() : PythonPath;
        if (string.IsNullOrWhiteSpace(python)) return (false, "找不到 python.exe");
        if (!File.Exists(DepsScript)) return (false, $"找不到 {DepsScript}");
        try
        {
            var psi = new ProcessStartInfo
            {
                FileName = python,
                Arguments = $"-X utf8 \"{DepsScript}\" --data-dir \"{DataDir}\" --features \"{string.Join(",", features)}\" --install",
                WorkingDirectory = RepoRoot,
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                StandardOutputEncoding = Encoding.UTF8,
                StandardErrorEncoding = Encoding.UTF8,
            };
            // 子进程输出一律 UTF-8：Windows 默认 locale 编码（cp1252/cp936）会让脚本里的中文 print 崩
            psi.Environment["PYTHONUTF8"] = "1";
            psi.Environment["PYTHONIOENCODING"] = "utf-8";
            using var proc = Process.Start(psi);
            if (proc is null) return (false, "依赖安装进程未创建");
            var tail = new StringBuilder();
            proc.OutputDataReceived += (_, e) => { if (!string.IsNullOrWhiteSpace(e.Data)) { Say(e.Data); tail.AppendLine(e.Data); } };
            proc.ErrorDataReceived += (_, e) => { if (!string.IsNullOrWhiteSpace(e.Data)) { Say(e.Data); tail.AppendLine(e.Data); } };
            proc.BeginOutputReadLine();
            proc.BeginErrorReadLine();
            proc.WaitForExit();
            var ok = proc.ExitCode == 0;
            return (ok, ok ? "依赖安装完成" : $"依赖安装失败（退出码 {proc.ExitCode}）");
        }
        catch (Exception exc)
        {
            return (false, $"依赖安装异常：{exc.Message}");
        }
    }

    /// <summary>
    /// 只导出依赖清单：按所选功能算出合并/覆盖后的 requirements，不建 venv、不装任何包。
    /// 给"自己用 conda/pip 装"的人用（面板上就是「只导出依赖清单」按钮）。
    /// </summary>
    public (bool ok, string message) ExportDeps(IEnumerable<string> features)
    {
        var python = string.IsNullOrWhiteSpace(PythonPath) ? FindPython() : PythonPath;
        if (string.IsNullOrWhiteSpace(python)) return (false, "找不到 python.exe");
        if (!File.Exists(DepsScript)) return (false, $"找不到 {DepsScript}");
        var outPath = Path.Combine(DataDir, "deps", "requirements.txt");
        Say($"导出依赖清单（deps.py --export，不安装）：{outPath}");
        var (ok, output) = RunStreaming(
            python,
            $"-X utf8 \"{DepsScript}\" --data-dir \"{DataDir}\" --features \"{string.Join(",", features)}\" --export \"{outPath}\"",
            180,
            RepoRoot);
        if (!ok) return (false, $"导出依赖清单失败：{Tail(output)}");
        var count = File.Exists(outPath) ? File.ReadAllLines(outPath).Count(ln => ln.Trim().Length > 0 && !ln.StartsWith("#")) : 0;
        return (true, $"依赖清单已导出：{outPath}（{count} 个包，没有安装任何东西）");
    }

    // ── 能力档案（探测）─────────────────────────────────────

    public (bool ok, string message) Probe()
    {
        var (ready, reason) = CheckReady();
        if (!ready) return (false, reason);
        var python = string.IsNullOrWhiteSpace(PythonPath) ? FindPython() : PythonPath;
        var outPath = Path.Combine(DataDir, "capability.json");
        var (ok, output) = Run(python, $"-X utf8 \"{ProbeScript}\" --out \"{outPath}\"", 60, RepoRoot);
        return (ok, ok ? $"能力档案已生成：{outPath}" : $"探测失败：{output}");
    }

    public string ReadCapabilitySummary()
    {
        var path = Path.Combine(DataDir, "capability.json");
        if (!File.Exists(path)) return "（尚未生成 capability.json）";
        try
        {
            using var doc = JsonDocument.Parse(File.ReadAllText(path, Encoding.UTF8));
            var root = doc.RootElement;
            var tier = root.TryGetProperty("runtime_tier", out var t) ? t.GetString() : "";
            var real = 0;
            var info = 0;
            if (root.TryGetProperty("degraded", out var d) && d.ValueKind == JsonValueKind.Array)
            {
                foreach (var item in d.EnumerateArray())
                {
                    // level=info 的是说明项（单体没 Docker、适配器优先级…），不算降级
                    var isInfo = item.ValueKind == JsonValueKind.Object
                                 && item.TryGetProperty("level", out var lv)
                                 && lv.ValueKind == JsonValueKind.String
                                 && string.Equals(lv.GetString(), "info", StringComparison.OrdinalIgnoreCase);
                    if (isInfo) info++; else real++;
                }
            }
            return info > 0
                ? $"档位 {tier}，降级 {real} 项（另有 {info} 条说明）"
                : $"档位 {tier}，降级 {real} 项";
        }
        catch (Exception exc)
        {
            return $"读取失败：{exc.Message}";
        }
    }

    // ── 启停 ────────────────────────────────────────────────

    public (bool ok, string message) Start(IEnumerable<string> features, string modes, string openvikingUrl = "",
                                           bool noDeps = false)
    {
        var (ready, reason) = CheckReady();
        if (!ready) return (false, reason);
        if (_launcher is { HasExited: false })
            return (true, "单体模式已在运行");

        var python = string.IsNullOrWhiteSpace(PythonPath) ? FindPython() : PythonPath;
        var args = $"-X utf8 \"{LauncherScript}\" --repo-root \"{RepoRoot}\" --data-dir \"{DataDir}\" " +
                   $"--features \"{string.Join(",", features)}\"";
        // --no-deps：用户自己装了依赖就别在启动时又去对齐/安装
        if (noDeps) args += " --no-deps";
        if (!string.IsNullOrWhiteSpace(modes))
            args += $" --modes \"{modes}\"";
        if (!string.IsNullOrWhiteSpace(openvikingUrl))
            args += $" --openviking-url \"{openvikingUrl}\"";

        try
        {
            var psi = new ProcessStartInfo
            {
                FileName = python,
                Arguments = args,
                WorkingDirectory = RepoRoot,
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                StandardOutputEncoding = Encoding.UTF8,
                StandardErrorEncoding = Encoding.UTF8,
            };
            // 子进程输出一律 UTF-8：Windows 默认 locale 编码（cp1252/cp936）会让脚本里的中文 print 崩
            psi.Environment["PYTHONUTF8"] = "1";
            psi.Environment["PYTHONIOENCODING"] = "utf-8";
            _launcher = Process.Start(psi);
            if (_launcher is null) return (false, "启动失败：进程未创建");
            _launcher.OutputDataReceived += (_, e) => { if (!string.IsNullOrWhiteSpace(e.Data)) Say(e.Data); };
            _launcher.ErrorDataReceived += (_, e) => { if (!string.IsNullOrWhiteSpace(e.Data)) Say(e.Data); };
            _launcher.BeginOutputReadLine();
            _launcher.BeginErrorReadLine();
            return (true, $"已启动单体模式（pid {_launcher.Id}）");
        }
        catch (Exception exc)
        {
            return (false, $"启动失败：{exc.Message}");
        }
    }

    public (bool ok, string message) Stop()
    {
        var python = string.IsNullOrWhiteSpace(PythonPath) ? FindPython() : PythonPath;
        var notes = new StringBuilder();
        if (File.Exists(LauncherScript))
        {
            var (ok, output) = Run(python, $"-X utf8 \"{LauncherScript}\" --repo-root \"{RepoRoot}\" --data-dir \"{DataDir}\" --stop", 60, RepoRoot);
            notes.Append(ok ? "已按 pids.json 停止子进程" : $"停止脚本返回：{output}");
        }
        if (_launcher is { HasExited: false })
        {
            try { _launcher.Kill(true); notes.Append("；launcher 已结束"); } catch { }
        }
        _launcher = null;
        return (true, notes.ToString());
    }

    public bool IsRunning => _launcher is { HasExited: false };

    // ── 状态 ────────────────────────────────────────────────

    public List<string> StatusLines()
    {
        var lines = new List<string>();
        var pids = Path.Combine(DataDir, "pids.json");
        if (!File.Exists(pids))
        {
            lines.Add("未启动（没有 pids.json）");
            return lines;
        }
        try
        {
            using var doc = JsonDocument.Parse(File.ReadAllText(pids, Encoding.UTF8));
            var root = doc.RootElement;
            lines.Add($"启动时间：{(root.TryGetProperty("started_at", out var at) ? at.GetString() : "-")}");
            if (root.TryGetProperty("processes", out var procs) && procs.ValueKind == JsonValueKind.Array)
            {
                foreach (var item in procs.EnumerateArray())
                {
                    var id = item.TryGetProperty("id", out var n) ? n.GetString() : "?";
                    var pid = item.TryGetProperty("pid", out var p) ? p.GetInt32() : 0;
                    var ports = new List<string>();
                    if (item.TryGetProperty("ports", out var portObj) && portObj.ValueKind == JsonValueKind.Object)
                    {
                        foreach (var port in portObj.EnumerateObject())
                        {
                            var value = port.Value.GetInt32();
                            ports.Add($"{port.Name}={value}{(PortOpen(value) ? "✔" : "✘")}");
                        }
                    }
                    lines.Add($"{id,-28} pid {pid,-7} {string.Join(" ", ports)}");
                }
            }
            if (root.TryGetProperty("not_ready", out var notReady) && notReady.ValueKind == JsonValueKind.Array && notReady.GetArrayLength() > 0)
                lines.Add($"未就绪：{string.Join(", ", notReady.EnumerateArray().Select(x => x.GetString()))}");
            lines.Add($"能力档案：{ReadCapabilitySummary()}");
        }
        catch (Exception exc)
        {
            lines.Add($"读取 pids.json 失败：{exc.Message}");
        }
        return lines;
    }

    public static bool PortOpen(int port)
    {
        try
        {
            using var client = new TcpClient();
            var task = client.ConnectAsync("127.0.0.1", port);
            return task.Wait(400) && client.Connected;
        }
        catch { return false; }
    }

    public string TailLog(string serviceId, int lines = 40)
    {
        var path = Path.Combine(DataDir, "logs", $"{serviceId}.log");
        if (!File.Exists(path)) return $"（没有 {serviceId} 的日志）";
        try
        {
            // 服务在跑时 launcher 一直持有这个文件的写句柄，必须显式允许共享读写，否则 ReadAllLines 会报"另一个进程占用"
            using var stream = new FileStream(path, FileMode.Open, FileAccess.Read,
                FileShare.ReadWrite | FileShare.Delete);
            using var reader = new StreamReader(stream, Encoding.UTF8);
            var all = new List<string>();
            while (reader.ReadLine() is { } line) all.Add(line);
            return string.Join(Environment.NewLine, all.TakeLast(lines));
        }
        catch (Exception exc)
        {
            return $"读取日志失败：{exc.Message}";
        }
    }

    // ── 执行辅助 ────────────────────────────────────────────

    private static (bool ok, string output) Run(string file, string args, int timeoutSeconds, string workdir)
    {
        try
        {
            var psi = new ProcessStartInfo
            {
                FileName = file,
                Arguments = args,
                WorkingDirectory = workdir,
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                StandardOutputEncoding = Encoding.UTF8,
                StandardErrorEncoding = Encoding.UTF8,
            };
            // 子进程输出一律 UTF-8：Windows 默认 locale 编码（cp1252/cp936）会让脚本里的中文 print 崩
            psi.Environment["PYTHONUTF8"] = "1";
            psi.Environment["PYTHONIOENCODING"] = "utf-8";
            using var proc = Process.Start(psi);
            if (proc is null) return (false, "进程未创建");
            var output = new StringBuilder();
            proc.OutputDataReceived += (_, e) => { if (e.Data != null) output.AppendLine(e.Data); };
            proc.ErrorDataReceived += (_, e) => { if (e.Data != null) output.AppendLine(e.Data); };
            proc.BeginOutputReadLine();
            proc.BeginErrorReadLine();
            // 注意：timeoutSeconds 是秒；用带超时的重载，子进程卡死时不会把界面一起挂住
            if (!proc.WaitForExit(Math.Max(1, timeoutSeconds) * 1000))
            {
                try { proc.Kill(true); } catch { }
                return (false, $"超时（{timeoutSeconds}s）");
            }
            proc.WaitForExit();
            return (proc.ExitCode == 0, output.ToString().Trim());
        }
        catch (Exception exc)
        {
            return (false, exc.Message);
        }
    }
}
