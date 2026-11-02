using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Text.Json;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Documents;
using MyAgentAdminPanel.Services;

namespace MyAgentAdminPanel.Pages;

/// <summary>
/// 一键部署（k8s 路线）：**按功能**选择，不再按服务。
/// 功能清单来自 deploy/features.json；勾选后动态要求填写该功能所需的信息，
/// 缺必填就报错（提示"取消勾选即退化"），齐了再构建镜像 + apply 清单 + 同步配置。
/// </summary>
public partial class DeployPage : Page
{
    private readonly DockerService _docker;
    private readonly KubectlService _kubectl;

    private readonly List<FeatureDef> _features = new();
    private readonly Dictionary<string, Control> _editors = new(StringComparer.OrdinalIgnoreCase);
    private readonly Dictionary<string, string> _values = new(StringComparer.OrdinalIgnoreCase);

    // 服务 → 需要同步到 NFS 的配置（相对仓库根；多项用逗号分隔）
    private static readonly Dictionary<string, string> ServiceConfigMap = new(StringComparer.OrdinalIgnoreCase)
    {
        ["agent-orchestrator-service"] = "config/orchestrator/config,config/orchestrator/system_prompt",
        ["model-proxy-service"] = "config/model-proxy/config",
        ["openviking-server"] = "config/openviking",
        ["openviking-context-service"] = "config/openviking",
        ["qq-llbot-service"] = "config/qq-llbot",
        ["qq-official-service"] = "config/qq-official",
        ["tool-runtime-service"] = "config/openviking",
        ["dashboard-service"] = "config",
    };

    public DeployPage(DockerService docker, KubectlService kubectl)
    {
        InitializeComponent();
        _docker = docker;
        _kubectl = kubectl;
        Loaded += (_, _) => ReloadFeatures();
    }

    // ── 功能清单（deploy/features.json）─────────────────────

    private sealed class SettingDef
    {
        public string Key = "";
        public string Label = "";
        public string Type = "string";
        public bool Required;
        public string Default = "";
        public List<string> Values = new();
        public Dictionary<string, string> When = new(StringComparer.OrdinalIgnoreCase);
        public string Help = "";
        public string DashboardTab = "";
        public string DashboardPath = "";
    }

    private sealed class FeatureDef
    {
        public string Id = "";
        public string Name = "";
        public bool Required;
        public List<string> Modes = new();
        public string Mode = "";
        public List<string> Services = new();
        public Dictionary<string, List<string>> ModeServices = new(StringComparer.OrdinalIgnoreCase);
        public Dictionary<string, List<SettingDef>> ModeSettings = new(StringComparer.OrdinalIgnoreCase);
        public List<SettingDef> Settings = new();
        public Dictionary<string, List<string>> ModeDownloads = new(StringComparer.OrdinalIgnoreCase);
        public Dictionary<string, string> ModeStatus = new(StringComparer.OrdinalIgnoreCase);   // planned = 未实现
        public Dictionary<string, List<string>> ModeTiers = new(StringComparer.OrdinalIgnoreCase); // 该实现方式适用的环境档
        public bool Selected;
    }

    private string ProjectRoot => FindProjectRoot();

