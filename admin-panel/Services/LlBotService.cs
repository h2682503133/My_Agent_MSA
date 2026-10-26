using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Net.Http;
using System.Net.Http.Headers;
using System.Net.NetworkInformation;
using System.Net.Sockets;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using System.Threading;
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
    private System.Timers.Timer? _qrTimer;

    // ── 新版（bin\llbot 打包）日志跟随 ──────────────────────────
    // 新版的运行时（node llbot.js）把日志写到 `bin\llbot\data\logs\llbot-*.log`，
    // 启动器自己只打印少量信息。为了在新版下仍能拿到「二维码网址」这类运行时日志，
    // 这里额外跟随最新的那个日志文件（与 stdout 双写，靠去重避免刷两遍）。
    private FileSystemWatcher? _logWatcher;
    private System.Timers.Timer? _logTimer;
    private string _logFile = "";
    private long _logOffset;
    private string _logPending = "";
    private readonly Dictionary<string, DateTime> _recentLines = new(StringComparer.Ordinal);
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

    private string _llbotExe = "";
    private string _qqPath = "";
    private int _qqPid;
    private bool _isRunning;
    private bool _isRestarting;

    public event Action<string>? LogReceived;
    /// <summary>LLBot 进程自己退出了（崩溃 / 被外部结束 / 用户关掉了 QQ 窗口）。</summary>
    public event Action? LlbotExited;
    /// <summary>每次轮询的状态（用于刷新来源/倒计时），无论图是否变化。</summary>
    public event Action<QrCodeResult>? QrCodeStatus;
    public bool IsRunning => _isRunning;

    /// <summary>WebUI 地址（WebUI API 取码用）。</summary>
    public string WebuiBaseUrl { get; set; } = "http://127.0.0.1:3080";
    /// <summary>WebUI 的 x-webui-token（浏览器 cookie webui_token 的值）。</summary>
    public string WebuiToken { get; set; } = "";
    /// <summary>是否优先走 WebUI API（失败自动回落二维码文件）。</summary>
    public bool PreferWebuiApi { get; set; } = true;
    /// <summary>是否允许回落到二维码文件。默认 false：只走 WebUI API，完全不读二维码文件。</summary>
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

    /// <summary>
    /// LLBot 安装布局。判定**只看文件，不看 exe 名字**：
    /// - 新版（LuckyLillia 8.x 桌面端叫 LuckyLillia.exe，CLI 版仍叫 llbot.exe）：
    ///   exe 在安装根目录，运行时在 `bin\llbot\`（llbot.js / node.exe），
    ///   数据在 `bin\llbot\data\`（内含 temp\login-qrcode.png、logs\llbot-*.log）；
    /// - 旧版：`llbot.exe` 与 `qrcode.png`、`data\` 同级。
    /// </summary>
    public enum LlbotLayout
    {
        Unknown,
        Legacy,
        Bundled,
    }

    /// <summary>
    /// LLBot 启动器 exe 的完整路径（用户直接选文件，不再选文件夹）。
    /// 旧版是 `llbot.exe`；新版桌面端是 `LuckyLillia.exe`、CLI 版仍是 `llbot.exe`。
    /// </summary>
    public string LlbotExe
    {
        get => _llbotExe;
        set
        {
            var v = (value ?? "").Trim().Trim('"');
            if (v == _llbotExe) return;
            _llbotExe = v;
            SetupQrWatcher();
            if (Layout == LlbotLayout.Bundled)
                StartLogTail();
            else
                StopLogTail();
        }
    }

    /// <summary>安装根目录（由 exe 路径推导）。</summary>
    public string LlbotFolder =>
        string.IsNullOrWhiteSpace(_llbotExe) ? "" : (Path.GetDirectoryName(_llbotExe) ?? "");

    /// <summary>当前布局判定结果。</summary>
    public LlbotLayout Layout
    {
        get
        {
            var dir = LlbotFolder;
            if (dir.Length == 0 || !Directory.Exists(dir)) return LlbotLayout.Unknown;
            if (File.Exists(Path.Combine(dir, "bin", "llbot", "llbot.js"))) return LlbotLayout.Bundled;
            return LlbotLayout.Legacy;
        }
    }

    public string LayoutText => Layout switch
    {
        LlbotLayout.Bundled => "新版（bin\\llbot 打包运行时）",
        LlbotLayout.Legacy => "旧版（exe 与 qrcode.png 同级）",
        _ => "未识别（请选择安装目录下的启动器 exe）",
    };

    /// <summary>运行时数据目录：新版 `bin\llbot\data`，旧版安装根目录。</summary>
    public string DataDir => Layout == LlbotLayout.Bundled
        ? Path.Combine(LlbotFolder, "bin", "llbot", "data")
        : LlbotFolder;

    /// <summary>运行时日志目录：只有新版会写日志文件（`data\logs\llbot-*.log`）。</summary>
    public string LogDir => Layout == LlbotLayout.Bundled ? Path.Combine(DataDir, "logs") : "";

    /// <summary>二维码文件候选路径（新版 `data\temp\login-qrcode.png`，旧版 `qrcode.png`）。</summary>
    public IReadOnlyList<string> QrCodeCandidates
    {
        get
        {
            var dir = LlbotFolder;
            if (dir.Length == 0) return Array.Empty<string>();
            var list = new List<string>();
            if (Layout == LlbotLayout.Bundled)
            {
                list.Add(Path.Combine(DataDir, "temp", "login-qrcode.png"));
                list.Add(Path.Combine(DataDir, "login-qrcode.png"));
            }
            list.Add(Path.Combine(dir, "qrcode.png"));
            list.Add(Path.Combine(DataDir, "qrcode.png"));
            return list.Distinct(StringComparer.OrdinalIgnoreCase).ToList();
        }
    }

    /// <summary>实际要读的二维码文件：优先取存在的那个，都不存在时给第一个候选（用于报错信息）。</summary>
    public string QrCodePath
    {
        get
        {
            var candidates = QrCodeCandidates;
            return candidates.FirstOrDefault(File.Exists) ?? candidates.FirstOrDefault() ?? "";
        }
    }

    public string QrCodeUrl => "https://api.2dcode.biz/v1/create-qr-code?data=https://txz.qq.com/p?k=QsqDyabub8d9o7dDpZWYtNgkibYvHD1m&f=1600001604";

    /// <summary>最近一个二维码文件的最后写入时间（用于状态展示；没有文件返回 null）。</summary>
    public DateTime? QrCodeFileUpdatedAt
    {
        get
        {
            try
            {
                var candidates = QrCodeCandidates.Where(File.Exists).ToList();
                if (candidates.Count == 0) return null;
                return candidates.Max(p => File.GetLastWriteTimeUtc(p));
            }
            catch { return null; }
        }
    }

    /// <summary>运行时版本号（新版读 `bin\llbot\package.json`；旧版读不到返回空）。</summary>
    public string VersionText
    {
        get
        {
            try
            {
                var pkg = Path.Combine(LlbotFolder, "bin", "llbot", "package.json");
                if (!File.Exists(pkg)) return "";
                using var doc = JsonDocument.Parse(File.ReadAllText(pkg));
                return doc.RootElement.TryGetProperty("version", out var v) ? (v.GetString() ?? "") : "";
            }
            catch { return ""; }
        }
    }

    /// <summary>常见启动器文件名（新旧版本都在这里）。</summary>
    private static readonly string[] KnownExeNames =
    {
        "llbot.exe", "LLBot.exe", "LuckyLillia.exe", "LuckyLilliaBot.exe",
    };

    /// <summary>
    /// 解析要启动的 exe：先用户选的那个；选错/失效时按常见名字在同目录兜底。
    /// 返回空串表示找不到（调用方负责报错提示）。
    /// </summary>
    public string ResolveLlbotExe()
    {
        if (!string.IsNullOrWhiteSpace(_llbotExe)
            && _llbotExe.EndsWith(".exe", StringComparison.OrdinalIgnoreCase)
            && File.Exists(_llbotExe))
            return _llbotExe;

        var dir = LlbotFolder;
        if (dir.Length == 0 || !Directory.Exists(dir)) return "";

        foreach (var name in KnownExeNames)
        {
            var p = Path.Combine(dir, name);
            if (File.Exists(p)) return p;
        }
        // 兜底：目录里任何名字像 llbot / lillia 的 exe（未来改名也能用）
        try
        {
            return Directory.GetFiles(dir, "*.exe", SearchOption.TopDirectoryOnly)
                .FirstOrDefault(p =>
                {
                    var n = Path.GetFileName(p);
                    return n.Contains("llbot", StringComparison.OrdinalIgnoreCase)
                        || n.Contains("lillia", StringComparison.OrdinalIgnoreCase);
                }) ?? "";
        }
        catch { return ""; }
    }

    /// <summary>
    /// 兼容旧设置：只存了文件夹（老版本 settings.json 的 LlbotFolder）时，
    /// 在目录里找出启动器 exe 再赋给 LlbotExe。返回是否找到。
    /// </summary>
    public bool AdoptFolder(string folder)
    {
        var dir = (folder ?? "").Trim().Trim('"');
        if (dir.Length == 0 || !Directory.Exists(dir)) return false;
        var exe = ResolveLlbotExeIn(dir);
        if (exe.Length == 0) return false;
        LlbotExe = exe;
        return true;
    }

    /// <summary>在指定目录里找启动器 exe（不依赖当前 LlbotExe）。</summary>
    public static string ResolveLlbotExeIn(string dir)
    {
        if (string.IsNullOrWhiteSpace(dir) || !Directory.Exists(dir)) return "";
        foreach (var name in KnownExeNames)
        {
            var p = Path.Combine(dir, name);
            if (File.Exists(p)) return p;
        }
        try
        {
            return Directory.GetFiles(dir, "*.exe", SearchOption.TopDirectoryOnly)
                .FirstOrDefault(p =>
                {
                    var n = Path.GetFileName(p);
                    return n.Contains("llbot", StringComparison.OrdinalIgnoreCase)
                        || n.Contains("lillia", StringComparison.OrdinalIgnoreCase);
                }) ?? "";
        }
        catch { return ""; }
    }

    public string QqPath
    {
        get => _qqPath;
        set => _qqPath = value;
    }

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

            // 直链模式（headless / 直连）：二维码由 LLBot 自己产出并**带缓存**，
            // 拉取不会像 PMHQ 那样"每调一次就换一张"。所以定时拉一次刷新界面，
            // 避免面板上一直贴着已经过期的码（扫码提示过期多半就是这个）。
            // PMHQ 模式下不拉：那个接口在 PMHQ 下必然失败，拉了也没图。
            if (online != true && HeadlessMode)
            {
                var latest = await FetchQrCodeAsync();
                if (latest.Ok)
                {
                    QrImageFetched?.Invoke(latest);   // 只刷新界面，不自动推送
                    QrCodeStatus?.Invoke(latest);
                }
            }
        }
        catch (Exception ex)
        {
            LogReceived?.Invoke($"[ERR] 二维码轮询异常: {ex.Message}");
        }
    }

    private readonly List<FileSystemWatcher> _qrWatchers = new();

    private void SetupQrWatcher()
    {
        foreach (var w in _qrWatchers)
        {
            try { w.Dispose(); } catch { }
        }
        _qrWatchers.Clear();

        // 默认不读二维码文件（只走 WebUI API）时，文件监视毫无意义，而且会形成自激循环：
        //   取码 → llbot 生成新码并写二维码文件 → watcher 触发 → 再取码 → …
        // 结果是二维码被毫秒级疯狂重生成，轮询间隔改成多少都没用。
        if (!UseQrCodeFile)
            return;

        // 新旧版本的二维码文件位置/文件名都不同，候选目录逐个监视：
        //   新版 data\temp\login-qrcode.png、旧版 <根目录>\qrcode.png
        foreach (var directory in QrCodeCandidates.Select(p => Path.GetDirectoryName(p) ?? "")
                                                  .Where(d => d.Length > 0 && Directory.Exists(d))
                                                  .Distinct(StringComparer.OrdinalIgnoreCase))
        {
            try
            {
                var watcher = new FileSystemWatcher(directory, "*.png")
                {
                    NotifyFilter = NotifyFilters.FileName | NotifyFilters.LastWrite | NotifyFilters.CreationTime,
                    EnableRaisingEvents = true,
                };
                watcher.Created += (s, e) => _ = OnQrFileTouched(e.FullPath);
                watcher.Changed += (s, e) => _ = OnQrFileTouched(e.FullPath);
                _qrWatchers.Add(watcher);
            }
            catch { }
        }
    }

    /// <summary>只有候选路径上的 png 变化才触发刷新（data\ 下别的图片不算）。</summary>
    private Task OnQrFileTouched(string path)
    {
        if (!QrCodeCandidates.Any(c => string.Equals(c, path, StringComparison.OrdinalIgnoreCase)))
            return Task.CompletedTask;
        return PollQrOnceAsync();
    }

    /// <summary>当前设置下要传给启动器的参数（拼装逻辑独立出来，便于自检）。</summary>
    public string BuildArguments()
    {
        var args = "";
        if (HeadlessMode)
        {
            // headless 模式：其它参数会被忽略，只传模式本身
            args = "--headless";
        }
        else if (!string.IsNullOrEmpty(_qqPath) && File.Exists(_qqPath))
        {
            args = $"--qq-path \"{_qqPath}\"";
        }

        // 签名接入点：命令行优先级最高，环境变量兜底（LLBot 与 PMHQ 分别认 CDN / PMHQ_CDN）
        var cdn = NormalizedCdn();
        if (cdn.Length > 0)
        {
            args = args.Length > 0 ? $"{args} --cdn={cdn}" : $"--cdn={cdn}";
        }
        return args;
    }

    /// <summary>归一化后的接入点（只允许 china / cf；其它值当作未设置）。</summary>
    public string NormalizedCdn()
    {
        var cdn = (SignCdn ?? "").Trim().ToLowerInvariant();
        return cdn is "china" or "cf" ? cdn : "";
    }

    public Process StartLlbot()
    {
        if (_isRunning) return _llbotProcess!;

        var exePath = ResolveLlbotExe();
        if (exePath.Length == 0)
            throw new FileNotFoundException(
                "找不到 LLBot 启动器 exe。请点「浏览…」选择安装目录下的 LuckyLillia.exe（新版）"
                + "或 llbot.exe（旧版）——新版这两个名字都可能在用，程序按文件结构自动识别。");

        var workDir = Path.GetDirectoryName(exePath) ?? LlbotFolder;

        // 新版启动器会按自身位置找 bin\pmhq 与 bin\llbot，所以工作目录必须是安装根目录
        if (Layout == LlbotLayout.Bundled)
        {
            var runtimeJs = Path.Combine(workDir, "bin", "llbot", "llbot.js");
            if (!File.Exists(runtimeJs))
                throw new FileNotFoundException($"新版运行时缺失：{runtimeJs}");
        }

        var utf8 = new UTF8Encoding(false);

        var args = BuildArguments();
        var cdn = NormalizedCdn();

        var psi = new ProcessStartInfo
        {
            FileName = exePath,
            Arguments = args,
            WorkingDirectory = workDir,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            StandardOutputEncoding = utf8,
            StandardErrorEncoding = utf8,
            UseShellExecute = false,
            CreateNoWindow = true
        };

        if (cdn.Length > 0)
        {
            psi.Environment["CDN"] = cdn;
            psi.Environment["PMHQ_CDN"] = cdn;
        }

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
        StartLogTail();
        _isRunning = true;
        _isRestarting = false;
        return _llbotProcess;
    }

    /// <summary>跟随新版运行时日志文件（`data\logs\llbot-*.log`，每次启动一个新文件）。</summary>
    private void StartLogTail()
    {
        if (Layout != LlbotLayout.Bundled) { StopLogTail(); return; }

        var dir = LogDir;
        if (dir.Length == 0 || !Directory.Exists(dir)) return;

        StopLogTail();
        try
        {
            _logWatcher = new FileSystemWatcher(dir, "llbot-*.log")
            {
                NotifyFilter = NotifyFilters.FileName | NotifyFilters.LastWrite | NotifyFilters.CreationTime,
                EnableRaisingEvents = true,
            };
            _logWatcher.Created += (s, e) => PumpLogFile();
            _logWatcher.Changed += (s, e) => PumpLogFile();
        }
        catch { _logWatcher = null; }

        // 监听到变化就补读一次；定时器负责兜底（有些写入不触发事件）
        _logTimer = new System.Timers.Timer(1500) { AutoReset = true };
        _logTimer.Elapsed += (s, e) => PumpLogFile();
        _logTimer.Start();
        PumpLogFile();
    }

    private void StopLogTail()
    {
        try { _logTimer?.Stop(); _logTimer?.Dispose(); } catch { }
        _logTimer = null;
        try { _logWatcher?.Dispose(); } catch { }
        _logWatcher = null;
        _logFile = "";
        _logOffset = 0;
        _logPending = "";
    }

    /// <summary>增量读取最新日志文件，把新增的整行喂给 OnLlbotLine。</summary>
    private void PumpLogFile()
    {
        try
        {
            var dir = LogDir;
            if (dir.Length == 0 || !Directory.Exists(dir)) return;

            var newest = new DirectoryInfo(dir)
                .GetFiles("llbot-*.log", SearchOption.TopDirectoryOnly)
                .OrderByDescending(f => f.LastWriteTimeUtc)
                .FirstOrDefault();
            if (newest == null) return;

            if (!string.Equals(newest.FullName, _logFile, StringComparison.OrdinalIgnoreCase))
            {
                // 换了文件（新实例/轮转）：从头读，才能补上启动阶段就打印的二维码网址
                _logFile = newest.FullName;
                _logOffset = 0;
                _logPending = "";
            }

            using var fs = new FileStream(_logFile, FileMode.Open, FileAccess.Read,
                FileShare.ReadWrite | FileShare.Delete);
            if (fs.Length < _logOffset)
            {
                // 文件被截断/重建
                _logOffset = 0;
                _logPending = "";
            }
            if (fs.Length == _logOffset) return;

            fs.Seek(_logOffset, SeekOrigin.Begin);
            using var reader = new StreamReader(fs, new UTF8Encoding(false));
            var chunk = reader.ReadToEnd();
            _logOffset = fs.Length;
            if (chunk.Length == 0) return;

            _logPending += chunk;
            var parts = _logPending.Split('\n');
            _logPending = parts[^1];
            for (var i = 0; i < parts.Length - 1; i++)
            {
                var line = parts[i].TrimEnd('\r');
                if (line.Length > 0) OnLlbotLine(line);
            }
        }
        catch { }
    }

    /// <summary>
    /// 行去重：新版下运行时日志会同时出现在 stdout 与日志文件里（两份措辞可能只差时间戳），
    /// 这里按「去掉时间戳/级别后的正文」在 8 秒窗口内去重，避免界面刷两遍。
    /// </summary>
    private static readonly Regex LogTimePrefix =
        new(@"^\s*\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?\s*", RegexOptions.Compiled);

    private bool IsDuplicateLine(string line)
    {
        var key = LogTimePrefix.Replace(line, "").Trim();
        if (key.Length == 0) return true;

        var now = DateTime.UtcNow;
        if (_recentLines.TryGetValue(key, out var at) && (now - at).TotalSeconds < 8)
            return true;
        _recentLines[key] = now;

        if (_recentLines.Count > 400)
        {
            foreach (var stale in _recentLines.Where(kv => (now - kv.Value).TotalSeconds > 30)
                                              .Select(kv => kv.Key).ToList())
                _recentLines.Remove(stale);
        }
        return false;
    }

    private void OnLlbotLine(string line)
    {
        if (IsDuplicateLine(line)) return;
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
        StopLogTail();
        LogReceived?.Invoke($"[系统] {Path.GetFileName(_llbotExe.Length > 0 ? _llbotExe : "LLBot")} 已退出");
        LlbotExited?.Invoke();
        if (!_isRestarting)
        {
            LogReceived?.Invoke("[系统] 如需重启请手动点击启动");
        }
    }

    /// <summary>从日志里抓 QQ 进程 PID（停止时按 PID 精确清理，避免误杀其它 QQ）。</summary>
    private void TryTrackQqPid(string line)
    {
        // 旧版：`QQ 进程 PID: 1234`
        const string marker = "QQ 进程 PID:";
        var idx = line.IndexOf(marker, StringComparison.Ordinal);
        if (idx >= 0)
        {
            var rest = line.Substring(idx + marker.Length).Trim();
            var digits = new string(rest.TakeWhile(char.IsDigit).ToArray());
            if (int.TryParse(digits, out var pid) && pid > 0)
            {
                _qqPid = pid;
                return;
            }
        }

        // 新版启动器的进程表提示形如 `... (PID: 1234)`，标签是英文。
        // 这里只在「该 PID 的进程名确实含 QQ」时才采纳，避免把 pmhq/node 的 PID 记成 QQ。
        foreach (var known in new[] { "QQ 进程", "QQ", "qq" })
        {
            if (!line.Contains(known, StringComparison.Ordinal)) continue;
            var m = Regex.Match(line, @"PID\s*[:：]\s*(\d+)", RegexOptions.IgnoreCase);
            if (!m.Success) continue;
            if (!int.TryParse(m.Groups[1].Value, out var candidate) || candidate <= 0) continue;
            try
            {
                var proc = Process.GetProcessById(candidate);
                if (proc.ProcessName.Contains("QQ", StringComparison.OrdinalIgnoreCase))
                    _qqPid = candidate;
            }
            catch { }
            return;
        }
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
        StopLogTail();
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

        // 兜底：把安装目录下还活着的 LLBot 进程（PMHQ / 内嵌 node / 别的启动器实例）一并清掉。
        // 这一步是「停止按钮按下去了却还有残留」的补丁：面板自己的句柄管不到它们。
        KillInstallProcesses();
    }

    /// <summary>
    /// 枚举**安装目录下**的 LLBot 进程：启动器（LuckyLillia/llbot）+ PMHQ + 内嵌 node。
    ///
    /// 为什么不能只靠 `_llbotProcess`：面板只知道"自己启动的那个"子进程。
    /// 一旦启动器是手动/旧版面板/命令行拉起来的，或者 QQ、PMHQ 被重新拉起过，
    /// 面板的句柄就管不到它们 —— 这就是"点了停止却没停干净"的根因。
    /// 所以这里按**可执行文件路径**兜底：只认安装目录内的进程，
    /// 用户的 Node.js、别的 QQ 不会被误杀。
    /// </summary>
    public List<(int Pid, string Name, string Path)> FindInstallProcesses()
    {
        var found = new List<(int, string, string)>();
        var root = LlbotFolder;
        if (root.Length == 0 || !Directory.Exists(root)) return found;
        var prefix = root.TrimEnd('\\') + "\\";

        foreach (var name in new[] { "LuckyLillia", "LuckyLilliaBot", "llbot", "pmhq", "node" })
        {
            Process[] procs;
            try { procs = Process.GetProcessesByName(name); }
            catch { continue; }

            foreach (var proc in procs)
            {
                try
                {
                    var path = proc.MainModule?.FileName ?? "";
                    if (path.StartsWith(prefix, StringComparison.OrdinalIgnoreCase))
                        found.Add((proc.Id, proc.ProcessName, path));
                }
                catch
                {
                    // 取不到路径（权限/已退出）→ 不是我们能确认的目标，跳过
                }
                finally
                {
                    proc.Dispose();
                }
            }
        }
        return found.OrderBy(f => f.Item1).ToList();
    }

    /// <summary>把安装目录下的 LLBot 进程全部结束（含 PMHQ 与内嵌 node），返回已结束的列表。</summary>
    public List<string> KillInstallProcesses()
    {
        var killed = new List<string>();
        foreach (var (pid, name, _) in FindInstallProcesses())
        {
            try
            {
                using var proc = Process.GetProcessById(pid);
                proc.Kill(entireProcessTree: true);
                proc.WaitForExit(3000);
                killed.Add($"{name}({pid})");
            }
            catch { }
        }
        if (killed.Count > 0)
            LogReceived?.Invoke($"[系统] 已结束残留进程：{string.Join("、", killed)}");
        return killed;
    }

    /// <summary>
    /// 是否用 headless 模式启动（LLBot 直连 QQ 协议，不经过 PMHQ）。
    ///
    /// 两个模式都是官方支持的（见 `LuckyLillia.exe --help`）：
    /// - PMHQ（默认）：复用真实 QQ 客户端，但二维码只画在 QQ 窗口里，
    ///   PMHQ 抠不出来（`qrcode_available` 恒 false），面板/WebUI 拿不到图；
    /// - headless：LLBot 自己跑协议、自己产出二维码 —— 终端打印 + 落盘到
    ///   `data\temp\login-qrcode.png` + WebUI `/api/login-qrcode` 可用，
    ///   于是面板的二维码卡片、日志网址、推送 Dashboard 整套都能照旧工作。
    /// </summary>
    public bool HeadlessMode { get; set; }

    /// <summary>
    /// 签名接入点：`china`（国内镜像 llbot-api-auth.wumiao.wang，仅 IPv4）/ `cf`
    /// （api-auth.luckylillia.com，Cloudflare，含 AAAA —— IPv6 抖动时会 Network unreachable）。
    /// 空 = 交给 LLBot 自己决定（默认 cf）。
    /// </summary>
    public string SignCdn { get; set; } = "china";


    /// <summary>
    /// 把 QQ 窗口切到前台。
    ///
    /// PMHQ 模式下二维码是 QQ 自己画的：LLBot 通过注入去读 QQ 内存里的码，
    /// 读不到（`/get_login_qrcode` 返回 `qrcode not ready yet`）时面板和 WebUI 都没有图可显示，
    /// 但 QQ 窗口里是正常的。用户点了「启动」之后 QQ 可能被别的窗口挡住，
    /// 所以给一个按钮直接把 QQ 拉到前面来。
    /// </summary>
    /// <summary>
    /// 找「QQ 登录窗口」那个进程。
    /// 机器上多半同时开着用户自己的 QQ：优先挑配置里那个（机器人 QQ），
    /// 再按启动时间取最新的，避免把用户自己的 QQ 拉到前台。
    /// </summary>
    private Process? FindQqWindowProcess()
    {
        var configured = (QqPath ?? "").Trim();
        try
        {
            return Process.GetProcessesByName("QQ")
                .Where(p =>
                {
                    try { return p.MainWindowHandle != IntPtr.Zero; }
                    catch { return false; }
                })
                .OrderByDescending(p => IsConfiguredQq(p, configured))
                .ThenByDescending(p =>
                {
                    try { return p.StartTime; }
                    catch { return DateTime.MinValue; }
                })
                .FirstOrDefault();
        }
        catch { return null; }
    }

    public string FocusQqWindow()
    {
        var target = FindQqWindowProcess();
        if (target == null)
            return "没找到带窗口的 QQ：可能还没启动完成，或它停在后台进程上";

        try
        {
            ShowWindow(target.MainWindowHandle, SwRestore);
            SetForegroundWindow(target.MainWindowHandle);
            return $"已切到 QQ 窗口（PID {target.Id}）：二维码 / 快速登录都在里面";
        }
        catch (Exception ex)
        {
            return $"切换 QQ 窗口失败：{ex.Message}";
        }
        finally
        {
            target.Dispose();
        }
    }

    /// <summary>
    /// 启动后等 QQ 登录窗口出现（最多 timeoutSeconds），出现就前置。
    /// QQ 从「启动」到「画出登录窗口」通常要几秒，立刻切会扑空。
    /// </summary>
    public async Task<string> FocusQqWindowWhenReadyAsync(int timeoutSeconds = 30)
    {
        var deadline = DateTime.UtcNow.AddSeconds(timeoutSeconds);
        while (DateTime.UtcNow < deadline)
        {
            var target = FindQqWindowProcess();
            if (target != null)
            {
                target.Dispose();
                return FocusQqWindow();
            }
            await Task.Delay(1000);
        }
        return "等待 QQ 登录窗口超时（可手动点「🔗 打开 QQ 窗口」）";
    }

    /// <summary>
    /// 把 QQ 登录窗口的画面截成 PNG。
    /// 用途：PMHQ 模式下 PMHQ 抠不到二维码，但 QQ 窗口里是有码的 ——
    /// 截下来放进面板的二维码卡片，等于把旧版「在面板里扫码」的能力还回来。
    /// </summary>
    public (byte[]? Png, string Message) CaptureQqWindow()
    {
        var target = FindQqWindowProcess();
        if (target == null)
            return (null, "没找到 QQ 登录窗口，无法截图");

        try
        {
            var handle = target.MainWindowHandle;
            ShowWindow(handle, SwRestore);
            SetForegroundWindow(handle);
            Thread.Sleep(150);   // 等窗口重绘，避免截到半张

            if (!GetWindowRect(handle, out var rect))
                return (null, "读取 QQ 窗口位置失败");

            var width = rect.Right - rect.Left;
            var height = rect.Bottom - rect.Top;
            if (width < 160 || height < 160)
                return (null, $"QQ 窗口尺寸异常（{width}x{height}）");

            using var bitmap = new System.Drawing.Bitmap(width, height);
            using (var graphics = System.Drawing.Graphics.FromImage(bitmap))
            {
                graphics.CopyFromScreen(rect.Left, rect.Top, 0, 0, bitmap.Size);
            }
            using var buffer = new MemoryStream();
            bitmap.Save(buffer, System.Drawing.Imaging.ImageFormat.Png);
            return (buffer.ToArray(), $"已截取 QQ 窗口（{width}x{height}）：二维码就在里面，用手机扫这张图即可");
        }
        catch (Exception ex)
        {
            return (null, $"截取 QQ 窗口失败：{ex.Message}");
        }
        finally
        {
            target.Dispose();
        }
    }

    [System.Runtime.InteropServices.DllImport("user32.dll")]
    private static extern bool GetWindowRect(IntPtr hWnd, out Rect rect);

    [System.Runtime.InteropServices.StructLayout(System.Runtime.InteropServices.LayoutKind.Sequential)]
    private struct Rect
    {
        public int Left, Top, Right, Bottom;
    }


    /// <summary>该进程是不是配置里那个 QQ（按可执行文件路径比对）。</summary>
    private static bool IsConfiguredQq(Process proc, string configuredPath)
    {
        if (string.IsNullOrEmpty(configuredPath)) return false;
        try
        {
            var path = proc.MainModule?.FileName ?? "";
            return path.Length > 0
                && string.Equals(Path.GetFullPath(path), Path.GetFullPath(configuredPath),
                    StringComparison.OrdinalIgnoreCase);
        }
        catch { return false; }
    }

    private const int SwRestore = 9;

    [System.Runtime.InteropServices.DllImport("user32.dll")]
    private static extern bool SetForegroundWindow(IntPtr hWnd);

    [System.Runtime.InteropServices.DllImport("user32.dll")]
    private static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);

    /// <summary>
    /// 启动前检查：PMHQ(13000) / WebUI(3080) 是否已被占用。
    /// 新旧版都用这两个默认端口，被占用说明（很可能）已经有一个实例在跑 ——
    /// 此时再启动会端口冲突，新版启动器还会在控制台里问「是否关闭这些进程」，
    /// 而面板没有给它 stdin，等于卡住后再无声退出。所以先问用户。
    /// </summary>
    public IReadOnlyList<int> BusyPorts()
    {
        var busy = new List<int>();
        if (!HeadlessMode && IsPortInUse(PmhqPort)) busy.Add(PmhqPort);   // headless 不用 PMHQ 端口
        if (IsPortInUse(WebuiPort)) busy.Add(WebuiPort);
        return busy;
    }

    private static bool IsPortInUse(int port)
    {
        try
        {
            var props = IPGlobalProperties.GetIPGlobalProperties();
            foreach (var listener in props.GetActiveTcpListeners())
                if (listener.Port == port) return true;

            foreach (var conn in props.GetActiveTcpConnections())
            {
                if (conn.LocalEndPoint.Port != port) continue;
                // 只把「真的有人占着」算占用：
                //   Established / SynSent  = 正在连
                //   TimeWait / CloseWait…  = 进程已经退出、系统还在回收的残留 socket
                // 把 TIME_WAIT 也算占用的话，刚停掉 LLBot 后的 1~4 分钟里
                // 面板会一直报"端口被占用"，明明没有任何 LLBot 进程（本项目踩过）。
                if (conn.State == TcpState.Established || conn.State == TcpState.SynSent)
                    return true;
            }
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
        StopLogTail();
        foreach (var w in _qrWatchers)
        {
            try { w.Dispose(); } catch { }
        }
        _qrWatchers.Clear();
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
