# frontend-service

My_Agent MSA 的 Web 前端：Nginx 静态页面（登录 + 桌面/移动聊天页），并将 `/api/*` 反向代理到 `gateway-backend-service`。

## 端口与命名

```text
namespace: agent
image: agent/frontend-service:v15
service: frontend-service
port: 80 (HTTP)
入口: /login.html（根路径 302 跳转）
```

## 页面

| 文件 | 说明 |
|------|------|
| `login.html` | 用户登录页 |
| `chat.html` + `chat.css` + `chat.js` | 桌面端聊天页，通过 SSE 接收实时消息 |
| `chat-mobile.html` + `chat-mobile.css` | 移动端聊天页 |

## 反向代理规则（nginx.conf）

| location | 目标 | 说明 |
|----------|------|------|
| `/api/events` | `http://gateway-backend-service:5210` | SSE 事件流：`proxy_buffering off`、`proxy_read_timeout 3600s` |
| `/api/` | `http://gateway-backend-service:5210` | 其余 API（login / messages 等） |
| `/backgrounds/` | 本地目录 | `autoindex on` + `autoindex_format json`，返回背景图列表供前端轮换 |
| `/` | 本地文件 | `try_files $uri $uri/ /login.html` |

`client_max_body_size 64m`：图片以 base64 data URL 随消息提交，放宽请求体上限。

## 挂载

- `my-agent-assets-pvc` 的 `subPath: backgrounds` 挂载到 `/usr/share/nginx/html/backgrounds`，背景图存放于此。

## 构建镜像

```bash
cd frontend-service
docker build -t agent/frontend-service:v15 .
```

## 部署

```bash
kubectl apply -f deploy/services/frontend-service.yaml
```

## 访问

```bash
kubectl -n istio-system port-forward svc/istio-ingressgateway 8080:80
http://localhost:8080/login.html
```

或直接：

```bash
kubectl -n agent port-forward svc/frontend-service 8080:80
http://localhost:8080/login.html
```

## 数据流

```text
浏览器
  → /login.html / /chat.html
  → /api/login、/api/messages       (POST → gateway-backend-service:5210)
  → /api/events                     (SSE ← gateway-backend-service:5210)
```
