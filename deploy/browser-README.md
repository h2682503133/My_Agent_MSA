# 浏览器工具（browser）接入说明

让智能体操作**宿主 Windows 上的 Edge**（真实渲染、真实登录态），做页面级人机协作。

## 一条链路，三段

```
k8s pod（tool-runtime 的 browser 工具）
  → 172.18.0.1:9224         Docker 发布端口（容器 cdp-relay 转发）
  → host.docker.internal:9223   宿主中继（deploy/browser-cdp-relay.ps1）
  → 127.0.0.1:9222          Edge 的 DevTools 端口
```

为什么这么绕：

- Chromium 156 的 `--remote-debugging-address=0.0.0.0` 会被忽略，DevTools **只绑 127.0.0.1**；
- 容器（含 k8s pod）访问宿主只能用 Docker 发布的端口，所以需要「宿主中继 + 发布端口」两段；
- pod 里 `host.docker.internal` 不可达，但 **节点网段 .1（172.18.0.1）可达** ——
  tool-runtime 用 downward API 注入 `NODE_IP`，自动推出这个地址。

## 从 admin panel 启动（推荐）

面板 `http://127.0.0.1:5700` → **浏览器环境**：

| 按钮 | 作用 |
|---|---|
| 启动环境 | 下发 `start`：起 Edge（带调试端口）+ 宿主中继（9223）+ 确保 `cdp-relay` 容器发布 9224 |
| 停止转发 | 下发 `stop`：停中继 + 停容器（Edge 不动，可能你正在用） |
| 重探状态 | 下发 `status`，并把探活结果刷出来 |

页面每 5 秒自动探活一次（探测 `http://<节点网段>.1:9224/json/version`），
并显示最近一次执行结果（Edge / 中继 / 容器 各自状态）。

面板只负责**下发命令**，真正执行的是宿主上的代理脚本（双击即可）：

```
deploy\browser-boot.cmd          （或 powershell -File deploy/browser-boot.ps1）
```

它常驻轮询 `http://127.0.0.1:5700/api/browser/pending`，执行完把结果回 `/api/browser/report`。
所以：**代理脚本活着 + Edge 活着**，面板按钮才有意义。

## 启动步骤（宿主，手动版）

**1. 完全退出 Edge**（CDP 只在浏览器主进程启动时生效，已在运行的实例会忽略参数）

**2. 用独立 profile 启动 Edge 并开调试端口**

```powershell
& "C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe" `
  --remote-debugging-port=9222 `
  --user-data-dir="C:\edge-agent-profile"
```

> 为什么必须独立 profile：Chromium 136+ 出于防 cookie 窃取的考虑，**禁止对默认 profile 开调试端口**（带参数启动也会被忽略）。
> 这个窗口就是"智能体能看见的那个浏览器"，需要登录的站点请在这个窗口里登录一次。

**3. 起宿主中继**

```powershell
powershell -File deploy/browser-cdp-relay.ps1
```

**4. 确认发布端口在跑**

```powershell
docker ps --filter name=cdp-relay
# 容器名 cdp-relay，发布 9224 → host.docker.internal:9223
docker run -d --name cdp-relay --restart unless-stopped -p 9224:9224 `
  hub.rat.dev/alpine/socat:latest tcp-listen:9224,fork,reuseaddr tcp-connect:host.docker.internal:9223
```

**5. 告诉 tool-runtime 用哪个地址**（已配置则跳过）

```powershell
kubectl -n agent set env deploy/tool-runtime-service BROWSER_CDP_URL=http://172.18.0.1:9224
```

## 工具用法

```
工具调用:browser|open|https://example.com
工具调用:browser|read                          # 标题/URL/正文摘要 + 可交互元素（带 #序号）
工具调用:browser|click|#5                      # 也支持 CSS 或可见文本：click|登录
工具调用:browser|type|#3|要输入的内容           # 密码框不记录内容
工具调用:browser|key|Enter
工具调用:browser|shot|outputs/browser/a.png     # 页面在后台/最小化时截图会失败并给出提示
工具调用:browser|tabs / back / wait|1500
```

## 权限

浏览器工具走 `BROWSER_TOOL_NAMES`，复用 dashboard「杂项」→ **允许访问 Windows 宿主机的 user_id**（`shell_allowed_users`）。
不在名单里的用户调用会被拦：

```
工具调用被拦截：当前用户没有操作宿主浏览器的权限
```

## 排错

| 现象 | 原因 / 处理 |
|---|---|
| `找不到可用的浏览器调试端口` | Edge 没开调试端口；或中继/发布端口没起。按上面 1-4 步检查 |
| `连上了但 CDP 握手失败` | 中继端口通了但 Edge 已退出，重启 Edge |
| 截图超时 | 页面在后台标签或窗口最小化，切到前台再试 |
| 看到的页面不是自己日常那个 | 正常：智能体用的是 `C:\edge-agent-profile` 这个独立 profile |

自检：`kubectl -n agent exec -i deploy/tool-runtime-service -c tool-runtime-service -- python /app/tools/browser_tools_test.py`