    private void ReloadFeatures()
    {
        _features.Clear();
        FeaturePanel.Children.Clear();
        SettingsPanel.Children.Clear();
        _editors.Clear();
        _values.Clear();

        var path = Path.Combine(ProjectRoot, "deploy", "features.json");
        if (!File.Exists(path))
        {
            AppendLog($"[错误] 找不到 {path}");
            return;
        }
        try
        {
            using var doc = JsonDocument.Parse(File.ReadAllText(path));
            foreach (var feature in doc.RootElement.GetProperty("features").EnumerateArray())
            {
                var def = new FeatureDef
                {
                    Id = Str(feature, "id"),
                    Name = Str(feature, "name"),
                    Required = Bool(feature, "required"),
                    Selected = Bool(feature, "required"),
                };
                if (feature.TryGetProperty("modes", out var modes) && modes.ValueKind == JsonValueKind.Object)
                {
                    foreach (var mode in modes.EnumerateObject())
                    {
                        def.Modes.Add(mode.Name);
                        if (mode.Value.TryGetProperty("settings", out var settings) && settings.ValueKind == JsonValueKind.Array)
                            def.ModeSettings[mode.Name] = ParseSettings(settings);
                        if (mode.Value.TryGetProperty("downloads", out var downloads) && downloads.ValueKind == JsonValueKind.Array)
                            def.ModeDownloads[mode.Name] = downloads.EnumerateArray()
                                .Select(d => Str(d, "label")).Where(s => s.Length > 0).ToList();
                        if (mode.Value.TryGetProperty("tiers", out var modeTiers) && modeTiers.ValueKind == JsonValueKind.Array)
                            def.ModeTiers[mode.Name] = modeTiers.EnumerateArray()
                                .Select(t => t.GetString() ?? "").Where(t => t.Length > 0).ToList();
                        if (mode.Value.TryGetProperty("services", out var modeServices) && modeServices.ValueKind == JsonValueKind.Array)
                            def.ModeServices[mode.Name] = modeServices.EnumerateArray()
                                .Select(s => s.GetString() ?? "").Where(s => s.Length > 0).ToList();
                    }
                    def.Mode = def.Modes.FirstOrDefault() ?? "";
                }
                if (feature.TryGetProperty("mode_status", out var modeStatus) && modeStatus.ValueKind == JsonValueKind.Object)
                    foreach (var item in modeStatus.EnumerateObject())
                        def.ModeStatus[item.Name] = item.Value.GetString() ?? "";
                if (feature.TryGetProperty("settings", out var baseSettings) && baseSettings.ValueKind == JsonValueKind.Array)
                    def.Settings = ParseSettings(baseSettings);
                if (feature.TryGetProperty("services", out var servicesEl))
                {
                    if (servicesEl.ValueKind == JsonValueKind.Array)
                        def.Services.AddRange(servicesEl.EnumerateArray()
                            .Select(s => s.GetString() ?? "").Where(s => s.Length > 0));
                    else if (servicesEl.ValueKind == JsonValueKind.Object)
                        foreach (var item in servicesEl.EnumerateObject())
                            if (item.Value.ValueKind == JsonValueKind.Array)
                                def.ModeServices[item.Name] = item.Value.EnumerateArray()
                                    .Select(s => s.GetString() ?? "").Where(s => s.Length > 0).ToList();
                }
                _features.Add(def);
            }
        }
        catch (Exception exc)
        {
            AppendLog($"[错误] 解析 features.json 失败：{exc.Message}");
            return;
        }

        BuildFeaturePanel();
        RebuildSettings();
        RefreshPlan();
        AppendLog($"[系统] 已读取 {_features.Count} 个功能");
    }

    private static List<SettingDef> ParseSettings(JsonElement array)
    {
        var result = new List<SettingDef>();
        foreach (var item in array.EnumerateArray())
        {
            var def = new SettingDef
            {
                Key = Str(item, "key"),
                Label = Str(item, "label"),
                Type = Str(item, "type") is { Length: > 0 } t ? t : "string",
                Required = Bool(item, "required"),
                Default = Str(item, "default"),
                Help = Str(item, "help"),
            };
            if (item.TryGetProperty("values", out var values) && values.ValueKind == JsonValueKind.Array)
                def.Values.AddRange(values.EnumerateArray().Select(v => v.GetString() ?? "").Where(v => v.Length > 0));
            if (item.TryGetProperty("dashboard", out var dashboard) && dashboard.ValueKind == JsonValueKind.Object)
            {
                def.DashboardTab = Str(dashboard, "tab");
                def.DashboardPath = Str(dashboard, "path");
            }
            if (item.TryGetProperty("when", out var when) && when.ValueKind == JsonValueKind.Object)
                foreach (var pair in when.EnumerateObject())
                    def.When[pair.Name] = pair.Value.ValueKind switch
                    {
                        JsonValueKind.True => "true",
                        JsonValueKind.False => "false",
                        _ => pair.Value.ToString(),
                    };
            if (def.Key.Length > 0) result.Add(def);
        }
        return result;
    }

