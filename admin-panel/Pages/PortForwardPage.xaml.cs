using System.Collections.ObjectModel;
using System.Windows;
using System.Windows.Controls;
using MyAgentAdminPanel.Models;
using MyAgentAdminPanel.Services;

namespace MyAgentAdminPanel.Pages;

public partial class PortForwardPage : Page
{
    private readonly KubectlService _kubectl;
    public ObservableCollection<PortForwardItem> Items { get; } = new()
    {
        new() { ServiceName = "dashboard-service",  ServiceTarget = "dashboard-service",  LocalPort = 5601, RemotePort = 5601 },
        new() { ServiceName = "gateway-backend",    ServiceTarget = "gateway-backend-service", LocalPort = 5210, RemotePort = 5210 },
        new() { ServiceName = "image-assets",       ServiceTarget = "image-assets-service", LocalPort = 5102, RemotePort = 80 },
        new() { ServiceName = "istio-ingressgateway", ServiceTarget = "istio-ingressgateway", LocalPort = 8080, RemotePort = 80, Namespace = "istio-system" },
    };

    public PortForwardPage(KubectlService kubectl)
    {
        InitializeComponent();
        _kubectl = kubectl;
        ForwardList.ItemsSource = Items;
        Loaded += (s, e) => RefreshStatus();
        IsVisibleChanged += (s, e) => { if (IsVisible) RefreshStatus(); };
    }

    private void RefreshStatus()
    {
        foreach (var item in Items)
            item.IsRunning = _kubectl.IsRunning(item.ServiceName);
    }

    private void BtnStart_Click(object sender, RoutedEventArgs e)
    {
        if (sender is Button btn && btn.DataContext is PortForwardItem item)
        {
            _kubectl.StartPortForward(item.ServiceName, item.Namespace, item.ServiceTarget, item.LocalPort, item.RemotePort);
            item.IsRunning = true;
        }
    }

    private void BtnStop_Click(object sender, RoutedEventArgs e)
    {
        if (sender is Button btn && btn.DataContext is PortForwardItem item)
        {
            _kubectl.StopPortForward(item.ServiceName);
            item.IsRunning = false;
        }
    }

    private void BtnStartAll_Click(object sender, RoutedEventArgs e)
    {
        foreach (var item in Items)
        {
            _kubectl.StartPortForward(item.ServiceName, item.Namespace, item.ServiceTarget, item.LocalPort, item.RemotePort);
            item.IsRunning = true;
        }
    }

    private void BtnStopAll_Click(object sender, RoutedEventArgs e)
    {
        _kubectl.StopAll();
        foreach (var item in Items)
            item.IsRunning = false;
    }
}
