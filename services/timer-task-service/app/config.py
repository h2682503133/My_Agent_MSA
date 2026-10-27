import os


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return int(value)


TIMER_GRPC_HOST = os.getenv("TIMER_GRPC_HOST", "0.0.0.0")
TIMER_GRPC_PORT = env_int("TIMER_GRPC_PORT", 5103)

TIMER_TASK_DIR = os.getenv("TIMER_TASK_DIR", "./roaming/tasks")
TIMER_SCAN_INTERVAL_FAST = env_int("TIMER_SCAN_INTERVAL_FAST", 5)
TIMER_SCAN_INTERVAL_SLOW = env_int("TIMER_SCAN_INTERVAL_SLOW", 60)

# gRPC callback to task-scheduler for executing timer tasks
SCHEDULER_TARGET = os.getenv(
    "SCHEDULER_TARGET",
    "task-scheduler-service.agent.svc.cluster.local:5100",
)
SCHEDULER_GRPC_DEADLINE = env_int("SCHEDULER_GRPC_DEADLINE", 10)

# 「执行时段」之外的触发策略（第 6 参 window_str 给定后生效）。
#   realign（默认）：下一次触发若落在时段外，锚到下一个时段起点 + 一次重复偏移
#   skip          ：严格执行，错过就错过（固定间隔 + 时段时可能永远不执行）
# 兼容旧变量：TIMER_WINDOW_CATCH_UP=false 视为 skip。
def env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


TIMER_MISSED_WINDOW_POLICY = (
    os.getenv("TIMER_MISSED_WINDOW_POLICY", "").strip().lower()
    or ("skip" if not env_bool("TIMER_WINDOW_CATCH_UP", True) else "realign")
)
if TIMER_MISSED_WINDOW_POLICY not in {"realign", "skip"}:
    TIMER_MISSED_WINDOW_POLICY = "realign"