    private static string Str(JsonElement element, string name) =>
        element.TryGetProperty(name, out var value) && value.ValueKind == JsonValueKind.String
            ? value.GetString() ?? "" : "";

    private static bool Bool(JsonElement element, string name) =>
        element.TryGetProperty(name, out var value) && value.ValueKind == JsonValueKind.True;

    private const string CurrentTier = "T2";   // 一键部署页只服务 k8s 档

    /// <summary>该实现方式在当前环境档是否可用（未实现 / tiers 不含本档 → 不可用）。</summary>
    private static bool ModeUsable(FeatureDef feature, string mode)
    {
        if (feature.ModeStatus.TryGetValue(mode, out var status) && status == "planned") return false;
        if (feature.ModeTiers.TryGetValue(mode, out var tiers) && tiers.Count > 0 && !tiers.Contains(CurrentTier))
            return false;
        return true;
    }

    private void BuildFeaturePanel()
    {
        FeaturePanel.Children.Clear();
        foreach (var feature in _features)
        {
            var row = new StackPanel { Margin = new Thickness(0, 2, 0, 4) };
            var line = new StackPanel { Orientation = Orientation.Horizontal };
            var check = new CheckBox
            {
                IsChecked = feature.Selected,
                IsEnabled = !feature.Required,
                Content = feature.Name,
                FontWeight = FontWeights.SemiBold,
            };
            check.Checked += (_, _) => { feature.Selected = true; RebuildSettings(); RefreshPlan(); };
            check.Unchecked += (_, _) => { feature.Selected = false; RebuildSettings(); RefreshPlan(); };
            line.Children.Add(check);

            if (feature.Modes.Count > 0)
            {
                var combo = new ComboBox { Width = 130, Margin = new Thickness(10, 0, 0, 0), Height = 24 };
                foreach (var mode in feature.Modes)
                {
                    var label = mode;
                    if (feature.ModeStatus.TryGetValue(mode, out var st) && st == "planned") label += "（未实现）";
                    else if (feature.ModeTiers.TryGetValue(mode, out var tiers) && tiers.Count > 0 && !tiers.Contains(CurrentTier))
                        label += "（本环境不支持）";
                    combo.Items.Add(label);
                }
                var firstReady = feature.Modes.FirstOrDefault(m => ModeUsable(feature, m));
                if (firstReady is not null) feature.Mode = firstReady;
                combo.SelectedIndex = feature.Modes.IndexOf(feature.Mode);
                combo.SelectionChanged += (_, _) =>
                {
                    if (combo.SelectedItem is string mode)
                    {
                        feature.Mode = mode;
                        RebuildSettings();
                        RefreshPlan();
                    }
                };
                line.Children.Add(combo);
            }
            row.Children.Add(line);
            FeaturePanel.Children.Add(row);
        }
    }

    // ── 动态参数表单 ────────────────────────────────────────

    private readonly List<string> _dashboardHints = new();

