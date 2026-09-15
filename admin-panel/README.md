# MyAgent Admin Panel

Windows 桌面管理控制面板，用于 My_Agent_MSA 微服务集群的日常运维管理。

## 功能

| 页面 | 功能 |
|------|------|
| 🔌 端口转发 | 一键启停 `dashboard`、`gateway`、`istio` 三个端口转发 |
| 🚀 一键部署 | 从 `deploy-all.ps1` 自动解析服务列表，多选后构建镜像并部署到 K8s |
| 🤖 LLBot | 启动本地 `llbot.exe`，实时日志查看，二维码同步推送至 Dashboard（WebUI API 优先，文件兜底） |
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
2. **LLBot**：选择 LLBot 文件夹后点击启动，二维码出现后可点"推送到 Dashboard"
3. **部署**：服务列表自动从项目根 `deploy-all.ps1` 读取，修改版本号只需改那一个文件
4. **Dashboard**：WebView2 内嵌，右上角可刷新或用外部浏览器打开

## LLBot 二维码的两种取源

启动 LLBot 后，面板会**每 5 秒轮询**一次二维码（见 `LlBotService.FetchQrCodeAsync`）：

| 取源 | 条件 | 特点 |
|------|------|------|
| **WebUI API**（优先） | 配置了 WebUI 地址（默认 `http://127.0.0.1:3080`）且调用成功 | 调 `/api/login-qrcode`，**自带精确过期时间**；按实例端口取码，多实例不会互相混淆 |
| **二维码文件**（兜底） | API 不可用/未配置 token | 读 `<LLBot 文件夹>\qrcode.png` |

在「LLBot」页面右侧二维码卡片里展开 **「WebUI 取码设置」** 可填写：

- **WebUI 地址** —— 该实例的 WebUI 端口（多实例时各自不同，如 `http://127.0.0.1:3081`）
- **WebUI token** —— 浏览器登录 WebUI 后 cookie `webui_token` 的值

> 为什么需要 API 方式：多个 llbot 实例若共用同一个文件夹，`qrcode.png` 会互相覆盖，
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
│   └── LlBotService.cs        # llbot.exe 启动 + 二维码取源（WebUI API / 文件兜底）
├── Models/
│   ├── PortForwardItem.cs
│   └── ServiceInfo.cs
├── build.bat                  # 编译脚本
└── README.md
```

## 版本管理

服务版本号集中在项目根 `deploy-all.ps1` 的 `$IMAGES` 中维护。admin-panel 启动时自动解析，无需重新编译。

## 设置持久化

LLBot 文件夹路径、QQ 路径、WebUI 地址与 token 等设置保存在
`%LocalAppData%\MyAgentAdminPanel\settings.json`。
