"""结构化调用的收尾扇出写入：纯本地冒烟测试（不依赖集群 / openviking）。

覆盖：
  - 无计划＝普通聊天：只写一次，正文不加前缀、不带 metadata（与改造前完全一致）
  - 有计划：按在场列表逐人各写一份；发言者永远在内；正文首部写真实发言者并声明
    author_in_content；user_message 是摘掉指令区后的 text
  - 在场列表为空 → 失败闭合（只写发言者）
  - 某一份写失败 → 其余照写（不中断、不静默）
  - 结构不变量：写入内容强制 intermediate（以发言者本轮自己的产出为准）
  - 跨服务键名一致性（orchestrator ↔ openviking-context-service）

用法：
    python tools/prompt_plan_fanout_smoke_test.py
"""

import json
import os
import sys
import tempfile
import types
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVICE_DIR))

TMP = Path(tempfile.mkdtemp(prefix="prompt_plan_fanout_"))
os.environ["SYSTEM_PROMPT_DIR"] = str(TMP / "system_prompt")
os.environ["AGENT_CONFIG_PATH"] = str(TMP / "agent_list.json")
os.environ["PROCESS_DIR"] = str(TMP / "process")

# agent_runtime 会 import 需要「生成的 proto」的客户端；本测试不用它们，
# 按 interject_smoke_test 的同样做法先把缺失模块占位，避免去跑 gen_proto.sh。
for _name in ("timer_task_pb2", "timer_task_pb2_grpc"):
    sys.modules.setdefault(_name, types.ModuleType(_name))

from app import agent_runtime as ar  # noqa: E402
from app.agent_runtime import AgentRuntime  # noqa: E402
from app.context_client import AUTHOR_IN_CONTENT_KEY  # noqa: E402
from app.prompt_plan import PromptPlan, parse_prompt_plan  # noqa: E402
from app.task_runtime import RuntimeUser, TaskRuntime  # noqa: E402

# 日志是给生产排查用的，冒烟测试里只会淹没断言结果
ar.debug_log = lambda *a, **k: None
ar.chat_log = lambda *a, **k: None

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


class Recorder:
    """替身 context_client：记录每次 append_turn，并可按收件人模拟失败。"""

    def __init__(self, fail_for=()):
        self.calls: list[dict] = []
        self.fail_for = set(fail_for)

    def append_turn(self, **kwargs) -> bool:
        self.calls.append(kwargs)
        return kwargs.get("agent_id") not in self.fail_for


def run(plan, *, message="我说了一句话", speaker="alice", fail_for=(), content="玩家推开了门",
        session_id="rpg_world1"):
    rec = Recorder(fail_for=fail_for)
    original = AgentRuntime.context_client
    AgentRuntime.context_client = rec
    try:
        task = TaskRuntime(
            task_id="task-1",
            user=RuntimeUser(id="player", session_id=session_id),
            content=content,
            channel="web",
        )
        task.prompt_plan = plan
        AgentRuntime._append_turn_for_recipients(
            task,
            recorded_message=message,
            speaker_id=speaker,
            commit_limit=5,
            max_messages=7,
        )
        return task, rec
    finally:
        AgentRuntime.context_client = original


def plan_of(payload):
    parsed = parse_prompt_plan(json.dumps(payload, ensure_ascii=False))
    assert parsed is not None, payload
    return parsed


print("── 无计划（普通聊天）：必须与改造前完全一致 ──")
_task, rec = run(None)
check("只写一次", len(rec.calls) == 1, f"{len(rec.calls)}")
_call = rec.calls[0] if rec.calls else {}
check("agent_id = 发言者（存储层据此选 session）", _call.get("agent_id") == "alice", f"{_call.get('agent_id')}")
check("正文不加前缀（前缀仍由存储层补）",
      _call.get("assistant_message") == "我说了一句话", f"{_call.get('assistant_message')!r}")
check("不带 metadata", "metadata" not in _call, f"{list(_call)}")
check("user_message = task.content", _call.get("user_message") == "玩家推开了门")
check("commit_limit / max_messages 透传",
      _call.get("commit_limit") == 5 and _call.get("max_messages") == 7)
check("tool_summaries 为空列表", _call.get("tool_summaries") == [])


print("── 有计划：按在场列表扇出 ──")
_task, rec = run(plan_of({"text": "玩家推开了门", "present": ["bob", "carol"]}))
check("三个收件人、发言者在最前",
      [c["agent_id"] for c in rec.calls] == ["alice", "bob", "carol"],
      f"{[c['agent_id'] for c in rec.calls]}")
check("每份正文首部写真实发言者",
      all(c["assistant_message"] == "<alice>: 我说了一句话" for c in rec.calls),
      f"{[c['assistant_message'] for c in rec.calls]}")
check("每份都声明 author_in_content",
      all(c["metadata"] == {AUTHOR_IN_CONTENT_KEY: "true"} for c in rec.calls),
      f"{[c['metadata'] for c in rec.calls]}")
check("每份 user_message 都是叙事文本（不含指令区）",
      all(c["user_message"] == "玩家推开了门" for c in rec.calls),
      f"{[c['user_message'] for c in rec.calls]}")
check("每份 task_id / session_id 一致",
      all(c["task_id"] == "task-1" and c["session_id"] == "rpg_world1" for c in rec.calls))