    private void RebuildSettings()
    {
        _editors.Clear();
        SettingsPanel.Children.Clear();
        _dashboardHints.Clear();

        foreach (var feature in _features.Where(f => f.Selected))
        {
            var items = new List<SettingDef>();
            items.AddRange(feature.Settings);
            if (feature.Mode.Length > 0 && feature.ModeSettings.TryGetValue(feature.Mode, out var modeSettings))
                items.AddRange(modeSettings);

            var downloads = feature.Mode.Length > 0 && feature.ModeDownloads.TryGetValue(feature.Mode, out var dl)
                ? dl : new List<string>();

            if (items.Count == 0 && downloads.Count == 0) continue;

            SettingsPanel.Children.Add(new TextBlock
            {
                Text = feature.Name,
                FontWeight = FontWeights.SemiBold,
                Margin = new Thickness(0, 8, 0, 4),
            });

            if (downloads.Count > 0)
                SettingsPanel.Children.Add(new TextBlock
                {
                    Text = $"需下载：{string.Join("、", downloads)}",
                    FontSize = 11,
                    Foreground = System.Windows.Media.Brushes.DarkOrange,
                    Margin = new Thickness(0, 0, 0, 4),
                });

            foreach (var def in items)
            {
                if (!WhenSatisfied(def, feature)) continue;
                if (def.DashboardTab.Length > 0)
                {
                    // 能在 dashboard 里改的，不在部署页重复问，只提示去哪改
                    var hint = $"dashboard「{def.DashboardTab}」→ {def.DashboardPath}：{def.Label}";
                    if (!_dashboardHints.Contains(hint)) _dashboardHints.Add(hint);
                    continue;
                }
                SettingsPanel.Children.Add(BuildEditorRow(def));
            }
        }

        if (_dashboardHints.Count > 0)
        {
            var box = new StackPanel { Margin = new Thickness(0, 8, 0, 4) };
            box.Children.Add(new TextBlock
            {
                Text = "以下配置部署后在 dashboard 里设置（部署页不再重复询问）：",
                FontWeight = FontWeights.SemiBold,
                FontSize = 12,
            });
            foreach (var hint in _dashboardHints)
                box.Children.Add(new TextBlock
                {
                    Text = "· " + hint,
                    FontSize = 11,
                    Foreground = System.Windows.Media.Brushes.DimGray,
                    TextWrapping = TextWrapping.Wrap,
                });
            SettingsPanel.Children.Add(box);
        }

        if (SettingsPanel.Children.Count == 0)
            SettingsPanel.Children.Add(new TextBlock
            {
                Text = "所选功能无需填写资料。",
                Foreground = System.Windows.Media.Brushes.Gray,
                Margin = new Thickness(0, 4, 0, 4),
            });
    }

    private bool WhenSatisfied(SettingDef def, FeatureDef feature)
    {
        foreach (var pair in def.When)
        {
            // 条件键可能是：本功能的模式（如 mode=install）、其它设置项（如 ov.source）
            var current = pair.Key is "mode" or "source" or "transport" or "protocol" or "ws_mode"
                ? _features.FirstOrDefault(f => f.Mode.Length > 0 && f.ModeSettings.ContainsKey(f.Mode)) is { } any
                    ? CurrentModeOf(pair.Key)
                    : ""
                : ReadValue(pair.Key);
            if (current.Length == 0) current = ReadValue(pair.Key);
            if (!string.Equals(current, pair.Value, StringComparison.OrdinalIgnoreCase)) return false;
        }
        return true;
    }

    private string CurrentModeOf(string _) =>
        // features.json 里的 when 条件多用「功能内的子模式」，这里取第一个已选功能的当前模式
        _features.Where(f => f.Selected && f.Mode.Length > 0).Select(f => f.Mode).FirstOrDefault() ?? "";

    private string ReadValue(string key)
    {
        if (_values.TryGetValue(key, out var value) && value.Length > 0) return value;
        if (_editors.TryGetValue(key, out var editor))
        {
            return editor switch
            {
                TextBox tb => tb.Text,
                PasswordBox pb => pb.Password,
                ComboBox cb => cb.SelectedItem?.ToString() ?? "",
                _ => "",
            };
        }
        var def = _features.SelectMany(f => f.Settings)
            .Concat(_features.SelectMany(f => f.ModeSettings.Values.SelectMany(x => x)))
            .FirstOrDefault(d => d.Key == key);
        return def?.Default ?? "";
    }

