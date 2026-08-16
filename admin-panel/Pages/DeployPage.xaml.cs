using System;
using System.Collections.Generic;
using System.Collections.ObjectModel;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Net.Http;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Documents;
using MyAgentAdminPanel.Models;
using MyAgentAdminPanel.Services;

namespace MyAgentAdminPanel.Pages;

public partial class DeployPage : Page
{
    private readonly DockerService _docker;
    private readonly KubectlService _kubectl;
    public ObservableCollection<ServiceInfo> Services { get; } = new();

    /// <summary>必要服务（Web 对话最小闭环），与 deploy-all.ps1 的 $CORE_SERVICES 一致</summary>
    private static readonly string[] CoreServices =
    {
        "frontend-service", "gateway-backend-service",
        "task-scheduler-service", "agent-orchestrator-service", "model-proxy-service",
    };

    /// <summary>服务依赖：勾选 context 自动带 server，与 deploy-all.ps1 的 $DEPENDENCIES 一致</summary>
    private static readonly Dictionary<string, string[]> Dependencies = new(StringComparer.OrdinalIgnoreCase)
    {
        ["openviking-context-service"] = new[] { "openviking-server" },
    };

    /// <summary>服务 → 需要同步到 NFS 的配置（相对 config/），与 deploy-all.ps1 的 $CONFIG_MAP 一致</summary>
    private static readonly Dictionary<string, string[]> ConfigMap = new(StringComparer.OrdinalIgnoreCase)
    {
        ["agent-orchestrator-service"] = new[]
        {
            "config/orchestrator/config/agent_list.json",
            "config/orchestrator/config/system_settings.json",
            "config/orchestrator/system_prompt/",
        },
        ["model-proxy-service"] = new[] { "config/model-proxy/config/model_list.json" },
        ["openviking-server"] = new[] { "config/openviking/ov.conf" },
        ["openviking-context-service"] = new[] { "config/openviking/root_api_key", "config/openviking/api_key" },
        ["qq-llbot-service"] = new[] { "config/qq-llbot/qq_llbot_config.json" },
        ["tool-runtime-service"] = new[] { "config/openviking/api_key" },
        // dashboard 为管理面板，浏览/编辑整个配置目录
        ["dashboard-service"] = new[]
        {
            "config/orchestrator/config/agent_list.json",
            "config/orchestrator/config/system_settings.json",
            "config/orchestrator/system_prompt/",
            "config/model-proxy/config/model_list.json",
            "config/openviking/ov.conf",
            "config/openviking/root_api_key",
            "config/openviking/api_key",
            "config/qq-llbot/qq_llbot_config.json",
        },
    };

    private static readonly Dictionary<string, string> ServiceHints = new(StringComparer.OrdinalIgnoreCase)
    {
        ["frontend-service"] = "Web 前端（nginx）",
        ["gateway-backend-service"] = "Web 后端网关（SSE）",
        ["task-scheduler-service"] = "任务调度",
        ["agent-orchestrator-service"] = "Agent 运行时",
        ["model-proxy-service"] = "模型代理",
        ["timer-task-service"] = "定时任务",
        ["qq-llbot-service"] = "QQ 渠道",
        ["openviking-context-service"] = "长期记忆(RAG，自动带 server)",
        ["openviking-server"] = "语义检索库（外部镜像）",
        ["user-service"] = "用户信息",
        ["dashboard-service"] = "管理面板",
        ["tool-runtime-service"] = "工具执行（需 WSL）",
        ["image-assets-service"] = "图床",
    };

    private static readonly (string Name, string Yaml)[] YamlMap =
    {
        ("dashboard-service", "deploy/services/dashboard-service.yaml"),
        ("agent-orchestrator-service", "deploy/services/agent-orchestrator-service.yaml"),
        ("task-scheduler-service", "deploy/services/task-scheduler-service.yaml"),
        ("timer-task-service", "deploy/services/timer-task-service.yaml"),
        ("gateway-backend-service", "deploy/services/gateway-backend-service.yaml"),
        ("qq-llbot-service", "deploy/services/qq-llbot-service.yaml"),
        ("model-proxy-service", "deploy/services/model-proxy-service.yaml"),
        ("openviking-context-service", "deploy/services/openviking-context-service.yaml"),
        ("openviking-server", "deploy/services/openviking-server.yaml"),
        ("user-service", "deploy/services/user-service.yaml"),
        ("frontend-service", "deploy/services/frontend-service.yaml"),
        ("tool-runtime-service", ""), // 特殊部署脚本，无 YAML
    };

