using System;
using System.IO;
using System.Text.Json;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Documents;
using System.Windows.Media.Imaging;
using MyAgentAdminPanel.Services;

namespace MyAgentAdminPanel.Pages;

public partial class LLBotPage : Page
{
    private readonly LlBotService _llbot;
    private bool _isRunning;
    private static readonly string SettingsDir = Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
        "MyAgentAdminPanel");
    private static readonly string SettingsFile = Path.Combine(SettingsDir, "settings.json");

    public LLBotPage(LlBotService llbot)
    {
        InitializeComponent();
        _llbot = llbot;

        _llbot.LogReceived += OnLogReceived;
        _llbot.QrCodeChanged += OnQrCodeChanged;

        LoadSettings();
        if (!string.IsNullOrEmpty(TxtFolderPath.Text))
            _llbot.LlbotFolder = TxtFolderPath.Text;
        if (!string.IsNullOrEmpty(TxtQqPath.Text))
            _llbot.QqPath = TxtQqPath.Text;

        TxtQrUrl.Text = _llbot.QrCodeUrl;
    }

    private void OnLogReceived(string text)
    {
        Dispatcher.Invoke(() =>
        {
            var para = new Paragraph(new Run(text))
            {
                Margin = new Thickness(0),
                Foreground = System.Windows.Media.Brushes.LightGray
            };
            if (text.StartsWith("[ERR]"))
                para.Foreground = System.Windows.Media.Brushes.OrangeRed;
            else if (text.StartsWith("[系统]"))
                para.Foreground = System.Windows.Media.Brushes.Cyan;

            LogOutput.Document.Blocks.Add(para);
            LogOutput.ScrollToEnd();
        });
    }

    private async void OnQrCodeChanged(string path)
    {
        await Dispatcher.Invoke(async () =>
        {
            try
            {
                System.Threading.Thread.Sleep(300);
                var bitmap = new BitmapImage();
                bitmap.BeginInit();
                bitmap.CacheOption = BitmapCacheOption.OnLoad;
                bitmap.UriSource = new Uri(path);
                bitmap.EndInit();
                QrCodeImage.Source = bitmap;
                TxtQrStatus.Text = "二维码已更新";
                TxtQrStatus.Foreground = System.Windows.Media.Brushes.Green;
            }
            catch (Exception ex)
            {
                TxtQrStatus.Text = $"加载失败: {ex.Message}";
                TxtQrStatus.Foreground = System.Windows.Media.Brushes.Red;
            }
        });

        // 自动推送到 Dashboard
        await _llbot.PushQrCodeToDashboard();
    }

    private void TxtFolderPath_TextChanged(object sender, TextChangedEventArgs e)
    {
        SaveSettings();
        if (Directory.Exists(TxtFolderPath.Text))
            _llbot.LlbotFolder = TxtFolderPath.Text;
    }

    private void TxtQqPath_TextChanged(object sender, TextChangedEventArgs e)
    {
        _llbot.QqPath = TxtQqPath.Text;
        SaveSettings();
    }

    private void BtnBrowseFolder_Click(object sender, RoutedEventArgs e)
    {
        var dialog = new System.Windows.Forms.FolderBrowserDialog
        {
            Description = "选择 LLBot 文件夹"
        };
        if (dialog.ShowDialog() == System.Windows.Forms.DialogResult.OK)
            TxtFolderPath.Text = dialog.SelectedPath;
    }

    private void BtnBrowseQq_Click(object sender, RoutedEventArgs e)
    {
        var dialog = new System.Windows.Forms.OpenFileDialog
        {
            Title = "选择 QQ 程序",
            Filter = "QQ 程序|QQ.exe|所有文件|*.exe"
        };
        if (dialog.ShowDialog() == System.Windows.Forms.DialogResult.OK)
            TxtQqPath.Text = dialog.FileName;
    }

    private void BtnStartStop_Click(object sender, RoutedEventArgs e)
    {
        if (_isRunning)
        {
            _llbot.StopLlbot();
            _isRunning = false;
            BtnStartStop.Content = "▶ 启动";
            BtnStartStop.Background = new System.Windows.Media.SolidColorBrush(
                System.Windows.Media.Color.FromRgb(0, 120, 212));
            AppendLog("[系统] LLBot 已停止");
        }
        else
        {
            if (string.IsNullOrEmpty(TxtFolderPath.Text))
            {
                AppendLog("[系统] 请先选择 LLBot 文件夹");
                return;
            }
            try
            {
                _llbot.StartLlbot();
                _isRunning = true;
                BtnStartStop.Content = "⏹ 停止";
                BtnStartStop.Background = new System.Windows.Media.SolidColorBrush(
                    System.Windows.Media.Color.FromRgb(211, 47, 47));
                AppendLog("[系统] LLBot 已启动");
            }
            catch (Exception ex)
            {
                AppendLog($"[ERR] 启动失败: {ex.Message}");
            }
        }
    }

    private async void BtnPushQr_Click(object sender, RoutedEventArgs e)
    {
        await _llbot.PushQrCodeToDashboard();
    }

    private void AppendLog(string text)
    {
        OnLogReceived(text);
    }

    private void LoadSettings()
    {
        try
        {
            if (File.Exists(SettingsFile))
            {
                var json = File.ReadAllText(SettingsFile);
                var settings = JsonSerializer.Deserialize<SettingsData>(json);
                if (settings != null)
                {
                    TxtFolderPath.Text = settings.LlbotFolder ?? "";
                    TxtQqPath.Text = settings.QqPath ?? "";
                }
            }
        }
        catch { }
    }

    private void SaveSettings()
    {
        try
        {
            Directory.CreateDirectory(SettingsDir);
            var settings = new SettingsData
            {
                LlbotFolder = TxtFolderPath.Text,
                QqPath = TxtQqPath.Text
            };
            File.WriteAllText(SettingsFile, JsonSerializer.Serialize(settings));
        }
        catch { }
    }

    private class SettingsData
    {
        public string? LlbotFolder { get; set; }
        public string? QqPath { get; set; }
    }
}
