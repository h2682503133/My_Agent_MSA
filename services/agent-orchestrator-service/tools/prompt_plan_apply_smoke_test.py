"""结构化调用计划在**组装期**的应用：块屏蔽 + 四个锚点插入（纯本地，不依赖集群）。

覆盖：
  - 无计划（普通聊天）＝旧行为基线
  - exclude_block 的 tool / world_info / memory / openviking 各自关掉什么
  - 四个锚点的确切落点与顺序、插入内容一律 {role: system}
  - messages == base_head + base_tail（两阶段世界书走后者这条路径，必须一致）
  - 计划不污染实例上的 _prompt_parts（LRU 缓存，写进去会跨请求泄漏）
  - 插入内容不透明（不做 {agent_id} 替换）
  - 全局设定缺席时 before_persona 退化到最前

用法：
    python tools/prompt_plan_apply_smoke_test.py
"""

import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

SERVICE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVICE_DIR))

TMP = Path(tempfile.mkdtemp(prefix="prompt_plan_apply_"))
os.environ["SYSTEM_PROMPT_DIR"] = str(TMP / "system_prompt")
os.environ["AGENT_CONFIG_PATH"] = str(TMP / "agent_list.json")
os.environ["PROCESS_DIR"] = str(TMP / "process")

from app import config  # noqa: E402
from app.agent_config import load_agent_config  # noqa: E402
from app.context_builder import HISTORY_DISCLAIMER, ContextBuilder  # noqa: E402
from app.prompt_plan import parse_prompt_plan  # noqa: E402

FAILURES: list[str] = []
OK = 0


def ok(label: str) -> None:
    global OK
    OK += 1
    print(f"  OK   {label}")


def fail(label: str, detail: str = "") -> None:
    FAILURES.append(label)
    print(f"  FAIL {label}" + (f"\n       {detail}" if detail else ""))


def check(label: str, cond: bool, detail: str = "") -> None:
    ok(label) if cond else fail(label, detail)


# ── 造数据 ───────────────────────────────────────────────────
SP = config.SYSTEM_PROMPT_DIR


def write(rel: str, text: str) -> None:
    path = SP / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


GLOBAL_TEXT = "全局设定A"
SOUL_TEXT = "人设B"
TOOL_TEXT = "工具块C"
PROCESS_HEAD = "以下是你的长期事件记录（PROCESS）"
VARS_HEAD = "【当前状态变量】"
INPUT_HEAD = "以下为本次单轮对话内容"

write("GLOBAL_SETTING.md", GLOBAL_TEXT)
write("npc/SOUL.md", SOUL_TEXT)
write("npc/TOOL.md", TOOL_TEXT)
write("npc/world_info.md", "世界书声明D")
write("npc/PROCESS.md", "")
Path(os.environ["AGENT_CONFIG_PATH"]).write_text(
    json.dumps({"default": {"npc": {"files": ["SOUL.md", "TOOL.md", "world_info.md", "PROCESS.md"]}}},
               ensure_ascii=False),
    encoding="utf-8",
)
proc_path = config.PROCESS_DIR / "player" / "npc.json"
proc_path.parent.mkdir(parents=True, exist_ok=True)
proc_path.write_text(
    json.dumps({"items": [{"title": "事件", "content": "某事"}], "vars": {"hp": 10}}, ensure_ascii=False),
    encoding="utf-8",
)


class StubContextClient:
    def __init__(self):
        self.search_calls = 0

    def search_context(self, **kwargs):
        self.search_calls += 1
        return []


def make_agent():
    loader = SimpleNamespace(
        id="npc",
        user_id="player",
        config=load_agent_config(
            path=os.environ["AGENT_CONFIG_PATH"], user_id="player", agent_id="npc"
        ),
    )
    parts = ContextBuilder(loader).build_system_prompt()
    agent = SimpleNamespace(
        id="npc",
        user_id="player",
        config=loader.config,
        system_prompt=parts.messages,
        _identity_system_indexes=parts.identity_indexes,
        _when_system_rules=parts.when_rules,
        _tool_system_indexes=parts.tool_indexes,
        _global_system_index=parts.global_index,
        _soul_message_content=parts.soul_content,
        _world_info_override_content=parts.world_info_override,
        context_client=StubContextClient(),
    )
    return agent, parts


def build(plan=None, *, content="玩家推开了门"):
    agent, parts = make_agent()
    user = SimpleNamespace(id="player", session_id="rpg_world1")
    task = SimpleNamespace(
        metadata={}, user=user, caller=user, content=content,
        main_memory=[], images=[], prompt_plan=plan,
    )
    bundle = ContextBuilder(agent).build_send_context(
        task, content=content, dialog_images=[], image_enabled=True
    )
    return agent, parts, bundle


