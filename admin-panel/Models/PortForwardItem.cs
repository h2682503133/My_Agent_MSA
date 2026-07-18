using System.ComponentModel;
using System.Runtime.CompilerServices;

namespace MyAgentAdminPanel.Models;

public class PortForwardItem : INotifyPropertyChanged
{
    private string _status = "已停止";
    private bool _isRunning;

    public string ServiceName { get; set; } = "";
    public string Namespace { get; set; } = "agent";
    public string ServiceTarget { get; set; } = "";
    public int LocalPort { get; set; }
    public int RemotePort { get; set; }

    public bool IsRunning
    {
        get => _isRunning;
        set
        {
            _isRunning = value;
            Status = value ? "运行中" : "已停止";
            OnPropertyChanged();
        }
    }

    public string Status
    {
        get => _status;
        set { _status = value; OnPropertyChanged(); }
    }

    public event PropertyChangedEventHandler? PropertyChanged;
    protected void OnPropertyChanged([CallerMemberName] string? name = null)
        => PropertyChanged?.Invoke(this, new PropertyChangedEventArgs(name));
}
