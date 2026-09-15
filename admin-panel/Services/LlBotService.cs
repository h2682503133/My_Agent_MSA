using System;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Net.Http;
using System.Net.Http.Headers;
using System.Net.NetworkInformation;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using System.Threading.Tasks;

namespace MyAgentAdminPanel.Services;

/// <summary>二维码取源结果。</summary>
public class QrCodeResult
{
    /// <summary>二维码 PNG 字节（取到才有值）。</summary>
    public byte[]? Png { get; init; }
    /// <summary>取源：online（已登录，无需扫码） / api / file / none</summary>
    public string Source { get; init; } = "none";
    /// <summary>剩余有效秒数（WebUI API 提供）。</summary>
    public int? ExpireInSeconds { get; init; }
    /// <summary>失败原因 / 附加说明。</summary>
    public string? Error { get; init; }
    /// <summary>文件来源时的路径。</summary>
    public string? Path { get; init; }
    /// <summary>是否已登录（WebUI API 可查时）。</summary>
    public bool Online { get; init; }
    public string Uin { get; init; } = "";
    public string Nick { get; init; } = "";
    public bool Ok => Png is { Length: > 0 };
}

public class LlBotService
{
    private const int PmhqPort = 13000;
    private const int WebuiPort = 3080;

    private Process? _llbotProcess;
    private FileSystemWatcher? _qrWatcher;
    private System.Timers.Timer? _qrTimer;
    // 最近一次取到的二维码及其取到时间。
    // 用途：推送时必须复用「刚取到的那一张」，绝不重新取码——
    // llbot 的 /api/login-qrcode 每次调用都会重新生成二维码，
    // 二次取码会让上一张立刻作废，于是「推送到 Dashboard 的码」与
    // 「llbot 当前有效码」不一致，用户扫码必然提示过期。
    private QrCodeResult? _lastQr;
    private DateTime _lastQrAt = DateTime.MinValue;

    // llbot 日志里最新的「二维码网址」——这张才是 llbot 当前真正在等用户扫的码。
    // 与 /api/login-qrcode 的区别至关重要：调那个接口会让 llbot 重新生成一张二维码、
    // 旧码立即作废，所以「主动取码」永远在追一张刚被自己作废的码。
    // 读日志则零副作用，所以二维码的主路径是它。
    private static readonly Regex QrUrlRegex = new(@"二维码网址[:：]\s*(\S+)", RegexOptions.Compiled);
    private string _lastQrUrl = "";
    private DateTime _lastQrUrlAt = DateTime.MinValue;
    /// <summary>llbot 日志里出现新的「二维码网址」时触发（携带该网址）。</summary>
    public event Action<string>? QrUrlChanged;
    /// <summary>通过 API/文件兜底取到二维码图片时触发（仅用于界面显示，不触发推送）。</summary>
    public event Action<QrCodeResult>? QrImageFetched;
    /// <summary>最近的二维码网址与其捕获时间。</summary>
    public string LastQrUrl => _lastQrUrl;
    public DateTime LastQrUrlAt => _lastQrUrlAt;
    /// <summary>日志网址是否足够新鲜（默认 300 秒内），过期视为"当前没有在等的码"。</summary>
    public bool HasFreshQrUrl => !string.IsNullOrEmpty(_lastQrUrl)
                                 && (DateTime.UtcNow - _lastQrUrlAt).TotalSeconds < 300;

    private string _llbotFolder = "";
    private string _qqPath = "";
    private int _qqPid;
    private bool _isRunning;
    private bool _isRestarting;

    public event Action<string>? LogReceived;
    /// <summary>每次轮询的状态（用于刷新来源/倒计时），无论图是否变化。</summary>
    public event Action<QrCodeResult>? QrCodeStatus;
    public bool IsRunning => _isRunning;