    public DeployPage(DockerService docker, KubectlService kubectl)
    {
        InitializeComponent();
        _docker = docker;
        _kubectl = kubectl;
        ServiceList.ItemsSource = Services;
        Loaded += (s, e) => ParseDeployScript();
    }

    private void ParseDeployScript()
    {
        var projectRoot = FindProjectRoot();
        var ps1Path = Path.Combine(projectRoot, "deploy-all.ps1");

        if (!File.Exists(ps1Path))
        {
            AppendLog($"[系统] 找不到 {ps1Path}，使用内置服务列表");
            LoadBuiltinServices();
            return;
        }

        try
        {
            var content = File.ReadAllText(ps1Path);
            // $IMAGES: "name" = @{ dir = "..."; ... }  版本号不再写在脚本里，从部署文件读取
            var imagePattern = @"""([^""]+)""\s*=\s*@\{\s*dir\s*=\s*""([^""]+)""";
            var yamlPattern = @"""([^""]+)""\s*=\s*""([^""]+)""";

            var images = Regex.Matches(content, imagePattern);
            var yamls = new Dictionary<string, string>();
            var yamlSection = Regex.Match(content, @"\$YAML_MAP\s*=\s*@\{([^}]+)\}", RegexOptions.Singleline);
            if (yamlSection.Success)
            {
                var yamlMatches = Regex.Matches(yamlSection.Groups[1].Value, yamlPattern);
                foreach (Match m in yamlMatches)
                    yamls[m.Groups[1].Value] = m.Groups[2].Value;
            }

            var parsed = new List<(string name, string dir, string yaml, string tag)>();
            foreach (Match m in images)
            {
                var name = m.Groups[1].Value;
                var dir = m.Groups[2].Value;
                var yaml = yamls.TryGetValue(name, out var y) ? y : "";
                var tag = ResolveTagFromDeployFiles(projectRoot, name, yaml);
                parsed.Add((name, dir, yaml, tag));
            }

            BuildServiceList(parsed);
            AppendLog($"[系统] 从 {ps1Path} 解析到 {Services.Count} 个服务（版本号自动从部署文件读取）");
        }
        catch (Exception ex)
        {
            AppendLog($"[系统] 解析失败: {ex.Message}");
            LoadBuiltinServices();
        }
    }

    /// <summary>构建选择列表：core 组合项 + 各服务 + tool-runtime 的 codex/clawhub 子项</summary>
    private void BuildServiceList(List<(string name, string dir, string yaml, string tag)> parsed)
    {
        Services.Clear();

        // 1) 必要服务组合项（默认勾选）
        Services.Add(new ServiceInfo
        {
            Name = "必要服务",
            Hint = "Web 对话最小闭环（推荐）",
            Tag = "×5",
            IsCore = true,
            IsSelected = true,
        });

        // 2) 各服务（tool-runtime 后紧跟其功能子项 codex / clawhub）
        foreach (var (name, dir, yaml, tag) in parsed)
        {
            Services.Add(new ServiceInfo
            {
                Name = name,
                Directory = dir,
                Tag = tag,
                YamlPath = yaml,
                Hint = ServiceHints.TryGetValue(name, out var h) ? h : "",
                IsSelected = CoreServices.Contains(name),
            });

            if (name == "tool-runtime-service")
            {
                Services.Add(new ServiceInfo { Name = "codex", Hint = "代码生成（需 @openai/codex）", IsFeature = true, ParentName = "tool-runtime-service", IsSelected = true });
                Services.Add(new ServiceInfo { Name = "clawhub", Hint = "技能执行（需 node + clawhub）", IsFeature = true, ParentName = "tool-runtime-service", IsSelected = true });
            }
        }
    }

