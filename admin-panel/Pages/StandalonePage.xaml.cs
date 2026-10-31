using System;
using System.Collections.Generic;
using System.Collections.ObjectModel;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text;
using System.Windows;
using System.Windows.Controls;
using MyAgentAdminPanel.Services;

namespace MyAgentAdminPanel.Pages;

public partial class StandalonePage : Page
{
    private readonly StandaloneService _standalone;
    private const string CurrentTier = "T0";   // 单体页服务"无 Docker"档
    public ObservableCollection<FeatureItem> Features { get; } = new();

    public StandalonePage(StandaloneService standalone)
    {
        InitializeComponent();
        _standalone = standalone;
        _standalone.Log += line => Dispatcher.Invoke(() => AppendLog(line));
        FeatureList.ItemsSource = Features;

        if (string.IsNullOrWhiteSpace(_standalone.RepoRoot))
            _standalone.RepoRoot = StandaloneService.FindRepoRoot();
        TxtRepoRoot.Text = _standalone.RepoRoot;
        TxtDataDir.Text = _standalone.DataDir;
        TxtPython.Text = $"Python：{StandaloneService.FindPython() switch { "" => "未找到", var p => p }}";

        LoadFeatures();
        LoadOvConfig();
        Loaded += (_, _) => RefreshStatus();
        IsVisibleChanged += (_, _) => { if (IsVisible) RefreshStatus(); };
        AppendLog("单体模式就绪（无 Docker 路线）");
    }

    private void AppendLog(string line)
    {
        TxtLog.AppendText($"[{DateTime.Now:HH:mm:ss}] {line}{Environment.NewLine}");
        TxtLog.ScrollToEnd();
    }

    // ── 功能清单：读 deploy/features.json ────────────────────

    private void LoadFeatures()
    {
        Features.Clear();
        var path = Path.Combine(_standalone.RepoRoot, "deploy", "features.json");
        if (!File.Exists(path))
        {
            AppendLog($"找不到 {path}，无法加载功能清单");
            return;
        }
        try
        {
            using var doc = JsonDocument.Parse(File.ReadAllText(path));
            foreach (var feature in doc.RootElement.GetProperty("features").EnumerateArray())
            {
                var id = feature.GetProperty("id").GetString() ?? "";
                var name = feature.TryGetProperty("name", out var n) ? n.GetString() ?? id : id;
                var required = feature.TryGetProperty("required", out var r) && r.ValueKind == JsonValueKind.True;
                var modeNames = new List<string>();
                var usableModes = new List<string>();
                if (feature.TryGetProperty("modes", out var m) && m.ValueKind == JsonValueKind.Object)
                {
                    foreach (var mode in m.EnumerateObject())
                    {
                        modeNames.Add(mode.Name);
                        var tierLimited = mode.Value.TryGetProperty("tiers", out var tiers)
                                          && tiers.ValueKind == JsonValueKind.Array
                                          && !tiers.EnumerateArray().Any(t => (t.GetString() ?? "") == CurrentTier);
                        if (!tierLimited) usableModes.Add(mode.Name);
                    }
                }
                var modes = string.Join("/", modeNames);
                Features.Add(new FeatureItem
                {
                    Id = id,
                    Name = name,
                    Hint = required
                        ? (modes.Length > 0 ? $"（必选；{modes}）" : "（必选）")
                        : (modeNames.Count > 0 && usableModes.Count == 0
                            ? "（本环境不支持：需要 Docker/外部托管）"
                            : (modes.Length > 0 ? $"（{modes}）" : "")),
                    Enabled = required || id is "memory",
                    CanToggle = !required && !(modeNames.Count > 0 && usableModes.Count == 0),
                });
            }
            CmbLogService.Items.Clear();
            foreach (var feature in Features) { /* 日志下拉在刷新状态时填 */ _ = feature; }
        }
        catch (Exception exc)
        {
            AppendLog($"解析 features.json 失败：{exc.Message}");
        }
    }

    private List<string> SelectedFeatures() =>
        Features.Where(item => item.Enabled).Select(item => item.Id).ToList();

    private string BuildModes()
    {
        var memory = (CmbMemory.SelectedItem as ComboBoxItem)?.Content?.ToString() ?? "local";
        var qq = (CmbQq.SelectedItem as ComboBoxItem)?.Content?.ToString() ?? "satori";
        return $"memory={memory},qq={qq}";
    }


