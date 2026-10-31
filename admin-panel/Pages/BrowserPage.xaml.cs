using System;
using System.Diagnostics;
using System.IO;
using System.Text.Json;
using System.Windows;
using System.Windows.Controls;
using Microsoft.Win32;
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
        Loaded += (s, e) => { RefreshStatus(); };
        IsVisibleChanged += (s, e) => { if (IsVisible) RefreshStatus(); };

        TxtEdgePath.Text = string.IsNullOrWhiteSpace(_browser.EdgePath)
            ? BrowserService.FindEdge()
            : _browser.EdgePath;
        TxtProfileDir.Text = _browser.ProfileDir;
        TxtPorts.Text = $"{_browser.DebugPort},{_browser.RelayPort},{_browser.PublishedPort}";
        AppendLog($"面板就绪；Edge 路径：{(string.IsNullOrWhiteSpace(TxtEdgePath.Text) ? "（未找到）" : TxtEdgePath.Text)}");
    }

    private void OnLog(string line)
    {
        Dispatcher.Invoke(() => AppendLog(line));
    }

    private void AppendLog(string line)
    {
        TxtLog.AppendText($"[{DateTime.Now:HH:mm:ss}] {line}{Environment.NewLine}");
        TxtLog.ScrollToEnd();
    }

    private void ApplySettings()
    {
        _browser.EdgePath = (TxtEdgePath.Text ?? "").Trim();
        var profile = (TxtProfileDir.Text ?? "").Trim();
        if (!string.IsNullOrWhiteSpace(profile)) _browser.ProfileDir = profile;

        var parts = (TxtPorts.Text ?? "").Split(',', StringSplitOptions.TrimEntries | StringSplitOptions.RemoveEmptyEntries);
        if (parts.Length == 3 &&
            int.TryParse(parts[0], out var debug) &&
            int.TryParse(parts[1], out var relay) &&
            int.TryParse(parts[2], out var published))
        {
            _browser.DebugPort = debug;
            _browser.RelayPort = relay;
            _browser.PublishedPort = published;
        }
    }

    private void RefreshStatus()
    {
        ApplySettings();
        var (running, _) = _browser.ContainerState();
        TxtStatus.Text =
            $"Edge 调试端口 {_browser.DebugPort}: {(_browser.IsEdgeDebugPortUp() ? "监听中" : "未监听")}\n" +
            $"宿主机中继 {_browser.RelayPort}: {(_browser.IsRelayUp() ? "监听中" : "未监听")}\n" +
            $"发布容器 {_browser.ContainerName} ({_browser.PublishedPort}): {(running ? "运行中" : "未运行")}\n" +
            $"Profile: {_browser.ProfileDir}\n" +
            $"集群侧探活: http://127.0.0.1:{_browser.PublishedPort}/json/version";
    }

    private void BtnRefresh_Click(object sender, RoutedEventArgs e) => RefreshStatus();

    private void BtnDetectEdge_Click(object sender, RoutedEventArgs e)
    {
        var found = BrowserService.FindEdge();
        TxtEdgePath.Text = found;
        AppendLog(string.IsNullOrWhiteSpace(found) ? "没找到 msedge.exe，请手动指定" : $"已检测到：{found}");
    }

    private void BtnPickProfile_Click(object sender, RoutedEventArgs e)
    {
        // 目录选择用 WinForms 的 FolderBrowserDialog（项目已开启 UseWindowsForms）
        using var dialog = new System.Windows.Forms.FolderBrowserDialog
        {
            Description = "选择 Edge 的独立 profile 目录（智能体能看到的那个浏览器）",
            SelectedPath = Directory.Exists(TxtProfileDir.Text) ? TxtProfileDir.Text : SettingsDir,
        };
        if (dialog.ShowDialog() == System.Windows.Forms.DialogResult.OK)
            TxtProfileDir.Text = dialog.SelectedPath;
    }

    private void BtnSave_Click(object sender, RoutedEventArgs e)
    {
        ApplySettings();
        SaveSettings();
        AppendLog("设置已保存（%LOCALAPPDATA%\\MyAgentAdminPanel\\settings.json）");
        RefreshStatus();
    }

    private void BtnStartAll_Click(object sender, RoutedEventArgs e)
    {
        ApplySettings();
        SaveSettings();
        AppendLog("开始启动浏览器环境…");
        var (ok, message) = _browser.StartAll();
        AppendLog(message);
        AppendLog(ok ? "✔ 浏览器环境已就绪" : "✘ 启动未完成，请按上面的提示处理");
        RefreshStatus();
    }

    private void BtnStartEdge_Click(object sender, RoutedEventArgs e)
    {
        ApplySettings();
        var (ok, message) = _browser.StartEdge();
        AppendLog((ok ? "✔ " : "✘ ") + message);
        RefreshStatus();
    }

    private void BtnStopRelay_Click(object sender, RoutedEventArgs e)
    {
        AppendLog(_browser.StopAll());
        RefreshStatus();
    }

    private void BtnOpenDashboard_Click(object sender, RoutedEventArgs e)
    {
        var url = "http://127.0.0.1:5700";
        try
        {
            Process.Start(new ProcessStartInfo { FileName = url, UseShellExecute = true });
            AppendLog($"已在默认浏览器打开 {url}");
        }
        catch (Exception exc)
        {
            AppendLog($"打开失败：{exc.Message}");
        }
    }

    // ── 设置持久化（只写项目自己的 settings.json，不动注册表）──

    private void SaveSettings()
    {
        try
        {
            Directory.CreateDirectory(SettingsDir);
            var data = new BrowserSettings
            {
                EdgePath = _browser.EdgePath,
                ProfileDir = _browser.ProfileDir,
                DebugPort = _browser.DebugPort,
                RelayPort = _browser.RelayPort,
                PublishedPort = _browser.PublishedPort,
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
        public string? EdgePath { get; set; }
        public string? ProfileDir { get; set; }
        public int DebugPort { get; set; } = 9222;
        public int RelayPort { get; set; } = 9223;
        public int PublishedPort { get; set; } = 9224;
    }
}
