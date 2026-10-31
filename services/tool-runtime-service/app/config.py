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

# ---- web-search（web_search.py）----
# 主通道：Pod 内的 SearXNG sidecar
SEARXNG_URL = os.getenv("SEARXNG_URL", "http://localhost:8080")
# 回落通道：SearXNG 引擎池依赖境外站点，实测宿主机出网代理抖动时整池不可达，
# 而 SearXNG 仍返回 200 且 results 为空，与「确实没有结果」不可区分。
# 命中 0 条时回落到 Bing 中国站。
WEB_SEARCH_CN_FALLBACK = env_bool("WEB_SEARCH_CN_FALLBACK", True)
CN_SEARCH_BASE_URL = os.getenv("CN_SEARCH_BASE_URL", "https://cn.bing.com")
# 回落通道独立超时：不能沿用请求级 timeout（生产为 600s），否则一旦卡住会长时间占用
# 一个 gRPC worker。
CN_SEARCH_TIMEOUT_SECONDS = env_int("CN_SEARCH_TIMEOUT_SECONDS", 15)

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

# ─── dsh（DeepSeek Harness）外部大脑 ──────────────────────────
# 本服务充当 OneBot v11 反向 WS 客户端，拨 dsh-onebot-qq 的专用通道。
DSH_ONEBOT_WS = os.getenv("DSH_ONEBOT_WS", "ws://host.docker.internal:8644/ws")
DSH_ONEBOT_TOKEN = os.getenv("DSH_ONEBOT_TOKEN", "")
DSH_SELF_ID = env_int("DSH_SELF_ID", 10001)
# 发信人 id：必须写进 dsh 侧 adminUsers（插件 /workspace 仅管理员可用）
DSH_SENDER_ID = env_int("DSH_SENDER_ID", 10086)
DSH_REPLY_TIMEOUT_SECONDS = env_int("DSH_REPLY_TIMEOUT_SECONDS", 300)
# 走共用会话（默认 chat，绑在你正在聊的那条）时的等待上限：那会话同时只能跑一轮
DSH_SHARED_CHAT_TIMEOUT_SECONDS = env_int("DSH_SHARED_CHAT_TIMEOUT_SECONDS", 300)
DSH_MERGE_MILLIS = env_int("DSH_MERGE_MILLIS", 2500)
# 会话权限预设：留空则不切。workspace-noapproval = 只写工作区 + 提权请求自动拒绝（不阻塞）
# 请求里没声明工作区时用的那条 chat（按名称绑定到你在 Web 里聊的会话）；
# 留空则按 session/user 派生。
DSH_DEFAULT_CHAT = os.getenv("DSH_DEFAULT_CHAT", "group:548018")
# 默认会话的工作目录（dsh 侧本地路径；留空=沿用插件配置，UNC 路径会导致 pwsh 不可用）
DSH_DEFAULT_CHAT_WORKSPACE = os.getenv("DSH_DEFAULT_CHAT_WORKSPACE", "")
# 默认权限预设（留空 = 不在请求里声明 perm=<预设> 就不发 /permission）
DSH_PERMISSION_PRESET = os.getenv("DSH_PERMISSION_PRESET", "")
# 绑定到已有 dsh 会话（不新建 onebot-* 会话）：填 session id，空 = 用 chat 派生会话
DSH_TARGET_SESSION = os.getenv("DSH_TARGET_SESSION", "")
# dsh 侧看到的"同一个工作区"的根（Windows 路径/UNC）；留空则沿用 dsh 侧默认工作区
DSH_WORKSPACE_ROOT = os.getenv("DSH_WORKSPACE_ROOT", "")
# ─────────────────────────────────────────────────────────────

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
# OpenViking 统一收束口（openviking-context-service）：viking-search 走它检索
OPENVIKING_CONTEXT_TARGET = os.getenv(
    "OPENVIKING_CONTEXT_TARGET", "openviking-context-service:5301"
)

ENABLE_SHELL_TOOLS = env_bool("ENABLE_SHELL_TOOLS", True)

# ---- image-view（image_tools.py）----
# 单张图片字节上限：base64 膨胀 4/3，7.5MB 原文 ≈ 10MB data URL，
# 与主流多模态接口的单图上限对齐；超限直接报错，不丢给上游去失败。
IMAGE_VIEW_MAX_BYTES = env_int("IMAGE_VIEW_MAX_BYTES", int(7.5 * 1024 * 1024))
# 远程 URL 下载超时（image-view 支持直接传 http(s) 链接）
IMAGE_VIEW_TIMEOUT_SECONDS = env_int("IMAGE_VIEW_TIMEOUT_SECONDS", 30)

# PROCESS 长期事件记录存储：config PVC 挂载在 /app/system_prompts（orchestrator 侧同目录为 /app/config）
PROCESS_DIR = os.getenv("PROCESS_DIR", "/app/system_prompts/orchestrator/config/process")

# 世界书（World Info）存储：与 PROCESS 同级（orchestrator 侧同目录为 /app/config/world_info）
WORLD_INFO_DIR = os.getenv("WORLD_INFO_DIR", "/app/system_prompts/orchestrator/config/world_info")

# 回复关键词 → 自动配图规则（与 orchestrator 共用同一份 NFS 文件）
REPLY_IMAGES_DIR = os.getenv(
    "REPLY_IMAGES_DIR", "/app/system_prompts/orchestrator/config/.reply_images"
)
# 回复配图用的图床地址：必须是**集群内**可达的（适配器按 URL 取图）
REPLY_IMAGE_BASE_URL = os.getenv(
    "REPLY_IMAGE_BASE_URL", "http://image-assets-service/assets"
)

# 图床 PVC 在容器里的挂载点（回复配图、本地图转 URL 都往这里写）
IMAGE_ASSET_DIR = os.getenv("IMAGE_ASSET_DIR", "/app/assets/images")
# 图床对外 URL 前缀（image-url / send-image 用；必须是集群内可达地址）
IMAGE_BASE_URL = os.getenv("IMAGE_BASE_URL", "http://image-assets-service/assets")

# 浏览器：优先用 Playwright MCP（浏览器由 MCP 托管）；留空则退回 CDP 直连宿主 Edge。
# MCP 端点要能从 pod 访问（通常经「发布端口 → 宿主中继 → MCP 的 9333」）。
BROWSER_MCP_URL = os.getenv("BROWSER_MCP_URL", "")
WORLD_INFO_PATH = os.getenv("WORLD_INFO_PATH", os.path.join(WORLD_INFO_DIR, "world_info.json"))
WORLD_INFO_GROUPS_PATH = os.getenv("WORLD_INFO_GROUPS_PATH", os.path.join(WORLD_INFO_DIR, "groups.json"))
