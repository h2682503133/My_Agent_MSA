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
/// 浏览器环境（Edge + CDP）：
///   - 用独立 profile 启动 Edge 并开调试端口（Chromium 136+ 禁止对默认 profile 开）；
///   - 进程内做 TCP 中继：RelayPort → 127.0.0.1:DebugPort（DevTools 只绑回环）；
///   - 确保 cdp-relay 容器把 PublishedPort 发布出来（k8s pod 走节点网段 .1 访问它）。
/// 全部靠本面板执行，不写注册表、不做开机自启。
/// </summary>
public class BrowserService : IDisposable
{
    public string EdgePath { get; set; } = "";
    public string ProfileDir { get; set; } = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
        "MyAgentAdminPanel", "edge-profile");
    public int DebugPort { get; set; } = 9222;
    public int RelayPort { get; set; } = 9223;
    public int PublishedPort { get; set; } = 9224;
    public string ContainerName { get; set; } = "cdp-relay";
    public string SocatImage { get; set; } = "hub.rat.dev/alpine/socat:latest";

    private TcpListener? _relay;
    private CancellationTokenSource? _relayCts;
    private readonly ConcurrentDictionary<int, TcpClient> _relayClients = new();

    public event Action<string>? Log;

    private void Say(string message) => Log?.Invoke(message);

    // ── 通用工具 ─────────────────────────────────────────────

    public static bool IsPortListening(int port)
    {
        try
        {
            using var client = new TcpClient();
            var task = client.ConnectAsync(IPAddress.Loopback, port);
            return task.Wait(600) && client.Connected;
        }
        catch
        {
            return false;
        }
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

    // ── Edge ────────────────────────────────────────────────

    public bool IsEdgeDebugPortUp() => IsPortListening(DebugPort);

    public (bool ok, string message) StartEdge()
    {
        if (IsEdgeDebugPortUp())
            return (true, $"Edge 调试端口已在监听（{DebugPort}），未重复启动");

        var exe = string.IsNullOrWhiteSpace(EdgePath) ? FindEdge() : EdgePath;
        if (!File.Exists(exe))
            return (false, "找不到 msedge.exe，请在面板里手动指定路径");

        try
        {
            Directory.CreateDirectory(ProfileDir);
        }
        catch (Exception exc)
        {
            return (false, $"无法创建 profile 目录：{exc.Message}");
        }

        try
        {
            // 注意：不传 --remote-debugging-address（Chromium 会忽略），DevTools 只绑回环，
            // 由下面的进程内中继把端口暴露给容器网络。
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

    // ── 进程内 TCP 中继 ──────────────────────────────────────

    public bool IsRelayUp() => IsPortListening(RelayPort);

    public (bool ok, string message) StartRelay()
    {
        if (IsRelayUp()) return (true, $"中继已在监听（{RelayPort}）");

        try
        {
            _relayCts = new CancellationTokenSource();
            _relay = new TcpListener(IPAddress.Any, RelayPort);
            _relay.Start();
        }
        catch (Exception exc)
        {
            return (false, $"中继监听 {RelayPort} 失败：{exc.Message}");
        }

        var token = _relayCts.Token;
        var listener = _relay;
        _ = Task.Run(async () =>
        {
            while (!token.IsCancellationRequested)
            {
                TcpClient inbound;
                try
                {
                    inbound = await listener.AcceptTcpClientAsync(token);
                }
                catch (OperationCanceledException) { break; }
                catch (Exception exc)
                {
                    Say($"[relay] 接收连接失败：{exc.Message}");
                    continue;
                }
                _ = Task.Run(() => PumpAsync(inbound, token), token);
            }
        }, token);

        Say($"[relay] 监听 0.0.0.0:{RelayPort} → 127.0.0.1:{DebugPort}");
        return (true, $"中继已启动（{RelayPort} → {DebugPort}）");
    }

    private async Task PumpAsync(TcpClient inbound, CancellationToken token)
    {
        var id = inbound.GetHashCode();
        _relayClients[id] = inbound;
        using var upstream = new TcpClient();
        try
        {
            await upstream.ConnectAsync(IPAddress.Loopback, DebugPort, token);
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

    // ── 容器（把中继端口发布出去）────────────────────────────

    public (bool running, string message) ContainerState()
    {
        var (ok, output) = Run("docker", $"ps --filter \"name=^{ContainerName}$\" --format \"{{{{.Names}}}}\"");
        if (!ok) return (false, output);
        var running = output.Contains(ContainerName);
        return (running, running ? "运行中" : "未运行");
    }

    public (bool ok, string message) EnsureContainer()
    {
        var (existsOk, exists) = Run("docker", $"ps -a --filter \"name=^{ContainerName}$\" --format \"{{{{.Names}}}}\"");
        if (!existsOk) return (false, $"docker 不可用：{exists}");

        if (exists.Contains(ContainerName))
        {
            var (running, _) = ContainerState();
            if (running) return (true, "cdp-relay 容器运行中");
            var (startOk, startOut) = Run("docker", $"start {ContainerName}");
            return (startOk, startOk ? "cdp-relay 已重新启动" : $"启动容器失败：{startOut}");
        }

        var args = $"run -d --name {ContainerName} --restart unless-stopped " +
                   $"-p {PublishedPort}:{PublishedPort} {SocatImage} " +
                   $"tcp-listen:{PublishedPort},fork,reuseaddr " +
                   $"tcp-connect:host.docker.internal:{RelayPort}";
        var (ok, output) = Run("docker", args, 120000);
        return (ok, ok ? $"已创建并发布 {PublishedPort}" : $"创建容器失败：{output}");
    }

    public (bool ok, string message) StopContainer()
    {
        var (running, _) = ContainerState();
        if (!running) return (true, "cdp-relay 容器本来就没运行");
        var (ok, output) = Run("docker", $"stop {ContainerName}", 60000);
        return (ok, ok ? "已停止 cdp-relay 容器" : $"停止容器失败：{output}");
    }

    // ── 一键 ────────────────────────────────────────────────

    public (bool ok, string message) StartAll()
    {
        var notes = new StringBuilder();

        var (edgeOk, edgeMsg) = StartEdge();
        notes.AppendLine($"[Edge] {edgeMsg}");
        if (!edgeOk) return (false, notes.ToString().Trim());

        var (relayOk, relayMsg) = StartRelay();
        notes.AppendLine($"[中继] {relayMsg}");
        if (!relayOk) return (false, notes.ToString().Trim());

        var (containerOk, containerMsg) = EnsureContainer();
        notes.AppendLine($"[容器] {containerMsg}");
        return (containerOk, notes.ToString().Trim());
    }

    public string StopAll()
    {
        StopRelay();
        var (containerOk, containerMsg) = StopContainer();
        return $"已停止中继；{containerMsg}\n（Edge 未关闭，可能你正在用）";
    }

    public string StatusSummary()
    {
        var (running, _) = ContainerState();
        return $"Edge 调试端口 {DebugPort}: {(IsEdgeDebugPortUp() ? "监听中" : "未监听")}\n" +
               $"宿主机中继 {RelayPort}: {(IsRelayUp() ? "监听中" : "未监听")}\n" +
               $"发布容器 {ContainerName} ({PublishedPort}): {(running ? "运行中" : "未运行")}\n" +
               $"Profile: {ProfileDir}";
    }

    public void Dispose()
    {
        StopRelay();
    }
}
