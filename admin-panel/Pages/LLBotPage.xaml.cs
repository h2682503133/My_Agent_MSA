using System;
using System.Linq;
using System.Threading.Tasks;
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
        _llbot.LlbotExited += OnLlbotExited;
        _llbot.QrImageFetched += OnQrCodeChanged;   // 兜底取到的码：只显示，不推送

        LoadSettings();
        ApplySelectedExe();
        CmbMode.SelectedIndex = _llbot.HeadlessMode ? 1 : 0;
        CmbCdn.SelectedIndex = _llbot.SignCdn == "cf" ? 1 : 0;
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

    private void TxtExePath_TextChanged(object sender, TextChangedEventArgs e)
    {
        SaveSettings();
        ApplySelectedExe();
    }

    /// <summary>把输入框里的 exe 路径同步给服务并刷新识别结果。</summary>
    private void ApplySelectedExe()
    {
        var path = (TxtExePath.Text ?? "").Trim().Trim('"');
        if (path.Length > 0)
            _llbot.LlbotExe = path;
        UpdateLlbotInfo();
    }

    /// <summary>展示识别结果：新旧布局 / 运行时版本 / 数据目录 / 二维码文件位置。</summary>
    private void UpdateLlbotInfo()
    {
        var exe = _llbot.ResolveLlbotExe();
        if (exe.Length == 0)
        {
            TxtLlbotInfo.Text = "⚠ 未找到启动器 exe：请选择安装根目录下的 LuckyLillia.exe（新版）或 llbot.exe（旧版）";
            TxtLlbotInfo.Foreground = System.Windows.Media.Brushes.IndianRed;
            return;
        }

        var version = _llbot.VersionText;
        var layout = _llbot.LayoutText + (version.Length > 0 ? $" · 运行时 v{version}" : "");
        var qr = _llbot.QrCodePath;
        var leftovers = _llbot.FindInstallProcesses().Count;
        TxtLlbotInfo.Text =
            $"识别为：{layout}    工作目录：{_llbot.LlbotFolder}    数据目录：{_llbot.DataDir}"
            + (qr.Length > 0 ? $"    二维码文件：{qr}" : "")
            + (leftovers > 0 ? $"    ⚠ 检测到 {leftovers} 个残留进程（点「清理残留」）" : "");

        // 只有「新版 + PMHQ 模式」才需要这几样：PMHQ 抠不到 QQ 窗口里的码
        var pmhq = _llbot.Layout == LlBotService.LlbotLayout.Bundled && !_llbot.HeadlessMode;
        TxtPmhqHint.Visibility = pmhq ? Visibility.Visible : Visibility.Collapsed;
        BtnFocusQq.Visibility = pmhq ? Visibility.Visible : Visibility.Collapsed;
        BtnCaptureQq.Visibility = pmhq ? Visibility.Visible : Visibility.Collapsed;
        TxtLlbotInfo.Foreground = leftovers > 0
            ? System.Windows.Media.Brushes.DarkOrange
            : System.Windows.Media.Brushes.DimGray;
    }

    /// <summary>
    /// 清理残留：结束安装目录下仍在运行的启动器 / PMHQ / 内嵌 node。
    /// 典型场景：面板被直接关掉（没点停止）、或启动器是命令行拉起来的 ——
    /// 这时面板没有它们的句柄，只能按安装路径认领。
    /// </summary>


    /// <summary>进程自己退出（崩溃/被外部结束）时，把按钮和内部状态同步回来。</summary>
    private void OnLlbotExited()
    {
        Dispatcher.Invoke(() =>
        {
            _isRunning = false;
            BtnStartStop.Content = "▶ 启动";
            BtnStartStop.Background = new System.Windows.Media.SolidColorBrush(
                System.Windows.Media.Color.FromRgb(0, 120, 212));
            UpdateLlbotInfo();
        });
    }


    /// <summary>启动后：等 QQ 登录窗口出现并前置（只为省一次找窗口，不截图）。</summary>
    private async Task BringQqWindowAsync()
    {
        var focus = await _llbot.FocusQqWindowWhenReadyAsync();
        AppendLog("[系统] " + focus);
    }


    /// <summary>PMHQ / headless 切换（两种都是官方模式）。</summary>
    private void CmbMode_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (_llbot == null) return;
        var headless = CmbMode.SelectedIndex == 1;
        if (_llbot.HeadlessMode == headless) return;
        _llbot.HeadlessMode = headless;
        SaveSettings();
        UpdateLlbotInfo();
        AppendLog(headless
            ? "[系统] 已切到 headless 模式（LLBot 直连）：启动后二维码会直接显示在面板里"
            : "[系统] 已切到 PMHQ 模式（真实 QQ 客户端）：二维码在 QQ 登录窗口里");
    }


    /// <summary>签名接入点切换：china（国内 IPv4）/ cf（Cloudflare）。</summary>
    private void CmbCdn_SelectionChanged(object sender, SelectionChangedEventArgs e)
    {
        if (_llbot == null) return;
        var cdn = CmbCdn.SelectedIndex == 1 ? "cf" : "china";
        if (_llbot.SignCdn == cdn) return;
        _llbot.SignCdn = cdn;
        SaveSettings();
        AppendLog(cdn == "china"
            ? "[系统] 签名接入点：国内镜像（仅 IPv4）"
            : "[系统] 签名接入点：Cloudflare（含 IPv6，IPv6 不稳定时会报 Network unreachable）");
    }

    private void BtnCaptureQq_Click(object sender, RoutedEventArgs e)
    {
        var (png, message) = _llbot.CaptureQqWindow();
        AppendLog("[系统] " + message);
        if (png != null) ShowQrImage(png, "QQ 登录窗口实拍（在这里扫码）");
    }

    /// <summary>把 PNG 字节显示到二维码卡片（与二维码轮询共用）。</summary>
    private void ShowQrImage(byte[] png, string statusText)
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
            TxtQrStatus.Text = statusText;
            TxtQrStatus.Foreground = System.Windows.Media.Brushes.Green;
        }
        catch (Exception ex)
        {
            TxtQrStatus.Text = $"加载失败: {ex.Message}";
            TxtQrStatus.Foreground = System.Windows.Media.Brushes.Red;
        }
    }

    private void BtnFocusQq_Click(object sender, RoutedEventArgs e)
    {
        AppendLog("[系统] " + _llbot.FocusQqWindow());
    }

    private void BtnCleanup_Click(object sender, RoutedEventArgs e)
    {
        var found = _llbot.FindInstallProcesses();
        if (found.Count == 0)
        {
            AppendLog("[系统] 没有检测到 LLBot 残留进程");
            UpdateLlbotInfo();
            return;
        }

        var names = string.Join("、", found.Select(f => $"{f.Name}({f.Pid})"));
        var answer = MessageBox.Show(
            $"检测到 {found.Count} 个 LLBot 进程仍在运行：\n{names}\n\n要强制结束它们吗？（QQ 进程不会被结束）",
            "清理 LLBot 残留",
            MessageBoxButton.YesNo,
            MessageBoxImage.Warning);
        if (answer != MessageBoxResult.Yes)
        {
            AppendLog("[系统] 已取消清理");
            return;
        }

        var killed = _llbot.KillInstallProcesses();
        AppendLog($"[系统] 清理完成，已结束 {killed.Count} 个进程");
        UpdateLlbotInfo();
    }

    private void TxtQqPath_TextChanged(object sender, TextChangedEventArgs e)
    {
        _llbot.QqPath = TxtQqPath.Text;
        SaveSettings();
    }

    private void BtnBrowseExe_Click(object sender, RoutedEventArgs e)
    {
        var dialog = new System.Windows.Forms.OpenFileDialog
        {
            Title = "选择 LLBot 启动器（新版 LuckyLillia.exe / 旧版 llbot.exe）",
            Filter = "LLBot 启动器|*.exe|所有文件|*.*",
            CheckFileExists = true,
        };
        var folder = _llbot.LlbotFolder;
        if (folder.Length > 0 && Directory.Exists(folder))
            dialog.InitialDirectory = folder;
        if (dialog.ShowDialog() == System.Windows.Forms.DialogResult.OK)
            TxtExePath.Text = dialog.FileName;
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
            if (_llbot.ResolveLlbotExe().Length == 0)
            {
                AppendLog("[系统] 请先选择 LLBot 启动器 exe（新版 LuckyLillia.exe / 旧版 llbot.exe）");
                return;
            }

            // 端口占用检查：新旧版都占 PMHQ 13000 / WebUI 3080。
            // 已有实例在跑时再启动会冲突（新版启动器还会在控制台问「是否关闭这些进程」，
            // 而面板没给它 stdin，结果是卡住后无声退出），所以先让用户决定。
            var busy = _llbot.BusyPorts();
            if (busy.Count > 0)
            {
                AppendLog($"[系统] 检测到端口被占用：{string.Join("、", busy)}（多半已有一个 LLBot 实例在运行）");
                var answer = MessageBox.Show(
                    $"端口 {string.Join("、", busy)} 已被占用，通常说明已经有一个 LLBot 实例在运行。\n\n继续启动可能与已有实例冲突（登录互踢 / 二维码互相覆盖）。\n是否仍要继续？",
                    "LLBot 端口占用",
                    MessageBoxButton.YesNo,
                    MessageBoxImage.Warning);
                if (answer != MessageBoxResult.Yes)
                {
                    AppendLog("[系统] 已取消启动");
                    return;
                }
            }

            try
            {
                _llbot.StartLlbot();
                _isRunning = true;
                BtnStartStop.Content = "⏹ 停止";
                BtnStartStop.Background = new System.Windows.Media.SolidColorBrush(
                    System.Windows.Media.Color.FromRgb(211, 47, 47));
                AppendLog("[系统] LLBot 已启动");
                _ = BringQqWindowAsync();   // PMHQ 模式：等 QQ 登录窗口出现后前置并截图
                // 轮询间隔按模式取：headless 的码会自己过期，密一点刷新；
                // PMHQ 模式码根本拿不到，没必要频繁打接口。
                _llbot.StartQrPolling(_llbot.HeadlessMode ? 20000 : 120000);
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
                    // 新版设置直接存 exe；只有旧版设置（LlbotFolder）时，
                    // 在目录里找出启动器 exe 自动迁移过来（不要求用户重新选）。
                    var exe = settings.LlbotExe ?? "";
                    if (string.IsNullOrWhiteSpace(exe) && !string.IsNullOrWhiteSpace(settings.LlbotFolder))
                    {
                        var migrated = LlBotService.ResolveLlbotExeIn(settings.LlbotFolder!);
                        if (migrated.Length > 0) exe = migrated;
                    }
                    TxtExePath.Text = exe;
                    TxtQqPath.Text = settings.QqPath ?? "";
                    _llbot.HeadlessMode = settings.Headless == true;
                    _llbot.SignCdn = string.IsNullOrWhiteSpace(settings.Cdn) ? "cf" : settings.Cdn!;
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
                LlbotExe = TxtExePath.Text,
                Headless = _llbot.HeadlessMode,
                Cdn = _llbot.SignCdn,
                LlbotFolder = _llbot.LlbotFolder,
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
        public string? LlbotExe { get; set; }
        public bool Headless { get; set; }
        public string? Cdn { get; set; }
        public string? LlbotFolder { get; set; }
        public string? QqPath { get; set; }
        public string? WebuiUrl { get; set; }
        public string? WebuiToken { get; set; }
    }
}
