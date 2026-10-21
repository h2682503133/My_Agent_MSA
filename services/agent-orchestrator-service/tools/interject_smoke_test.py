#!/usr/bin/env python3
"""打断 / 插话（barge-in）本地冒烟测试。

覆盖「/stop 打断 = 保留现场暂停，下一条消息作为插话接着跑」这条链路里最容易
写错、又最难在真机上复现的部分：

  1. 暂停的判定（该停 / 不该停）
  2. 暂停时「现场」是否真的一个字都没动
  3. 插话是否真的并进了栈顶调用者的输入（而不是被丢掉或走错分支）
  4. 「询问挂起 → /stop → 插话」这条由 scheduler 打标记的路径
  5. request_stop 的定位语义 + 运行中任务登记表的生命周期
  6. **登记时机**：第一次模型调用期间就必须能被 /stop 找到

第 6 项来自一次真实链路实测：第一版把运行中登记放在主循环里（_continue_task），
而主循环要等第一次模型调用返回才进入 —— 那次第一次模型调用花了十几秒，
用户在这期间按 /stop 被告知「没有正在执行的任务」，而任务明明在跑。
登记因此移到了 process_task / resume_task 的最前面，这个测试就是钉住它。

关于「怎么模拟 /stop 到达」：清残留标记发生在执行入口（process_task /
resume_task），不在 _continue_task 里，所以下面既可以直接在调 _continue_task
前置位标记（= 第一次模型调用期间到达），也可以在替身 send() 里置位
（= 后续某一步执行期间到达），两种真实时序都能覆盖。

与网络、模型完全解耦：智能体用替身对象替代。不需要 grpc 生成文件——下游客户端
大多软失败，只有 timer_task_client 在 import 期直接 raise，这里塞个空模块让它
过去（本测试根本不碰定时任务）。

用法：python tools/interject_smoke_test.py
"""

import inspect
import sys
import types
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parents[1]
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

for _name in ("timer_task_pb2", "timer_task_pb2_grpc"):
    sys.modules.setdefault(_name, types.ModuleType(_name))

from app import agent_runtime as ar  # noqa: E402
from app.agent_runtime import INTERJECT_PAUSE_TEXT, AgentRuntime  # noqa: E402
from app.task_runtime import RuntimeUser, TaskRuntime  # noqa: E402

# 日志是给生产排查用的，冒烟测试里只会淹没断言结果
ar.debug_log = lambda *a, **k: None
ar.chat_log = lambda *a, **k: None

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASSED.append(name)
        print(f"  [PASS] {name}")
    else:
        FAILED.append(name)
        print(f"  [FAIL] {name}{(' -> ' + detail) if detail else ''}")


class StubAgent:
    """替身智能体：记录收到的输入，并可通过 on_send 钩子模拟「下一步动作」。

    真实 send() 压栈是因为它发起了一件事（工具 / 转交 / 询问）。默认这里代表
    「这一轮做完就给定论」（不压栈）→ 主循环弹到用户帧收尾；需要多轮时由
    on_send 自己压帧。
    """

    def __init__(self, agent_id: str = "main"):
        self.id = agent_id
        self.config: dict = {}
        self.received: list[str] = []
        self.reply = "最终回复"
        self.on_send = None

    def send(self, task, emit) -> None:
        self.received.append(task.consume_temp_dialog_input() or "")
        if self.on_send is not None:
            self.on_send(task, emit)
        else:
            task.set_temp_dialog_output(self.reply)


class FirstCallProbeAgent(StubAgent):
    """模拟「第一次模型调用」：期间检查登记状态，并让 /stop 在这期间到达。"""

    def __init__(self, request_stop: bool):
        super().__init__("main")
        self.request_stop = request_stop
        self.registered_during_first_call = None
        self.calls = 0

    def send(self, task, emit) -> None:
        self.calls += 1
        self.received.append(task.consume_temp_dialog_input() or "")
        self.registered_during_first_call = AgentRuntime.RUNNING_TASKS.get(task.user.id) is task
        if self.request_stop:
            # 模型这一轮发起了工具调用（压帧），而 /stop 恰在此刻到达
            task.push_context(self, "【本轮已执行的工具】\n调用工具:search|A", kind="tool")
            task.set_temp_dialog_output("工具结果:A=1")
            task.interrupt_event.set()
        else:
            task.set_temp_dialog_output(self.reply)


