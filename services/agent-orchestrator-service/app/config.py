import os
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
BASE_DIR = APP_DIR.parent

ORCHESTRATOR_GRPC_PORT = int(os.getenv("ORCHESTRATOR_GRPC_PORT", "5300"))

OPENVIKING_CONTEXT_TARGET = os.getenv(
    "OPENVIKING_CONTEXT_TARGET",
    "openviking-context-service:5301",
)

MODEL_PROXY_TARGET = os.getenv(
    "MODEL_PROXY_TARGET",
    "model-proxy-service:5302",
)

TOOL_RUNTIME_TARGET = os.getenv(
    "TOOL_RUNTIME_TARGET",
    "tool-runtime-service:5303",
)

TIMER_TASK_TARGET = os.getenv(
    "TIMER_TASK_TARGET",
    "timer-task-service.agent.svc.cluster.local:5103",
)

MAX_AGENT_STEPS = int(os.getenv("MAX_AGENT_STEPS", "20"))
TOOL_TIMEOUT_SECONDS = int(os.getenv("TOOL_TIMEOUT_SECONDS", "120"))
MODEL_TIMEOUT_SECONDS = int(os.getenv("MODEL_TIMEOUT_SECONDS", "300"))
CONTEXT_TIMEOUT_SECONDS = int(os.getenv("CONTEXT_TIMEOUT_SECONDS", "30"))

# no-mock build: orchestrator always calls downstream services.
MOCK_DOWNSTREAM = False

AGENT_CONFIG_PATH = Path(os.getenv("AGENT_CONFIG_PATH", str(BASE_DIR / "config" / "agent_list.json")))
SYSTEM_PROMPT_DIR = Path(os.getenv("SYSTEM_PROMPT_DIR", str(BASE_DIR / "system_prompt")))
WORKSPACE_DIR = Path(os.getenv("WORKSPACE_DIR", str(BASE_DIR / "workspace")))
