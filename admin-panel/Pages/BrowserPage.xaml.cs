using System;
using System.Diagnostics;
using System.IO;
using System.Text.Json;
using System.Windows;
using System.Windows.Controls;
using MyAgentAdminPanel.Services;

namespace MyAgentAdminPanel.Pages;

public partial class BrowserPage : Page
{
    private readonly BrowserService _browser;
    private static readonly string SettingsDir = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
        "MyAgentAdminPanel");
    private static readonly string SettingsFile = Path.Combine(SettingsDir, "settings.json");

    public BrowserPage(BrowserService browser)
    {
        InitializeComponent();
        _browser = browser;
        _browser.Log += OnLog;
        Loaded += (_, _) => RefreshStatus();
        IsVisibleChanged += (_, _) => { if (IsVisible) RefreshStatus(); };

        TxtNpxPath.Text = string.IsNullOrWhiteSpace(_browser.NpxPath) ? BrowserService.FindNpx() : _browser.NpxPath;
        TxtEdgePath.Text = string.IsNullOrWhiteSpace(_browser.EdgePath) ? BrowserService.FindEdge() : _browser.EdgePath;
        TxtMcpProfile.Text = _browser.McpProfileDir;
        TxtPorts.Text = $"{_browser.McpPort},{_browser.McpRelayPort},{_browser.McpPublishedPort}";
        AppendLog("面板就绪（推荐用 Playwright MCP）");
    }

    private void OnLog(string line) => Dispatcher.Invoke(() => AppendLog(line));

    private void AppendLog(string line)
    {
        TxtLog.AppendText($"[{DateTime.Now:HH:mm:ss}] {line}{Environment.NewLine}");
        TxtLog.ScrollToEnd();
    }

    private void ApplySettings()
    {
        _browser.NpxPath = (TxtNpxPath.Text ?? "").Trim();
        _browser.EdgePath = (TxtEdgePath.Text ?? "").Trim();
        var profile = (TxtMcpProfile.Text ?? "").Trim();
        if (!string.IsNullOrWhiteSpace(profile)) _browser.McpProfileDir = profile;

        var parts = (TxtPorts.Text ?? "").Split(',', StringSplitOptions.TrimEntries | StringSplitOptions.RemoveEmptyEntries);
        if (parts.Length == 3 &&
            int.TryParse(parts[0], out var mcp) &&
            int.TryParse(parts[1], out var relay) &&
            int.TryParse(parts[2], out var published))
        {
            _browser.McpPort = mcp;
            _browser.McpRelayPort = relay;
            _browser.McpPublishedPort = published;
        }
    }

    private void RefreshStatus()
    {
        ApplySettings();
        TxtMcpStatus.Text = _browser.McpStatusSummary();
        TxtCdpStatus.Text = _browser.CdpStatusSummary();
    }

    private void BtnRefresh_Click(object sender, RoutedEventArgs e) => RefreshStatus();

    private void BtnDetectNpx_Click(object sender, RoutedEventArgs e)
    {
        var found = BrowserService.FindNpx();
        TxtNpxPath.Text = found;
        AppendLog(string.IsNullOrWhiteSpace(found) ? "没找到 npx.cmd，请安装 Node.js" : $"npx：{found}");
    }

    private void BtnDetectEdge_Click(object sender, RoutedEventArgs e)
    {
        var found = BrowserService.FindEdge();
        TxtEdgePath.Text = found;
        AppendLog(string.IsNullOrWhiteSpace(found) ? "没找到 msedge.exe，请手动指定" : $"Edge：{found}");
    }

    private void BtnPickProfile_Click(object sender, RoutedEventArgs e)
    {
        using var dialog = new System.Windows.Forms.FolderBrowserDialog
        {
            Description = "选择 MCP 的 Edge profile 目录（登录态存在这里）",
            SelectedPath = Directory.Exists(TxtMcpProfile.Text) ? TxtMcpProfile.Text : SettingsDir,
        };
        if (dialog.ShowDialog() == System.Windows.Forms.DialogResult.OK)
            TxtMcpProfile.Text = dialog.SelectedPath;
    }

    private void BtnSave_Click(object sender, RoutedEventArgs e)
    {
        ApplySettings();
        SaveSettings();
        AppendLog("设置已保存");
        RefreshStatus();
    }

    private void BtnMcpStartAll_Click(object sender, RoutedEventArgs e)
    {
        ApplySettings();
        SaveSettings();
        AppendLog("开始启动 MCP 环境（首次运行 npx 要下载包，可能要等一两分钟）…");
        var (ok, message) = _browser.StartMcpAll();
        AppendLog(message);
        AppendLog(ok ? "✔ MCP 环境已就绪" : "✘ 启动未完成，按上面的提示处理");
        RefreshStatus();
    }

    private void BtnMcpStop_Click(object sender, RoutedEventArgs e)
    {
        AppendLog(_browser.StopMcpAll());
        RefreshStatus();
    }

    private void BtnCdpStartAll_Click(object sender, RoutedEventArgs e)
    {
        ApplySettings();
        SaveSettings();
        AppendLog("开始启动 CDP 环境…");
        var (ok, message) = _browser.StartCdpAll();
        AppendLog(message);
        AppendLog(ok ? "✔ CDP 环境已就绪" : "✘ 启动未完成");
        RefreshStatus();
    }

    private void BtnCdpStop_Click(object sender, RoutedEventArgs e)
    {
        AppendLog(_browser.StopCdpAll());
        RefreshStatus();
    }

    private void BtnOpenDashboard_Click(object sender, RoutedEventArgs e)
    {
        const string url = "http://127.0.0.1:5700";
        try
        {
            Process.Start(new ProcessStartInfo { FileName = url, UseShellExecute = true });
            AppendLog($"已打开 {url}");
        }
        catch (Exception exc)
        {
            AppendLog($"打开失败：{exc.Message}");
        }
    }

    private void SaveSettings()
    {
        try
        {
            Directory.CreateDirectory(SettingsDir);
            var data = new BrowserSettings
            {
                NpxPath = _browser.NpxPath,
                EdgePath = _browser.EdgePath,
                McpProfileDir = _browser.McpProfileDir,
                McpPort = _browser.McpPort,
                McpRelayPort = _browser.McpRelayPort,
                McpPublishedPort = _browser.McpPublishedPort,
            };
            File.WriteAllText(SettingsFile, JsonSerializer.Serialize(data));
        }
        catch (Exception exc)
        {
            AppendLog($"保存设置失败：{exc.Message}");
        }
    }

    private class BrowserSettings
    {
        public string? NpxPath { get; set; }
        public string? EdgePath { get; set; }
        public string? McpProfileDir { get; set; }
        public int McpPort { get; set; } = 9333;
        public int McpRelayPort { get; set; } = 9334;
        public int McpPublishedPort { get; set; } = 9335;
    }
}
