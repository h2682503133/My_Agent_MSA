import os
from pathlib import Path


def _read_secret_file(env_name: str, default: str = "") -> str:
    """Read secret from file if env var points to a file path, else return env value."""
    value = os.getenv(env_name, "")
    if value and os.path.isfile(value):
        try:
            return Path(value).read_text().strip()
        except Exception:
            pass
    file_path = os.getenv(f"{env_name}_FILE", "")
    if file_path and os.path.isfile(file_path):
        try:
            return Path(file_path).read_text().strip()
        except Exception:
            pass
    return value


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return int(value)


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value.lower() in {"1", "true", "yes", "on"}


TOOL_RUNTIME_HOST = os.getenv("TOOL_RUNTIME_HOST", "0.0.0.0")
TOOL_RUNTIME_PORT = env_int("TOOL_RUNTIME_PORT", 5303)

# model-proxy（LLM / Embedding 代理）：fetch 语义召回（策略二）与 LLM 提取（策略三）使用
MODEL_PROXY_TARGET = os.getenv("MODEL_PROXY_TARGET", "model-proxy-service:5302")
MODEL_TIMEOUT_SECONDS = env_int("MODEL_TIMEOUT_SECONDS", 120)

WORKSPACE_DIR = os.getenv("WORKSPACE_DIR", "/app/workspace")
MAX_LIST_FILES = env_int("MAX_LIST_FILES", 500)
MAX_READ_BYTES = env_int("MAX_READ_BYTES", 1024 * 1024)
MAX_UPLOAD_BYTES = env_int("MAX_UPLOAD_BYTES", 48 * 1024 * 1024)
GRPC_MAX_MESSAGE_BYTES = env_int("GRPC_MAX_MESSAGE_BYTES", 64 * 1024 * 1024)
PORT_PROXY_RANGE = os.getenv("PORT_PROXY_RANGE", "5800-5899")
PORT_PROXY_BASE_URL = os.getenv("PORT_PROXY_BASE_URL", "http://localhost:8080/api/port")
DEFAULT_TIMEOUT_SECONDS = env_int("DEFAULT_TIMEOUT_SECONDS", 30)
WEB_SEARCH_MAX_RESULTS = env_int("WEB_SEARCH_MAX_RESULTS", 10)

# ---- fetch 体积控制与信息提取（fetch_tools.py）----
# 清洗后文本长度 <= FETCH_MAX_RAW_CHARS：直接返回原始文本
FETCH_MAX_RAW_CHARS = env_int("FETCH_MAX_RAW_CHARS", 1500)
# 清洗后文本长度 <= FETCH_MAX_FULL_TEXT_CHARS：返回完整清洗文本（不触发提取策略）
FETCH_MAX_FULL_TEXT_CHARS = env_int("FETCH_MAX_FULL_TEXT_CHARS", 6000)
# 分块参数（字符）：块大小 / 块间重叠
FETCH_CHUNK_SIZE = env_int("FETCH_CHUNK_SIZE", 600)
FETCH_CHUNK_OVERLAP = env_int("FETCH_CHUNK_OVERLAP", 100)
# 语义召回 Top-K（多个子问题合并去重后的上限）
FETCH_TOP_K = env_int("FETCH_TOP_K", 4)
# 大纲输出上限（字符）
FETCH_OUTLINE_MAX_CHARS = env_int("FETCH_OUTLINE_MAX_CHARS", 2000)
# 语义召回使用的 embedding 模型 profile（model_list.json 中的别名/模型名）
FETCH_EMBEDDING_PROFILE = os.getenv("FETCH_EMBEDDING_PROFILE", "default-embedding")
# LLM 提取（降级）使用的 chat 模型 profile
FETCH_EXTRACT_PROFILE = os.getenv("FETCH_EXTRACT_PROFILE", "default-reader")
# LLM 提取最多处理的块数（超出按关键词得分取前 N 块）
FETCH_LLM_EXTRACT_CHUNKS = env_int("FETCH_LLM_EXTRACT_CHUNKS", 10)

SKILL_ROOT_DIR = os.getenv("SKILL_ROOT_DIR", os.path.join(WORKSPACE_DIR, "skill"))

OPENVIKING_SERVER_URL = os.getenv("OPENVIKING_SERVER_URL", "http://openviking.agent.svc.cluster.local:1933")
OPENVIKING_API_KEY = _read_secret_file("OPENVIKING_API_KEY", "")
OPENVIKING_ACCOUNT = os.getenv("OPENVIKING_ACCOUNT", "my-agent")
# OpenViking root-key access to tenant-scoped APIs requires tenant + user context.
# Keep these defaults aligned with openviking-context-service's root-key ping style.
OPENVIKING_USER = os.getenv("OPENVIKING_USER", "system")
OPENVIKING_AGENT = os.getenv("OPENVIKING_AGENT", "skills")

CLAW_DOWNLOAD_MODE = os.getenv("CLAW_DOWNLOAD_MODE", "external-vm").lower()
CLAW_EXTERNAL_VM_HOST = os.getenv("CLAW_EXTERNAL_VM_HOST", "")
CLAW_EXTERNAL_VM_USER = os.getenv("CLAW_EXTERNAL_VM_USER", "")
CLAW_EXTERNAL_VM_PORT = env_int("CLAW_EXTERNAL_VM_PORT", 22)
CLAW_EXTERNAL_VM_SSH_KEY = os.getenv("CLAW_EXTERNAL_VM_SSH_KEY", "")
CLAW_EXTERNAL_VM_SKILL_ROOT_DIR = os.getenv("CLAW_EXTERNAL_VM_SKILL_ROOT_DIR", "/srv/nfs/my-agent/workspace/skill")
CLAW_EXTERNAL_VM_CLAWHUB_BIN = os.getenv("CLAW_EXTERNAL_VM_CLAWHUB_BIN", "clawhub")
CODEX_BIN_PATH = os.getenv("CODEX_BIN_PATH", "codex")
CODEX_EXTERNAL_VM_WORKSPACE = os.getenv("CODEX_EXTERNAL_VM_WORKSPACE", "/srv/nfs/my-agent/workspace")
CLAW_EXTERNAL_VM_STRICT_HOST_KEY_CHECKING = env_bool("CLAW_EXTERNAL_VM_STRICT_HOST_KEY_CHECKING", False)

# 功能开关：部署时未勾选对应功能则工具调用会提示"未启用"（默认开启，兼容旧部署）
ENABLE_CODEX = env_bool("ENABLE_CODEX", True)
ENABLE_CLAWHUB = env_bool("ENABLE_CLAWHUB", True)
# 技能知识库（OpenViking）开关：未勾选 openviking-server 时技能查询/导入会提示未启用
ENABLE_OPENVIKING = env_bool("ENABLE_OPENVIKING", True)

ENABLE_SHELL_TOOLS = env_bool("ENABLE_SHELL_TOOLS", True)

# PROCESS 长期事件记录存储：config PVC 挂载在 /app/system_prompts（orchestrator 侧同目录为 /app/config）
PROCESS_DIR = os.getenv("PROCESS_DIR", "/app/system_prompts/orchestrator/config/process")
