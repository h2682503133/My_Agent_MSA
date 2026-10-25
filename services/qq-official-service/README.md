# qq-official-service

QQ **官方**机器人（bot.q.qq.com，v2 API）接入服务，与 `qq-llbot-service` 同级。

- 传输：WebSocket 网关长连接（**不需要公网 IP / 域名 / HTTPS 证书**）
- 框架契约：与 llbot 完全一致 —— 入站 `scheduler.create_task`，出站
  `scheduler.subscribe_events` + `ack_event`
- 渠道名：`qqofficial`（路由键；llbot 用的是 `qq`，两者互不抢事件）
- 会话 id 形状与 llbot 相同：私聊 `qq_{user_id}`，群聊 `qq_g_{群号|group_openid}`

## 身份与上下文

官方 openid 按 AppID 隔离，**拿不到 QQ 号**（文档原文："与真实的 ID 无关"）。
要跟 llbot 共用同一段上下文，必须做一次绑定：

```
user-service.BindChannel(
    user_id        = <规范用户 id，例如 QQ 号>,
    channel        = "qqofficial",
    channel_user_id= <user_openid>,
)
```

绑定后 user-service 会写别名指针，之后 `GetUser(openid)` 直接返回规范记录，
本服务据此把 `user_id` 换成规范 id、`session_id` 换成 `qq_{规范id}` —— 与 llbot 的
上下文键完全一致，openviking 记忆自然共享。

未绑定时退化为 `user_id = openid`、`session_id = qq_{openid}`（流程一样，只是上下文独立）。

### 两条绑定入口（写同一份数据）

1. **在 QQ 里自助**（推荐，不需要知道 openid 以外的任何东西）：
   - 私聊官方机器人发 `/id` → 机器人回复 `qqofficial id: <openid>`
   - 再发 `/bind <你的QQ号>` → 直接绑定并回复结果
2. **Dashboard → QQ 设置 → 官方 Bot**：填 `user_id` + `qqofficial id` 后点绑定；
   页面上的列表就是从 user-service 读出来的，可解绑。

群聊里 `/id` 只会返回 `member_openid`（**仅本群有效**，不能当跨群身份），所以群里
只做提示，绑定时必须私聊。

```js
// dashboard 的转发端点最终落到 user-service：
GET  /channels/qqofficial       → 列出绑定
POST /channels/bind             → {user_id, channel, channel_user_id, priority}
POST /channels/unbind           → {user_id, channel}
```

群聊没有"群号"可拿，如需与 llbot 共享群上下文，在配置里写 `group_map`：

```json
{ "group_map": { "<group_openid>": "<群号>" } }
```

## 出站投递

- 优先**被动回复**：`msg_id`（事件 `d.id`）+ 递增 `msg_seq`。
  官方规则是「**每条用户消息**可回复 N 次、窗口内有效」：单聊 60 分钟 / 4 次，
  群聊 5 分钟 / 5 次 —— 不是每个会话每小时 4 次。
- **额度分配（主动消息很贵，必须省着用）**：一条用户消息的 4 次被动额度里，
  中间消息最多用 3 次，固定留 1 次给最终回复：
  - 前 2 条中间消息：需要就立刻发（`reserve=1`，不碰最后那 1 次）
  - 第 3 条及之后的中间消息：**全部暂存**，不发
  - 收到最终回复时：先把暂存的中间消息**合并成一条**发出，再单独发最终回复（两条）
  - 中间消息即时发送失败也会转暂存，绝不丢内容
- **合并超长时走 `reader` 智能体摘要**：合并结果超过 `reply_chunk_chars` 就调用
  model-proxy 的 `reader` 模型档案（与 orchestrator 的 `tool_log` 摘要同一套），
  压成一条短消息，保证「合并中间 + 最终」最多占 2 次被动额度；
  摘要失败/超时则退化为本地头尾截断（保留首尾 + 省略标记），绝不阻塞投递
- 按会话保留最近 4 条 `msg_id` 的额度并**轮转使用**（旧的过期自动淘汰），
  全部用尽才退化主动消息
- 窗口或 msg_id 失效（`40034128` / `40034005` / `40034024`）→ 自动改发主动消息
- 长度超限（`40054007` / `40054018`）→ 折半重发（最多 2 层）
- 长文本按 `reply_chunk_chars`（默认 1500）预切分
- 图片暂不回传（富媒体要先 upload 换 `file_info`，见后续计划）

## 配置

`config/qq_official_config.json`（NFS 挂载到 `/service/config`），环境变量同名覆盖：

| 键 | 环境变量 | 说明 |
|---|---|---|
| `app_id` | `QQ_OFFICIAL_APP_ID` | 机器人 AppID |
| `client_secret` | `QQ_OFFICIAL_CLIENT_SECRET` | 机器人密钥 |
| `intents` | `QQ_OFFICIAL_INTENTS` | 默认 `1<<25`（单聊 + 群 @） |
| `group_map` | `QQ_OFFICIAL_GROUP_MAP` | `{group_openid: 群号}` |
| `model_proxy_target` | `MODEL_PROXY_TARGET` | model-proxy gRPC（摘要用） |
| `reader_profile` | `QQ_OFFICIAL_READER_PROFILE` | 摘要模型档案，默认 `reader` |
| `summary_timeout_seconds` | `QQ_OFFICIAL_SUMMARY_TIMEOUT` | 摘要超时，默认 20s |
| `channel` | `QQ_OFFICIAL_CHANNEL` | 默认 `qqofficial` |
| `scheduler_target` | `SCHEDULER_TARGET` | 调度器 gRPC |
| `user_service_target` | `USER_SERVICE_TARGET` | user-service gRPC |

**没有 appid/secret 时服务会直接报错退出**，不会空转。

## 自测

```bash
python tools/session_target_smoke_test.py
python tools/bridge_smoke_test.py
```

两个测试都不联网、不需要 proto 生成（gRPC 客户端被 mock）。