    /// <summary>
    /// 版本号自动从部署文件读取：优先 deploy/services/&lt;name&gt;.yaml 的 image 字段；
    /// tool-runtime 无 YAML，从 deploy/tool-runtime-apply.sh 的 TOOL_RUNTIME_IMAGE 读取。
    /// </summary>
    private static string ResolveTagFromDeployFiles(string projectRoot, string name, string yaml)
    {
        try
        {
            if (name == "tool-runtime-service")
            {
                var scriptPath = Path.Combine(projectRoot, "deploy", "tool-runtime-apply.sh");
                if (File.Exists(scriptPath))
                {
                    var m = Regex.Match(File.ReadAllText(scriptPath),
                        @"TOOL_RUNTIME_IMAGE[^:]*:\s*[^:]+:([^\s""}]+)");
                    if (m.Success) return m.Groups[1].Value;
                }
                return "";
            }

            if (string.IsNullOrEmpty(yaml)) return "";
            var yamlPath = Path.Combine(projectRoot, yaml);
            if (!File.Exists(yamlPath)) return "";
            var line = Regex.Match(File.ReadAllText(yamlPath), @"(?m)^\s*image:\s*(\S+)\s*$").Groups[1].Value;
            if (string.IsNullOrEmpty(line)) return "";
            var parts = line.Split(':');
            return parts[parts.Length - 1];
        }
        catch
        {
            return "";
        }
    }

    private void LoadBuiltinServices()
    {
        var builtin = new (string name, string dir, string tag)[]
        {
            ("dashboard-service", "services/dashboard-service", "v2"),
            ("agent-orchestrator-service", "services/agent-orchestrator-service", "v11"),
            ("task-scheduler-service", "services/task-scheduler-service", "v5"),
            ("timer-task-service", "services/timer-task-service", "v2"),
            ("gateway-backend-service", "services/gateway-backend-service", "v4"),
            ("qq-llbot-service", "services/qq-llbot-service", "v1"),
            ("model-proxy-service", "services/model-proxy-service", "v3"),
            ("openviking-context-service", "services/openviking-context-service", "v17"),
            ("openviking-server", "external", "v0.4.10"),
            ("tool-runtime-service", "services/tool-runtime-service", "v1"),
            ("user-service", "services/user-service", "v1"),
            ("frontend-service", "services/frontend-service", "v1"),
        };
        var projectRoot = FindProjectRoot();
        var parsed = new List<(string, string, string, string)>();
        foreach (var (name, dir, tag) in builtin)
        {
            var yaml = YamlMap.FirstOrDefault(x => x.Name == name).Yaml ?? "";
            var resolved = ResolveTagFromDeployFiles(projectRoot, name, yaml);
            parsed.Add((name, dir, yaml, string.IsNullOrEmpty(resolved) ? tag : resolved));
        }
        BuildServiceList(parsed);
    }