    /// <summary>WebUI 地址（WebUI API 取码用）。</summary>
    public string WebuiBaseUrl { get; set; } = "http://127.0.0.1:3080";
    /// <summary>WebUI 的 x-webui-token（浏览器 cookie webui_token 的值）。</summary>
    public string WebuiToken { get; set; } = "";
    /// <summary>是否优先走 WebUI API（失败自动回落二维码文件）。</summary>
    public bool PreferWebuiApi { get; set; } = true;
    /// <summary>是否允许回落到二维码文件。默认 false：只走 WebUI API，完全不读 qrcode.png。</summary>
    public bool UseQrCodeFile
    {
        get => _useQrCodeFile;
        set
        {
            if (_useQrCodeFile == value) return;
            _useQrCodeFile = value;
            SetupQrWatcher();   // 开关变化时重建/停掉文件监视
        }
    }
    private bool _useQrCodeFile;

    public string LlbotFolder
    {
        get => _llbotFolder;
        set
        {
            _llbotFolder = value;
            SetupQrWatcher();
        }
    }

    public string QqPath
    {
        get => _qqPath;
        set => _qqPath = value;
    }

    public string QrCodePath => Path.Combine(_llbotFolder, "qrcode.png");
    public string QrCodeUrl => "https://api.2dcode.biz/v1/create-qr-code?data=https://txz.qq.com/p?k=QsqDyabub8d9o7dDpZWYtNgkibYvHD1m&f=1600001604";

    // ── 二维码取源：WebUI API 优先，失败回落文件 ──────────────

    /// <summary>
    /// 归一化 WebUI 凭据：llbot WebUI 的 x-webui-token 是【SHA-256(密码)】而非明文密码
    /// （前端 Gt() = SHA-256）。所以这里允许直接填密码：非 64 位十六进制即视为密码并求哈希。
    /// </summary>
    public static string NormalizeWebuiToken(string? token)
    {
        var t = (token ?? "").Trim();
        if (t.Length == 0) return "";
        bool isHex64 = t.Length == 64 && t.All(Uri.IsHexDigit);
        if (isHex64) return t.ToLowerInvariant();
        using var sha = SHA256.Create();
        var hash = sha.ComputeHash(Encoding.UTF8.GetBytes(t));
        return Convert.ToHexString(hash).ToLowerInvariant();
    }

    /// <summary>
    /// 查 WebUI 登录状态。返回 (online, uin, nick, error)。
    /// 必须先查这个：llbot 的 /api/login-qrcode 在【已登录】时会一直阻塞不返回。
    /// </summary>
    public async Task<(bool? online, string uin, string nick, string? error)> FetchLoginStateAsync()
    {
        var baseUrl = (WebuiBaseUrl ?? "").Trim().TrimEnd('/');
        if (baseUrl.Length == 0)
            return (null, "", "", "未配置 WebUI 地址");
        try
        {
            using var client = new HttpClient { Timeout = TimeSpan.FromSeconds(5) };
            using var req = new HttpRequestMessage(HttpMethod.Get, $"{baseUrl}/api/login-info");
            var token = NormalizeWebuiToken(WebuiToken);
            if (token.Length > 0)
                req.Headers.TryAddWithoutValidation("x-webui-token", token);

            using var resp = await client.SendAsync(req);
            var body = await resp.Content.ReadAsStringAsync();
            if (!resp.IsSuccessStatusCode)
            {
                string msg = body;
                try
                {
                    using var ed = JsonDocument.Parse(body);
                    if (ed.RootElement.TryGetProperty("message", out var m)) msg = m.GetString() ?? body;
                }
                catch { }
                return (null, "", "", $"HTTP {(int)resp.StatusCode}：{msg}");
            }
            using var doc = JsonDocument.Parse(body);
            var root = doc.RootElement;
            if (!root.TryGetProperty("data", out var data))
                return (null, "", "", "返回中没有 data");
            bool online = data.TryGetProperty("online", out var on) && on.ValueKind == JsonValueKind.True;
            string uin = data.TryGetProperty("uin", out var u) ? (u.GetString() ?? "") : "";
            string nick = data.TryGetProperty("nick", out var n) ? (n.GetString() ?? "") : "";
            return (online, uin, nick, null);
        }
        catch (Exception ex)
        {
            return (null, "", "", $"连接 WebUI 失败：{ex.Message}");
        }
    }

