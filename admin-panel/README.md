# MyAgent Admin Panel

Windows 桌面管理控制面板，用于 My_Agent_MSA 微服务集群的日常运维管理。

## 功能

| 页面 | 功能 |
|------|------|
| 🔌 端口转发 | 一键启停 `dashboard`、`gateway`、`istio` 三个端口转发 |
| 🚀 一键部署 | 从 `deploy-all.ps1` 自动解析服务列表，多选后构建镜像并部署到 K8s |
| 🤖 LLBot | 选择并启动本地 LLBot 启动器 exe，实时日志查看，二维码同步推送至 Dashboard（WebUI API 优先，文件兜底） |
| 📊 Dashboard | WebView2 内嵌 `http://localhost:5700`，直接访问管理后台 |

## 环境要求

- Windows 10 / 11
- [.NET 8 Runtime](https://dotnet.microsoft.com/download/dotnet/8.0)（仅运行）
- [.NET 8 SDK](https://dotnet.microsoft.com/download/dotnet/8.0)（如需编译）
- Docker Desktop（已启用 Kubernetes）
- kubectl（通常随 Docker Desktop 安装）

## 快速开始

### 方式一：直接运行编译好的 exe

```
publish\MyAgentAdminPanel.exe
```

### 方式二：从源码编译

```bat
build.bat
```

编译产物在 `publish\MyAgentAdminPanel.exe`。

## 使用说明

1. **端口转发**：先启动需要的端口转发，Dashboard 页面依赖此功能
2. **LLBot**：点「浏览…」**直接选中 LLBot 启动器 exe**（不是文件夹），再点启动；二维码出现后可点"推送到 Dashboard"
3. **部署**：服务列表自动从项目根 `deploy-all.ps1` 读取，修改版本号只需改那一个文件
4. **Dashboard**：WebView2 内嵌，右上角可刷新或用外部浏览器打开

## LLBot 版本兼容（新旧版都支持）

LLBot 新版（LuckyLillia，8.x）把运行时塞进了 `bin\llbot\`，与旧版布局不同；
而且**新版的启动器名字还不统一**（桌面端 `LuckyLillia.exe`，CLI 版仍叫 `llbot.exe`）。
所以面板改成**直接选 exe**，再按目录结构自动识别版本——不再靠 exe 名字猜：

| | 旧版 | 新版（LuckyLillia 8.x） |
|---|---|---|
| 启动器 | `<根目录>\llbot.exe` | `<根目录>\LuckyLillia.exe`（桌面端）或 `llbot.exe`（CLI 版） |
| 运行时 | 与 exe 同级 | `bin\llbot\`（`llbot.js`、`node.exe`） |
| 数据目录 | `<根目录>\` | `bin\llbot\data\` |
| 二维码文件 | `qrcode.png` | `data\temp\login-qrcode.png` |
| 日志 | 只有 stdout | `data\logs\llbot-*.log`（面板会跟随最新日志文件） |
| 启动参数 | `--qq-path "<QQ.exe>"` | 同样支持 `--qq-path "<QQ.exe>"` |

识别的判据只有一个：`<exe 目录>\bin\llbot\llbot.js` 是否存在（存在 = 新版）。
面板顶部会显示识别结果：布局、运行时版本（读 `bin\llbot\package.json`）、数据目录、二维码文件路径。

**旧设置自动迁移**：`settings.json` 里只有旧字段 `LlbotFolder` 时，面板会在该目录里
按常见名字（`llbot.exe` / `LuckyLillia.exe` / 名字含 llbot、lillia 的 exe）找出启动器，
自动换成 exe 路径，不需要重新选；保存时同时写 `LlbotExe` 和 `LlbotFolder` 两个字段，
新旧面板互相都能读。

## 停止 / 清理残留（为什么以前「点了停止还在跑」）

面板只握有**自己启动的那个子进程**的句柄。只要出现下面任一情况，停止就会失灵、留下残留：

- 启动器是手动双击 / 命令行 / 旧版面板拉起来的 → 面板根本没有句柄，「停止」是空操作；
- 旧代码硬编码 `<文件夹>\llbot.exe`，新版没有这个文件 → 启动直接抛异常，
  用户只能自己在外面启动，面板全程不知情；
- 启动器被结束，但它拉起的 `pmhq.exe` / 内嵌 `node.exe` / QQ 变成孤儿进程。

现在的处理：

1. **停止** = 结束子进程树 → 按 PID 精确结束 QQ（旧版日志能给出 PID 时）→
   再扫一遍**安装目录**下仍在运行的 `LuckyLillia.exe` / `llbot.exe` / `pmhq.exe` / 内嵌 `node.exe` 并结束；
2. 页面信息栏会显示 `⚠ 检测到 N 个残留进程`，旁边的 **🧹 清理残留** 按钮可以
   在不重启面板的情况下一次清掉（会先列出 PID 让你确认）；
3. 认领范围**只限安装目录内的可执行文件**：你自己装的 Node.js、你自己的 QQ 都不会被误杀；
   QQ 进程任何时候都不主动结束。

> 提醒：QQ 账号互踢多半来自历史上多开的 QQ 实例，需要手动关掉多余的 QQ。

## LLBot 二维码的取源

启动 LLBot 后，面板主路径是**跟随日志里的「二维码网址」**（零副作用，就是实例当前在等的那张码）：

| 取源 | 条件 | 特点 |
|------|------|------|
| **日志网址**（主路径，自动推送） | llbot 日志里出现「二维码网址: …」 | 新旧版都支持；新版从 `data\logs\llbot-*.log` 跟随，旧版从 stdout 读取 |
| **WebUI API**（手动推送优先） | 配置了 WebUI 地址（默认 `http://127.0.0.1:3080`）且调用成功 | 调 `/api/login-qrcode`，**自带精确过期时间**；按实例端口取码，多实例不会互相混淆 |
| **二维码文件**（兜底） | API 不可用/未配置 token，且开启文件回落 | 新版读 `bin\llbot\data\temp\login-qrcode.png`，旧版读 `<根目录>\qrcode.png` |

在「LLBot」页面右侧二维码卡片里展开 **「WebUI 取码设置」** 可填写：

- **WebUI 地址** —— 该实例的 WebUI 端口（多实例时各自不同，如 `http://127.0.0.1:3081`）
- **WebUI token** —— 浏览器登录 WebUI 后 cookie `webui_token` 的值

> 为什么需要 API 方式：多个 llbot 实例若共用同一个文件夹，二维码文件会互相覆盖，
> 此时文件方式可能显示到别的实例的码；API 按端口取码，归属明确且带过期时间。
> token 留空时自动只用文件方式，行为与旧版一致。

设置与 WebUI 地址/token 一并保存在 `%LocalAppData%\MyAgentAdminPanel\settings.json`。

## 项目结构

```
admin-panel/
├── MyAgentAdminPanel.csproj   # .NET 8 WPF 项目文件
├── App.xaml / .cs             # 应用入口
├── MainWindow.xaml / .cs      # 主窗口（左侧导航 + 右侧 Frame）
├── Pages/
│   ├── PortForwardPage.xaml / .cs
│   ├── DeployPage.xaml / .cs
│   ├── LLBotPage.xaml / .cs
│   └── DashboardPage.xaml / .cs
├── Services/
│   ├── KubectlService.cs      # kubectl 子进程管理
│   ├── DockerService.cs       # docker 子进程管理
│   └── LlBotService.cs        # 启动器 exe 解析 + 新旧版布局识别 + 二维码取源 + 日志跟随
├── Models/
│   ├── PortForwardItem.cs
│   └── ServiceInfo.cs
├── build.bat                  # 编译脚本
└── README.md
```

## 版本管理

服务版本号集中在项目根 `deploy-all.ps1` 的 `$IMAGES` 中维护。admin-panel 启动时自动解析，无需重新编译。

## 设置持久化

LLBot 启动器 exe 路径（`LlbotExe`，兼容旧字段 `LlbotFolder`）、QQ 路径、WebUI 地址与 token
保存在 `%LocalAppData%\MyAgentAdminPanel\settings.json`。
