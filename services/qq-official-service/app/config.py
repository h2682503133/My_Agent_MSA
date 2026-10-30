"""配置加载：JSON 文件（NFS 挂载）+ 环境变量覆盖。

与 qq-llbot-service 的加载方式保持一致：文件提供默认值，环境变量优先，
这样同一份镜像可以在不同环境复用。
"""
import json
import os

CONFIG_PATH = os.getenv(
    "QQ_OFFICIAL_CONFIG_PATH", "/service/config/qq_official_config.json"
)

_DEFAULTS = {
    "scheduler_target": "task-scheduler-service.agent.svc.cluster.local:5100",
    "user_service_target": "user-service.agent.svc.cluster.local:5104",
    "model_proxy_target": "model-proxy-service:5302",
    "reader_profile": "reader",
    "subscriber_id": "qq-official-1",
    "channel": "qqofficial",
    "api_base": "https://api.bot.qq.com",
    "app_id": "",
    "client_secret": "",
    "intents": 1 << 25,  # GROUP_AND_C2C_EVENT：单聊 + 群 @ 事件
    "group_map": {},
    "workspace_dir": "/app/workspace",
    "reply_chunk_chars": 1500,
    "summary_timeout_seconds": 20,
}


def _load_file() -> dict:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        print(f"[qq-official] 配置文件不存在，使用默认值: {CONFIG_PATH}", flush=True)
        return {}
    except Exception as exc:
        print(f"[qq-official] 配置文件解析失败 {CONFIG_PATH}: {exc}", flush=True)
        return {}


_file = {**_DEFAULTS, **_load_file()}


def _get(name: str, env: str, cast=str):
    raw = os.getenv(env)
    if raw is not None and raw != "":
        try:
            return cast(raw)
        except Exception:
            pass
    return _file.get(name, _DEFAULTS.get(name))


SCHEDULER_TARGET = str(_get("scheduler_target", "SCHEDULER_TARGET"))
USER_SERVICE_TARGET = str(_get("user_service_target", "USER_SERVICE_TARGET"))
MODEL_PROXY_TARGET = str(_get("model_proxy_target", "MODEL_PROXY_TARGET"))
READER_PROFILE = str(_get("reader_profile", "QQ_OFFICIAL_READER_PROFILE"))
SUBSCRIBER_ID = str(_get("subscriber_id", "SUBSCRIBER_ID"))
CHANNEL = str(_get("channel", "QQ_OFFICIAL_CHANNEL"))

# 路由渠道：统一走 `qq`，由调度器在两个后端之间选（llbot 优先、官方兜底）。
# 绑定渠道仍用 `qqofficial`：user-service 里 openid 是挂在它下面的。
ROUTE_CHANNEL = str(_get("route_channel", "QQ_OFFICIAL_ROUTE_CHANNEL") or "qq")
BINDING_CHANNEL = CHANNEL
SUBSCRIBER_PRIORITY = int(_get("subscriber_priority", "QQ_OFFICIAL_SUBSCRIBER_PRIORITY") or 50)
API_BASE = str(_get("api_base", "QQ_OFFICIAL_API_BASE")).rstrip("/")
APP_ID = str(_get("app_id", "QQ_OFFICIAL_APP_ID"))
CLIENT_SECRET = str(_get("client_secret", "QQ_OFFICIAL_CLIENT_SECRET"))
INTENTS = int(_get("intents", "QQ_OFFICIAL_INTENTS", int))
WORKSPACE_DIR = str(_get("workspace_dir", "QQ_OFFICIAL_WORKSPACE"))
REPLY_CHUNK_CHARS = int(_get("reply_chunk_chars", "QQ_OFFICIAL_REPLY_CHARS", int))
SUMMARY_TIMEOUT_SECONDS = int(_get("summary_timeout_seconds", "QQ_OFFICIAL_SUMMARY_TIMEOUT", int))

# 官方渠道发图：下载图片的单独超时（图床在集群内，通常很快）
IMAGE_FETCH_TIMEOUT_SECONDS = int(
    os.getenv("QQ_OFFICIAL_IMAGE_FETCH_TIMEOUT", "15")
)

_group_map = _get("group_map", "QQ_OFFICIAL_GROUP_MAP", dict)
GROUP_MAP = {str(k): str(v) for k, v in (_group_map or {}).items()}
# 反向表：群号 → group_openid（出站时把 session 里的群号换回 openid）
GROUP_REVERSE = {v: k for k, v in GROUP_MAP.items()}


def is_configured() -> bool:
    """没有 appid/secret 时服务应当拒绝启动，而不是空转。"""
    return bool(APP_ID and CLIENT_SECRET)
