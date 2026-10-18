"""用户输入里的「RPG 结构化调用」解析（纯函数、无 IO、可单测）。

外部程序（文字 RPG 的驱动方）把一次调用所需的全部东西，作为**一段 JSON 文本**
放在用户输入（`task.content`）里；**整段必须就是一个 JSON 对象** —— 前后不夹别的正文，
`json.loads` 自己就会拒绝多余内容（这也是"全段为 `{}`"的强制方式，不需要额外校验）：

    {
      "exclude_block": ["tool", "world_info"],
      "insert": [
        {"at": "before_persona", "content": ["世界设定…"]},
        {"at": "before_history", "content": ["本场情势…", "旁白…"]}
      ],
      "present": ["alice", "bob"],
      "text": "本轮叙事 / 玩家输入"
    }

- `exclude_block`  **要屏蔽**的系统提示词块。只开放确实需要屏蔽的四个（见 `BLOCK_WHITELIST`），
  不存在"打开未声明的块"的写法 —— 那等于给运行期的 `@ref=`，会让 NPC 互读人设。
- `insert`  在指定位置插入自定义块。`content` 是字符串列表，**按数组顺序依次插入**；
  同一锚点可以出现多次（跨条目累积，顺序 = 声明顺序）。四个位置不必都有。
- `present` 本次事件在场的 NPC（由外部程序给出；缺省 → 空，调用方按"失败闭合"处理）。
- `text`    本轮叙事 / 玩家输入，解析后**取代** `task.content`。

两个阶段不要混
--------------
1. **发送阶段**：上面那些是**文本**（就在 content 里），本模块用 `json.loads` 还原，
   **不用正则**；
2. **填充阶段**：`insert` 的每条内容在插入提示词时，必须物化成
   `{"role": "system", "content": …}`（见 `insert_messages`）——提示词里不出现裸字符串。
   角色固定 `system`；除 `text` 之外没有需要 `user` 的地方。

插入内容的语义
--------------
插入的是**系统设定**，不是聊天记录。所以四个位置都不受历史免责框
（`HISTORY_DISCLAIMER`）的限制，也不额外加边界标记 —— 它本来就是拿来补充设定的。

关于"不影响原始服务"
--------------------
判定"这是不是结构化调用"：**是否出现任何一个已知键**（exclude_block / insert / present / text）。

- 不以 `{` 开头 / JSON 解析失败 / 顶层不是对象 / 一个已知键都没有
  → 返回 `None`：**这段内容当普通内容原样处理** —— 不丢弃、不报错、不做任何解析。
  普通聊天即使碰巧是合法 JSON 也不会被误吞。
- 一旦确认是结构化调用（出现了已知键），就**必须**有 `text`；其余字段但凡有问题一律
  **显式报错**，不静默降级（宁可吵，也不要出现"以为插进去了其实没插"）。
  注意这里与上面那条的分界：**JSON 层面解析不了 = 普通内容；解析得了但字段不合法 = 报错**。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

# 允许被屏蔽的块：只开放"确实需要屏蔽"的四个。
# - tool        NPC 不该有工具能力（外部接管重要事件与人工世界书编写 / 有限轮编写）
# - world_info  某些轮次不需要世界书
# - memory      框架自带的状态段（process / vars）
# - openviking  openviking 检索回来的历史块（会话摘要 / 相关记忆 / 对话历史）
#               注意：只屏蔽**注入**，不屏蔽写入 —— 事件照记，只是这一轮不喂给模型
BLOCK_WHITELIST = ("tool", "world_info", "memory", "openviking")

# 四个插入位置（与 context_builder 组装顺序对应）
INSERT_ANCHORS = ("before_persona", "before_history", "before_input", "after_input")

# 判定"这是不是结构化调用"用的已知键
_KNOWN_KEYS = ("exclude_block", "insert", "present", "text")


class PromptPlanError(Exception):
    """结构化调用**已确认成立**但内容不合法。调用方应显式失败，不要静默降级。"""


@dataclass
class PromptPlan:
    """一次结构化调用的解析结果。"""

    text: str
    exclude_block: tuple[str, ...] = ()
    # 锚点 → 按顺序的插入文本（填充阶段由 insert_messages 物化成 system 消息）
    inserts: dict[str, list[str]] = field(default_factory=dict)
    present: tuple[str, ...] = ()

    def excluded(self) -> frozenset[str]:
        return frozenset(self.exclude_block)

    def has_inserts(self) -> bool:
        return any(self.inserts.values())


def _require_str_list(value, *, key: str) -> list[str]:
    if not isinstance(value, list):
        raise PromptPlanError(f"{key} 必须是列表，实际是 {type(value).__name__}")
    items: list[str] = []
    for item in value:
        if not isinstance(item, str):
            raise PromptPlanError(f"{key} 的每一项必须是字符串，实际是 {type(item).__name__}")
        text = item.strip()
        if not text:
            raise PromptPlanError(f"{key} 里有空字符串")
        items.append(text)
    return items


def _dedup(items) -> tuple[str, ...]:
    """去重且保持首次出现顺序。"""
    seen: dict[str, None] = {}
    for item in items:
        seen.setdefault(item, None)
    return tuple(seen)


def parse_prompt_plan(content) -> PromptPlan | None:
    """解析用户输入里的结构化调用。

    返回 `None` 表示**不是**结构化调用：**这段内容当普通内容原样处理**
    （调用方必须原样继续，不丢弃、不报错、不改任何东西）；
    抛 `PromptPlanError` 表示确认是结构化调用但字段不合法。
    """
    if not isinstance(content, str):
        return None

    text = content.strip()
    # 廉价前置判断：不是对象开头就直接放弃（避免对整段叙事文本做 json 解析）
    if not text.startswith("{"):
        return None

    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return None

    if not isinstance(data, dict):
        return None
    if not any(key in data for key in _KNOWN_KEYS):
        return None

    # ── 到这里已经确认是结构化调用，之后一律显式报错 ──
    if "text" not in data:
        raise PromptPlanError(
            "结构化调用缺少必填键 text（除 text 外其余键都可缺省）"
        )
    plan_text = data["text"]
    if not isinstance(plan_text, str):
        raise PromptPlanError(f"text 必须是字符串，实际是 {type(plan_text).__name__}")

    exclude_block: tuple[str, ...] = ()
    raw_exclude = data.get("exclude_block")
    if raw_exclude is not None:
        names = _require_str_list(raw_exclude, key="exclude_block")
        unknown = [name for name in names if name not in BLOCK_WHITELIST]
        if unknown:
            raise PromptPlanError(
                f"exclude_block 里的 {'、'.join(repr(n) for n in unknown)} 不是可屏蔽的块；"
                f"当前只开放：{'、'.join(BLOCK_WHITELIST)}"
            )
        exclude_block = _dedup(names)

    inserts: dict[str, list[str]] = {}
    raw_insert = data.get("insert")
    if raw_insert is not None:
        if not isinstance(raw_insert, list):
            raise PromptPlanError(
                f"insert 必须是列表，实际是 {type(raw_insert).__name__}"
            )
        for index, entry in enumerate(raw_insert):
            if not isinstance(entry, dict):
                raise PromptPlanError(
                    f"insert[{index}] 必须是对象，实际是 {type(entry).__name__}"
                )
            anchor = entry.get("at")
            if anchor not in INSERT_ANCHORS:
                raise PromptPlanError(
                    f"insert[{index}].at={anchor!r} 不是插入位置；"
                    f"可选：{'、'.join(INSERT_ANCHORS)}"
                )
            if "content" not in entry:
                raise PromptPlanError(f"insert[{index}] 缺少 content")
            texts = _require_str_list(entry["content"], key=f"insert[{index}].content")
            if not texts:
                raise PromptPlanError(f"insert[{index}].content 不能是空列表")
            inserts.setdefault(anchor, []).extend(texts)

    present: tuple[str, ...] = ()
    raw_present = data.get("present")
    if raw_present is not None:
        present = _dedup(_require_str_list(raw_present, key="present"))

    return PromptPlan(
        text=plan_text,
        exclude_block=exclude_block,
        inserts=inserts,
        present=present,
    )


def insert_messages(plan: PromptPlan, anchor: str) -> list[dict]:
    """把某锚点的插入内容物化成提示词消息。

    填充阶段**始终**是 `{role, content}`（角色固定 `system`），提示词里不出现裸字符串。
    """
    return [{"role": "system", "content": text} for text in plan.inserts.get(anchor, ())]