def plan_of(payload):
    parsed = parse_prompt_plan(json.dumps(payload, ensure_ascii=False))
    assert parsed is not None, payload
    return parsed


def contents(bundle):
    return [m.get("content", "") for m in bundle.messages]


def find(c: list, text: str) -> int:
    return c.index(text) if text in c else -1


# ── 基线：无计划＝旧行为 ─────────────────────────────────────
print("── 基线（无计划 / 普通聊天）──")
_agent, parts, base = build(None)
c = contents(base)
check("段数 8（全局设定/人设/TOOL/PROCESS/免责框/vars/输入提示/输入）", len(c) == 8, f"{c}")
check("全局设定在最前", c[0] == GLOBAL_TEXT, f"{c[0]!r}")
check("人设第二", c[1] == SOUL_TEXT, f"{c[1]!r}")
check("TOOL 段第三", c[2] == TOOL_TEXT, f"{c[2]!r}")
check("PROCESS 段存在（紧跟系统提示词）", c[3].startswith(PROCESS_HEAD), f"{c[3]!r}")
check("免责框随后", c[4] == HISTORY_DISCLAIMER, f"{c[4]!r}")
check("vars 段存在", c[5].startswith(VARS_HEAD), f"{c[5]!r}")
check("本轮输入提示", c[6] == INPUT_HEAD, f"{c[6]!r}")
check("本轮输入内容", c[7] == "<player>玩家推开了门", f"{c[7]!r}")
check("两阶段世界书默认开启", base.two_phase_world_info is True)
check("messages == base_head + base_tail",
      base.messages == base.base_head + base.base_tail)
check("parts.global_index == 0", parts.global_index == 0, f"{parts.global_index}")
check("parts.tool_indexes == [2]", parts.tool_indexes == [2], f"{parts.tool_indexes}")
check("parts.messages 只有三个段", len(parts.messages) == 3, f"{len(parts.messages)}")


# ── exclude_block ───────────────────────────────────────────
print("── exclude_block：tool / world_info / memory / openviking ──")
_agent, _parts, b = build(plan_of({"text": "x", "exclude_block": ["tool"]}))
c = contents(b)
check("tool：TOOL 段消失", TOOL_TEXT not in c, f"{c}")
check("tool：段数 8 → 7", len(c) == 7, f"{len(c)}")
check("tool：其余段顺序不变", c[0] == GLOBAL_TEXT and c[1] == SOUL_TEXT and c[2].startswith(PROCESS_HEAD), f"{c}")

_agent, _parts, b = build(plan_of({"text": "x", "exclude_block": ["world_info"]}))
check("world_info：两阶段被关掉", b.two_phase_world_info is False)
check("world_info：messages 与基线完全一致（它不是段）",
      contents(b) == contents(base))

_agent, _parts, b = build(plan_of({"text": "x", "exclude_block": ["memory"]}))
c = contents(b)
check("memory：PROCESS 段消失", not any(x.startswith(PROCESS_HEAD) for x in c), f"{c}")
check("memory：vars 段消失", not any(x.startswith(VARS_HEAD) for x in c), f"{c}")
check("memory：段数 8 → 6", len(c) == 6, f"{len(c)}")

agent_ov, _parts, b = build(plan_of({"text": "x", "exclude_block": ["openviking"]}))
c = contents(b)
check("openviking：免责框消失", HISTORY_DISCLAIMER not in c, f"{c}")
check("openviking：段数 8 → 7", len(c) == 7, f"{len(c)}")
check("openviking：连检索都不发（省一次 gRPC）", agent_ov.context_client.search_calls == 0,
      f"{agent_ov.context_client.search_calls}")
check("openviking：系统提示词与 vars 不动",
      c[0] == GLOBAL_TEXT and c[1] == SOUL_TEXT and c[2] == TOOL_TEXT
      and any(x.startswith(PROCESS_HEAD) for x in c)
      and any(x.startswith(VARS_HEAD) for x in c), f"{c}")
check("openviking：两阶段世界书不受影响", b.two_phase_world_info is True)

_agent, _parts, b = build(plan_of({"text": "x", "exclude_block": ["tool", "memory"]}))
check("tool + memory 同用：段数 5", len(contents(b)) == 5, f"{len(b.messages)}")

_agent, _parts, b = build(plan_of({"text": "x", "exclude_block": ["tool", "world_info", "memory"]}))
c = contents(b)
check("三个全关：剩 5 条（world_info 不是段，与 tool+memory 同结果）",
      len(c) == 5, f"{c}")
check("三个全关：TOOL / PROCESS / vars 都不在",
      TOOL_TEXT not in c
      and not any(x.startswith(PROCESS_HEAD) for x in c)
      and not any(x.startswith(VARS_HEAD) for x in c), f"{c}")