    /// <summary>调 llbot WebUI 的 /api/login-qrcode 取二维码（自带过期时间）。</summary>
    public async Task<QrCodeResult> FetchQrCodeFromWebuiAsync()
    {
        var baseUrl = (WebuiBaseUrl ?? "").Trim().TrimEnd('/');
        if (baseUrl.Length == 0)
            return new QrCodeResult { Source = "api", Error = "未配置 WebUI 地址" };

        try
        {
            using var client = new HttpClient { Timeout = TimeSpan.FromSeconds(8) };
            using var req = new HttpRequestMessage(HttpMethod.Get, $"{baseUrl}/api/login-qrcode");
            var token = NormalizeWebuiToken(WebuiToken);
            if (token.Length > 0)
                req.Headers.TryAddWithoutValidation("x-webui-token", token);

            using var resp = await client.SendAsync(req);
            var body = await resp.Content.ReadAsStringAsync();
            if (!resp.IsSuccessStatusCode)
            {
                string msg = body;
                try
                {
                    using var ed = JsonDocument.Parse(body);
                    if (ed.RootElement.TryGetProperty("message", out var m)) msg = m.GetString() ?? body;
                }
                catch { }
                return new QrCodeResult { Source = "api", Error = $"HTTP {(int)resp.StatusCode}：{msg}" };
            }

            using var doc = JsonDocument.Parse(body);
            var root = doc.RootElement;
            if (root.TryGetProperty("success", out var okEl) && !okEl.GetBoolean())
            {
                var msg = root.TryGetProperty("message", out var m) ? m.GetString() : "接口返回失败";
                return new QrCodeResult { Source = "api", Error = msg };
            }
            if (!root.TryGetProperty("data", out var data))
                return new QrCodeResult { Source = "api", Error = "返回中没有 data" };

            var b64 = data.TryGetProperty("pngBase64QrcodeData", out var b) ? b.GetString() : null;
            if (string.IsNullOrWhiteSpace(b64))
                return new QrCodeResult { Source = "api", Error = "返回中没有二维码数据" };

            var comma = b64!.IndexOf(',');
            if (comma >= 0) b64 = b64[(comma + 1)..];
            var png = Convert.FromBase64String(b64);

            int? expireIn = null;
            if (data.TryGetProperty("expireTime", out var exp) && exp.ValueKind == JsonValueKind.Number)
            {
                var raw = exp.GetDouble();
                // 兼容两种：剩余秒数 或 到期时间戳(ms)
                expireIn = raw > 1e10 ? (int)Math.Max(0, (raw / 1000.0) - DateTimeOffset.UtcNow.ToUnixTimeSeconds())
                                      : (int)Math.Max(0, raw);
            }
            return new QrCodeResult { Png = png, Source = "api", ExpireInSeconds = expireIn };
        }
        catch (Exception ex)
        {
            return new QrCodeResult { Source = "api", Error = $"连接 WebUI 失败：{ex.Message}" };
        }
    }

    /// <summary>读二维码文件（兜底）。</summary>
    public QrCodeResult FetchQrCodeFromFile()
    {
        var p = QrCodePath;
        try
        {
            if (!File.Exists(p))
                return new QrCodeResult { Source = "file", Error = "未找到 qrcode.png", Path = p };
            using var fs = new FileStream(p, FileMode.Open, FileAccess.Read, FileShare.ReadWrite);
            using var ms = new MemoryStream();
            fs.CopyTo(ms);
            return new QrCodeResult { Png = ms.ToArray(), Source = "file", Path = p };
        }
        catch (Exception ex)
        {
            return new QrCodeResult { Source = "file", Error = $"读取失败：{ex.Message}", Path = p };
        }
    }

