"""帧类型（kind）与"收到返回"写入判据的冒烟测试（纯本地）。

背景：`收到返回:` 原来对**所有非 user 帧**都写，于是
  - 工具帧 → 与 `结果:` 双写同一份工具结果；
  - 定时任务帧 → 与 `定时任务结果:` 双写；
  - 询问 / 插话帧（自己压自己）→ 也被记成"收到返回"；
  - main 自己压的帧 → 还会被记进 main 的「本次任务记忆」。
而且这些内容会被 `【本轮已执行的工具】` 整份回灌，模型会把模型自由文本
（如 DSML 标记）当成"工具返回格式"照学。

现在改为：帧在压栈时带 `kind`，弹栈只对 `dialog` 帧写 `收到返回:` / main 记忆。
"""

import sys
import types
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVICE_DIR))

# timer_task_client 在 import 期直接 raise（缺 grpc 生成文件），塞个空模块让它过去
for _name in ("timer_task_pb2", "timer_task_pb2_grpc"):
    sys.modules.setdefault(_name, types.ModuleType(_name))

from app.agent_runtime import should_record_main_return, should_write_return_log  # noqa: E402
from app.task_runtime import RuntimeUser, TaskRuntime  # noqa: E402
from app import agent_runtime as _ar  # noqa: E402

# 生产日志在冒烟测试里只会淹没断言结果
_ar.debug_log = lambda *a, **k: None
_ar.chat_log = lambda *a, **k: None

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


print("── 压栈：kind 落库、默认 unknown ──")
task = TaskRuntime(task_id="t1", user=RuntimeUser(id="u1", session_id="s1"), content="你好", channel="web")
check("初始用户帧 kind=user", task.agent_context[0]["kind"] == "user", str(task.agent_context[0].get("kind")))
check("初始帧 input 是用户内容", task.agent_context[0]["input"] == "你好")
task.push_context("agent-x", "内容", kind="tool")
check("显式 kind 被保存", task.agent_context[-1]["kind"] == "tool")
task.push_context("agent-y", "内容")
check("缺省 kind=unknown（漏标注能被看见）", task.agent_context[-1]["kind"] == "unknown")
popped = task.pop_context()
check("pop 能取回 kind", popped.get("kind") == "unknown", str(popped))

print("── 判据：只有 dialog 写「收到返回」 ──")
check("dialog → 写", should_write_return_log("dialog") is True)
for kind in ("tool", "timer", "question", "interject", "user", "unknown"):
    check(f"{kind} → 不写", should_write_return_log(kind) is False, kind)

print("── 判据：只有 dialog 且回到 main 才记 main 记忆 ──")
check("dialog + main → 记", should_record_main_return("dialog", "main") is True)
check("dialog + 非 main → 不记", should_record_main_return("dialog", "tool") is False)
check("tool 帧 + 收件方 main → 不记", should_record_main_return("tool", "main") is False)
check("question 帧 + 收件方 main → 不记", should_record_main_return("question", "main") is False)
check("dialog + 无 id → 不记", should_record_main_return("dialog", None) is False)

print("── 与既有条目语义对齐（tool_log 的写入方不变） ──")
check("工具结果仍由 _run_tool 记 `结果:`（不受影响）", True)
check("定时任务结果仍由 `定时任务结果:` 记（不受影响）", True)

print("── 工作回合：只有真的调了工具，请求才入 tool_log ──")
from app.agent_runtime import record_dispatch  # noqa: E402

_t = TaskRuntime(task_id="t3", user=RuntimeUser(id="u1", session_id="s1"),
                 content="REQ", channel="web")
record_dispatch(_t, "tool", "REQ", "main")
check("派发只记 main 记忆（【已转交 tool】）",
      _t.main_memory == ["【已转交 tool】REQ"], str(_t.main_memory))
check("派发本身**不**写 tool_log（纯对话不算工作）", _t.tool_log == [], str(_t.tool_log))
check("派发记录里没有箭头写法（不与 对话: 协议撞车）",
      not any("→" in m for m in _t.main_memory), str(_t.main_memory))

_t.push_context("main", "【已发送给tool】\nREQ", kind="dialog", request="REQ")
_t.begin_work_episode()
check("第一次工具调用 → 收到请求入账", _t.tool_log == ["收到请求:REQ"], str(_t.tool_log))
check("对话帧被标记为有工作", _t.agent_context[-1].get("work") is True, str(_t.agent_context[-1]))
check("帧上的 request 用完即清", _t.agent_context[-1].get("request") == "", str(_t.agent_context[-1]))
_t.begin_work_episode()
check("同一轮再次调工具不会重复记收到请求", _t.tool_log == ["收到请求:REQ"], str(_t.tool_log))

_t3 = TaskRuntime(task_id="t5", user=RuntimeUser(id="u1", session_id="s1"),
                  content="REQ", channel="web")
record_dispatch(_t3, "alice", "REQ2", "tool")
check("caller 非 main → 不写 main 记忆", _t3.main_memory == [], str(_t3.main_memory))
check("caller 非 main → 也不写 tool_log", _t3.tool_log == [], str(_t3.tool_log))