    private FrameworkElement BuildEditorRow(SettingDef def)
    {
        var panel = new StackPanel { Margin = new Thickness(0, 2, 0, 6) };
        panel.Children.Add(new TextBlock
        {
            Text = def.Required ? $"{def.Label} *" : def.Label,
            FontSize = 12,
            FontWeight = FontWeights.SemiBold,
        });

        Control editor;
        if (def.Type == "enum" && def.Values.Count > 0)
        {
            var combo = new ComboBox { Height = 26 };
            foreach (var value in def.Values) combo.Items.Add(value);
            combo.SelectedItem = _values.TryGetValue(def.Key, out var current) && def.Values.Contains(current)
                ? current
                : (def.Default.Length > 0 && def.Values.Contains(def.Default) ? def.Default : def.Values[0]);
            _values[def.Key] = combo.SelectedItem?.ToString() ?? "";
            combo.SelectionChanged += (_, _) =>
            {
                _values[def.Key] = combo.SelectedItem?.ToString() ?? "";
                RebuildSettings();
                RefreshPlan();
            };
            editor = combo;
        }
        else if (def.Type == "secret")
        {
            var box = new PasswordBox { Height = 26, Password = _values.GetValueOrDefault(def.Key, "") };
            box.PasswordChanged += (_, _) => _values[def.Key] = box.Password;
            editor = box;
        }
        else
        {
            var box = new TextBox
            {
                Height = 26,
                Text = _values.TryGetValue(def.Key, out var v) && v.Length > 0 ? v : def.Default,
            };
            box.TextChanged += (_, _) => _values[def.Key] = box.Text;
            editor = box;
        }
        panel.Children.Add(editor);
        _editors[def.Key] = editor;

        if (def.Help.Length > 0)
            panel.Children.Add(new TextBlock
            {
                Text = def.Help,
                FontSize = 11,
                Foreground = System.Windows.Media.Brushes.Gray,
                TextWrapping = TextWrapping.Wrap,
            });
        return panel;
    }

    // ── 计划 ────────────────────────────────────────────────

    private List<string> SelectedServices(out List<string> missingRequired)
    {
        missingRequired = new List<string>();
        var services = new List<string>();
        foreach (var feature in _features.Where(f => f.Selected))
        {
            foreach (var service in feature.Services)
                if (!services.Contains(service)) services.Add(service);
            if (feature.Mode.Length > 0 && feature.ModeServices.TryGetValue(feature.Mode, out var modeServices))
                foreach (var service in modeServices)
                    if (!services.Contains(service)) services.Add(service);

            var defs = new List<SettingDef>(feature.Settings);
            if (feature.Mode.Length > 0 && feature.ModeSettings.TryGetValue(feature.Mode, out var modeSettings))
                defs.AddRange(modeSettings);
            foreach (var def in defs.Where(d => d.Required && WhenSatisfied(d, feature)))
            {
                if (ReadValue(def.Key).Trim().Length == 0)
                    missingRequired.Add($"{feature.Name} · {def.Label}（{def.Key}）");
            }
        }
        return services;
    }

    private void RefreshPlan()
    {
        var services = SelectedServices(out var missing);
        var lines = new List<string>
        {
            "部署目标：k8s（当前面板执行后端）",
            $"已选功能：{string.Join("、", _features.Where(f => f.Selected).Select(f => f.Name))}",
            $"将启动服务（{services.Count}）：{string.Join("、", services)}",
        };
        if (!_features.Any(f => f.Selected && f.Id == "tools.basic"))
            lines.Add("⚠ 未勾选「工具调用」：不部署 tool-runtime，工具目录不注入");
        var downloads = _features.Where(f => f.Selected)
            .SelectMany(f => f.Mode.Length > 0 && f.ModeDownloads.TryGetValue(f.Mode, out var dl) ? dl : new List<string>())
            .Distinct().ToList();
        if (downloads.Count > 0) lines.Add($"需下载：{string.Join("、", downloads)}");
        if (_dashboardHints.Count > 0) lines.Add("部署后到 dashboard 设置：" + string.Join("；", _dashboardHints));
        if (missing.Count > 0) lines.Add($"缺少必填：{string.Join("；", missing)}");
        TxtPlan.Text = string.Join(Environment.NewLine, lines);
    }