def make_task(
    user_id: str = "u1",
    session_id: str = "web_u1",
    content: str = "帮我查一下 A",
    task_id: str = "",
) -> TaskRuntime:
    user = RuntimeUser(id=user_id, session_id=session_id)
    return TaskRuntime(
        task_id=task_id or f"task-{user_id}",
        user=user,
        content=content,
        channel="web",
    )


def collect():
    events: list = []

    def emit(ev):
        events.append(ev)

    return events, emit


def reset_state() -> None:
    with AgentRuntime.PENDING_LOCK:
        AgentRuntime.PENDING_TASKS.clear()
    with AgentRuntime.RUNNING_LOCK:
        AgentRuntime.RUNNING_TASKS.clear()


def types_of(events) -> list[str]:
    return [e.type for e in events]


def interrupt_pause(events):
    return [e for e in events if e.type == "task_waiting_user"]


# ══════════════════════════════════════════════════════════════
print("\n[1] 该不该暂停")
# ══════════════════════════════════════════════════════════════
reset_state()

# 1a) 栈顶是用户帧 → 不暂停（下一步本来就是把最终回复发出去）
task = make_task()
events, emit = collect()
check("栈顶是用户帧时 _pause_for_interject 返回 False",
      AgentRuntime._pause_for_interject(task, emit) is False)
check("拒绝暂停时不发任何事件", events == [], str(types_of(events)))
check("拒绝暂停时状态保持 running（没被改成 suspended）", task.status == "running", task.status)

# 1b) 循环运行中收到停止、但任务已近收尾 → 放它跑完并清掉标记
task = make_task()
task.set_temp_dialog_output("最终回复")
task.interrupt_event.set()
events, emit = collect()
final = AgentRuntime._continue_task(task, emit)
check("近收尾时不暂停：返回最终回复", final == "最终回复", repr(final))
check("近收尾时不暂停：不发 task_waiting_user",
      "task_waiting_user" not in types_of(events), str(types_of(events)))
check("近收尾时不暂停：最终回复照常发出",
      any(e.type == "assistant_message" and e.text == "最终回复" for e in events))
check("近收尾时不暂停：停止标记被清掉（否则下一轮会立刻又触发）",
      not task.interrupt_event.is_set())

# ══════════════════════════════════════════════════════════════
print("\n[2a] /stop 在第一次模型调用期间到达 → 返回后立刻暂停（真机回归）")
# ══════════════════════════════════════════════════════════════
reset_state()
task = make_task(user_id="u1a")
agent = StubAgent("main")
task.target = agent
# 第一次模型调用已经返回：它压了帧（发起了工具调用）并产出了结果
task.push_context(agent, "【本轮已执行的工具】\n调用工具:search|A", kind="tool")
task.set_temp_dialog_output("工具结果:A=1")
task.interrupt_event.set()          # ← /stop 在模型思考期间就到了
events, emit = collect()
final = AgentRuntime._continue_task(task, emit)

check("首调期间收到：暂停生效（返回空串）", final == "", repr(final))
check("首调期间收到：状态为 suspended", task.status == "suspended", task.status)
check("首调期间收到：发出打断式暂停事件",
      len(interrupt_pause(events)) == 1, str(types_of(events)))
check("首调期间收到：事件 interrupt=true",
      bool(events) and events[0].metadata.get("interrupt") == "true")
check("首调期间收到：不再多花一次模型调用（智能体一次都没被调）",
      agent.received == [], str(len(agent.received)))
check("首调期间收到：现场保留（栈未动、产出还在）",
      len(task.agent_context) == 2 and task.temp_dialog_output == "工具结果:A=1")

# ══════════════════════════════════════════════════════════════
print("\n[2b] /stop 在后续某一步执行期间到达 → 下一个检查点暂停，现场一字不动")
# ══════════════════════════════════════════════════════════════
reset_state()
task = make_task(content="帮我查 A 和 B")
agent = StubAgent("main")
task.target = agent
task.default_agent = agent
task.push_context(agent, "【本轮已执行的工具】\n调用工具:search|A", kind="tool")
task.set_temp_dialog_output("工具结果:A=1")
task.tool_log.append("调用工具:search|A")
task.main_memory.append("→tool: 查 A")


def midway(task, emit):
    """第一轮 send 内部：压新帧（发起下一步）+ 模拟 /stop 此刻从别的线程到达。"""
    task.push_context(agent, "【本轮已执行的工具】\n调用工具:search|A\n结果:A=1", kind="tool")
    task.set_temp_dialog_output("工具结果:A=2")
    task.tool_log.append("结果:A=1")
    task.interrupt_event.set()