    /// <summary>取二维码：先查登录态（已登录则不需要码），再 API 优先、文件兜底。</summary>
    public async Task<QrCodeResult> FetchQrCodeAsync()
    {
        if (PreferWebuiApi)
        {
            var (online, uin, nick, stErr) = await FetchLoginStateAsync();
            if (online == true)
                return new QrCodeResult { Source = "online", Online = true, Uin = uin, Nick = nick };

            if (!UseQrCodeFile)
                return await FetchQrCodeFromWebuiAsync();   // 完全不读图片

            var api = await FetchQrCodeFromWebuiAsync();
            if (api.Ok) return api;
            if (!UseQrCodeFile)
                return api;   // 默认不读图片：取不到就如实报错，避免两个实例共用 qrcode.png 互相覆盖
            var file = FetchQrCodeFromFile();
            if (file.Ok)
                return new QrCodeResult
                {
                    Png = file.Png, Source = "file", Path = file.Path,
                    Error = string.IsNullOrEmpty(api.Error) ? null : $"WebUI API 不可用：{api.Error}"
                };
            return api;   // 两边都失败，报 API 的原因（若 API 未连上也附上状态查询的错误）
        }
        return FetchQrCodeFromFile();
    }

    /// <summary>开始轮询二维码（默认 2 分钟一次；过于频繁会不断生成新码，导致扫码时刚好被刷掉）。</summary>
    public void StartQrPolling(int intervalMs = 120000)
    {
        StopQrPolling();
        _qrTimer = new System.Timers.Timer(intervalMs) { AutoReset = true };
        _qrTimer.Elapsed += async (_, _) => await PollQrOnceAsync();
        _qrTimer.Start();
        _ = PollQrOnceAsync();
    }

    public void StopQrPolling()
    {
        try { _qrTimer?.Stop(); _qrTimer?.Dispose(); } catch { }
        _qrTimer = null;
    }

    /// <summary>
    /// 轮询：**只刷新登录状态，绝不主动取码**。
    ///
    /// 原实现每轮都调 /api/login-qrcode 取码，而 llbot 每次调用该接口都会重新生成
    /// 二维码、并让上一张立即作废 —— 于是代码每 2 分钟就把用户正在看的码换掉，
    /// 扫码时刚好被刷掉，表现为"稳定推过期码"。
    /// 现在二维码一律来自 llbot 日志（QrUrlChanged），轮询只负责刷新登录态。
    /// </summary>
    private async Task PollQrOnceAsync()
    {
        try
        {
            var (online, uin, nick, stErr) = await FetchLoginStateAsync();
            QrCodeResult r;
            if (online == true)
            {
                r = new QrCodeResult { Source = "online", Online = true, Uin = uin, Nick = nick };
            }
            else if (HasFreshQrUrl)
            {
                // 有 llbot 日志里的码：状态取源标为 log（前端据此显示"日志二维码"）
                r = new QrCodeResult
                {
                    Source = "log",
                    Online = false,
                    Error = online == null ? stErr : null,
                };
            }
            else
            {
                r = new QrCodeResult
                {
                    Source = "none",
                    Online = false,
                    Error = stErr ?? "尚未在 llbot 日志里看到二维码网址（等待实例产出二维码）",
                };
            }
            QrCodeStatus?.Invoke(r);
        }
        catch (Exception ex)
        {
            LogReceived?.Invoke($"[ERR] 二维码轮询异常: {ex.Message}");
        }
    }

    private void SetupQrWatcher()
    {
        _qrWatcher?.Dispose();
        _qrWatcher = null;

        // 默认不读二维码文件（只走 WebUI API）时，文件监视毫无意义，而且会形成自激循环：
        //   取码 → llbot 生成新码并写 qrcode.png → watcher 触发 → 再取码 → …
        // 结果是二维码被毫秒级疯狂重生成，轮询间隔改成多少都没用。
        if (!UseQrCodeFile)
            return;
        if (string.IsNullOrEmpty(_llbotFolder) || !Directory.Exists(_llbotFolder))
            return;

        _qrWatcher = new FileSystemWatcher(_llbotFolder, "qrcode.png")
        {
            NotifyFilter = NotifyFilters.FileName | NotifyFilters.LastWrite | NotifyFilters.CreationTime,
            EnableRaisingEvents = true
        };

        _qrWatcher.Created += (s, e) => _ = PollQrOnceAsync();
        _qrWatcher.Changed += (s, e) => _ = PollQrOnceAsync();
    }

