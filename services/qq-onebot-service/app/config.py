"""qq-onebot-service 配置。

设计成"能连上任何 OneBot 11 实现"：NapCat、LLOneBot、go-cqhttp 都行。
satori 那条（qq-llbot-service）保持不变，两者是平级适配器。

连接方式二选一（部署时定）：
  forward  我们主动连对方的 WS（ws://<host>:<port>/）—— 最常见
  reverse  对方连我们的 WS（NapCat 里填 ws 反向地址）—— 不用改对端 IP
"""
import os
from pathlib import Path

WORKSPACE_DIR = Path(os.getenv("WORKSPACE_DIR", "/app/workspace"))

# forward | reverse | auto（auto 先试 forward，失败后开反向监听）
ONEBOT_MODE = os.getenv("ONEBOT_MODE", "forward").strip().lower() or "forward"
ONEBOT_WS_URL = os.getenv("ONEBOT_WS_URL", "ws://127.0.0.1:3001/").strip()
ONEBOT_WS_LISTEN = os.getenv("ONEBOT_WS_LISTEN", "0.0.0.0:6700").strip()
ONEBOT_TOKEN = os.getenv("ONEBOT_TOKEN", "").strip()
# 机器人自己的 QQ 号（判断"是否被 @"，reverse 模式下从事件里也能学到）
ONEBOT_SELF_ID = os.getenv("ONEBOT_SELF_ID", "").strip()

# 群里必须 @ 机器人才响应（私聊不受影响）
ONEBOT_GROUP_REQUIRE_AT = os.getenv("ONEBOT_GROUP_REQUIRE_AT", "true").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
# 调 action 接口的超时（秒）
ONEBOT_API_TIMEOUT = float(os.getenv("ONEBOT_API_TIMEOUT", "10"))
# 断线重连退避上限（秒）
ONEBOT_RECONNECT_MAX = float(os.getenv("ONEBOT_RECONNECT_MAX", "30"))

SCHEDULER_TARGET = os.getenv("SCHEDULER_TARGET", "task-scheduler-service.agent.svc.cluster.local:5100")
SUBSCRIBER_ID = os.getenv("SUBSCRIBER_ID", "qq-onebot-1")
# satori 那条是 100，官方 50；OneBot 放 90：两者同时在线时 satori 优先
SUBSCRIBER_PRIORITY = int(os.getenv("SUBSCRIBER_PRIORITY", "90"))
OFFLINE_SEND_FAILURES = int(os.getenv("OFFLINE_SEND_FAILURES", "3"))

# 单条消息最多下载几张图片
ONEBOT_MAX_IMAGES = int(os.getenv("ONEBOT_MAX_IMAGES", "9"))
# 相对图片路径（如 /api/assets/x.jpg）的补全基址；留空=原样发送。
# NapCat 在别的机器上时，必须填它访问得到的地址（如 http://10.0.0.5:5210）。
ONEBOT_MEDIA_BASE = os.getenv("ONEBOT_MEDIA_BASE", "").strip().rstrip("/")

# 图片下载超时（秒）
ONEBOT_IMAGE_TIMEOUT = float(os.getenv("ONEBOT_IMAGE_TIMEOUT", "15"))
