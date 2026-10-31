# 浏览器工具（browser）接入说明

让智能体操作**宿主 Windows 上的 Edge**：真实渲染、真实登录态，做页面级人机协作。
**推荐 Playwright MCP**（浏览器由 MCP 托管）；CDP 直连作为兜底保留。

> 结论来自实测（《Edge_Playwright_MCP_经验汇总》）：CDP 9222 那条路要杀光用户所有 Edge、
> 参数会被浏览器丢弃、端口还可能被回收；MCP 自己托管浏览器 + 固定 profile，稳定得多。

## 两种模式

| 模式 | 谁启动浏览器 | 调试端口 | 适用 |
|---|---|---|---|
| **Playwright MCP（推荐）** | MCP 自己起 Edge（`--browser msedge`） | 不需要 | 日常人机协作、登录后抓数据 |
| CDP 兼容 | 面板用 `--remote-debugging-port` 起 Edge | 需要 | MCP 不可用时的兜底 |

共同点：都用**独立 profile**（Chromium 136+ 禁止对默认 profile 开调试端口），登录态跟着 profile 走。

## 面板操作（唯一入口）

`admin-panel/publish/MyAgentAdminPanel.exe` → 左侧「🌐 浏览器环境」：

| 区块 | 按钮 | 作用 |
|---|---|---|
| Playwright MCP（推荐） | **启动 MCP 环境** | 起 `npx @playwright/mcp`（含首次下载）→ 起进程内中继 → 确保 `mcp-relay` 容器发布 |
| | 停止 MCP | 结束 MCP（连带它托管的 Edge）+ 停中继 + 停容器 |
| CDP 兼容模式 | 启动 CDP 环境 | 起 Edge(9222) → 中继(9223) → 容器发布(9224) |
| | 停止转发 | 停中继 + 停容器（Edge 不动） |
| 状态 | 刷新状态 | 三行状态：端口 / 中继 / 容器 |

设置项（存 `%LOCALAPPDATA%\MyAgentAdminPanel\settings.json`，不写注册表）：
npx 路径、Edge 路径、MCP profile 目录、端口 `9333,9334,9335`。

## 链路

```
k8s pod（tool-runtime 的 browser 工具）
  → 172.18.0.1:9335            Docker 发布端口（容器 mcp-relay）
  → host.docker.internal:9334  宿主中继（面板进程内；python 版见 deploy/mcp-relay.py）
  → ::1:9333                   Playwright MCP（只监听 IPv6 回环）
  → MCP 自己拉起的 Edge
```

为什么必须两段转发：MCP 只绑 `::1`、容器又只能通过 Docker 发布端口访问宿主；
pod 里 `host.docker.internal` 不可达，但**节点网段 .1（172.18.0.1）可达**
（tool-runtime 用 downward API 注入 `NODE_IP` 自动推出）。

## tool-runtime 配置

```powershell
kubectl -n agent set env deploy/tool-runtime-service BROWSER_MCP_URL=http://172.18.0.1:9335/mcp
```

- 配了 `BROWSER_MCP_URL` → 走 MCP；
- 没配 → 自动退回 CDP（`BROWSER_CDP_URL`，默认依序试 host.docker.internal / 默认网关 / 节点 .1）。

## 工具用法（MCP 语义）

```
工具调用:browser|open|https://example.com      → browser_navigate
工具调用:browser|read                          → browser_snapshot（无障碍树 + ref）
工具调用:browser|click|e13                     → 用 snapshot 里的 ref 点击（含 f2e13 这种 frame 前缀）
工具调用:browser|click|登录                     → 文本兜底（内部用 JS 找元素再点）
工具调用:browser|type|e5|我的账号                → ref 输入；也可用 placeholder 文字
工具调用:browser|key|Enter
工具调用:browser|find|关键词                    → browser_find（在快照里搜）
工具调用:browser|eval|() => document.title      → browser_evaluate（抓结构化数据）
工具调用:browser|select|e9|选项A|选项B           → browser_select_option
工具调用:browser|tabs / back / wait|2 / shot|outputs/browser/a.png
```

三条心法（与经验汇总一致）：

1. **先 read 再动手**：`read` 返回无障碍树，元素带 `ref=e13`（含 frame 前缀如 `f2e13`），
   点击/输入直接用 ref，比猜 CSS 选择器稳；
2. **抓数据用 eval**，不要「截图 → OCR」；`shot` 只用于给人看；
3. **等条件而不是死等**：优先用 `find` / 重新 `read` 判断状态。

## 权限

浏览器工具走 `BROWSER_TOOL_NAMES`，复用 dashboard「杂项」→ **允许访问 Windows 宿主机的 user_id**
（`shell_allowed_users`）。不在名单里的用户调用会被拦：

```
工具调用被拦截：当前用户没有操作宿主浏览器的权限
```

## 排错

| 现象 | 原因 / 处理 |
|---|---|
| `找不到可用的浏览器调试端口`（CDP 模式） | Edge 没开调试端口；用面板「启动 CDP 环境」 |
| `Browser is already in use for ...`（MCP） | 同一 profile 有别的浏览器在跑：面板「停止 MCP」→「启动 MCP 环境」 |
| MCP 起不来、日志里有 npm 下载 | 首次运行要下载 `@playwright/mcp`，等一两分钟再刷新 |
| `Session not found` | 客户端会自动重新 initialize；仍失败就重启 MCP |
| 看到的页面不是日常那个 | 正常：用的是独立 profile（`…\MyAgentAdminPanel\mcp-edge-profile`），需在该窗口登录一次 |
| 截图超时 | 页面在后台/最小化，切前台再试 |

## 备用：命令行启动（没有面板时）

```powershell
# MCP
npx --yes @playwright/mcp@latest --port 9333 --browser msedge --user-data-dir D:\mcp\edge-profile --shared-browser-context --allowed-hosts * --allowed-origins *
# 宿主中继（9334 → ::1:9333）
python deploy/mcp-relay.py
# 发布容器（9335 → host.docker.internal:9334）
docker run -d --name mcp-relay --restart unless-stopped -p 9335:9335 hub.rat.dev/alpine/socat:latest tcp-listen:9335,fork,reuseaddr tcp-connect:host.docker.internal:9334
```

> `--shared-browser-context` 很重要：不加的话每个 MCP 会话都会新起一个浏览器，
> 同一个 profile 上第二次就会报 `Browser is already in use`。

自检：

```powershell
kubectl -n agent exec -i deploy/tool-runtime-service -c tool-runtime-service -- python /app/tools/browser_mcp_test.py
kubectl -n agent exec -i deploy/tool-runtime-service -c tool-runtime-service -- python /app/tools/browser_tools_test.py
```
