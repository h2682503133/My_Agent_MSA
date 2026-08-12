using System;
using System.Diagnostics;
using System.IO;
using System.Net.Http;
using System.Net.Http.Headers;
using System.Text;
using System.Threading.Tasks;

namespace MyAgentAdminPanel.Services;

public class LlBotService
{
    private Process? _llbotProcess;
    private FileSystemWatcher? _qrWatcher;
    private string _llbotFolder = "";
    private string _qqPath = "";
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
                LogReceived?.Invoke(e.Data);
                CheckAutoRestart(e.Data);
            }
        };
        _llbotProcess.ErrorDataReceived += (s, e) =>
        {
            if (!string.IsNullOrEmpty(e.Data))
            {
                LogReceived?.Invoke($"[ERR] {e.Data}");
                CheckAutoRestart(e.Data);
            }
        };

        _llbotProcess.Exited += (s, e) =>
        {
            _isRunning = false;
            LogReceived?.Invoke("[系统] llbot.exe 已退出");
            // 如果正在自动重启流程中，不再额外处理
            if (!_isRestarting)
            {
                LogReceived?.Invoke("[系统] 如需重启请手动点击启动");
            }
        };

        _llbotProcess.Start();
        _llbotProcess.BeginOutputReadLine();
        _llbotProcess.BeginErrorReadLine();
        _isRunning = true;
        _isRestarting = false;
        return _llbotProcess;
    }

    private async void CheckAutoRestart(string line)
    {
        if (_isRestarting) return;
        if (!line.Contains("正在终止 QQ 进程")) return;

        _isRestarting = true;
        LogReceived?.Invoke("[系统] 检测到「正在终止 QQ 进程」，3 秒后自动重启 LLBot...");
        await Task.Delay(3000);

        // 停止当前进程
        StopLlbot();
        await Task.Delay(1000);

        // 重新启动
        try
        {
            StartLlbot();
            _isRestarting = false;
        }
        catch (Exception ex)
        {
            LogReceived?.Invoke($"[ERR] 自动重启失败: {ex.Message}");
            _isRestarting = false;
        }
    }

    public void StopLlbot()
    {
        if (_llbotProcess == null || !_isRunning) return;
        try
        {
            if (!_llbotProcess.HasExited)
            {
                _llbotProcess.Kill(entireProcessTree: true);
                _llbotProcess.WaitForExit(3000);
            }
        }
        catch { }
        _isRunning = false;
        _llbotProcess = null;
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
