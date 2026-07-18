using System;
using System.Collections.Generic;
using System.Collections.ObjectModel;
using System.Diagnostics;
using System.IO;
using System.Linq;
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
        ("user-service", "deploy/services/user-service.yaml"),
        ("frontend-service", "deploy/services/frontend-service.yaml"),
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
            var imagePattern = @"""([^""]+)""\s*=\s*@\{\s*dir\s*=\s*""([^""]+)"";\s*tag\s*=\s*""([^""]+)""";
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

            Services.Clear();
            foreach (Match m in images)
            {
                var name = m.Groups[1].Value;
                var dir = m.Groups[2].Value;
                var tag = m.Groups[3].Value;
                var yaml = yamls.TryGetValue(name, out var y) ? y : "";
                Services.Add(new ServiceInfo { Name = name, Directory = dir, Tag = tag, YamlPath = yaml });
            }

            AppendLog($"[系统] 从 {ps1Path} 解析到 {Services.Count} 个服务");
        }
        catch (Exception ex)
        {
            AppendLog($"[系统] 解析失败: {ex.Message}");
            LoadBuiltinServices();
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
            ("tool-runtime-service", "services/tool-runtime-service", "v1"),
            ("user-service", "services/user-service", "v1"),
            ("frontend-service", "services/frontend-service", "v1"),
        };
        Services.Clear();
        foreach (var (name, dir, tag) in builtin)
        {
            var yaml = YamlMap.FirstOrDefault(x => x.Name == name).Yaml ?? "";
            Services.Add(new ServiceInfo { Name = name, Directory = dir, Tag = tag, YamlPath = yaml });
        }
    }

    private async void BtnDeploy_Click(object sender, RoutedEventArgs e)
    {
        BtnDeploy.IsEnabled = false;
        LogOutput.Document.Blocks.Clear();

        var selected = Services.Where(s => s.IsSelected).ToList();
        if (selected.Count == 0)
        {
            AppendLog("[系统] 没有选择任何服务");
            BtnDeploy.IsEnabled = true;
            return;
        }

        AppendLog($"[系统] 开始部署 {selected.Count} 个服务...");

        // 确保 namespace 和 RBAC
        AppendLog("[系统] 确保 namespace 和 RBAC...");
        _kubectl.RunCommand("create namespace agent --dry-run=client -o yaml | kubectl apply -f -");
        _kubectl.RunCommand("-n agent create role pod-log-reader --verb=get,list --resource=pods,namespaces,pods/log --dry-run=client -o yaml | kubectl apply -f -");
        _kubectl.RunCommand("-n agent create rolebinding pod-log-reader-binding --role=pod-log-reader --serviceaccount=agent:default --dry-run=client -o yaml | kubectl apply -f -");

        // 构建 Docker 镜像
        AppendLog("\n═══ Docker 构建 ═══");
        var projectRoot = FindProjectRoot();
        foreach (var svc in selected)
        {
            var dir = Path.Combine(projectRoot, svc.Directory);
            if (!Directory.Exists(dir) || !File.Exists(Path.Combine(dir, "Dockerfile")))
            {
                AppendLog($"[跳过] {svc.Name} - 目录或 Dockerfile 不存在");
                continue;
            }

            AppendLog($"\n> 构建 agent/{svc.Name}:{svc.Tag} ...");
            await RunAndWait($"docker build -t agent/{svc.Name}:{svc.Tag} {dir}");
        }

        // K8s 部署
        AppendLog("\n═══ K8s 部署 ═══");
        foreach (var svc in selected)
        {
            if (string.IsNullOrEmpty(svc.YamlPath)) continue;
            var yamlPath = Path.Combine(projectRoot, svc.YamlPath);
            if (!File.Exists(yamlPath))
            {
                AppendLog($"[跳过] {svc.Name} - YAML 不存在: {yamlPath}");
                continue;
            }
            AppendLog($"\n> 部署 {svc.Name} ...");
            await RunAndWait($"kubectl apply -f {yamlPath}");
        }

        AppendLog("\n[系统] 部署完成！");
        BtnDeploy.IsEnabled = true;
    }

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

    private void AppendLog(string text)
    {
        var para = new Paragraph(new Run(text))
        {
            Margin = new Thickness(0, 0, 0, 0),
            Foreground = System.Windows.Media.Brushes.LightGray
        };
        if (text.StartsWith("[ERR]"))
            para.Foreground = System.Windows.Media.Brushes.OrangeRed;
        else if (text.StartsWith("[系统]"))
            para.Foreground = System.Windows.Media.Brushes.Cyan;
        else if (text.StartsWith(">"))
            para.Foreground = System.Windows.Media.Brushes.Yellow;

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
