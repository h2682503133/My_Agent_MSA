using System.ComponentModel;
using System.Runtime.CompilerServices;

namespace MyAgentAdminPanel.Models;

public class ServiceInfo : INotifyPropertyChanged
{
    private bool _isSelected = true;
    private bool _featureEnabled = true;

    public string Name { get; set; } = "";
    public string Directory { get; set; } = "";
    public string Tag { get; set; } = "";
    public string YamlPath { get; set; } = "";

    /// <summary>界面说明文字（服务用途 / 子项说明）</summary>
    public string Hint { get; set; } = "";

    /// <summary>是否为「必要服务」组合项（core，勾选即选中一组核心服务）</summary>
    public bool IsCore { get; set; }

    /// <summary>是否为 tool-runtime 的功能子项（codex / clawhub）</summary>
    public bool IsFeature { get; set; }

    /// <summary>feature 子项所属的服务名</summary>
    public string ParentName { get; set; } = "";

    /// <summary>feature 是否启用（部署 tool-runtime 时传入 ENABLE_*）</summary>
    public bool FeatureEnabled
    {
        get => _featureEnabled;
        set { _featureEnabled = value; OnPropertyChanged(); }
    }

    public bool IsSelected
    {
        get => _isSelected;
        set { _isSelected = value; OnPropertyChanged(); }
    }

    public event PropertyChangedEventHandler? PropertyChanged;
    protected void OnPropertyChanged([CallerMemberName] string? name = null)
        => PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(name));
}