    public Process StartLlbot()
    {
        if (_isRunning) return _llbotProcess!;

        var exePath = Path.Combine(_llbotFolder, "llbot.exe");
        if (!File.Exists(exePath))
            throw new FileNotFoundException($"找不到 {exePath}");

        var utf8 = new UTF8Encoding(false);

        var args = "";
        if (!string.IsNullOrEmpty(_qqPath) && File.Exists(_qqPath))
            args = $"--qq-path \"{_qqPath}\"";

        var psi = new ProcessStartInfo
        {
            FileName = exePath,
            Arguments = args,
            WorkingDirectory = _llbotFolder,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            StandardOutputEncoding = utf8,
            StandardErrorEncoding = utf8,
            UseShellExecute = false,
            CreateNoWindow = true
        };

        _llbotProcess = new Process { StartInfo = psi };
        _llbotProcess.OutputDataReceived += (s, e) =>
        {
            if (!string.IsNullOrEmpty(e.Data))
            {
                OnLlbotLine(e.Data);
            }
        };
        _llbotProcess.ErrorDataReceived += (s, e) =>
        {
            if (!string.IsNullOrEmpty(e.Data))
            {
                OnLlbotLine($"[ERR] {e.Data}");
            }
        };

        _llbotProcess.Exited += OnLlbotExited;

        _llbotProcess.Start();
        _llbotProcess.BeginOutputReadLine();
        _llbotProcess.BeginErrorReadLine();
        _isRunning = true;
        _isRestarting = false;
        return _llbotProcess;
    }

    private void OnLlbotLine(string line)
    {
        LogReceived?.Invoke(line);
        TryTrackQqPid(line);
        CheckAutoRestart(line);
        TryCaptureQrUrl(line);
    }

    /// <summary>
    /// 从 llbot 日志抓「二维码网址」——零副作用的取码方式，且这张就是 llbot 当前等待扫的码。
    /// 注意：绝不能改用 /api/login-qrcode 去"取最新码"，那会让 llbot 重新生成一张、把这张作废。
    /// </summary>
    private void TryCaptureQrUrl(string line)
    {
        var m = QrUrlRegex.Match(line);
        if (!m.Success) return;

        var url = m.Groups[1].Value.Trim();
        if (string.IsNullOrEmpty(url) || url == _lastQrUrl) return;

        _lastQrUrl = url;
        _lastQrUrlAt = DateTime.UtcNow;
        QrUrlChanged?.Invoke(url);
    }

    private void OnLlbotExited(object? sender, EventArgs e)
    {
        // 只处理当前进程的退出，避免旧进程异步退出事件覆盖新进程状态
        if (!ReferenceEquals(sender, _llbotProcess)) return;

        _isRunning = false;
        LogReceived?.Invoke("[系统] llbot.exe 已退出");
        if (!_isRestarting)
        {
            LogReceived?.Invoke("[系统] 如需重启请手动点击启动");
        }
    }

    private void TryTrackQqPid(string line)
    {
        const string marker = "QQ 进程 PID:";
        var idx = line.IndexOf(marker, StringComparison.Ordinal);
        if (idx < 0) return;

        var rest = line.Substring(idx + marker.Length).Trim();
        var digits = new string(rest.TakeWhile(char.IsDigit).ToArray());
        if (int.TryParse(digits, out var pid) && pid > 0)
            _qqPid = pid;
    }

