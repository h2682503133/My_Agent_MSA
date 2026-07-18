using System;
using System.Windows;
using System.Windows.Controls;
using MyAgentAdminPanel.Pages;
using MyAgentAdminPanel.Services;

namespace MyAgentAdminPanel;

public partial class MainWindow : Window
{
    private readonly KubectlService _kubectl = new();
    private readonly DockerService _docker = new();
    private readonly LlBotService _llbot = new();
    private Button? _activeNav;

    public MainWindow()
    {
        InitializeComponent();
        _activeNav = BtnPortForward;
        NavigateToPortForward();
    }

    private void NavButton_Click(object sender, RoutedEventArgs e)
    {
        if (sender is not Button btn) return;

        // 重置所有按钮样式
        foreach (var child in ((StackPanel)btn.Parent).Children)
        {
            if (child is Button b)
                b.Style = (Style)FindResource("NavButton");
        }
        btn.Style = (Style)FindResource("NavButtonActive");
        _activeNav = btn;

        if (btn == BtnPortForward) NavigateToPortForward();
        else if (btn == BtnDeploy) NavigateToDeploy();
        else if (btn == BtnLLBot) NavigateToLLBot();
        else if (btn == BtnDashboard) NavigateToDashboard();
    }

    private void NavigateToPortForward()
        => ContentFrame.Navigate(new PortForwardPage(_kubectl));

    private void NavigateToDeploy()
        => ContentFrame.Navigate(new DeployPage(_docker, _kubectl));

    private void NavigateToLLBot()
        => ContentFrame.Navigate(new LLBotPage(_llbot));

    private void NavigateToDashboard()
        => ContentFrame.Navigate(new DashboardPage());

    protected override void OnClosed(EventArgs e)
    {
        _kubectl.StopAll();
        _llbot.Dispose();
        base.OnClosed(e);
    }
}
