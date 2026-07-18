using System.ComponentModel;
using System.Runtime.CompilerServices;

namespace MyAgentAdminPanel.Models;

public class ServiceInfo : INotifyPropertyChanged
{
    private bool _isSelected = true;

    public string Name { get; set; } = "";
    public string Directory { get; set; } = "";
    public string Tag { get; set; } = "";
    public string YamlPath { get; set; } = "";

    public bool IsSelected
    {
        get => _isSelected;
        set { _isSelected = value; OnPropertyChanged(); }
    }

    public event PropertyChangedEventHandler? PropertyChanged;
    protected void OnPropertyChanged([CallerMemberName] string? name = null)
        => PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(name));
}