    private void BtnReload_Click(object sender, RoutedEventArgs e) => ReloadFeatures();

    private void BtnPreview_Click(object sender, RoutedEventArgs e) => RefreshPlan();

    // ── 部署 ────────────────────────────────────────────────

    private async void BtnDeploy_Click(object sender, RoutedEventArgs e)
    {
        RefreshPlan();
        var services = SelectedServices(out var missing);
        if (missing.Count > 0)
        {
            AppendLog("[错误] 以下必填项为空：" + string.Join("；", missing));
            AppendLog("       不想填就取消勾选对应功能（走退化版本），不要留空。");
            return;
        }
        if (services.Count == 0)
        {
            AppendLog("[错误] 没有需要部署的服务");
            return;
        }

        BtnDeploy.IsEnabled = false;
        LogOutput.Document.Blocks.Clear();
        var root = ProjectRoot;
        AppendLog($"[系统] 开始部署 {services.Count} 个服务：{string.Join(", ", services)}");

        AppendLog("[系统] 确保 namespace 和 RBAC...");
        _kubectl.RunCommand("create namespace agent --dry-run=client -o yaml | kubectl apply -f -");
        _kubectl.RunCommand("-n agent create role pod-log-reader --verb=get,list --resource=pods,pods/log --dry-run=client -o yaml | kubectl apply -f -");
        _kubectl.RunCommand("-n agent create rolebinding pod-log-reader-binding --role=pod-log-reader --serviceaccount=agent:default --dry-run=client -o yaml | kubectl apply -f -");

        await SyncConfigAsync(services);
        await WriteCapabilityAsync();

        AppendLog("\n═══ Docker 构建 ═══");
        var tag = DateTime.Now.ToString("MMddHHmm");
        foreach (var service in services)
        {
            var dir = Path.Combine(root, "services", service);
            if (!File.Exists(Path.Combine(dir, "Dockerfile")))
            {
                AppendLog($"[跳过] {service} - 外部镜像/无 Dockerfile");
                continue;
            }
            await RunAndWait($"docker build -t agent/{service}:{tag} \"{dir}\"");
        }

        AppendLog("\n═══ K8s 部署 ═══");
        var openviking = false;
        foreach (var service in services)
        {
            if (service == "tool-runtime-service")
            {
                var clawhub = _features.Any(f => f.Selected && f.Id == "skills");
                var codex = _features.Any(f => f.Selected && f.Id == "external-exec");
                var memory = _features.Any(f => f.Selected && f.Id == "memory" && f.Mode == "openviking");
                var repoWsl = WslPath(root);
                var env = $"ENABLE_CLAWHUB={(clawhub ? "true" : "false")} ENABLE_CODEX={(codex ? "true" : "false")} ENABLE_OPENVIKING={(memory ? "true" : "false")}";
                AppendLog($"> 部署 tool-runtime-service（WSL 特殊脚本，{env}）");
                await RunWslAsync($"cd '{repoWsl}' && {env} bash deploy/tool-runtime-apply.sh");
                continue;
            }

            var yaml = Path.Combine(root, "deploy", "services", $"{service}.yaml");
            if (!File.Exists(yaml))
            {
                AppendLog($"[跳过] {service} - 清单不存在: {yaml}");
                continue;
            }
            await RunAndWait($"kubectl apply -f \"{yaml}\"");
            if (File.Exists(Path.Combine(root, "services", service, "Dockerfile")))
                await RunAndWait($"kubectl -n agent set image deploy/{service} {service}=agent/{service}:{tag}");
            if (service == "openviking-server") openviking = true;
        }

        if (openviking) await InitOpenvikingAsync();

        AppendLog("\n[系统] 部署完成（降级说明见 dashboard「杂项 → 运行环境」）");
        BtnDeploy.IsEnabled = true;
        _ = _docker;
    }

