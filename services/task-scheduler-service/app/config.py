import os


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return int(value)


SCHEDULER_HOST = os.getenv("SCHEDULER_HOST", "0.0.0.0")
SCHEDULER_PORT = env_int("SCHEDULER_PORT", 5100)

# K8s Service DNS 示例：agent-orchestrator-service:5300
ORCHESTRATOR_TARGET = os.getenv("ORCHESTRATOR_TARGET", "agent-orchestrator-service:5300")

# 对应原 scheduler.py 的 BATCH_SIZE / MAX_TASK_TIME / 队列容量
BATCH_SIZE = env_int("SCHEDULER_BATCH_SIZE", 2)
MAX_TASK_TIME = env_int("SCHEDULER_MAX_TASK_TIME", 300)
GRPC_DEADLINE_SECONDS = env_int("GRPC_DEADLINE_SECONDS", MAX_TASK_TIME + 10)
USER_QUEUE_SIZE = env_int("SCHEDULER_USER_QUEUE_SIZE", 10)

# 询问挂起：用户超过该秒数未回复时，系统替用户回复并恢复任务（默认 1 小时）
SUSPEND_TTL_SECONDS = env_int("SCHEDULER_SUSPEND_TTL_SECONDS", 3600)
# 挂起超时扫描间隔
SUSPEND_SWEEP_INTERVAL = env_int("SCHEDULER_SUSPEND_SWEEP_INTERVAL", 30)

# 离线代答上限：用户离线（消息只能靠推送/转投送达）时收到推送类任务，
# 系统替用户回复「用户当前无法回复，请自行判断执行」以推动挂起任务收尾。
# 若模型仍反复提问导致再次挂起，最多代答该次数后停止代答，避免往复死循环。
MAX_OFFLINE_REPLIES = env_int("SCHEDULER_MAX_OFFLINE_REPLIES", 3)

# /stop 打断：向 orchestrator 发 StopTask 的超时。
# 该 RPC 是协作式的 —— orchestrator 只置位标志、不等暂停落地，所以这里不需要
# 长超时；设长了反而会在 orchestrator 卡住时把 CreateTask 一起拖住。
STOP_REQUEST_TIMEOUT_SECONDS = env_int("STOP_REQUEST_TIMEOUT_SECONDS", 10)

# 定时任务（timer_task）的结果**不允许**投递到的渠道。
# 官方 QQ bot 的主动消息受限（用户可关闭、有频控），定时推送走它基本等于丢失，
# 所以默认屏蔽 qqofficial：事件会按用户其它渠道的优先级转投（如 qq / web）。
SCHEDULED_EXCLUDED_CHANNELS = {
    item.strip()
    for item in os.getenv("SCHEDULER_EXCLUDED_CHANNELS", "qqofficial").split(",")
    if item.strip()
}
