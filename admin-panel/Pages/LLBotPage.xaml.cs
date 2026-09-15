using System;
using System.IO;
using System.Net.Http;
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
        _llbot.QrCodeStatus += OnQrCodeStatus;
        _llbot.QrUrlChanged += OnQrUrlChanged;
        _llbot.QrImageFetched += OnQrCodeChanged;   // 兜底取到的码：只显示，不推送

        LoadSettings();
        if (!string.IsNullOrEmpty(TxtFolderPath.Text))
            _llbot.LlbotFolder = TxtFolderPath.Text;
        if (!string.IsNullOrEmpty(TxtQqPath.Text))
            _llbot.QqPath = TxtQqPath.Text;
        _llbot.WebuiBaseUrl = string.IsNullOrWhiteSpace(TxtWebuiUrl.Text)
            ? "http://127.0.0.1:3080" : TxtWebuiUrl.Text.Trim();
        _llbot.WebuiToken = (TxtWebuiToken.Text ?? "").Trim();

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

    /// <summary>二维码内容变化：从字节流加载（不再依赖文件路径）。</summary>
    private async void OnQrCodeChanged(QrCodeResult qr)
    {
        await Dispatcher.InvokeAsync(() =>
        {
            try
            {
                var bitmap = new BitmapImage();
                bitmap.BeginInit();
                bitmap.CacheOption = BitmapCacheOption.OnLoad;
                bitmap.StreamSource = new MemoryStream(qr.Png!);
                bitmap.EndInit();
                bitmap.Freeze();
                QrCodeImage.Source = bitmap;
                TxtQrStatus.Text = qr.Source == "api" ? "二维码已更新（WebUI API）" : "二维码已更新（文件）";
                TxtQrStatus.Foreground = System.Windows.Media.Brushes.Green;
            }
            catch (Exception ex)
            {
                TxtQrStatus.Text = $"加载失败: {ex.Message}";
                TxtQrStatus.Foreground = System.Windows.Media.Brushes.Red;
            }
        });

        // 注意：这里【不再自动推送】。
        // 推送只走 OnQrUrlChanged（llbot 日志里的「二维码网址」）——那才是实例当前在等的码；
        // 用 API 取来的码去推送，等于让 llbot 又生成一张新码、把正在等的码作废。
        // API/文件路径只在用户手动点「推送到 Dashboard」且日志里没有网址时才使用。
    }

    /// <summary>
    /// llbot 日志里出现新的「二维码网址」——主路径（零副作用）。
    /// 该网址对应 llbot 当前真正等待扫码的那张码，所以这里拉图展示 + 推送到 Dashboard，
    /// 推送内容与 llbot 状态天然一致，不会推过期码。
    /// </summary>
    private async void OnQrUrlChanged(string url)
    {
        byte[]? png = null;
        try
        {
            using var client = new HttpClient { Timeout = TimeSpan.FromSeconds(10) };
            png = await client.GetByteArrayAsync(url);
        }
        catch (Exception ex)
        {
            AppendLog($"[ERR] 拉取二维码图片失败: {ex.Message}");
        }

        if (png is { Length: > 0 })
        {
            await Dispatcher.InvokeAsync(() =>
            {
                try
                {
                    var bitmap = new BitmapImage();
                    bitmap.BeginInit();
                    bitmap.CacheOption = BitmapCacheOption.OnLoad;
                    bitmap.StreamSource = new MemoryStream(png);
                    bitmap.EndInit();
                    bitmap.Freeze();
                    QrCodeImage.Source = bitmap;
                    TxtQrStatus.Text = "二维码已更新（llbot 日志网址）";
                    TxtQrStatus.Foreground = System.Windows.Media.Brushes.Green;
                }
                catch (Exception ex)
                {
                    TxtQrStatus.Text = $"加载失败: {ex.Message}";
                    TxtQrStatus.Foreground = System.Windows.Media.Brushes.Red;
                }
            });
        }

        // 推送这一张（与 llbot 正在等的码同一张）
        await _llbot.PushQrUrlToDashboard("http://localhost:5700", url);
    }

    /// <summary>轮询状态：刷新取源、剩余有效期或已登录信息。</summary>
    private void OnQrCodeStatus(QrCodeResult qr)
    {
        Dispatcher.Invoke(() =>
        {
            // 已登录：不需要二维码（llbot 此时取码接口会阻塞）
            if (qr.Source == "online")
            {
                TxtQrSource.Text = $"已登录：{qr.Nick}（{qr.Uin}）· 无需扫码";
                TxtQrSource.Foreground = System.Windows.Media.Brushes.SeaGreen;
                return;
            }
            // 主路径：llbot 日志里有二维码网址（图片由 OnQrUrlChanged 单独拉取展示）
            if (qr.Source == "log")
            {
                var age = (DateTime.UtcNow - _llbot.LastQrUrlAt).TotalSeconds;
                TxtQrSource.Text = $"取源：llbot 日志网址（零副作用，就是实例在等的码）· {age:F0} 秒前更新";
                TxtQrSource.Foreground = System.Windows.Media.Brushes.DimGray;
                return;
            }
            if (!qr.Ok)
            {
                TxtQrSource.Text = qr.Error ?? "未取到二维码";
                TxtQrSource.Foreground = System.Windows.Media.Brushes.IndianRed;
                return;
            }
            var src = qr.Source switch
            {
                "api" => "取源：WebUI API",
                "file" => "取源：二维码文件（兜底）",
                _ => "取源：未知",
            };
            if (qr.ExpireInSeconds is int left)
                src += left > 0 ? $" · 剩余 {left / 60} 分 {left % 60} 秒" : " · 已过期，正在刷新…";
            if (!string.IsNullOrEmpty(qr.Error)) src += "\n" + qr.Error;
            TxtQrSource.Text = src;
            TxtQrSource.Foreground = System.Windows.Media.Brushes.DimGray;
        });
    }

    private void TxtWebuiUrl_TextChanged(object sender, TextChangedEventArgs e)
    {
        _llbot.WebuiBaseUrl = string.IsNullOrWhiteSpace(TxtWebuiUrl.Text)
            ? "http://127.0.0.1:3080" : TxtWebuiUrl.Text.Trim();
        SaveSettings();
    }

    private void TxtWebuiToken_TextChanged(object sender, TextChangedEventArgs e)
    {
        _llbot.WebuiToken = (TxtWebuiToken.Text ?? "").Trim();
        SaveSettings();
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
            _llbot.StopQrPolling();
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
                // 启动后按 WebUI API 优先、文件兜底的方式轮询二维码
                _llbot.StartQrPolling();
            }
            catch (Exception ex)
            {
                AppendLog($"[ERR] 启动失败: {ex.Message}");
            }
        }
    }

    private async void BtnPushQr_Click(object sender, RoutedEventArgs e)
    {
        // 优先推送 llbot 日志里的「二维码网址」（零副作用）；
        // 只有日志里还没有网址时才回退到 API 取码（会生成新码）。
        await _llbot.PushLatestToDashboard();
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
                    TxtWebuiUrl.Text = settings.WebuiUrl ?? "http://127.0.0.1:3080";
                    TxtWebuiToken.Text = settings.WebuiToken ?? "";
                }
            }
            else
            {
                TxtWebuiUrl.Text = "http://127.0.0.1:3080";
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
                QqPath = TxtQqPath.Text,
                WebuiUrl = TxtWebuiUrl.Text,
                WebuiToken = TxtWebuiToken.Text
            };
            File.WriteAllText(SettingsFile, JsonSerializer.Serialize(settings));
        }
        catch { }
    }

    private class SettingsData
    {
        public string? LlbotFolder { get; set; }
        public string? QqPath { get; set; }
        public string? WebuiUrl { get; set; }
        public string? WebuiToken { get; set; }
    }
}