    // ── 配置同步 / capability ───────────────────────────────

    private async Task SyncConfigAsync(IEnumerable<string> services)
    {
        AppendLog("[系统] 同步配置到 NFS...");
        var root = ProjectRoot;
        var repoWsl = WslPath(root);
        var nfsRoot = Environment.GetEnvironmentVariable("NFS_ROOT");
        if (string.IsNullOrEmpty(nfsRoot)) nfsRoot = "/srv/nfs/my-agent";

        foreach (var service in services)
        {
            if (!ServiceConfigMap.TryGetValue(service, out var spec)) continue;
            foreach (var raw in spec.Split(',', StringSplitOptions.RemoveEmptyEntries))
            {
                var rel = raw.Trim().Replace("\\", "/").TrimEnd('/');
                var parent = rel.Contains('/') ? rel[..rel.LastIndexOf('/')] : "";
                var destDir = parent.Length > 0 ? $"{nfsRoot}/{parent}" : nfsRoot;
                await RunWslAsync($"mkdir -p '{destDir}' && cp -r '{repoWsl}/{rel}' '{destDir}/' 2>/dev/null || true");
                AppendLog($"[sync] {service} ← {rel}");
            }
        }
        AppendLog("[OK] 配置已同步");
    }

    /// <summary>按勾选的功能写 capability.json（orchestrator 与 dashboard 共读同一份）。</summary>

    private async Task WriteCapabilityAsync()
    {
        bool Selected(string id) => _features.Any(f => f.Selected && f.Id == id);
        string Mode(string id) => _features.FirstOrDefault(f => f.Id == id)?.Mode ?? "";

        var features = new Dictionary<string, object>
        {
            ["tool_runtime"] = Selected("tools.basic"),
            ["memory_mode"] = Mode("memory") == "openviking" ? "openviking" : "local",
            ["openviking"] = Mode("memory") == "openviking",
            ["clawhub"] = Selected("skills"),
            ["skill_exec"] = Selected("skills"),
            ["browser"] = Selected("browser") ? Mode("browser") : "off",
            ["search"] = Selected("search") ? Mode("search") : "off",
            ["external_exec"] = Selected("external-exec") ? Mode("external-exec") : "off",
            ["host_files"] = Selected("hostfiles"),
            ["image_gen"] = true,
            ["fetch_extract"] = "full",
            ["qq_adapter"] = Selected("qq") ? Mode("qq") : "none",
            ["lan_expose"] = false,
            ["storage"] = "nfs",
        };
        var degraded = new List<Dictionary<string, string>>();
        if (!Selected("tools.basic")) degraded.Add(new() { ["feature"] = "tool_runtime", ["reason"] = "未勾选工具调用" });
        if (Mode("memory") != "openviking") degraded.Add(new() { ["feature"] = "memory", ["reason"] = "使用本地窗口滚动" });
        if (!Selected("browser")) degraded.Add(new() { ["feature"] = "browser", ["reason"] = "未勾选浏览器操作" });
        if (!Selected("external-exec")) degraded.Add(new() { ["feature"] = "external-exec", ["reason"] = "未勾选外部执行" });

        var payload = new Dictionary<string, object>
        {
            ["schema"] = 1,
            ["runtime_tier"] = "T2",
            ["os"] = "windows",
            ["mode"] = "k8s",
            ["generated_at"] = DateTime.Now.ToString("yyyy-MM-ddTHH:mm:sszzz"),
            ["features"] = features,
            ["degraded"] = degraded,
        };
        var temp = Path.Combine(Path.GetTempPath(), $"capability-{Guid.NewGuid():N}.json");
        File.WriteAllText(temp, JsonSerializer.Serialize(payload, new JsonSerializerOptions { WriteIndented = true }));
        var nfsRoot = Environment.GetEnvironmentVariable("NFS_ROOT");
        if (string.IsNullOrEmpty(nfsRoot)) nfsRoot = "/srv/nfs/my-agent";
        var target = $"{nfsRoot}/config/orchestrator/config/capability.json";
        await RunWslAsync($"mkdir -p \"$(dirname '{target}')\" && cp '{WslPath(temp)}' '{target}'");
        AppendLog($"[OK] capability.json → {target}");
    }