check("三个全关：两阶段关闭", b.two_phase_world_info is False)

_agent, _parts, b = build(plan_of({
    "text": "x", "exclude_block": ["tool", "world_info", "memory", "openviking"]}))
c = contents(b)
check("四个全关：剩 4 条（全局设定/人设/输入提示/输入）", len(c) == 4, f"{c}")
check("四个全关：只剩人设骨架",
      c[0] == GLOBAL_TEXT and c[1] == SOUL_TEXT
      and c[2] == INPUT_HEAD and c[3] == "<player>玩家推开了门", f"{c}")
check("四个全关：两阶段关闭", b.two_phase_world_info is False)


# ── 四个锚点插入 ─────────────────────────────────────────────
print("── 四个锚点插入 ──")
P1, P2, H, I, A = "设定P1", "设定P2", "设定H", "设定I", "设定A"
plan = plan_of({"text": "x", "insert": [
    {"at": "before_persona", "content": [P1, P2]},
    {"at": "before_history", "content": [H]},
    {"at": "before_input", "content": [I]},
    {"at": "after_input", "content": [A]},
]})
_agent, _parts, b = build(plan)
c = contents(b)
check("before_persona：紧跟全局设定、两条保序",
      c[0] == GLOBAL_TEXT and c[1] == P1 and c[2] == P2 and c[3] == SOUL_TEXT, f"{c}")
check("before_persona 不跨过 TOOL 段", c[4] == TOOL_TEXT, f"{c}")
check("before_history：在 PROCESS 之后、免责框之前",
      c[find(c, H) - 1].startswith(PROCESS_HEAD) and c[find(c, H) + 1] == HISTORY_DISCLAIMER, f"{c}")
check("before_input：紧贴本轮输入之前", c[find(c, I) + 1] == INPUT_HEAD, f"{c}")
check("after_input：在最后", c[-1] == A, f"{c}")
check("插入段一律 {role: system, content}",
      all(set(m) == {"role", "content"} and m["role"] == "system"
          for m in b.messages if m.get("content") in (P1, P2, H, I, A)))
check("messages == base_head + base_tail（插入后仍成立）",
      b.messages == b.base_head + b.base_tail)
check("插入数量正确（4 个锚点 5 条）", len(b.messages) == len(base.messages) + 5, f"{len(b.messages)}")

# ── 不污染实例缓存 ───────────────────────────────────────────
_agent2, parts2, _b2 = build(plan_of({"text": "x", "insert": [
    {"at": "before_persona", "content": ["污染检查"]}]}))
check("计划不写进实例 _prompt_parts（仍是三段）", len(parts2.messages) == 3, f"{len(parts2.messages)}")
check("计划不写进实例 _prompt_parts（内容未变）",
      [m["content"] for m in parts2.messages] == [GLOBAL_TEXT, SOUL_TEXT, TOOL_TEXT],
      f"{[m['content'] for m in parts2.messages]}")

# ── 插入内容不透明 ───────────────────────────────────────────
_agent3, _parts3, b3 = build(plan_of({"text": "x", "insert": [
    {"at": "before_input", "content": ["你是 {agent_id}"]}]}))
check("插入内容不做 {agent_id} 替换（不透明）",
      contents(b3)[find(contents(b3), "你是 {agent_id}")] == "你是 {agent_id}")

# ── 全局设定缺席时 before_persona 退化 ────────────────────────
print("── 全局设定缺席时 before_persona 退化为最前 ──")
_global_path = SP / "GLOBAL_SETTING.md"
_saved = _global_path.read_text(encoding="utf-8")
_global_path.unlink()
try:
    _agent4, parts4, b4 = build(plan_of({"text": "x", "insert": [
        {"at": "before_persona", "content": ["设定P"]}]}))
    c4 = contents(b4)
    check("global_index == -1", parts4.global_index == -1, f"{parts4.global_index}")
    check("插入落在最前", c4[0] == "设定P", f"{c4}")
    check("人设紧随其后", c4[1] == SOUL_TEXT, f"{c4}")
finally:
    _global_path.write_text(_saved, encoding="utf-8")

# ── 无计划 vs 空计划（只有 text）等价 ────────────────────────
_agent5, _parts5, b5 = build(plan_of({"text": "我们走吧"}))
check("空计划（只有 text）与无计划完全等价", contents(b5) == contents(base))
check("空计划时两阶段仍开启", b5.two_phase_world_info is True)


print()
if FAILURES:
    print(f"FAILED {len(FAILURES)} / {OK + len(FAILURES)}")
    for name in FAILURES:
        print(f"  - {name}")
    sys.exit(1)
print(f"ALL OK — {OK} checks")