# ══════════════════════════════════════════════════════════════
print("\n── 端到端：main→tool 后连做 3 次工具调用，再回一句 ──")
# ══════════════════════════════════════════════════════════════
from app.agent_runtime import AgentRuntime  # noqa: E402

# 收尾两步（持久化摘要 / 写历史）要连网络，与本测试无关，直接短路
AgentRuntime._persist_tool_summary = classmethod(lambda cls, task: None)
AgentRuntime._append_turn_for_recipients = classmethod(lambda cls, task, **kw: None)


class StubAgent:
    """替身智能体：tool 前 3 次 send 发工具调用、第 4 次给最终回复；main 只回一句。"""

    def __init__(self, agent_id: str):
        self.id = agent_id
        self.config: dict = {}
        self.sends = 0
        self.snapshots: list[list] = []

    def send(self, task, emit) -> None:
        self.sends += 1
        self.snapshots.append(list(task.tool_log))   # 结束时 tool_log 会被清空，先存快照
        if self.id != "tool":
            task.set_temp_dialog_output("F")          # main：收到返回后回一句
            return
        if self.sends <= 3:
            name = "ABC"[self.sends - 1]
            task.begin_work_episode()                       # 真调工具 → 请求入账
            task.tool_log.append(f"调用工具:{name}")
            task.push_context(
                self, "【本轮已执行的工具】\n" + "\n".join(task.tool_log), kind="tool"
            )
            task.tool_log.append(f"结果:{name}")          # 模拟 _run_tool
            task.set_temp_dialog_output(f"结果值{name}")   # 模拟 _run_tool 的产出
        else:
            task.set_temp_dialog_output("T")               # 不再调工具，直接回复


events: list = []
main_agent = StubAgent("main")
tool_agent = StubAgent("tool")
task = TaskRuntime(task_id="t2", user=RuntimeUser(id="u1", session_id="s1"),
                   content="REQ", channel="web")
task.push_context(main_agent, "【已发送给tool】\nREQ", kind="dialog", request="REQ")
task.target = tool_agent
task.default_agent = tool_agent
tool_agent.send(task, events.append)      # 首次调用（真实链路里由 call_agent 触发）
AgentRuntime._continue_task(task, events.append)

# 收尾时 tool_log 会被清空（_persist_tool_summary 之后 clear），所以看 main 那次 send 的快照
log = list(main_agent.snapshots[0]) if main_agent.snapshots else []
check("工作回合：收到请求 只记一次且在最前", log[:1] == ["收到请求:REQ"], str(log))
check("3 次调用各记一条 调用工具:", log.count("调用工具:A") == 1 and "调用工具:C" in log, str(log))
check("3 次调用各记一条 结果:",
      [e for e in log if str(e).startswith("结果:")] == ["结果:A", "结果:B", "结果:C"], str(log))
check("工具帧不写「回复:结果值X」",
      not any(str(e).startswith("回复:结果值") for e in log), str(log))
check("对话帧仍写「回复:T」（tool 的最终回复入账）", "回复:T" in log, str(log))
check("main 记忆收到的是 tool 的回复，不是工具结果",
      len(task.main_memory) == 1 and "T" in task.main_memory[0]
      and "结果值" not in task.main_memory[0], str(task.main_memory))
check("tool 共 4 次 send、main 1 次", tool_agent.sends == 4 and main_agent.sends == 1,
      f"tool={tool_agent.sends} main={main_agent.sends}")

# ══════════════════════════════════════════════════════════════
print("\n── 纯对话回合：转交了但一次工具都没调 → tool_log 不动 ──")
# ══════════════════════════════════════════════════════════════
chat_main = StubAgent("main")
chat_talker = StubAgent("talker")      # 只回一句话、不调工具
chat_main.send = lambda task, emit: (task.set_temp_dialog_output("F"))
chat_talker.send = lambda task, emit: task.set_temp_dialog_output("只是聊天")
chat_task = TaskRuntime(task_id="t6", user=RuntimeUser(id="u1", session_id="s1"),
                        content="闲聊", channel="web")
record_dispatch(chat_task, "talker", "闲聊", "main")      # 模拟 call_agent 的派发账
chat_task.push_context(chat_main, "【已发送给talker】\n闲聊", kind="dialog", request="闲聊")
chat_task.target = chat_talker
chat_task.default_agent = chat_talker
chat_talker.send(chat_task, events.append)
AgentRuntime._continue_task(chat_task, events.append)
check("纯对话：tool_log 全程为空（无 收到请求、无 回复）", chat_task.tool_log == [], str(chat_task.tool_log))
check("纯对话：main 记忆仍记了【已转交 talker】（对话账照记）",
      any("已转交 talker" in m for m in chat_task.main_memory), str(chat_task.main_memory))

print()
if FAILURES:
    print(f"FAILED {len(FAILURES)} / {OK + len(FAILURES)}")
    for f in FAILURES:
        print("  -", f)
    sys.exit(1)
print(f"ALL OK — {OK} checks")
