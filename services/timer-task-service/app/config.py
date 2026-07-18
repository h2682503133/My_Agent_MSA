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