    // ── openviking 记忆：读写 data/config/launcher.json ──────────────
    // 用户不动环境变量：面板把地址写进配置文件，launcher 启动时读它。

    private string LauncherConfigPath => Path.Combine(_standalone.DataDir, "config", "launcher.json");

    private void LoadOvConfig()
    {
        try
        {
            var path = LauncherConfigPath;
            if (!File.Exists(path))
            {
                var template = Path.Combine(_standalone.RepoRoot, "config", "launcher.json");
                if (File.Exists(template))
                {
                    Directory.CreateDirectory(Path.GetDirectoryName(path)!);
                    File.Copy(template, path, overwrite: false);
                }
            }
            if (!File.Exists(path))
            {
                TxtOvHint.Text = "还没初始化数据目录（先点「初始化」）；留空则记忆用本地文件模式。";
                return;
            }
            using var doc = JsonDocument.Parse(File.ReadAllText(path));
            if (doc.RootElement.TryGetProperty("openviking", out var ov) &&
                ov.TryGetProperty("server_url", out var url))
            {
                TxtOvUrl.Text = url.GetString() ?? "";
            }
            UpdateOvHint();
        }
        catch (Exception exc)
        {
            TxtOvHint.Text = $"读取配置失败：{exc.Message}";
        }
    }

    private void UpdateOvHint()
    {
        var url = TxtOvUrl.Text.Trim();
        TxtOvHint.Text = string.IsNullOrWhiteSpace(url)
            ? "当前：未配置 → 记忆走本地文件模式（不写库、不检索）。"
            : $"当前：{url}（重启单体模式后生效；连不上会在服务日志里显式报错）";
    }