    // ── openviking 初始化 ───────────────────────────────────

    private async Task InitOpenvikingAsync()
    {
        AppendLog("[系统] 初始化 openviking（创建 agent-service 用户）...");
        var root = ProjectRoot;
        var rootKeyFile = Path.Combine(root, "config", "openviking", "root_api_key");
        if (!File.Exists(rootKeyFile))
        {
            AppendLog("[警告] 缺少 config/openviking/root_api_key，跳过初始化");
            return;
        }
        await RunWslAsync($"cd '{WslPath(root)}' && bash deploy/openviking-init.sh 2>/dev/null || true");
    }

    // ── 执行辅助 ────────────────────────────────────────────

    private async Task RunAndWait(string command)
    {
        AppendLog($"> {command}");
        var psi = new ProcessStartInfo
        {
            FileName = "cmd.exe",
            Arguments = $"/c {command}",
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false,
            CreateNoWindow = true,
        };
        using var proc = Process.Start(psi)!;
        proc.OutputDataReceived += (_, e) => { if (!string.IsNullOrEmpty(e.Data)) Dispatcher.Invoke(() => AppendLog(e.Data)); };
        proc.ErrorDataReceived += (_, e) => { if (!string.IsNullOrEmpty(e.Data)) Dispatcher.Invoke(() => AppendLog($"[ERR] {e.Data}")); };
        proc.BeginOutputReadLine();
        proc.BeginErrorReadLine();
        await proc.WaitForExitAsync();
    }

    private async Task RunWslAsync(string bashCommand)
    {
        var psi = new ProcessStartInfo
        {
            FileName = "wsl",
            Arguments = $"-e bash -lc \"{bashCommand}\"",
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false,
            CreateNoWindow = true,
        };
        using var proc = Process.Start(psi)!;
        proc.OutputDataReceived += (_, e) => { if (!string.IsNullOrEmpty(e.Data)) Dispatcher.Invoke(() => AppendLog(e.Data)); };
        proc.ErrorDataReceived += (_, e) => { if (!string.IsNullOrEmpty(e.Data)) Dispatcher.Invoke(() => AppendLog($"[ERR] {e.Data}")); };
        proc.BeginOutputReadLine();
        proc.BeginErrorReadLine();
        await proc.WaitForExitAsync();
    }

    private static string WslPath(string projectRoot)
    {
        if (projectRoot.Length < 2) return projectRoot.Replace("\\", "/");
        var drive = char.ToLower(projectRoot[0]);
        var rest = projectRoot[2..].Replace("\\", "/");
        return $"/mnt/{drive}{rest}";
    }

    private void AppendLog(string text)
    {
        Dispatcher.Invoke(() =>
        {
            LogOutput.Document.Blocks.Add(new Paragraph(new Run(text)) { Margin = new Thickness(0) });
            LogOutput.ScrollToEnd();
        });
    }

    private string FindProjectRoot()
    {
        var dir = AppContext.BaseDirectory;
        for (var i = 0; i < 6 && !string.IsNullOrEmpty(dir); i++)
        {
            if (File.Exists(Path.Combine(dir, "deploy", "features.json"))) return dir;
            dir = Path.GetDirectoryName(dir.TrimEnd(Path.DirectorySeparatorChar)) ?? "";
        }
        return Directory.GetCurrentDirectory();
    }
}
