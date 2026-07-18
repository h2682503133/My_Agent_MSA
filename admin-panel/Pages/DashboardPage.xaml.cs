using System;
using System.Diagnostics;
using System.Net.Http;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Controls;
using Microsoft.Web.WebView2.Core;

namespace MyAgentAdminPanel.Pages;

public partial class DashboardPage : Page
{
    private const string DashboardUrl = "http://localhost:5601";

    public DashboardPage()
    {
        InitializeComponent();
        Loaded += async (s, e) => await InitializeWebView();
    }

    private async Task InitializeWebView()
    {
        try
        {
            // 确保 WebView2 运行时
            await DashboardWebView.EnsureCoreWebView2Async();

            // 检测 Dashboard 是否可达
            if (await IsDashboardReachable())
            {
                DashboardWebView.CoreWebView2.Navigate(DashboardUrl);
                ErrorOverlay.Visibility = Visibility.Collapsed;
            }
            else
            {
                ErrorOverlay.Visibility = Visibility.Visible;
            }
        }
        catch (Exception)
        {
            ErrorOverlay.Visibility = Visibility.Visible;
        }
    }

    private async Task<bool> IsDashboardReachable()
    {
        try
        {
            using var client = new HttpClient { Timeout = TimeSpan.FromSeconds(3) };
            var resp = await client.GetAsync(DashboardUrl);
            return resp.IsSuccessStatusCode;
        }
        catch
        {
            return false;
        }
    }

    private void BtnRefresh_Click(object sender, RoutedEventArgs e)
    {
        if (DashboardWebView.CoreWebView2 != null)
            DashboardWebView.CoreWebView2.Reload();
        else
            _ = InitializeWebView();
    }

    private void BtnOpenBrowser_Click(object sender, RoutedEventArgs e)
    {
        Process.Start(new ProcessStartInfo
        {
            FileName = DashboardUrl,
            UseShellExecute = true
        });
    }
}