    private void BtnSaveOvUrl_Click(object sender, RoutedEventArgs e)
    {
        ApplyPaths();
        var path = LauncherConfigPath;
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(path)!);
            JsonNode rootNode;
            if (File.Exists(path))
            {
                rootNode = JsonNode.Parse(File.ReadAllText(path)) ?? new JsonObject();
            }
            else
            {
                rootNode = new JsonObject();
            }
            if (rootNode["openviking"] is not JsonObject ov)
            {
                ov = new JsonObject();
                rootNode["openviking"] = ov;
            }
            ov["server_url"] = TxtOvUrl.Text.Trim();
            ov["account"] ??= "my-agent";
            ov["account_mode"] ??= "fixed";
            ov["file_fallback"] ??= true;
            File.WriteAllText(path, rootNode.ToJsonString(new JsonSerializerOptions { WriteIndented = true }),
                new UTF8Encoding(false));
            AppendLog($"openviking 服务器地址已保存：{path}（停止 → 启动 后生效）");
            UpdateOvHint();
        }
        catch (Exception exc)
        {
            AppendLog($"保存 openviking 配置失败：{exc.Message}");
        }
    }

    // ── 按钮 ────────────────────────────────────────────────

    private void ApplyPaths()
    {
        _standalone.RepoRoot = TxtRepoRoot.Text.Trim();
        var data = TxtDataDir.Text.Trim();
        if (!string.IsNullOrWhiteSpace(data)) _standalone.DataDir = data;
    }

    private void BtnDetect_Click(object sender, RoutedEventArgs e)
    {
        var root = StandaloneService.FindRepoRoot();
        if (!string.IsNullOrWhiteSpace(root)) TxtRepoRoot.Text = root;
        TxtPython.Text = $"Python：{StandaloneService.FindPython() switch { "" => "未找到", var p => p }}";
        AppendLog(string.IsNullOrWhiteSpace(root) ? "没找到仓库根目录（需要 deploy/features.json）" : $"仓库根：{root}");
        if (string.IsNullOrWhiteSpace(_standalone.RepoRoot) && !string.IsNullOrWhiteSpace(root))
        {
            _standalone.RepoRoot = root;
            LoadFeatures();
        }
    }

    private void BtnOpenDataDir_Click(object sender, RoutedEventArgs e)
    {
        ApplyPaths();
        try
        {
            Directory.CreateDirectory(_standalone.DataDir);
            Process.Start(new ProcessStartInfo { FileName = _standalone.DataDir, UseShellExecute = true });
        }
        catch (Exception exc) { AppendLog($"打开数据目录失败：{exc.Message}"); }
    }

    private void BtnInit_Click(object sender, RoutedEventArgs e)
    {
        ApplyPaths();
        var (ok, message) = _standalone.InitDataDir();
        AppendLog(message);
        if (ok) AppendLog("提示：模型密钥请在 data/config/model-proxy/config/model_list.json 里填");
    }

    private void BtnProbe_Click(object sender, RoutedEventArgs e)
    {
        ApplyPaths();
        var (ok, message) = _standalone.Probe();
        AppendLog(message);
        if (!ok) AppendLog("可手动跑：python services/agent-orchestrator-service/tools/probe_env.py --print");
        RefreshStatus();
    }


    private void BtnInstallDeps_Click(object sender, RoutedEventArgs e)
    {
        ApplyPaths();
        var features = SelectedFeatures();
        AppendLog($"开始安装依赖（共享 venv，服务 {features.Count} 个；首次会下载并生成 gRPC stubs，请稍候）…");
        var (ok, message) = _standalone.InstallDeps(features);
        AppendLog(message);
        if (ok) AppendLog("提示：之后启动会自动用 <数据目录>/venv 里的解释器");
    }

    private void BtnStart_Click(object sender, RoutedEventArgs e)
    {
        ApplyPaths();
        var features = SelectedFeatures();
        if (features.Count == 0)
        {
            AppendLog("至少勾选一个功能");
            return;
        }
        AppendLog("提示：模型/embedding 请在 dashboard「配置管理」→ model-proxy/config/model_list.json 里填；"
                  + "模板里的占位 Key 不改会导致调用失败。");
        var ovUrl = TxtOvUrl.Text.Trim();
        var (ok, message) = _standalone.Start(features, BuildModes(), ovUrl);
        AppendLog(message);
        RefreshStatus();
    }

    private void BtnStop_Click(object sender, RoutedEventArgs e)
    {
        ApplyPaths();
        var (_, message) = _standalone.Stop();
        AppendLog(message);
        RefreshStatus();
    }

    private void BtnRefresh_Click(object sender, RoutedEventArgs e) => RefreshStatus();

    private void BtnOpenDashboard_Click(object sender, RoutedEventArgs e)
    {
        const string url = "http://127.0.0.1:5700";
        try
        {
            Process.Start(new ProcessStartInfo { FileName = url, UseShellExecute = true });
            AppendLog($"已打开 {url}");
        }
        catch (Exception exc) { AppendLog($"打开失败：{exc.Message}"); }
    }

    // ── 状态与日志 ──────────────────────────────────────────

    private void RefreshStatus()
    {
        ApplyPaths();
        TxtStatus.Text = string.Join(Environment.NewLine, _standalone.StatusLines());

        var services = ReadServiceIds();
        var current = (CmbLogService.SelectedItem as string) ?? services.FirstOrDefault() ?? "";
        if (!services.SequenceEqual(CmbLogService.Items.Cast<string>()))
        {
            CmbLogService.Items.Clear();
            foreach (var item in services) CmbLogService.Items.Add(item);
            if (!string.IsNullOrEmpty(current)) CmbLogService.SelectedItem = current;
        }
    }

    private List<string> ReadServiceIds()
    {
        var pids = Path.Combine(_standalone.DataDir, "pids.json");
        var result = new List<string>();
        if (!File.Exists(pids)) return result;
        try
        {
            using var doc = JsonDocument.Parse(File.ReadAllText(pids));
            if (doc.RootElement.TryGetProperty("processes", out var procs) && procs.ValueKind == JsonValueKind.Array)
            {
                foreach (var item in procs.EnumerateArray())
                    if (item.TryGetProperty("id", out var id) && id.GetString() is { } name) result.Add(name);
            }
        }
        catch { }
        return result;
    }

    private void CmbLogService_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (CmbLogService.SelectedItem is not string id) return;
        TxtLog.Text = _standalone.TailLog(id);
        TxtLog.ScrollToEnd();
    }

    public sealed class FeatureItem
    {
        public string Id { get; init; } = "";
        public string Name { get; init; } = "";
        public string Hint { get; init; } = "";
        public bool Enabled { get; set; }
        public bool CanToggle { get; init; } = true;
    }
}
