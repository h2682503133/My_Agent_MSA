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

MAX_AGENT_STEPS = int(os.getenv("MAX_AGENT_STEPS", "30"))
TOOL_TIMEOUT_SECONDS = int(os.getenv("TOOL_TIMEOUT_SECONDS", "120"))
MODEL_TIMEOUT_SECONDS = int(os.getenv("MODEL_TIMEOUT_SECONDS", "300"))
CONTEXT_TIMEOUT_SECONDS = int(os.getenv("CONTEXT_TIMEOUT_SECONDS", "30"))

# no-mock build: orchestrator always calls downstream services.
MOCK_DOWNSTREAM = False

AGENT_CONFIG_PATH = Path(os.getenv("AGENT_CONFIG_PATH", str(BASE_DIR / "config" / "agent_list.json")))
SYSTEM_PROMPT_DIR = Path(os.getenv("SYSTEM_PROMPT_DIR", str(BASE_DIR / "system_prompt")))
WORKSPACE_DIR = Path(os.getenv("WORKSPACE_DIR", str(BASE_DIR / "workspace")))

# 系统杂项设置（dashboard 写入）：当前仅 image_receive_enabled 图像接收开关
SYSTEM_SETTINGS_PATH = Path(
    os.getenv("SYSTEM_SETTINGS_PATH", str(BASE_DIR / "config" / "system_settings.json"))
)

# PROCESS 长期事件记录存储（按 <user_id>/<agent_id>.json 分片）
PROCESS_DIR = Path(os.getenv("PROCESS_DIR", str(BASE_DIR / "config" / "process")))

# ─── 世界书（World Info）──────────────────────────────────────
# 与 PROCESS 同级：/app/config/world_info/（NFS config/orchestrator/config/world_info/）
# - world_info.json：条目（scope 默认当前 agent，无全局）
# - groups.json：agent_id : 群组id 映射（"agent_groups" 字段）
# orchestrator 挂载可写；管理命令（世界书:add 等）与 tool-runtime 的
# worldinfo-* 工具直接原子写这里。
WORLD_INFO_DIR = Path(os.getenv("WORLD_INFO_DIR", str(BASE_DIR / "config" / "world_info")))
WORLD_INFO_PATH = Path(os.getenv("WORLD_INFO_PATH", str(WORLD_INFO_DIR / "world_info.json")))
WORLD_INFO_GROUPS_PATH = Path(os.getenv("WORLD_INFO_GROUPS_PATH", str(WORLD_INFO_DIR / "groups.json")))
# 注入预算（与 search_context 的 max_tokens 互不影响）
WORLD_INFO_MAX_TOKENS = int(os.getenv("WORLD_INFO_MAX_TOKENS", "1500"))
WORLD_INFO_MAX_ENTRIES = int(os.getenv("WORLD_INFO_MAX_ENTRIES", "20"))
# 世界书注入总开关（默认开启）
WORLD_INFO_ENABLED = os.getenv("WORLD_INFO_ENABLED", "true").lower() == "true"