    private async void CheckAutoRestart(string line)
    {
        if (_isRestarting) return;
        if (!line.Contains("正在终止 QQ 进程")) return;

        _isRestarting = true;
        LogReceived?.Invoke("[系统] 检测到「正在终止 QQ 进程」，3 秒后自动重启 LLBot...");
        await Task.Delay(3000);

        // 停止当前进程（连同 llbot 启动的 QQ）
        StopLlbot();

        // 等待旧实例占用的端口释放，避免新实例 PMHQ 连接失败
        if (!await WaitPortsFreeAsync(TimeSpan.FromSeconds(20)))
            LogReceived?.Invoke("[系统] 端口未在限时内释放，仍尝试重启");

        try
        {
            StartLlbot();
        }
        catch (Exception ex)
        {
            LogReceived?.Invoke($"[ERR] 自动重启失败: {ex.Message}");
        }
        _isRestarting = false;
    }

    public void StopLlbot()
    {
        var proc = _llbotProcess;
        _llbotProcess = null;
        _isRunning = false;

        try
        {
            if (proc != null && !proc.HasExited)
            {
                proc.Kill(entireProcessTree: true);
                proc.WaitForExit(3000);
            }
        }
        catch { }

        // llbot 已退出时 QQ 可能变成孤儿进程，按 PID 精确清理，避免误杀其他 QQ
        if (_qqPid > 0)
        {
            try
            {
                var qq = Process.GetProcessById(_qqPid);
                if (!qq.HasExited && qq.ProcessName.Contains("QQ", StringComparison.OrdinalIgnoreCase))
                {
                    qq.Kill(entireProcessTree: true);
                    qq.WaitForExit(3000);
                }
            }
            catch { }
            _qqPid = 0;
        }
    }

    private static bool IsPortInUse(int port)
    {
        try
        {
            var props = IPGlobalProperties.GetIPGlobalProperties();
            foreach (var listener in props.GetActiveTcpListeners())
                if (listener.Port == port) return true;
            foreach (var conn in props.GetActiveTcpConnections())
                if (conn.LocalEndPoint.Port == port) return true;
        }
        catch
        {
            return true;
        }
        return false;
    }

    private static async Task<bool> WaitPortsFreeAsync(TimeSpan timeout)
    {
        var deadline = DateTime.UtcNow + timeout;
        while (DateTime.UtcNow < deadline)
        {
            if (!IsPortInUse(PmhqPort) && !IsPortInUse(WebuiPort))
                return true;
            await Task.Delay(500);
        }
        return !IsPortInUse(PmhqPort) && !IsPortInUse(WebuiPort);
    }

    public void Dispose()
    {
        StopQrPolling();
        StopLlbot();
        _qrWatcher?.Dispose();
    }

    /// <summary>
    /// 把二维码推送到 Dashboard。
    ///
    /// 关键：**不要在这里重新取码**。llbot 的 /api/login-qrcode 每调用一次就会
    /// 重新生成二维码，所以「取码 → 触发推送 → 推送时又取码」会让刚取到的那张
    /// 立刻作废，最终推给 Dashboard 的码与 llbot 当前有效码不一致，
    /// 用户扫码必然提示过期（表现为"稳定推送过期二维码"）。
    ///
    /// 因此：调用方若已有刚取到的码（QrCodeChanged 事件里就带着），直接传入复用；
    /// 未传时用最近一次轮询缓存的码；只有缓存缺失或已接近过期才去取一次新码。
    /// </summary>
    public async Task PushQrCodeToDashboard(string dashboardUrl = "http://localhost:5700", QrCodeResult? qr = null)
    {
        qr ??= _lastQr;

        // 缓存判定：没有码、取码失败、或距取到已超过有效期（留 10 秒余量）才重新取。
        var ageSeconds = (DateTime.UtcNow - _lastQrAt).TotalSeconds;
        var remaining = qr?.ExpireInSeconds is int left ? left - ageSeconds : 0;
        var needFresh = qr == null || !qr.Ok || remaining <= 10;

        if (needFresh)
        {
            var fetched = await FetchQrCodeAsync();
            if (fetched.Source == "online")
            {
                LogReceived?.Invoke($"[系统] 已登录（{fetched.Nick} / {fetched.Uin}），无需推送二维码");
                return;
            }
            if (!fetched.Ok)
            {
                LogReceived?.Invoke($"[系统] 推送失败：{fetched.Error ?? "未取到二维码"}");
                return;
            }
            qr = fetched;
            _lastQr = fetched;
            _lastQrAt = DateTime.UtcNow;
        }

        if (qr?.Png is not { Length: > 0 })
        {
            LogReceived?.Invoke("[系统] 推送失败：未取到二维码");
            return;
        }

        var srcLabel = qr.Source == "api" ? "WebUI API" : "二维码文件";
        // 通知界面显示这张兜底取到的码（不触发推送，避免递归）
        QrImageFetched?.Invoke(qr);
        await PostQrPngToDashboard(dashboardUrl, qr.Png, srcLabel);
    }