agent.on_send = midway
events, emit = collect()
final = AgentRuntime._continue_task(task, emit)

check("暂停：返回空串（scheduler 据此不补终态事件）", final == "", repr(final))
check("暂停：状态为 suspended", task.status == "suspended", task.status)
check("暂停：标记为打断式暂停（恢复时走插话路径）", task.paused_by_interrupt is True)
check("暂停：恰好发一条事件", types_of(events) == ["task_waiting_user"], str(types_of(events)))
pause_ev = events[0] if events else None
check("暂停：事件 metadata.interrupt=true", bool(pause_ev) and pause_ev.metadata.get("interrupt") == "true")
check("暂停：事件 metadata.suspend=true", bool(pause_ev) and pause_ev.metadata.get("suspend") == "true")
check("暂停：事件对用户可见（才会被 ack / 跨渠道转投 / 离线缓冲）",
      bool(pause_ev) and pause_ev.metadata.get("visible_to_user") == "true")
check("暂停：文本说明「进度已保留」", bool(pause_ev) and pause_ev.text == INTERJECT_PAUSE_TEXT,
      repr(pause_ev.text if pause_ev else None))
check("暂停：上下文栈未被动过（2 帧都还在）", len(task.agent_context) == 2, f"len={len(task.agent_context)}")
# 注意：暂停发生在「第二轮」循环头，第一轮是正常跑完的（停止就是从那一轮到达的），
# 所以 tool_log / main_memory 比循环开始前多一条 —— 那是这一轮的正常累积，不是丢数据。
# 要断言的是「原有内容没被清掉/覆盖」。
check("暂停：tool_log 原有记录仍在其位", bool(task.tool_log) and task.tool_log[0] == "调用工具:search|A",
      str(task.tool_log))
check("暂停：工具帧不写「收到返回」（结果由 结果: 承载，避免双写）",
      "结果:A=1" in task.tool_log
      and not any(str(e).startswith("回复:") for e in task.tool_log), str(task.tool_log))
check("暂停：main_memory 原有记录仍在其位",
      bool(task.main_memory) and task.main_memory[0] == "→tool: 查 A", str(task.main_memory))
check("暂停：待回喂的产出还在（没被提前消费掉）", task.temp_dialog_output == "工具结果:A=2",
      repr(task.temp_dialog_output))
check("登记：进入 PENDING_TASKS（ResumeTask 才找得回来）",
      AgentRuntime.PENDING_TASKS.get("task-u1") is task)
check("登记：暂停后不再算运行中（此时 /stop 应回「已处于暂停」）",
      "u1" not in AgentRuntime.RUNNING_TASKS)

# ══════════════════════════════════════════════════════════════
print("\n[3] 插话恢复 → 接着同一个检查点继续跑")
# ══════════════════════════════════════════════════════════════
n_before = len(agent.received)
agent.on_send = None                     # 恢复后这一轮直接给定论
resumed = AgentRuntime.pop_pending_task("task-u1")
check("恢复：能从挂起表取回同一个任务对象", resumed is task)
events2, emit2 = collect()
AgentRuntime.resume_task(task, "算了，只查 A", emit2)

check("插话：智能体被再次调用（不是新任务、不是丢弃）", len(agent.received) == n_before + 1,
      f"n={len(agent.received)}")
received = agent.received[-1]
check("插话：输入含【用户插话】标记", "【用户插话】" in received)
check("插话：输入含用户新说的话", "算了，只查 A" in received)
check("插话：输入仍含之前累积的工具上下文（现场没丢）", "【本轮已执行的工具】" in received)
check("插话：真实产出被一并回喂（先看到发生了什么）", "工具结果:A=2" in received)
check("插话：插话排在产出之后（模型先因后果）",
      received.find("工具结果:A=2") < received.find("【用户插话】"), repr(received[:200]))
check("插话：标记已复位", task.paused_by_interrupt is False)
check("插话：任务正常收尾", task.status == "completed", task.status)
check("插话：最终回复发出", any(e.type == "assistant_message" for e in events2), str(types_of(events2)))
check("插话：写进本轮用户输入（持久化上下文里能看到用户中途说了什么）",
      "【用户插话】" in task.content and "算了，只查 A" in task.content, repr(task.content))
