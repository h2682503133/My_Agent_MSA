"""记忆组装：把「检索到的候选」变成「要注入上下文的记忆块」。

为什么单独一个文件：`viking_store.py` 负责跟 OpenViking 说话（发请求、收命中），
而"哪几条该注入、按什么顺序、摘要还是全文、草稿要不要用"是**策略**，
一旦要接 OpenViking 的类型/状态/时间去分层，策略会长得比传输层大，混在一起就没法测了。

设计约定：
- 这个模块**不联网、不读配置、不打印**，纯函数 + 数据类，方便单测（tools/memory_assembly_test.py）。
- 输入统一成 `MemoryItem`；OpenViking 命中/会话摘要/本地召回都用 `from_hit()` 归一化。
- 输出 `AssemblyResult`：`selected`（有序、已去重、已按预算裁剪）+ `dropped`（带原因，便于排障）。
- 默认策略尽量与改造前一致：无元数据时按分数排序、内容去重、上限 MAX_MEMORY_HITS —— 避免行为突变。

OpenViking 侧可用的分层信息（见 core/directories.py）：
  memories/{profile.md,identity.md,soul.md,preferences,entities,events,cases,patterns,
            tools,skills,trajectories,experiences}
  每条记忆带 MEMORY_FIELDS：memory_type / version / status(draft|staging|production|deprecated|archived) /
            links / source_extraction_id；目录另有 .abstract.md(L0) 与 .overview.md(L1)。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

# ── 记忆类型 ────────────────────────────────────────────────
# 顺序即"注入优先级"：常驻画像类在前，其次是偏好，最后是按查询命中的经验类。
TYPE_PROFILE = "profile"
TYPE_IDENTITY = "identity"
TYPE_SOUL = "soul"
TYPE_PREFERENCES = "preferences"
TYPE_ENTITIES = "entities"
TYPE_EVENTS = "events"
TYPE_CASES = "cases"
TYPE_PATTERNS = "patterns"
TYPE_TOOLS = "tools"
TYPE_SKILLS = "skills"
TYPE_TRAJECTORIES = "trajectories"
TYPE_EXPERIENCES = "experiences"
TYPE_SESSION = "session"
TYPE_RESOURCE = "resource"
TYPE_UNKNOWN = "unknown"

# 常驻：与查询无关，只要有就注入（很小，稳定人设/画像）
RESIDENT_TYPES = (TYPE_PROFILE, TYPE_IDENTITY, TYPE_SOUL)

# 默认优先级：同分时按这个顺序取；未列出的类型排在后面
DEFAULT_TYPE_ORDER = (
    TYPE_PROFILE, TYPE_IDENTITY, TYPE_SOUL,
    TYPE_PREFERENCES,
    TYPE_ENTITIES, TYPE_EVENTS, TYPE_CASES, TYPE_PATTERNS,
    TYPE_TOOLS, TYPE_SKILLS, TYPE_EXPERIENCES, TYPE_TRAJECTORIES,
    TYPE_SESSION, TYPE_RESOURCE, TYPE_UNKNOWN,
)

# 目录/URI 片段 → 类型（profile.md 这类单文件也走这里）
_SEGMENT_TO_TYPE = {
    "memories": TYPE_UNKNOWN,
    "profile.md": TYPE_PROFILE,
    "identity.md": TYPE_IDENTITY,
    "soul.md": TYPE_SOUL,
    "preferences": TYPE_PREFERENCES,
    "entities": TYPE_ENTITIES,
    "events": TYPE_EVENTS,
    "cases": TYPE_CASES,
    "patterns": TYPE_PATTERNS,
    "tools": TYPE_TOOLS,
    "skills": TYPE_SKILLS,
    "trajectories": TYPE_TRAJECTORIES,
    "experiences": TYPE_EXPERIENCES,
    "sessions": TYPE_SESSION,
    "resources": TYPE_RESOURCE,
}

# 状态：这些等于"已废弃"，默认不注入；draft 允许（当前集群里全是 draft），但会在输出里标出来
DEAD_STATUSES = ("deprecated", "archived")

# 注入标记：profile 单独成段（消费者把它放到历史免责框之上，当"系统设定"而不是历史）。
# 走内容前缀而不是加 proto 字段：gRPC 的 MemoryHit 只有 id/content/score/token_count，
# 加字段要三处 pb2 一起重生成并与旧版本锁步发布，前缀方案跨版本兼容。
PROFILE_MARKER = "【用户画像】"

# 来源优先级：同类型同分时，归档摘要 > 本会话命中 > 全库命中 > 本地召回。
# 目的：无元数据的老数据（类型全是 unknown）仍保持改造前的"来源顺序"行为，不出现行为突变。
SOURCE_ORDER = ("archive", "session", "global", "recall", "window", "other")


@dataclass
class MemoryItem:
    """一条候选记忆（已归一化，与 OpenViking 的返回结构解耦）。"""

    content: str
    score: float = 0.0
    memory_id: str = ""
    uri: str = ""
    memory_type: str = TYPE_UNKNOWN
    status: str = ""
    tags: tuple[str, ...] = ()
    created_at: str = ""
    token_count: int = 0
    source: str = ""          # session / global / recall / window / archive
    level: str = "full"       # abstract / overview / full
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def is_resident(self) -> bool:
        return self.memory_type in RESIDENT_TYPES

    def as_payload(self) -> dict[str, Any]:
        """给上游（orchestrator）的兼容结构：老字段不变，新字段只增不改。"""
        payload: dict[str, Any] = {
            "memory_id": self.memory_id,
            "content": self.content,
            "score": self.score,
            "token_count": self.token_count,
        }
        if self.memory_type not in ("", TYPE_UNKNOWN):
            payload["memory_type"] = self.memory_type
        if self.status:
            payload["status"] = self.status
        if self.uri:
            payload["uri"] = self.uri
        if self.source:
            payload["source"] = self.source
        if self.level and self.level != "full":
            payload["level"] = self.level
        return payload


@dataclass
class AssemblyResult:
    selected: list[MemoryItem] = field(default_factory=list)
    dropped: list[tuple[MemoryItem, str]] = field(default_factory=list)

    def payloads(self) -> list[dict[str, Any]]:
        return [item.as_payload() for item in self.selected]

    @property
    def by_type(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in self.selected:
            counts[item.memory_type] = counts.get(item.memory_type, 0) + 1
        return counts

    @property
    def drop_reasons(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for _, reason in self.dropped:
            counts[reason] = counts.get(reason, 0) + 1
        return counts


# ── 归一化 ──────────────────────────────────────────────────


def classify_uri(uri: str) -> str:
    """从 URI / 相对路径推记忆类型（`viking://user/u1/memories/profile.md` → profile）。"""
    text = str(uri or "").strip()
    if not text:
        return TYPE_UNKNOWN
    parts = [p for p in re.split(r"[\\/]+", text) if p and p not in ("viking:", "user", "agent")]
    # 先看文件名（profile.md / identity.md / soul.md），再看目录层级
    if parts:
        name = parts[-1].lower()
        if name in _SEGMENT_TO_TYPE:
            return _SEGMENT_TO_TYPE[name]
    for part in reversed(parts):
        key = str(part).lower()
        if key in _SEGMENT_TO_TYPE and key != "memories":
            return _SEGMENT_TO_TYPE[key]
    return TYPE_UNKNOWN


def _pick_type(hit: dict[str, Any]) -> str:
    for key in ("memory_type", "type"):
        value = str(hit.get(key) or "").strip().lower()
        if value:
            return value
    meta = hit.get("metadata") or hit.get("meta") or {}
    if isinstance(meta, dict):
        value = str(meta.get("memory_type") or "").strip().lower()
        if value:
            return value
    return classify_uri(str(hit.get("uri") or hit.get("path") or ""))


def _pick_status(hit: dict[str, Any]) -> str:
    for key in ("status",):
        value = str(hit.get(key) or "").strip().lower()
        if value:
            return value
    meta = hit.get("metadata") or hit.get("meta") or {}
    if isinstance(meta, dict):
        return str(meta.get("status") or "").strip().lower()
    return ""


def _pick_tags(hit: dict[str, Any]) -> tuple[str, ...]:
    raw = hit.get("tags")
    if raw is None:
        meta = hit.get("metadata") or hit.get("meta") or {}
        if isinstance(meta, dict):
            raw = meta.get("tags")
    if not isinstance(raw, list):
        return ()
    return tuple(str(item).strip() for item in raw if str(item).strip())


def _pick_text(hit: dict[str, Any]) -> tuple[str, str]:
    """返回 (正文, 层级)。优先摘要（省 token），再 overview，最后全文。"""
    for key, level in (("abstract", "abstract"), ("overview", "overview"), ("content", "full"), ("text", "full")):
        value = str(hit.get(key) or "").strip()
        if value:
            return value, level
    return "", "full"


def from_hit(hit: dict[str, Any], *, source: str = "") -> MemoryItem | None:
    """OpenViking 的命中 / 归档摘要 / 本地召回结果 → MemoryItem（空内容返回 None）。"""
    if not isinstance(hit, dict):
        return None
    content, level = _pick_text(hit)
    content = str(content or "").strip()
    if not content:
        return None
    try:
        score = float(hit.get("score", 0.0) or 0.0)
    except (TypeError, ValueError):
        score = 0.0
    try:
        token_count = int(hit.get("token_count", 0) or 0)
    except (TypeError, ValueError):
        token_count = 0
    return MemoryItem(
        content=content,
        score=score,
        memory_id=str(hit.get("memory_id") or hit.get("id") or hit.get("uri") or ""),
        uri=str(hit.get("uri") or hit.get("path") or ""),
        memory_type=_pick_type(hit),
        status=_pick_status(hit),
        tags=_pick_tags(hit),
        created_at=str(hit.get("created_at") or ""),
        token_count=token_count,
        source=source or str(hit.get("source") or ""),
        level=level,
    )


def from_hits(hits: Iterable[Any], *, source: str = "") -> list[MemoryItem]:
    items: list[MemoryItem] = []
    for hit in hits or []:
        item = from_hit(hit, source=source) if isinstance(hit, dict) else None
        if item is not None:
            items.append(item)
    return items


# ── 组装策略 ────────────────────────────────────────────────


def truncate_for_injection(text: str, limit: int) -> str:
    """按字符上限截断一条记忆（limit<=0 = 不截断）。

    为什么需要：OpenViking 侧没有文件级硬上限（模板里只有"每条 bullet < 30 词"这种提示），
    profile 这类 `merge_op: patch` 的文件会一直长（实测 17KB），命中后会整段进上下文。
    截断保留开头（画像/原则类信息密度最高在前半），并标注截掉了多少。
    """
    raw = str(text or "")
    cap = int(limit or 0)
    if cap <= 0 or len(raw) <= cap:
        return raw
    kept = raw[:cap]
    # 尽量在完整行/句处截断，避免半截词
    for sep in ("\n", "。", "；", "，", " "):
        cut = kept.rfind(sep)
        if cut > cap // 2:
            kept = kept[:cut]
            break
    return f"{kept.rstrip()}…（已截断 {len(raw) - len(kept)} 字）"


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _type_rank(memory_type: str, order: tuple[str, ...]) -> int:
    try:
        return order.index(memory_type)
    except ValueError:
        return len(order)


def _source_rank(source: str) -> int:
    key = str(source or "other").strip().lower()
    try:
        return SOURCE_ORDER.index(key)
    except ValueError:
        return len(SOURCE_ORDER)


def assemble(
    items: Iterable[MemoryItem],
    *,
    max_items: int = 5,
    max_chars: int = 0,
    max_item_chars: int = 0,
    min_score: float = 0.0,
    keep_statuses: Iterable[str] | None = None,
    drop_dead_statuses: bool = True,
    type_order: tuple[str, ...] = DEFAULT_TYPE_ORDER,
    per_type_limit: dict[str, int] | None = None,
    dedupe: bool = True,
    fallback: MemoryItem | None = None,
) -> AssemblyResult:
    """挑选要注入的记忆。

    规则（依次）：
    1. 空内容/低分（`score < min_score`，`score < 0` 的兜底标记不受限制）丢弃；
    2. `status` 为 deprecated/archived 丢弃（`drop_dead_statuses=False` 可保留）；
       给了 `keep_statuses` 就只保留这些状态（空串表示"无状态"也放行）；
    3. 单条按 `max_item_chars` 截断（0 = 不截断），截断后再参与去重/预算；
    4. 内容去重（保留分高的那条）；
    5. 按 (类型优先级, 来源优先级, 分数) 排序；常驻类型（profile/identity/soul）不受 `per_type_limit` 限制；
    6. 按 `per_type_limit` 与 `max_items`、`max_chars`（累计正文字符数，0=不限）裁剪。

    `fallback`：全部被裁掉时兜底注入的那一条（例如最近一条用户消息），
    会以 score=-1 标记、不受阈值/预算/单条上限限制地放进结果。
    """
    limit_map = dict(per_type_limit or {})
    keep = {str(s).strip().lower() for s in keep_statuses} if keep_statuses is not None else None

    candidates: list[MemoryItem] = []
    dropped: list[tuple[MemoryItem, str]] = []

    for item in items or []:
        if not str(item.content or "").strip():
            dropped.append((item, "empty"))
            continue
        if item.score < min_score and item.score >= 0:
            dropped.append((item, "low_score"))
            continue
        status = str(item.status or "").strip().lower()
        if drop_dead_statuses and status in DEAD_STATUSES:
            dropped.append((item, "dead_status"))
            continue
        if keep is not None and status not in keep:
            dropped.append((item, "status_filter"))
            continue
        item.content = truncate_for_injection(item.content, max_item_chars)
        candidates.append(item)

    if dedupe:
        best: dict[str, MemoryItem] = {}
        for item in candidates:
            key = _norm(item.content)
            current = best.get(key)
            if current is None or item.score > current.score:
                if current is not None:
                    dropped.append((current, "duplicate"))
                best[key] = item
            else:
                dropped.append((item, "duplicate"))
        candidates = list(best.values())

    candidates.sort(key=lambda it: (_type_rank(it.memory_type, type_order), _source_rank(it.source), -it.score))

    selected: list[MemoryItem] = []
    used_chars = 0
    type_counts: dict[str, int] = {}
    for item in candidates:
        cap = limit_map.get(item.memory_type)
        if cap is not None and not item.is_resident and type_counts.get(item.memory_type, 0) >= cap:
            dropped.append((item, "type_limit"))
            continue
        if len(selected) >= max_items:
            dropped.append((item, "max_items"))
            continue
        if max_chars and used_chars + len(item.content) > max_chars and selected:
            dropped.append((item, "max_chars"))
            continue
        selected.append(item)
        type_counts[item.memory_type] = type_counts.get(item.memory_type, 0) + 1
        used_chars += len(item.content)

    if not selected and fallback is not None and str(fallback.content or "").strip():
        selected = [fallback]

    return AssemblyResult(selected=selected, dropped=dropped)


def render(result: AssemblyResult, *, header: str = "") -> str:
    """把组装结果渲染成可注入文本（带类型标注，便于模型区分画像/经验/会话）。"""
    if not result.selected:
        return ""
    lines: list[str] = []
    if header:
        lines.append(header)
    for item in result.selected:
        tag = item.memory_type if item.memory_type != TYPE_UNKNOWN else "memory"
        if item.status and item.status != "production":
            tag += "/" + item.status
        lines.append(f"- [{tag}] {item.content}")
    return "\n".join(lines)