    private async void BtnDeploy_Click(object sender, RoutedEventArgs e)
    {
        BtnDeploy.IsEnabled = false;
        LogOutput.Document.Blocks.Clear();
        var projectRoot = FindProjectRoot();

        // 展开选择：core 组合项 → 核心服务；排除 feature 子项
        var selected = Services
            .Where(s => s.IsSelected && !s.IsFeature && s.Name != "必要服务")
            .ToList();
        if (Services.FirstOrDefault(s => s.IsCore)?.IsSelected == true)
        {
            foreach (var name in CoreServices)
            {
                var svc = Services.FirstOrDefault(s => s.Name == name);
                if (svc != null && !selected.Contains(svc))
                {
                    selected.Add(svc);
                    AppendLog($"[提示] 必要服务：已自动加入 {name}");
                }
            }
        }

        // 依赖补齐：openviking-context-service → openviking-server
        foreach (var svc in selected.ToList())
        {
            if (Dependencies.TryGetValue(svc.Name, out var deps))
            {
                foreach (var dep in deps)
                {
                    if (!selected.Any(s => s.Name == dep))
                    {
                        var depSvc = Services.FirstOrDefault(s => s.Name == dep);
                        if (depSvc != null)
                        {
                            AppendLog($"[提示] {svc.Name} 依赖 {dep}，已自动加入");
                            selected.Add(depSvc);
                        }
                    }
                }
            }
        }

        if (selected.Count == 0)
        {
            AppendLog("[系统] 没有选择任何服务");
            BtnDeploy.IsEnabled = true;
            return;
        }

        AppendLog($"[系统] 开始部署 {selected.Count} 个服务: {string.Join(", ", selected.Select(s => s.Name))}");

        // 确保 namespace 和 RBAC
        AppendLog("[系统] 确保 namespace 和 RBAC...");
        _kubectl.RunCommand("create namespace agent --dry-run=client -o yaml | kubectl apply -f -");
        _kubectl.RunCommand("-n agent create role pod-log-reader --verb=get,list --resource=pods,pods/log --dry-run=client -o yaml | kubectl apply -f -");
        _kubectl.RunCommand("-n agent create rolebinding pod-log-reader-binding --role=pod-log-reader --serviceaccount=agent:default --dry-run=client -o yaml | kubectl apply -f -");

        // 同步各服务需要的配置到 NFS
        await SyncConfigToNfsAsync(selected.Select(s => s.Name));

        // 构建 Docker 镜像
        AppendLog("\n═══ Docker 构建 ═══");
        foreach (var svc in selected)
        {
            var dir = Path.Combine(projectRoot, svc.Directory);
            if (!Directory.Exists(dir) || !File.Exists(Path.Combine(dir, "Dockerfile")))
            {
                if (svc.Name == "openviking-server" || svc.Name == "image-assets-service")
                    AppendLog($"[跳过] {svc.Name} - 外部镜像，无需构建");
                else
                    AppendLog($"[跳过] {svc.Name} - 目录或 Dockerfile 不存在");
                continue;
            }

            AppendLog($"\n> 构建 agent/{svc.Name}:{svc.Tag} ...");
            await RunAndWait($"docker build -t agent/{svc.Name}:{svc.Tag} {dir}");
        }

        // K8s 部署
        AppendLog("\n═══ K8s 部署 ═══");
        var deployedOpenviking = false;
        foreach (var svc in selected)
        {
            // tool-runtime 特殊部署脚本（WSL），传入 codex/clawhub/OpenViking 开关
            if (svc.Name == "tool-runtime-service")
            {
                var clawhub = Services.FirstOrDefault(x => x.Name == "clawhub")?.IsSelected ?? true;
                var codex = Services.FirstOrDefault(x => x.Name == "codex")?.IsSelected ?? true;
                var openviking = selected.Any(x => x.Name == "openviking-server");
                var repoWsl = WslPath(projectRoot);
                var env = $"ENABLE_CLAWHUB={(clawhub ? "true" : "false")} ENABLE_CODEX={(codex ? "true" : "false")} ENABLE_OPENVIKING={(openviking ? "true" : "false")}";
                AppendLog($"\n> 部署 tool-runtime-service（WSL 特殊脚本）...");
                await RunWslAsync($"cd '{repoWsl}' && {env} bash deploy/tool-runtime-apply.sh");
                continue;
            }

            if (string.IsNullOrEmpty(svc.YamlPath))
            {
                AppendLog($"[跳过] {svc.Name} - 无 YAML 映射");
                continue;
            }
            var yamlPath = Path.Combine(projectRoot, svc.YamlPath);
            if (!File.Exists(yamlPath))
            {
                AppendLog($"[跳过] {svc.Name} - YAML 不存在: {yamlPath}");
                continue;
            }
            AppendLog($"\n> 部署 {svc.Name} ...");
            await RunAndWait($"kubectl apply -f {yamlPath}");
            if (svc.Name == "openviking-server")
                deployedOpenviking = true;
        }

        // openviking 初始化：创建 agent-service 用户并生成 api_key
        if (deployedOpenviking)
            await InitOpenvikingAsync();

        AppendLog("\n[系统] 部署完成！");
        BtnDeploy.IsEnabled = true;
    }

    // ─── 配置同步到 NFS（只复制各服务需要的配置）───────────────