check("插话：收尾后登记表不残留", "u1" not in AgentRuntime.RUNNING_TASKS)

# ══════════════════════════════════════════════════════════════
print("\n[4] 询问挂起 + /stop → 按插话恢复（scheduler 打 resume_mode 标记）")
# ══════════════════════════════════════════════════════════════
reset_state()
task = make_task(user_id="u2", session_id="web_u2")
agent2 = StubAgent("main")
# 「询问」挂起的现场就是这样：send() 压了一帧，temp_dialog_output 被清空
task.push_context(agent2, "【已发送给用户】\n询问：要查 A 还是 B？", kind="question")
task.target = agent2
task.set_temp_dialog_output("")
task.metadata["resume_mode"] = "interrupt"
events3, emit3 = collect()
AgentRuntime.resume_task(task, "都别查了", emit3)

r = agent2.received[0] if agent2.received else ""
check("询问转插话：智能体被再次调用", len(agent2.received) == 1, f"n={len(agent2.received)}")
check("询问转插话：含【用户插话】", "【用户插话】" in r and "都别查了" in r)
check("询问转插话：保留原询问上下文（询问方知道刚才问过什么）", "询问：要查 A 还是 B？" in r)
check("询问转插话：不喂「输出已丢失」占位符（询问挂起的输出本就是空）", "输出已丢失" not in r, repr(r))
check("询问转插话：resume_mode 用后即清（不会污染下一次恢复）", "resume_mode" not in task.metadata)

# ══════════════════════════════════════════════════════════════
print("\n[5] request_stop 的定位语义")
# ══════════════════════════════════════════════════════════════
reset_state()
check("空闲时返回 False（scheduler 据此回「没有正在执行的任务」）",
      AgentRuntime.request_stop("nobody") is False)

task = make_task(user_id="u3")
AgentRuntime._register_running(task)
check("有运行中任务时返回 True", AgentRuntime.request_stop("u3") is True)
check("停止标记已置位", task.interrupt_event.is_set())
task.interrupt_event.clear()
check("task_id 不匹配时不误伤别的任务", AgentRuntime.request_stop("u3", task_id="task-other") is False)
check("task_id 匹配时命中", AgentRuntime.request_stop("u3", task_id=task.task_id) is True)
AgentRuntime._unregister_running(task)
check("注销后返回 False", AgentRuntime.request_stop("u3") is False)

# ══════════════════════════════════════════════════════════════
print("\n[6] 登记表生命周期：旧任务收尾不得踢掉新任务")
# ══════════════════════════════════════════════════════════════
reset_state()
old = make_task(user_id="u4", task_id="task-old")
new = make_task(user_id="u4", task_id="task-new")
AgentRuntime._register_running(old)
AgentRuntime._register_running(new)
AgentRuntime._unregister_running(old)
check("旧任务收尾后登记表仍指向新任务", AgentRuntime.RUNNING_TASKS.get("u4") is new)
AgentRuntime._unregister_running(new)
check("新任务收尾后登记表清空", "u4" not in AgentRuntime.RUNNING_TASKS)

# ══════════════════════════════════════════════════════════════
print("\n[7] 登记时机：第一次模型调用期间 /stop 就必须能定位到任务（真机回归）")
# ══════════════════════════════════════════════════════════════
reset_state()
task = make_task(user_id="u7")
probe = FirstCallProbeAgent(request_stop=True)

# 只替掉「取智能体」这一步（否则会去读部署里的 agent_list.json）
original_first_call = AgentRuntime.first_call.__func__
AgentRuntime.first_call = staticmethod(lambda t: setattr(t, "target", probe))
events7, emit7 = collect()
try:
    AgentRuntime.process_task(task, emit7)
finally:
    AgentRuntime.first_call = classmethod(original_first_call)

check("登记时机：第一次模型调用期间任务已在 RUNNING_TASKS（/stop 找得到）",
      probe.registered_during_first_call is True, str(probe.registered_during_first_call))
check("登记时机：第一次模型调用期间到达的 /stop → 模型返回后立刻暂停",
      task.status == "suspended", task.status)
check("登记时机：发出打断式暂停事件", len(interrupt_pause(events7)) == 1, str(types_of(events7)))
check("登记时机：模型没有被白调第二次（只调了一次）", probe.calls == 1, str(probe.calls))
check("登记时机：暂停后交出运行中登记（此后 /stop 应回「已处于暂停」）",
      "u7" not in AgentRuntime.RUNNING_TASKS, str(list(AgentRuntime.RUNNING_TASKS)))
