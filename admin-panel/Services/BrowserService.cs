using System;
using System.Collections.Concurrent;
using System.Diagnostics;
using System.IO;
using System.Net;
using System.Net.Sockets;
using System.Text;
using System.Threading;
using System.Threading.Tasks;

namespace MyAgentAdminPanel.Services;

/// <summary>
/// 浏览器环境（两套模式，都由本面板执行；不写注册表、不做开机自启）：
///
/// A. Playwright MCP（推荐，见《Edge_Playwright_MCP_经验汇总》）：
///    面板起 `npx @playwright/mcp`（它自己托管 Edge + 固定 profile，不需要调试端口），
///    再把只监听回环的 MCP 端口经「进程内中继 → 发布容器」暴露给 k8s pod：
///       pod → 172.18.0.1:9335（容器）→ host.docker.internal:9334（中继）→ ::1:9333（MCP）
///
/// B. CDP 兼容模式（旧方案，保留作兜底）：
///    面板以 `--remote-debugging-port=9222` 启动独立 profile 的 Edge：
///       pod → 172.18.0.1:9224（容器）→ host.docker.internal:9223（中继）→ 127.0.0.1:9222
///    注意：Chromium 136+ 禁止对默认 profile 开调试端口，所以必须是独立 profile。
/// </summary>
public class BrowserService : IDisposable
{
    // ── 通用 ────────────────────────────────────────────────
    public string ContainerName { get; set; } = "cdp-relay";
    public string SocatImage { get; set; } = "hub.rat.dev/alpine/socat:latest";

