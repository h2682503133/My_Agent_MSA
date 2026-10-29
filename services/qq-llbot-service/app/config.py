"""
配置加载：JSON 文件优先，环境变量可覆盖。

加载顺序：
1. 读取 $QQ_LLBOT_CONFIG_PATH 指定的 JSON 文件（默认 /service/config/qq_llbot_config.json）
2. 环境变量覆盖对应字段（空字符串视为未设置，不覆盖 JSON 值）

K8s 部署时，该文件通过 my-agent-config-pvc 的 subPath=qq-llbot 挂载，
对应 NFS 路径：/srv/nfs/my-agent/config/qq-llbot/qq_llbot_config.json
"""
import json
import os
from pathlib import Path
from typing import Any

_CONFIG_PATH = Path(os.getenv("QQ_LLBOT_CONFIG_PATH", "/service/config/qq_llbot_config.json"))


def _load_json_config() -> dict[str, Any]:
    if _CONFIG_PATH.exists():
        try:
            with open(_CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def _env(key: str, default: str) -> str:
    """环境变量取值，空字符串视为未设置，回退到 default"""
    val = os.getenv(key)
    if val is not None and val != "":
        return val
    return default


_cfg = _load_json_config()
_satori = _cfg.get("satori", {})

SCHEDULER_TARGET = _env(
    "SCHEDULER_TARGET",
    _cfg.get("scheduler_target", "task-scheduler-service.agent.svc.cluster.local:5100"),
)

SATORI_HOST = _env("SATORI_HOST", _satori.get("host", "qq-satori-adapter"))
SATORI_PORT = int(_env("SATORI_PORT", str(_satori.get("port", 5600))))
SATORI_TOKEN = _env("SATORI_TOKEN", _satori.get("token", ""))

SUBSCRIBER_ID = _env("SUBSCRIBER_ID", _cfg.get("subscriber_id", "qq-llbot-1"))

# 同渠道多订阅者时的优先级：llbot 优先（数值大者优先），官方渠道兜底
SUBSCRIBER_PRIORITY = int(_env("SUBSCRIBER_PRIORITY", _cfg.get("subscriber_priority", 100)))

# 连续多少次"用户可见事件"发送失败，就判定为掉线边沿 → 主动退订事件流。
# 退订后调度器会把可见事件按优先级交给 qqofficial；上线边沿（Satori 状态机）再重订。
# 不用心跳：只靠发送结果与状态机事件这两个边沿。
OFFLINE_SEND_FAILURES = int(_env("OFFLINE_SEND_FAILURES", _cfg.get("offline_send_failures", 3)))

# 群聊消息是否必须 @ 机器人才下发（私聊不受影响）
GROUP_AT_REQUIRED = _env("GROUP_AT_REQUIRED", _cfg.get("group_at_required", "true")).lower() == "true"

# 用户工作空间根目录（K8s 部署时挂载 my-agent-workspace-pvc → /app/workspace，
# 与 tool-runtime / orchestrator 共享同一 NFS workspace 目录）
WORKSPACE_DIR = Path(os.getenv("WORKSPACE_DIR", "/app/workspace"))

# 单文件下载大小上限（字节），默认 50MB
MAX_FILE_BYTES = int(_env("MAX_FILE_BYTES", str(_cfg.get("max_file_bytes", 50 * 1024 * 1024))))