print("── 发言者永远在内 / 失败闭合 / 去重 ──")
_task, rec = run(plan_of({"text": "x", "present": []}))
check("在场列表为空 → 失败闭合，只写发言者",
      [c["agent_id"] for c in rec.calls] == ["alice"], f"{[c['agent_id'] for c in rec.calls]}")
_task, rec = run(plan_of({"text": "x", "present": ["alice", "bob"]}))
check("在场列表已含发言者 → 不重复写",
      [c["agent_id"] for c in rec.calls] == ["alice", "bob"], f"{[c['agent_id'] for c in rec.calls]}")
# 解析器本来就拒绝 present 里的空字符串（见 prompt_plan_smoke_test），
# 所以扇出里那层过滤是纵深防御 —— 这里直接构造 plan 绕过解析器来测它。
_task, rec = run(PromptPlan(text="x", present=("", "  ", "bob")))
check("空 / 空白名字被过滤（纵深防御）",
      [c["agent_id"] for c in rec.calls] == ["alice", "bob"], f"{[c['agent_id'] for c in rec.calls]}")
_task, rec = run(plan_of({"text": "x", "present": ["alice", "bob", "carol"]}), fail_for=("bob",))
check("某一份写失败不影响其余（三个都尝试了）",
      [c["agent_id"] for c in rec.calls] == ["alice", "bob", "carol"],
      f"{[c['agent_id'] for c in rec.calls]}")

print("── 群共享 session：隔离失效可以接受，但不能写重 ──")
_task, rec = run(plan_of({"text": "x", "present": ["bob", "carol"]}), session_id="qq_g_123456")
check("群前缀 session → 退化为单次写入（不告错）",
      [c["agent_id"] for c in rec.calls] == ["alice"], f"{[c['agent_id'] for c in rec.calls]}")
_task, rec = run(plan_of({"text": "x", "present": ["bob"]}), session_id="grp_scene1")
check("grp_ 前缀同样退化", [c["agent_id"] for c in rec.calls] == ["alice"],
      f"{[c['agent_id'] for c in rec.calls]}")
_task, rec = run(plan_of({"text": "x", "present": ["bob", "carol"]}), session_id="rpg_world1")
check("非群前缀仍正常扇出", len(rec.calls) == 3, f"{len(rec.calls)}")

print("── 端到端：原始 JSON 不会漏进任何收件人的历史 ──")
raw = json.dumps({"present": ["bob"], "exclude_block": ["tool"], "text": "玩家推开了门"},
                 ensure_ascii=False)
rec = Recorder()
_original = AgentRuntime.context_client
AgentRuntime.context_client = rec
try:
    task = TaskRuntime(task_id="t", user=RuntimeUser(id="player", session_id="rpg_world1"),
                       content=raw, channel="web")
    plan = parse_prompt_plan(task.content)
    task.prompt_plan = plan
    task.replace_input_content(plan.text)
    AgentRuntime._append_turn_for_recipients(
        task, recorded_message="门开了", speaker_id="alice")
finally:
    AgentRuntime.context_client = _original
check("两份写入都没有 JSON 残留",
      all("exclude_block" not in c["user_message"] and "present" not in c["user_message"]
          for c in rec.calls), f"{[c['user_message'] for c in rec.calls]}")
check("两份 user_message 都是纯叙事",
      all(c["user_message"] == "玩家推开了门" for c in rec.calls))

print("── 结构不变量 ──")
src = (SERVICE_DIR / "app" / "agent_runtime.py").read_text(encoding="utf-8")
_i_read = src.find('record_mode = str(default_agent_config.get("record_mode", "final")')
_i_force = src.find("if task.prompt_plan is not None:", _i_read)
_i_set = src.find('record_mode = "intermediate"', _i_force)
_i_branch = src.find('if record_mode == "intermediate":', _i_read)
check("读取 record_mode 之后才有强制逻辑", 0 <= _i_read < _i_force, f"{_i_read} < {_i_force}")
check("强制赋值落在 intermediate 分支之前", 0 <= _i_force < _i_set < _i_branch,
      f"{_i_force} < {_i_set} < {_i_branch}")
check("扇出正文首部写真实发言者",
      'assistant_message=f"<{speaker_id}>: {recorded_message}"' in src)
check("扇出写入声明 author_in_content",
      'metadata={AUTHOR_IN_CONTENT_KEY: "true"}' in src)
check("发言者被无条件放进收件人列表", "recipients = [speaker_id]" in src)

_i_none = src.find("if plan is None:", src.find("def _append_turn_for_recipients"))
_i_ret = src.find("return", _i_none)
check("无计划分支不带 metadata（普通路径零变化）",
      _i_none > 0 and "metadata=" not in src[_i_none:_i_ret], f"{_i_none}..{_i_ret}")

storage = (SERVICE_DIR.parent / "openviking-context-service" / "app" / "viking_store.py").read_text(encoding="utf-8")
check("跨服务键名一致（orchestrator 与存储层）",
      f'AUTHOR_IN_CONTENT_KEY = "{AUTHOR_IN_CONTENT_KEY}"' in storage,
      f"orchestrator={AUTHOR_IN_CONTENT_KEY!r}")


print()
if FAILURES:
    print(f"FAILED {len(FAILURES)} / {OK + len(FAILURES)}")
    for name in FAILURES:
        print(f"  - {name}")
    sys.exit(1)
print(f"ALL OK — {OK} checks")