    private async Task SyncConfigToNfsAsync(IEnumerable<string> services)
    {
        AppendLog("[系统] 同步配置到 NFS（只复制各服务需要的配置）...");
        var projectRoot = FindProjectRoot();
        var repoWsl = WslPath(projectRoot);
        var nfsRoot = Environment.GetEnvironmentVariable("NFS_ROOT");
        if (string.IsNullOrEmpty(nfsRoot)) nfsRoot = "/srv/nfs/my-agent";

        foreach (var svc in services)
        {
            if (!ConfigMap.TryGetValue(svc, out var srcs)) continue;
            foreach (var src in srcs)
            {
                var rel = src.Replace("\\", "/").TrimEnd('/');
                var idx = rel.LastIndexOf('/');
                var destDir = idx > 0 ? $"{nfsRoot}/{rel.Substring(0, idx)}" : nfsRoot;
                var cmd = $"mkdir -p '{destDir}' && cp -r '{repoWsl}/{rel}' '{destDir}/' 2>/dev/null || true";
                AppendLog($"[sync] {svc} -> {rel}");
                await RunWslAsync(cmd);
            }
        }
        AppendLog("[OK] 配置已同步到 NFS");
    }

    // ─── openviking 初始化：创建 agent-service 用户并生成 api_key ──

    private async Task InitOpenvikingAsync()
    {
        AppendLog("[系统] 初始化 openviking（创建 agent-service 用户）...");
        var projectRoot = FindProjectRoot();
        var rootKeyFile = Path.Combine(projectRoot, "config", "openviking", "root_api_key");
        if (!File.Exists(rootKeyFile))
        {
            AppendLog("[跳过] 缺少 config/openviking/root_api_key");
            return;
        }
        var rootKey = File.ReadAllText(rootKeyFile).Trim();
        if (rootKey.Length == 0)
        {
            AppendLog("[跳过] root_api_key 为空");
            return;
        }

        AppendLog("  等待 openviking Pod ready ...");
        _kubectl.RunCommand("-n agent wait --for=condition=ready pod -l app=openviking --timeout=180s");

        var pf = _kubectl.StartPortForward("openviking-init", "agent", "openviking", 1933, 1933);
        await Task.Delay(3000);
        try
        {
            using var client = new HttpClient { Timeout = TimeSpan.FromSeconds(15) };
            client.DefaultRequestHeaders.Add("X-API-Key", rootKey);
            var body = new StringContent("{\"user_id\":\"agent-service\",\"role\":\"user\"}", Encoding.UTF8, "application/json");
            var resp = await client.PostAsync("http://127.0.0.1:1933/api/v1/admin/accounts/my-agent/users", body);
            var respText = await resp.Content.ReadAsStringAsync();

            if (resp.IsSuccessStatusCode)
            {
                string? key = null;
                using (var doc = JsonDocument.Parse(respText))
                {
                    if (doc.RootElement.TryGetProperty("result", out var r) &&
                        r.TryGetProperty("user_key", out var uk))
                        key = uk.GetString();
                    else if (doc.RootElement.TryGetProperty("user_key", out var uk2))
                        key = uk2.GetString();
                }
                if (!string.IsNullOrEmpty(key))
                {
                    File.WriteAllText(Path.Combine(projectRoot, "config", "openviking", "api_key"), key);
                    AppendLog("[OK] agent-service 用户已创建，key 已写入 config/openviking/api_key");
                    await SyncConfigToNfsAsync(new[] { "openviking-context-service", "tool-runtime-service", "dashboard-service" });
                    _kubectl.RunCommand("-n agent rollout restart deployment/openviking-context-service");
                    _kubectl.RunCommand("-n agent rollout restart deployment/tool-runtime-service");
                }
                else
                {
                    AppendLog($"[ERR] 响应中没有 user_key: {respText}");
                }
            }
            else if ((int)resp.StatusCode == 409)
            {
                AppendLog("[提示] agent-service 用户已存在（409），保留现有 api_key");
            }
            else
            {
                AppendLog($"[ERR] 创建 agent-service 用户失败: {(int)resp.StatusCode} {respText}");
            }
        }
        catch (Exception ex)
        {
            AppendLog($"[ERR] 初始化 openviking 失败: {ex.Message}");
        }
        finally
        {
            _kubectl.StopPortForward("openviking-init");
        }
    }

