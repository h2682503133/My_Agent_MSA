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
    # Also try default file path
    file_path = os.getenv(f"{env_name}_FILE", "")
    if file_path and os.path.isfile(file_path):
        try:
            return Path(file_path).read_text().strip()
        except Exception:
            pass
    return value

OPENVIKING_CONTEXT_GRPC_PORT = int(os.getenv("OPENVIKING_CONTEXT_GRPC_PORT", "5301"))
VIKING_DATA_DIR = Path(os.getenv("VIKING_DATA_DIR", "/app/viking_data"))
OPENVIKING_BACKEND = os.getenv("OPENVIKING_BACKEND", "server")
OPENVIKING_SERVER_URL = os.getenv("OPENVIKING_SERVER_URL", "http://openviking.agent.svc.cluster.local:1933")
OPENVIKING_API_KEY = _read_secret_file("OPENVIKING_API_KEY", "")
OPENVIKING_ROOT_API_KEY = _read_secret_file("OPENVIKING_ROOT_API_KEY", "")
OPENVIKING_ACCOUNT = os.getenv("OPENVIKING_ACCOUNT", "my-agent")
# "fixed" = 所有用户共用 OPENVIKING_ACCOUNT（默认，兼容旧行为）
# "user_id" = 每个用户的 OpenViking Account 就是其 user_id，画像按用户隔离
OPENVIKING_ACCOUNT_MODE = os.getenv("OPENVIKING_ACCOUNT_MODE", "fixed")
OPENVIKING_FILE_FALLBACK = os.getenv("OPENVIKING_FILE_FALLBACK", "false").lower() == "true"
MOCK_VIKING = os.getenv("MOCK_VIKING", "false").lower() == "true"
DEFAULT_MAX_MESSAGES = int(os.getenv("DEFAULT_MAX_MESSAGES", "6"))
DEFAULT_TOKEN_BUDGET = int(os.getenv("DEFAULT_TOKEN_BUDGET", "3000"))
USER_SERVICE_URL = os.getenv("USER_SERVICE_URL", "http://user-service.agent.svc.cluster.local:5204")

# ─── 世界书（World Info）──────────────────────────────────────
# 世界书是纯本地文件 + 关键词匹配，不依赖 OpenViking server / embedding。
# 挂载路径：openviking-context-service 挂载整个 config 根（无 subPath），
# 故 world_info 位于 config/orchestrator/config/world_info/ 时容器内路径为：
#   /app/config/orchestrator/config/world_info/world_info.json
WORLD_INFO_PATH = Path(os.getenv("WORLD_INFO_PATH", "/app/config/orchestrator/config/world_info/world_info.json"))
WORLD_INFO_GROUPS_PATH = Path(os.getenv("WORLD_INFO_GROUPS_PATH", "/app/config/orchestrator/config/world_info/groups.json"))
WORLD_INFO_MAX_TOKENS = int(os.getenv("WORLD_INFO_MAX_TOKENS", "1500"))
WORLD_INFO_MAX_ENTRIES = int(os.getenv("WORLD_INFO_MAX_ENTRIES", "20"))
