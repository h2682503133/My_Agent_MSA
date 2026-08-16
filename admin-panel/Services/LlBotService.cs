using System;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Net.Http;
using System.Net.Http.Headers;
using System.Net.NetworkInformation;
using System.Text;
using System.Threading.Tasks;

namespace MyAgentAdminPanel.Services;

public class LlBotService
{
    private const int PmhqPort = 13000;
    private const int WebuiPort = 3080;

    private Process? _llbotProcess;
    private FileSystemWatcher? _qrWatcher;
    private string _llbotFolder = "";
    private string _qqPath = "";
    private int _qqPid;
    private bool _isRunning;
    private bool _isRestarting;

    public event Action<string>? LogReceived;
    public event Action<string>? QrCodeChanged;
    public bool IsRunning => _isRunning;

    public string LlbotFolder
    {
        get => _llbotFolder;
        set
        {
            _llbotFolder = value;
            SetupQrWatcher();
        }
    }

    public string QqPath
    {
        get => _qqPath;
        set => _qqPath = value;
    }

    public string QrCodePath => Path.Combine(_llbotFolder, "qrcode.png");
    public string QrCodeUrl => "https://api.2dcode.biz/v1/create-qr-code?data=https://txz.qq.com/p?k=QsqDyabub8d9o7dDpZWYtNgkibYvHD1m&f=1600001604";

    private void SetupQrWatcher()
    {
        _qrWatcher?.Dispose();
        if (string.IsNullOrEmpty(_llbotFolder) || !Directory.Exists(_llbotFolder))
            return;

        _qrWatcher = new FileSystemWatcher(_llbotFolder, "qrcode.png")
        {
            NotifyFilter = NotifyFilters.FileName | NotifyFilters.LastWrite | NotifyFilters.CreationTime,
            EnableRaisingEvents = true
        };

        _qrWatcher.Created += (s, e) => QrCodeChanged?.Invoke(e.FullPath);
        _qrWatcher.Changed += (s, e) => QrCodeChanged?.Invoke(e.FullPath);
    }

    public Process StartLlbot()
    {
        if (_isRunning) return _llbotProcess!;

        var exePath = Path.Combine(_llbotFolder, "llbot.exe");
        if (!File.Exists(exePath))
            throw new FileNotFoundException($"找不到 {exePath}");

        var utf8 = new UTF8Encoding(false);

        var args = "";
        if (!string.IsNullOrEmpty(_qqPath) && File.Exists(_qqPath))
            args = $"--qq-path \"{_qqPath}\"";

        var psi = new ProcessStartInfo
        {
            FileName = exePath,
            Arguments = args,
            WorkingDirectory = _llbotFolder,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            StandardOutputEncoding = utf8,
            StandardErrorEncoding = utf8,
            UseShellExecute = false,
            CreateNoWindow = true
        };

        _llbotProcess = new Process { StartInfo = psi };
        _llbotProcess.OutputDataReceived += (s, e) =>
        {
            if (!string.IsNullOrEmpty(e.Data))
            {
                OnLlbotLine(e.Data);
            }
        };
        _llbotProcess.ErrorDataReceived += (s, e) =>
        {
            if (!string.IsNullOrEmpty(e.Data))
            {
                OnLlbotLine($"[ERR] {e.Data}");
            }
        };

        _llbotProcess.Exited += OnLlbotExited;

        _llbotProcess.Start();
        _llbotProcess.BeginOutputReadLine();
        _llbotProcess.BeginErrorReadLine();
        _isRunning = true;
        _isRestarting = false;
        return _llbotProcess;
    }

    private void OnLlbotLine(string line)
    {
        LogReceived?.Invoke(line);
        TryTrackQqPid(line);
        CheckAutoRestart(line);
    }

    private void OnLlbotExited(object? sender, EventArgs e)
    {
        // 只处理当前进程的退出，避免旧进程异步退出事件覆盖新进程状态
        if (!ReferenceEquals(sender, _llbotProcess)) return;

        _isRunning = false;
        LogReceived?.Invoke("[系统] llbot.exe 已退出");
        if (!_isRestarting)
        {
            LogReceived?.Invoke("[系统] 如需重启请手动点击启动");
        }
    }

