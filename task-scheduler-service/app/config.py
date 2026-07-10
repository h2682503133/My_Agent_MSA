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