check("登记时机：任务已进 PENDING_TASKS，可被 ResumeTask 取回",
      AgentRuntime.PENDING_TASKS.get("task-u7") is task)
check("登记时机：task_started 仍然照发（没被登记逻辑挤掉）",
      "task_started" in types_of(events7), str(types_of(events7)))

# ══════════════════════════════════════════════════════════════
print("\n[8] 模型调用前的检查点：/stop 之后不再开新的模型调用（真机回归）")
# ══════════════════════════════════════════════════════════════
# 背景：智能体是嵌套调用的（main.send() 内部 call_agent → tool.send()），
# 主循环检查点要等整条嵌套链返回才轮到；实测单次模型调用 40 秒以上，
# 于是 /stop 会「几分钟后才生效」。这个检查点只阻止**新**的模型调用。
reset_state()
task = make_task(user_id="u8", content="帮我查 A")
agent = StubAgent("main")
task.target = agent
# 本次本来要发给这个智能体的输入（例如主循环弹帧后拼好的回喂内容）
pending = "【收到返回】\n工具结果:X"
task.set_temp_dialog_input(pending)

events8, emit8 = collect()
paused = AgentRuntime._pause_before_model_call(task, agent, emit8)

check("调用前暂停：返回 True", paused is True)
check("调用前暂停：状态为 suspended", task.status == "suspended", task.status)
check("调用前暂停：标记为打断式暂停", task.paused_by_interrupt is True)
check("调用前暂停：恰发一条打断事件", len(interrupt_pause(events8)) == 1, str(types_of(events8)))
check("调用前暂停：智能体这次没被调用（模型调用确实没发生）", agent.received == [], str(agent.received))
check("调用前暂停：原输入被压成帧保留（恢复时要用）",
      len(task.agent_context) == 2 and task.agent_context[-1]["input"] == pending,
      str(task.agent_context))
check("调用前暂停：栈顶帧归属该智能体（恢复时交回它自己）",
      task.agent_context[-1]["from"] is agent)
check("调用前暂停：temp_dialog_input 已清（恢复时不会被重复喂一遍）",
      task.temp_dialog_input is None, repr(task.temp_dialog_input))
check("调用前暂停：任务已进 PENDING_TASKS",
      AgentRuntime.PENDING_TASKS.get("task-u8") is task)

# 恢复：应带着「原输入 + 插话」把这个智能体再跑一次
resumed8 = AgentRuntime.pop_pending_task("task-u8")
check("调用前暂停：可从挂起表取回同一个任务", resumed8 is task)
events9, emit9 = collect()
AgentRuntime.resume_task(task, "改用 B 方案", emit9)
r8 = agent.received[0] if agent.received else ""
check("调用前暂停：恢复后该智能体被调用", len(agent.received) == 1, str(agent.received))
check("调用前暂停：恢复后仍拿到原本要发的输入（没丢）", pending in r8, repr(r8))
check("调用前暂停：恢复后并上了用户插话", "【用户插话】" in r8 and "改用 B 方案" in r8, repr(r8))

# 结构守卫：上面用的是替身 send()，无法覆盖真实 AgentRuntime.send() 里的接线，
# 所以这里直接断言它确实把检查点放在「消费输入 / 构建上下文 / 调模型」之前。
send_src = inspect.getsource(AgentRuntime.send)
check("接线：真实 send() 里存在打断检查点", "interrupt_event.is_set" in send_src)
check("接线：检查点在消费输入之前（否则这次输入会被白白吃掉）",
      send_src.find("interrupt_event.is_set") < send_src.find("consume_temp_dialog_input"),
      f"at={send_src.find('interrupt_event.is_set')} vs {send_src.find('consume_temp_dialog_input')}")
check("接线：检查点在模型调用之前",
      send_src.find("interrupt_event.is_set") < send_src.find("chat_completion"),
      f"at={send_src.find('interrupt_event.is_set')} vs {send_src.find('chat_completion')}")

# ══════════════════════════════════════════════════════════════
print("\n" + "=" * 60)
total = len(PASSED) + len(FAILED)
print(f"打断/插话冒烟测试：{len(PASSED)}/{total} PASS")
if FAILED:
    print("失败项：")
    for name in FAILED:
        print(f"  - {name}")
    sys.exit(1)
print("全部通过")