    private void TryTrackQqPid(string line)
    {
        const string marker = "QQ 进程 PID:";
        var idx = line.IndexOf(marker, StringComparison.Ordinal);
        if (idx < 0) return;

        var rest = line.Substring(idx + marker.Length).Trim();
        var digits = new string(rest.TakeWhile(char.IsDigit).ToArray());
        if (int.TryParse(digits, out var pid) && pid > 0)
            _qqPid = pid;
    }

    private async void CheckAutoRestart(string line)
    {
        if (_isRestarting) return;
        if (!line.Contains("正在终止 QQ 进程")) return;

        _isRestarting = true;
        LogReceived?.Invoke("[系统] 检测到「正在终止 QQ 进程」，3 秒后自动重启 LLBot...");
        await Task.Delay(3000);

        // 停止当前进程（连同 llbot 启动的 QQ）
        StopLlbot();

        // 等待旧实例占用的端口释放，避免新实例 PMHQ 连接失败
        if (!await WaitPortsFreeAsync(TimeSpan.FromSeconds(20)))
            LogReceived?.Invoke("[系统] 端口未在限时内释放，仍尝试重启");

        try
        {
            StartLlbot();
        }
        catch (Exception ex)
        {
            LogReceived?.Invoke($"[ERR] 自动重启失败: {ex.Message}");
        }
        _isRestarting = false;
    }

    public void StopLlbot()
    {
        var proc = _llbotProcess;
        _llbotProcess = null;
        _isRunning = false;

        try
        {
            if (proc != null && !proc.HasExited)
            {
                proc.Kill(entireProcessTree: true);
                proc.WaitForExit(3000);
            }
        }
        catch { }

        // llbot 已退出时 QQ 可能变成孤儿进程，按 PID 精确清理，避免误杀其他 QQ
        if (_qqPid > 0)
        {
            try
            {
                var qq = Process.GetProcessById(_qqPid);
                if (!qq.HasExited && qq.ProcessName.Contains("QQ", StringComparison.OrdinalIgnoreCase))
                {
                    qq.Kill(entireProcessTree: true);
                    qq.WaitForExit(3000);
                }
            }
            catch { }
            _qqPid = 0;
        }
    }

    private static bool IsPortInUse(int port)
    {
        try
        {
            var props = IPGlobalProperties.GetIPGlobalProperties();
            foreach (var listener in props.GetActiveTcpListeners())
                if (listener.Port == port) return true;
            foreach (var conn in props.GetActiveTcpConnections())
                if (conn.LocalEndPoint.Port == port) return true;
        }
        catch
        {
            return true;
        }
        return false;
    }

    private static async Task<bool> WaitPortsFreeAsync(TimeSpan timeout)
    {
        var deadline = DateTime.UtcNow + timeout;
        while (DateTime.UtcNow < deadline)
        {
            if (!IsPortInUse(PmhqPort) && !IsPortInUse(WebuiPort))
                return true;
            await Task.Delay(500);
        }
        return !IsPortInUse(PmhqPort) && !IsPortInUse(WebuiPort);
    }

    public void Dispose()
    {
        StopLlbot();
        _qrWatcher?.Dispose();
    }

    public async Task PushQrCodeToDashboard(string dashboardUrl = "http://localhost:5700")
    {
        var qrPath = QrCodePath;
        if (!File.Exists(qrPath))
        {
            LogReceived?.Invoke("[系统] 推送失败：qrcode.png 不存在");
            return;
        }

        try
        {
            using var client = new HttpClient { Timeout = TimeSpan.FromSeconds(5) };
            using var formData = new MultipartFormDataContent();
            var fileBytes = File.ReadAllBytes(qrPath);
            var fileContent = new ByteArrayContent(fileBytes);
            fileContent.Headers.ContentType = new MediaTypeHeaderValue("image/png");
            formData.Add(fileContent, "file", "qrcode.png");

            var resp = await client.PostAsync($"{dashboardUrl}/api/qrcode", formData);
            var body = await resp.Content.ReadAsStringAsync();
            LogReceived?.Invoke($"[系统] 二维码推送结果: {resp.StatusCode} {body}");
        }
        catch (Exception ex)
        {
            LogReceived?.Invoke($"[系统] 推送二维码到 Dashboard 失败: {ex.Message}");
        }
    }
}
