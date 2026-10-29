# 敏感数据注入（LLBot / 有头 QQ）

仓库里**不放任何真实 token / QQ 号**。和 `tool-runtime-apply.sh` 一样，
运行时通过环境变量或交互式输入注入：

```bash
export LLBOT_QQ=<机器人 QQ 号>
export LLBOT_AUTH_TOKEN=<auth.luckylillia.com 的 Auth Token>
export LLBOT_WEBUI_TOKEN=<WebUI 登录密码>
export LLBOT_SATORI_TOKEN=<qq-llbot-service 连 satori 用的 token>
bash deploy/llbot-apply.sh
```

不 export 也可以直接跑，脚本会提示输入（token 用 `read -s`，不回显）。

## 脚本做了什么

1. 在 NFS 数据目录写 `config_<QQ>.json`（satori 开启、端口 5600）、`auth_token.txt`、`webui_token.txt`；
2. 建/更新 k8s Secret `llbot-secrets`（键 `pmhq-auth-token`、`auto-login-qq`）；
3. `envsubst` 渲染 `deploy/services/llbot.yaml` 与 `llbot-pmhq.yaml` 后 apply。

## 清单里的引用方式

```yaml
env:
  - name: PMHQ_AUTH_TOKEN
    valueFrom:
      secretKeyRef: { name: llbot-secrets, key: pmhq-auth-token }
  - name: AUTO_LOGIN_QQ
    valueFrom:
      secretKeyRef: { name: llbot-secrets, key: auto-login-qq }
```

NFS 位置也是占位符（`${NFS_SERVER}` / `${NFS_PATH_LLBOT}` / `${NFS_PATH_PMHQ}`），
默认值写在 `deploy/llbot-apply.sh` 顶部，换环境只改那里或直接 export。

## 轮换

改完环境变量重跑 `llbot-apply.sh` 即可：Secret 与 NFS 文件都会被覆盖，
Pod 会因 Secret 变更滚动重启。
