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
# ─── 记忆语义检索相似度阈值 ──────────────────────────────────
# session 内检索：score 低于该值丢弃（默认 0.4，滤掉明显噪音）
# 全局兜底检索（search，跨 session 意图分析）：阈值更严（默认 0.5）
CONTEXT_SIMILARITY_THRESHOLD = float(os.getenv("CONTEXT_SIMILARITY_THRESHOLD", "0.4"))
GLOBAL_SIMILARITY_THRESHOLD = float(os.getenv("GLOBAL_SIMILARITY_THRESHOLD", "0.5"))
# 达到阈值进入上下文的记忆条数上限（默认 5）；未达阈值时保底输出最近一条消息
MAX_MEMORY_HITS = int(os.getenv("MAX_MEMORY_HITS", "5"))
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

# ─── 未 commit 会话内容的向量召回（session_recall.py）──────────────
# 会话 messages.jsonl 不被 OpenViking 建索引，窗口外、尚未 commit 的消息会成为盲区；
# 这里在本地用 embedding-service（唯一向量计算入口）补一层相似度召回。
# 本服务不硬编码 ollama / OpenAI 地址 —— provider 由 embedding-service 决定。
EMBEDDING_SERVICE_TARGET = os.getenv("EMBEDDING_SERVICE_TARGET", "embedding-service:5303")
EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "qwen3-embedding:0.6b")
EMBEDDING_TIMEOUT_SECONDS = float(os.getenv("EMBEDDING_TIMEOUT_SECONDS", "30"))
SESSION_RECALL_ENABLED = os.getenv("SESSION_RECALL_ENABLED", "true").lower() == "true"
SESSION_RECALL_TOP_K = int(os.getenv("SESSION_RECALL_TOP_K", "3"))
SESSION_RECALL_MIN_SCORE = float(os.getenv("SESSION_RECALL_MIN_SCORE", "0.45"))
SESSION_RECALL_MAX_MESSAGES = int(os.getenv("SESSION_RECALL_MAX_MESSAGES", "40"))
# 单轮最多补算多少条向量（冷启动时分摊到多轮，避免一轮等十几秒）
SESSION_RECALL_EMBED_MAX_PER_CALL = int(os.getenv("SESSION_RECALL_EMBED_MAX_PER_CALL", "16"))
SESSION_RECALL_EMBED_BATCH = int(os.getenv("SESSION_RECALL_EMBED_BATCH", "16"))
# 等召回结果的总超时：超过就放弃本次命中（任务留后台继续跑，顺手把缓存热起来）
SESSION_RECALL_TOTAL_TIMEOUT_SECONDS = float(os.getenv("SESSION_RECALL_TOTAL_TIMEOUT_SECONDS", "3"))
# 向量缓存按用户分桶：每桶条数上限 / 最多保留多少个用户（LRU 整桶淘汰）
SESSION_RECALL_CACHE_PER_USER = int(os.getenv("SESSION_RECALL_CACHE_PER_USER", "256"))
SESSION_RECALL_CACHE_MAX_USERS = int(os.getenv("SESSION_RECALL_CACHE_MAX_USERS", "64"))