    /// <summary>
    /// 推送"当前最新"的二维码到 Dashboard：
    /// 优先用 llbot 日志里的「二维码网址」（零副作用，且就是 llbot 在等的那张）；
    /// 只有日志里还没出现网址时，才回退到 WebUI API 取码（此时必然生成一张新码，属兜底行为）。
    /// </summary>
    public async Task<bool> PushLatestToDashboard(string dashboardUrl = "http://localhost:5700")
    {
        if (HasFreshQrUrl)
            return await PushQrUrlToDashboard(dashboardUrl, _lastQrUrl);

        LogReceived?.Invoke("[系统] llbot 日志里还没有二维码网址，回退到 WebUI API 取码（会生成新码）");
        await PushQrCodeToDashboard(dashboardUrl);
        return true;
    }

    /// <summary>
    /// 把 llbot 日志里的「二维码网址」渲染成图片并推送到 Dashboard（主路径，零副作用）。
    /// 该网址就是 llbot 当前正在等待用户扫码的那张码，因此推送内容与 llbot 状态始终一致。
    /// </summary>
    public async Task<bool> PushQrUrlToDashboard(string dashboardUrl, string qrUrl)
    {
        if (string.IsNullOrWhiteSpace(qrUrl))
        {
            LogReceived?.Invoke("[系统] 推送失败：尚未从 llbot 日志中读到「二维码网址」");
            return false;
        }

        byte[] png;
        try
        {
            using var client = new HttpClient { Timeout = TimeSpan.FromSeconds(10) };
            png = await client.GetByteArrayAsync(qrUrl);
        }
        catch (Exception ex)
        {
            LogReceived?.Invoke($"[系统] 拉取二维码图片失败：{ex.Message}");
            return false;
        }

        return await PostQrPngToDashboard(dashboardUrl, png, "llbot 日志网址");
    }

    /// <summary>把二维码 PNG POST 到 Dashboard（统一入口，便于日志统一）。</summary>
    private async Task<bool> PostQrPngToDashboard(string dashboardUrl, byte[] png, string srcLabel)
    {
        try
        {
            using var client = new HttpClient { Timeout = TimeSpan.FromSeconds(10) };
            using var formData = new MultipartFormDataContent();
            var fileContent = new ByteArrayContent(png);
            fileContent.Headers.ContentType = new MediaTypeHeaderValue("image/png");
            formData.Add(fileContent, "file", "qrcode.png");

            var resp = await client.PostAsync($"{dashboardUrl}/api/qrcode", formData);
            var body = await resp.Content.ReadAsStringAsync();
            LogReceived?.Invoke($"[系统] 二维码推送结果（来源 {srcLabel}）: {resp.StatusCode} {body}");
            return resp.IsSuccessStatusCode;
        }
        catch (Exception ex)
        {
            LogReceived?.Invoke($"[系统] 推送二维码到 Dashboard 失败: {ex.Message}");
            return false;
        }
    }
}
