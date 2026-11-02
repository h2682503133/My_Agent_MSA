import json
import os
import re
from pathlib import Path

from app.logger import debug_log

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

# ─── 记忆组装（memory_assembly.py）──────────────────────────────
# 组装层负责"哪几条注入、什么顺序、摘要还是全文"，这里只放开关：
# 1) 字符预算：0 = 不限（与改造前一致）；设了之后按顺序累加到超限为止
MEMORY_ASSEMBLY_MAX_CHARS = int(os.getenv("MEMORY_ASSEMBLY_MAX_CHARS", "0"))
# 1b) 单条上限（字符）：一条记忆最多注入多少字，超出截断（OpenViking 侧没有硬上限，
#     profile 这类 patch 累积的文件能长到 17KB，不截断就会整段进 prompt）
MEMORY_ITEM_MAX_CHARS = int(os.getenv("MEMORY_ITEM_MAX_CHARS", "1200"))
# 2) 每类上限，例如 "experiences=2,cases=1"；不写 = 不按类型限制（与改造前一致）
#    作用：别让向量分数把所有名额都占给同一类（例如全是 experiences）
MEMORY_ASSEMBLY_PER_TYPE = os.getenv("MEMORY_ASSEMBLY_PER_TYPE", "")
# 3) 是否丢弃 status=deprecated/archived 的记忆（默认丢；draft 仍然保留）
MEMORY_DROP_DEAD_STATUSES = os.getenv("MEMORY_DROP_DEAD_STATUSES", "true").lower() == "true"


def memory_per_type_limits() -> dict[str, int]:
    """解析 MEMORY_ASSEMBLY_PER_TYPE（"experiences=2,cases=1"）→ {类型: 上限}。"""
    limits: dict[str, int] = {}
    for chunk in str(MEMORY_ASSEMBLY_PER_TYPE or "").split(","):
        name, _, raw = chunk.partition("=")
        key = name.strip().lower()
        if not key:
            continue
        try:
            value = int(str(raw).strip())
        except (TypeError, ValueError):
            continue
        if value > 0:
            limits[key] = value
    return limits

USER_SERVICE_URL = os.getenv("USER_SERVICE_URL", "http://user-service.agent.svc.cluster.local:5204")

# ─── 会话记忆策略（memory_policy，新建会话时带；存量会话不受影响）──────
# 为什么要它：OpenViking 每次 commit 默认会生成一份 7 段 Working Memory（约 45k tokens、
# 落盘 142KB 快照），我们从不读它 → 纯成本。默认关掉 WM，只保留"记忆抽取"这条路
# （memories/*.md 与 peers/*）。依据：session.py 里 _run_archive_summary() 只被
# memory_policy.working_memory.enabled 门控，抽取向是另一条独立路径。
OPENVIKING_WM_ENABLED = os.getenv("OPENVIKING_WM_ENABLED", "false").lower() == "true"
# 抽取哪些类型（OpenViking 只认 memory_policy.memory_types；experiences 会连带 cases/trajectories）
OPENVIKING_MEMORY_TYPES = os.getenv(
    "OPENVIKING_MEMORY_TYPES", "profile,preferences,entities,events,experiences"
)
OPENVIKING_SELF_ENABLED = os.getenv("OPENVIKING_SELF_ENABLED", "true").lower() == "true"
OPENVIKING_PEER_ENABLED = os.getenv("OPENVIKING_PEER_ENABLED", "true").lower() == "true"
# 消息的 peer_id 取什么：agent（默认，= 本轮交互的 agent/角色）/ off（不传，回到改造前）
OPENVIKING_PEER_ID_MODE = os.getenv("OPENVIKING_PEER_ID_MODE", "agent").strip().lower()
# 逃生口：直接给完整 JSON（原样透传，不再拼装）；给了就忽略上面四个变量
OPENVIKING_MEMORY_POLICY = os.getenv("OPENVIKING_MEMORY_POLICY", "").strip()

# OpenViking 只接受这四个键（services/.../session/memory_policy.py: _POLICY_KEYS）
_MEMORY_POLICY_KEYS = ("self", "peer", "memory_types", "working_memory")


def build_memory_policy(
    *,
    working_memory: bool = False,
    memory_types: str = "",
    self_enabled: bool = True,
    peer_enabled: bool = True,
) -> dict:
    """拼 memory_policy（纯函数，方便单测）。空值 → 不写该键，交回 OpenViking 默认。"""
    policy: dict = {
        "working_memory": {"enabled": bool(working_memory)},
        "self": {"enabled": bool(self_enabled)},
        "peer": {"enabled": bool(peer_enabled)},
    }
    types = [item.strip().lower() for item in re.split(r"[,，、;；\s]+", str(memory_types or "")) if item.strip()]
    if types:
        policy["memory_types"] = types
    return policy


def memory_policy() -> dict:
    """本次进程要给新会话用的 memory_policy；返回 {} 表示"不加策略，交给默认"。"""
    if OPENVIKING_MEMORY_POLICY:
        try:
            data = json.loads(OPENVIKING_MEMORY_POLICY)
        except Exception as exc:  # noqa: BLE001
            debug_log(f"[memory_policy] OPENVIKING_MEMORY_POLICY 不是合法 JSON，改用默认拼装: {exc}")
        else:
            if isinstance(data, dict):
                unknown = set(data) - set(_MEMORY_POLICY_KEYS)
                if unknown:
                    debug_log(f"[memory_policy] 忽略未知键 {sorted(unknown)}（OpenViking 只认 4 个键）")
                    data = {k: v for k, v in data.items() if k in _MEMORY_POLICY_KEYS}
                return data
            debug_log("[memory_policy] OPENVIKING_MEMORY_POLICY 必须是对象，改用默认拼装")
    return build_memory_policy(
        working_memory=OPENVIKING_WM_ENABLED,
        memory_types=OPENVIKING_MEMORY_TYPES,
        self_enabled=OPENVIKING_SELF_ENABLED,
        peer_enabled=OPENVIKING_PEER_ENABLED,
    )

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