    // ─── 进程执行辅助 ─────────────────────────────────────────

    private async Task RunAndWait(string command)
    {
        var psi = new ProcessStartInfo
        {
            FileName = "cmd.exe",
            Arguments = $"/c {command}",
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false,
            CreateNoWindow = true
        };

        using var proc = Process.Start(psi)!;
        proc.OutputDataReceived += (s, e) =>
        {
            if (!string.IsNullOrEmpty(e.Data))
                Dispatcher.Invoke(() => AppendLog(e.Data));
        };
        proc.ErrorDataReceived += (s, e) =>
        {
            if (!string.IsNullOrEmpty(e.Data))
                Dispatcher.Invoke(() => AppendLog($"[ERR] {e.Data}"));
        };
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
            CreateNoWindow = true
        };

        using var proc = Process.Start(psi)!;
        proc.OutputDataReceived += (s, e) =>
        {
            if (!string.IsNullOrEmpty(e.Data))
                Dispatcher.Invoke(() => AppendLog(e.Data));
        };
        proc.ErrorDataReceived += (s, e) =>
        {
            if (!string.IsNullOrEmpty(e.Data))
                Dispatcher.Invoke(() => AppendLog($"[ERR] {e.Data}"));
        };
        proc.BeginOutputReadLine();
        proc.BeginErrorReadLine();
        await proc.WaitForExitAsync();
    }

    /// <summary>Windows 路径 → WSL 路径（E:\github\My_Agent_MSA → /mnt/e/github/My_Agent_MSA）</summary>
    private static string WslPath(string projectRoot)
    {
        var drive = Path.GetPathRoot(projectRoot)?.TrimEnd('\\', ':').ToLower() ?? "c";
        var idx = projectRoot.IndexOf(':');
        var rest = idx >= 0 ? projectRoot.Substring(idx + 1).Replace("\\", "/").TrimStart('/') : projectRoot.Replace("\\", "/");
        return $"/mnt/{drive}/{rest}";
    }

    private void AppendLog(string text)
    {
        var para = new Paragraph(new Run(text))
        {
            Margin = new Thickness(0, 0, 0, 0),
            Foreground = System.Windows.Media.Brushes.LightGray
        };
        if (text.StartsWith("[ERR]"))
            para.Foreground = System.Windows.Media.Brushes.OrangeRed;
        else if (text.StartsWith("[系统]") || text.StartsWith("[OK]"))
            para.Foreground = System.Windows.Media.Brushes.Cyan;
        else if (text.StartsWith(">"))
            para.Foreground = System.Windows.Media.Brushes.Yellow;
        else if (text.StartsWith("[提示]"))
            para.Foreground = System.Windows.Media.Brushes.Gold;

        LogOutput.Document.Blocks.Add(para);
        LogOutput.ScrollToEnd();
    }

    /// <summary>
    /// 从 exe 所在目录向上查找项目根目录（包含 deploy-all.ps1 或 services/）
    /// 优先级：exe路径向上 → admin-panel源码目录向上 → 回退内置列表
    /// </summary>
    private string FindProjectRoot()
    {
        // 1. 从运行目录向上找
        var dir = AppDomain.CurrentDomain.BaseDirectory;
        while (!string.IsNullOrEmpty(dir))
        {
            if (File.Exists(Path.Combine(dir, "deploy-all.ps1")) &&
                Directory.Exists(Path.Combine(dir, "services")))
                return dir;
            dir = Path.GetDirectoryName(dir)!;
        }

        // 2. 从 admin-panel 源码目录向上找（dev 场景）
        var srcDir = Path.GetFullPath(Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "..", "..", "..", ".."));
        if (File.Exists(Path.Combine(srcDir, "deploy-all.ps1")) &&
            Directory.Exists(Path.Combine(srcDir, "services")))
            return srcDir;

        return AppDomain.CurrentDomain.BaseDirectory;
    }
}
