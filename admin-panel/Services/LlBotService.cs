using System;
using System.Diagnostics;
using System.IO;
using System.Net.Http;
using System.Text;
using System.Text.Json;
using System.Threading.Tasks;

namespace MyAgentAdminPanel.Services;

public class LlBotService
{
    private Process? _llbotProcess;
    private FileSystemWatcher? _qrWatcher;
    private string _llbotFolder = "";
    private bool _isRunning;

    public event Action<string>? LogReceived;
    public event Action<string>? QrCodeChanged; // qrcode.png path
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

        var psi = new ProcessStartInfo
        {
            FileName = exePath,
            WorkingDirectory = _llbotFolder,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false,
            CreateNoWindow = true
        };

        _llbotProcess = new Process { StartInfo = psi };
        _llbotProcess.OutputDataReceived += (s, e) =>
        {
            if (!string.IsNullOrEmpty(e.Data))
                LogReceived?.Invoke(e.Data);
        };
        _llbotProcess.ErrorDataReceived += (s, e) =>
        {
            if (!string.IsNullOrEmpty(e.Data))
                LogReceived?.Invoke($"[ERR] {e.Data}");
        };

        _llbotProcess.Exited += (s, e) =>
        {
            _isRunning = false;
            LogReceived?.Invoke("[系统] llbot.exe 已退出");
        };

        _llbotProcess.Start();
        _llbotProcess.BeginOutputReadLine();
        _llbotProcess.BeginErrorReadLine();
        _isRunning = true;
        return _llbotProcess;
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

    // Push QR code to dashboard-service
    public async Task PushQrCodeToDashboard(string dashboardUrl = "http://localhost:5601")
    {
        var qrPath = QrCodePath;
        if (!File.Exists(qrPath)) return;

        try
        {
            using var client = new HttpClient { Timeout = TimeSpan.FromSeconds(5) };
            var content = new ByteArrayContent(File.ReadAllBytes(qrPath));
            content.Headers.ContentType = new System.Net.Http.Headers.MediaTypeHeaderValue("image/png");
            var resp = await client.PostAsync($"{dashboardUrl}/api/qrcode", content);
            LogReceived?.Invoke($"[系统] 二维码已推送到 Dashboard: {resp.StatusCode}");
        }
        catch (Exception ex)
        {
            LogReceived?.Invoke($"[系统] 推送二维码到 Dashboard 失败: {ex.Message}");
        }
    }
}