    // ── MCP 模式（默认）────────────────────────────────────
    public string NpxPath { get; set; } = "";
    public string McpProfileDir { get; set; } = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
        "MyAgentAdminPanel", "mcp-edge-profile");
    public int McpPort { get; set; } = 9333;
    public int McpRelayPort { get; set; } = 9334;
    public int McpPublishedPort { get; set; } = 9335;
    public string McpContainerName { get; set; } = "mcp-relay";

    // ── CDP 兼容模式 ───────────────────────────────────────
    public string EdgePath { get; set; } = "";
    public string ProfileDir { get; set; } = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
        "MyAgentAdminPanel", "edge-profile");
    public int DebugPort { get; set; } = 9222;
    public int RelayPort { get; set; } = 9223;
    public int PublishedPort { get; set; } = 9224;

    private TcpListener? _relay;
    private CancellationTokenSource? _relayCts;
    private readonly ConcurrentDictionary<int, TcpClient> _relayClients = new();
    private Process? _mcpProcess;

    public event Action<string>? Log;
    private void Say(string message) => Log?.Invoke(message);

    // ── 通用工具 ────────────────────────────────────────────

    public static bool IsPortListening(int port)
    {
        foreach (var address in new[] { IPAddress.Loopback, IPAddress.IPv6Loopback })
        {
            try
            {
                using var client = new TcpClient(address.AddressFamily);
                var task = client.ConnectAsync(address, port);
                if (task.Wait(600) && client.Connected) return true;
            }
            catch { }
        }
        return false;
    }

    public static string FindEdge()
    {
        var candidates = new[]
        {
            @"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            @"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
                @"Microsoft\Edge\Application\msedge.exe"),
        };
        foreach (var path in candidates)
            if (File.Exists(path)) return path;
        return "";
    }

    public static string FindNpx()
    {
        foreach (var dir in (Environment.GetEnvironmentVariable("PATH") ?? "").Split(Path.PathSeparator))
        {
            if (string.IsNullOrWhiteSpace(dir)) continue;
            foreach (var name in new[] { "npx.cmd", "npx.exe" })
            {
                try
                {
                    var path = Path.Combine(dir.Trim(), name);
                    if (File.Exists(path)) return path;
                }
                catch { }
            }
        }
        return "";
    }

    private static (bool ok, string output) Run(string file, string args, int timeoutMs = 20000)
    {
        try
        {
            var psi = new ProcessStartInfo
            {
                FileName = file,
                Arguments = args,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                UseShellExecute = false,
                CreateNoWindow = true,
            };
            using var proc = Process.Start(psi);
            if (proc is null) return (false, "进程启动失败");
            var output = proc.StandardOutput.ReadToEnd() + proc.StandardError.ReadToEnd();
            if (!proc.WaitForExit(timeoutMs))
            {
                try { proc.Kill(true); } catch { }
                return (false, "命令超时");
            }
            return (proc.ExitCode == 0, output.Trim());
        }
        catch (Exception exc)
        {
            return (false, exc.Message);
        }
    }

    // ── Playwright MCP ─────────────────────────────────────

    public bool IsMcpUp() => IsPortListening(McpPort);

    public (bool ok, string message) StartMcp()
    {
        if (IsMcpUp()) return (true, $"MCP 已在监听（{McpPort}）");

        var npx = string.IsNullOrWhiteSpace(NpxPath) ? FindNpx() : NpxPath;
        if (string.IsNullOrWhiteSpace(npx) || !File.Exists(npx))
            return (false, "找不到 npx.cmd，请安装 Node.js 或在设置里指定 npx 路径");

        try { Directory.CreateDirectory(McpProfileDir); }
        catch (Exception exc) { return (false, $"创建 MCP profile 目录失败：{exc.Message}"); }

        var args =
            $"--yes @playwright/mcp@latest --port {McpPort} --browser msedge " +
            $"--user-data-dir \"{McpProfileDir}\" --shared-browser-context " +
            "--allowed-hosts * --allowed-origins *";

        try
        {
            _mcpProcess = Process.Start(new ProcessStartInfo
            {
                FileName = npx,
                Arguments = args,
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
            });
            if (_mcpProcess is not null)
            {
                _mcpProcess.OutputDataReceived += (_, e) => { if (!string.IsNullOrWhiteSpace(e.Data)) Say($"[mcp] {e.Data}"); };
                _mcpProcess.ErrorDataReceived += (_, e) => { if (!string.IsNullOrWhiteSpace(e.Data)) Say($"[mcp] {e.Data}"); };
                _mcpProcess.BeginOutputReadLine();
                _mcpProcess.BeginErrorReadLine();
            }
        }
        catch (Exception exc)
        {
            return (false, $"启动 MCP 失败：{exc.Message}");
        }

        // 第一次运行要下载 npm 包，给足时间
        for (var i = 0; i < 120; i++)
        {
            Thread.Sleep(1000);
            if (IsMcpUp()) return (true, $"MCP 已启动（{McpPort}，profile={McpProfileDir}）");
        }
        return (false, "120 秒内没等到 MCP 监听；首次运行要下载 @playwright/mcp，稍后点「刷新状态」确认");
    }

    public (bool ok, string message) StopMcp()
    {
        if (_mcpProcess is { HasExited: false })
        {
            try
            {
                // Kill(true) 连带结束它拉起的 Edge
                _mcpProcess.Kill(true);
                _mcpProcess = null;
                return (true, "已结束面板启动的 MCP（含它托管的 Edge）");
            }
            catch (Exception exc)
            {
                return (false, $"结束 MCP 失败：{exc.Message}");
            }
        }

        if (!IsMcpUp()) return (true, "MCP 本来就没在运行");
        var (killed, message) = KillByPort(McpPort);
        return (killed, message);
    }

    // ── 中继（两种模式共用）────────────────────────────────

    public bool IsRelayUp(int port) => IsPortListening(port);

    public (bool ok, string message) StartRelay(int listenPort, int targetPort)
    {
        if (IsRelayUp(listenPort)) return (true, $"中继已在监听（{listenPort}）");

        try
        {
            StopRelay();
            _relayCts = new CancellationTokenSource();
            _relay = new TcpListener(IPAddress.Any, listenPort);
            _relay.Start();
        }
        catch (Exception exc)
        {
            return (false, $"中继监听 {listenPort} 失败：{exc.Message}");
        }

        var token = _relayCts.Token;
        var listener = _relay;
        _ = Task.Run(async () =>
        {
            while (!token.IsCancellationRequested)
            {
                TcpClient inbound;
                try { inbound = await listener.AcceptTcpClientAsync(token); }
                catch (OperationCanceledException) { break; }
                catch (Exception exc) { Say($"[relay] 接收连接失败：{exc.Message}"); continue; }
                _ = Task.Run(() => PumpAsync(inbound, targetPort, token), token);
            }
        }, token);

        Say($"[relay] 监听 0.0.0.0:{listenPort} → 回环:{targetPort}");
        return (true, $"中继已启动（{listenPort} → 回环:{targetPort}）");
    }

    private async Task PumpAsync(TcpClient inbound, int targetPort, CancellationToken token)
    {
        var id = inbound.GetHashCode();
        _relayClients[id] = inbound;
        using var upstream = new TcpClient();
        try
        {
            // MCP 监听 ::1、CDP 监听 127.0.0.1：两个都试
            var address = FirstReachable(new[] { IPAddress.IPv6Loopback, IPAddress.Loopback }, targetPort);
            await upstream.ConnectAsync(address, targetPort, token);
            var a = inbound.GetStream();
            var b = upstream.GetStream();
            var t1 = a.CopyToAsync(b, token);
            var t2 = b.CopyToAsync(a, token);
            await Task.WhenAny(t1, t2);
        }
        catch (Exception exc)
        {
            Say($"[relay] 转发结束：{exc.Message}");
        }
        finally
        {
            try { inbound.Close(); } catch { }
            try { upstream.Close(); } catch { }
            _relayClients.TryRemove(id, out _);
        }
    }

    private static IPAddress FirstReachable(IPAddress[] addresses, int port)
    {
        foreach (var address in addresses)
        {
            try
            {
                using var probe = new TcpClient(address.AddressFamily);
                var task = probe.ConnectAsync(address, port);
                if (task.Wait(1500) && probe.Connected) return address;
            }
            catch { }
        }
        return IPAddress.Loopback;
    }

    public void StopRelay()
    {
        try { _relayCts?.Cancel(); } catch { }
        try { _relay?.Stop(); } catch { }
        foreach (var client in _relayClients.Values)
        {
            try { client.Close(); } catch { }
        }
        _relayClients.Clear();
        _relay = null;
        _relayCts?.Dispose();
        _relayCts = null;
        Say("[relay] 已停止");
    }

    // ── 容器发布 ────────────────────────────────────────────

    public (bool running, string message) ContainerState(string name)
    {
        var (ok, output) = Run("docker", $"ps --filter \"name=^{name}$\" --format \"{{{{.Names}}}}\"");
        if (!ok) return (false, output);
        var running = output.Contains(name);
        return (running, running ? "运行中" : "未运行");
    }

    public (bool ok, string message) EnsureContainer(string name, int publishedPort, int relayPort)
    {
        var (existsOk, exists) = Run("docker", $"ps -a --filter \"name=^{name}$\" --format \"{{{{.Names}}}}\"");
        if (!existsOk) return (false, $"docker 不可用：{exists}");

        if (exists.Contains(name))
        {
            var (running, _) = ContainerState(name);
            if (running) return (true, $"{name} 容器运行中");
            var (startOk, startOut) = Run("docker", $"start {name}");
            return (startOk, startOk ? $"{name} 已重新启动" : $"启动容器失败：{startOut}");
        }

        var args = $"run -d --name {name} --restart unless-stopped " +
                   $"-p {publishedPort}:{publishedPort} {SocatImage} " +
                   $"tcp-listen:{publishedPort},fork,reuseaddr " +
                   $"tcp-connect:host.docker.internal:{relayPort}";
        var (ok, output) = Run("docker", args, 180000);
        return (ok, ok ? $"已创建并发布 {publishedPort}" : $"创建容器失败：{output}");
    }

    public (bool ok, string message) StopContainer(string name)
    {
        var (running, _) = ContainerState(name);
        if (!running) return (true, $"{name} 容器本来就没运行");
        var (ok, output) = Run("docker", $"stop {name}", 60000);
        return (ok, ok ? $"已停止 {name} 容器" : $"停止容器失败：{output}");
    }

    // ── 一键 ────────────────────────────────────────────────

    public (bool ok, string message) StartMcpAll()
    {
        var notes = new StringBuilder();

        var (mcpOk, mcpMsg) = StartMcp();
        notes.AppendLine($"[MCP] {mcpMsg}");
        if (!mcpOk) return (false, notes.ToString().Trim());

        var (relayOk, relayMsg) = StartRelay(McpRelayPort, McpPort);
        notes.AppendLine($"[中继] {relayMsg}");
        if (!relayOk) return (false, notes.ToString().Trim());

        var (containerOk, containerMsg) = EnsureContainer(McpContainerName, McpPublishedPort, McpRelayPort);
        notes.AppendLine($"[容器] {containerMsg}");
        return (containerOk, notes.ToString().Trim());
    }

    public string StopMcpAll()
    {
        StopRelay();
        var (_, containerMsg) = StopContainer(McpContainerName);
        var (_, mcpMsg) = StopMcp();
        return $"已停止中继；{containerMsg}；{mcpMsg}";
    }

    // ── CDP 兼容模式 ───────────────────────────────────────

    public bool IsEdgeDebugPortUp() => IsPortListening(DebugPort);

    public (bool ok, string message) StartEdge()
    {
        if (IsEdgeDebugPortUp())
            return (true, $"Edge 调试端口已在监听（{DebugPort}），未重复启动");

        var exe = string.IsNullOrWhiteSpace(EdgePath) ? FindEdge() : EdgePath;
        if (!File.Exists(exe))
            return (false, "找不到 msedge.exe，请在面板里手动指定路径");

        try { Directory.CreateDirectory(ProfileDir); }
        catch (Exception exc) { return (false, $"无法创建 profile 目录：{exc.Message}"); }

        try
        {
            Process.Start(new ProcessStartInfo
            {
                FileName = exe,
                Arguments = $"--remote-debugging-port={DebugPort} --user-data-dir=\"{ProfileDir}\" about:blank",
                UseShellExecute = false,
            });
        }
        catch (Exception exc)
        {
            return (false, $"启动 Edge 失败：{exc.Message}");
        }

        for (var i = 0; i < 20; i++)
        {
            Thread.Sleep(500);
            if (IsEdgeDebugPortUp()) return (true, $"Edge 已启动（profile={ProfileDir}）");
        }
        return (false, $"Edge 已启动但 {DebugPort} 未监听：确认没有别的 Edge 实例占用该 profile");
    }

    public (bool ok, string message) StartCdpAll()
    {
        var notes = new StringBuilder();

        var (edgeOk, edgeMsg) = StartEdge();
        notes.AppendLine($"[Edge] {edgeMsg}");
        if (!edgeOk) return (false, notes.ToString().Trim());

        var (relayOk, relayMsg) = StartRelay(RelayPort, DebugPort);
        notes.AppendLine($"[中继] {relayMsg}");
        if (!relayOk) return (false, notes.ToString().Trim());

        var (containerOk, containerMsg) = EnsureContainer(ContainerName, PublishedPort, RelayPort);
        notes.AppendLine($"[容器] {containerMsg}");
        return (containerOk, notes.ToString().Trim());
    }

    public string StopCdpAll()
    {
        StopRelay();
        var (_, containerMsg) = StopContainer(ContainerName);
        return $"已停止中继；{containerMsg}\n（Edge 未关闭，可能你正在用）";
    }

    // ── 状态汇总 ────────────────────────────────────────────

    public string McpStatusSummary()
    {
        var (running, _) = ContainerState(McpContainerName);
        return $"MCP 端口 {McpPort}: {(IsMcpUp() ? "监听中" : "未监听")}    profile: {McpProfileDir}\n" +
               $"中继 {McpRelayPort} → 回环:{McpPort}: {(IsRelayUp(McpRelayPort) ? "监听中" : "未监听")}\n" +
               $"发布容器 {McpContainerName} ({McpPublishedPort}): {(running ? "运行中" : "未运行")}\n" +
               $"tool-runtime 的 BROWSER_MCP_URL 应为：http://<节点网段>.1:{McpPublishedPort}/mcp";
    }

    public string CdpStatusSummary()
    {
        var (running, _) = ContainerState(ContainerName);
        return $"Edge 调试端口 {DebugPort}: {(IsEdgeDebugPortUp() ? "监听中" : "未监听")}    profile: {ProfileDir}\n" +
               $"中继 {RelayPort} → 回环:{DebugPort}: {(IsRelayUp(RelayPort) ? "监听中" : "未监听")}\n" +
               $"发布容器 {ContainerName} ({PublishedPort}): {(running ? "运行中" : "未运行")}";
    }

    private static (bool ok, string message) KillByPort(int port)
    {
        try
        {
            var (ok, output) = Run("netstat", "-ano");
            if (!ok) return (false, "netstat 执行失败");
            foreach (var line in output.Split('\n'))
            {
                if (!line.Contains($":{port}") || !line.Contains("LISTENING")) continue;
                var parts = line.Split(' ', StringSplitOptions.RemoveEmptyEntries);
                if (parts.Length < 4) continue;
                if (!int.TryParse(parts[^1], out var pid)) continue;
                try
                {
                    Process.GetProcessById(pid).Kill(true);
                    return (true, $"端口 {port} 上的进程（PID {pid}）已结束");
                }
                catch
                {
                    return (false, $"端口 {port} 被 PID {pid} 占用，但结束失败");
                }
            }
        }
        catch (Exception exc)
        {
            return (false, $"按端口结束进程失败：{exc.Message}");
        }
        return (true, $"端口 {port} 上没有监听进程");
    }

    public void Dispose() => StopRelay();
}
