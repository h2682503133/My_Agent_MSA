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

    // 页面缓存，复用实例
    private PortForwardPage? _portForwardPage;
    private DeployPage? _deployPage;
    private LLBotPage? _llbotPage;
    private DashboardPage? _dashboardPage;

    public MainWindow()
    {
        InitializeComponent();
        _activeNav = BtnPortForward;
        NavigateToPortForward();
    }

    private void NavButton_Click(object sender, RoutedEventArgs e)
    {
        if (sender is not Button btn) return;

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
    {
        _portForwardPage ??= new PortForwardPage(_kubectl);
        ContentFrame.Navigate(_portForwardPage);
    }

    private void NavigateToDeploy()
    {
        _deployPage ??= new DeployPage(_docker, _kubectl);
        ContentFrame.Navigate(_deployPage);
    }

    private void NavigateToLLBot()
    {
        _llbotPage ??= new LLBotPage(_llbot);
        ContentFrame.Navigate(_llbotPage);
    }

    private void NavigateToDashboard()
    {
        _dashboardPage ??= new DashboardPage();
        ContentFrame.Navigate(_dashboardPage);
    }

    protected override void OnClosed(EventArgs e)
    {
        _kubectl.StopAll();
        _llbot.Dispose();
        base.OnClosed(e);
    }
}
