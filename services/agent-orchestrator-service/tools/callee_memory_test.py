"""被调用者记忆 + 来源标注 自检（不联网，用假 context_client）。

覆盖：
  1. 正常：写入一条 agent_id=被调用者 的 turn，user_message 带 `<caller>:`
  2. 黑名单：tool / reader 不写
  3. 群共享 session（grp_ / qq_g_）：不写
  4. 空输出 / 空派发：不写
  5. 去重前缀：内容已带 `<x>:` 时不重复添加
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config  # noqa: E402
import app.agent_runtime as ar  # noqa: E402
from app.context_builder import GROUP_SESSION_PREFIXES  # noqa: E402

FAILS = []
CHECKS = 0


def check(name, cond, extra=""):
    global CHECKS
    CHECKS += 1
    if cond:
        print(f"  ok    {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL  {name} {extra}")


class FakeUser:
    def __init__(self, session_id):
        self.id = "u1"
        self.session_id = session_id


class FakeTask:
    def __init__(self, session_id="qq_1"):
        self.user = FakeUser(session_id)
        self.task_id = "task-1"


class FakeContextClient:
    def __init__(self):
        self.calls = []

    def append_turn(self, **kwargs):
        self.calls.append(kwargs)
        return True


class FakeAgent:
    def __init__(self, config_dict=None):
        self.config = config_dict or {}


def main():
    client = FakeContextClient()
    ar.AgentRuntime.context_client = client
    ar.AgentRuntime.get_agent = classmethod(
        lambda cls, agent_id, session_id, user_id="default": FakeAgent({"commit_limit": 3, "max_messages": 5})
    )

    print("正常写入")
    task = FakeTask()
    ok = ar.AgentRuntime._record_callee_turn(task, "luna", "<main>:讲个故事", "从前有座山")
    check("返回 True", ok is True)
    check("写了一 条", len(client.calls) == 1, str(len(client.calls)))
    call = client.calls[0]
    check("agent_id 是被调用者", call["agent_id"] == "luna", call.get("agent_id"))
    check("user_message 带 <main>:", call["user_message"].startswith("<main>:"), call["user_message"])
    check("assistant_message 是它的产出", call["assistant_message"] == "从前有座山")
    check("用被调用者配置 commit_limit", call["commit_limit"] == 3, str(call["commit_limit"]))
    check("用被调用者配置 max_messages", call["max_messages"] == 5, str(call["max_messages"]))

    print("黑名单")
    for name in ("tool", "reader"):
        before = len(client.calls)
        ok = ar.AgentRuntime._record_callee_turn(task, name, "<main>:做点事", "done")
        check(f"{name} 不写", ok is False and len(client.calls) == before)

    print("群共享 session")
    for session in GROUP_SESSION_PREFIXES:
        task_group = FakeTask(session_id=f"{session}123")
        before = len(client.calls)
        ok = ar.AgentRuntime._record_callee_turn(task_group, "luna", "<main>:hi", "hello")
        check(f"{session} 跳过", ok is False and len(client.calls) == before, session)

    print("空内容")
    before = len(client.calls)
    check("空输出不写", ar.AgentRuntime._record_callee_turn(task, "luna", "<main>:hi", "  ") is False
          and len(client.calls) == before)
    check("空派发不写", ar.AgentRuntime._record_callee_turn(task, "luna", "", "hello") is False
          and len(client.calls) == before)

    print("前缀去重（call_agent 逻辑）")
    def prefix(content, caller="main"):
        if content and not str(content).lstrip().startswith("<"):
            return f"<{caller}>:" + str(content)
        return content

    check("未带前缀 → 补上", prefix("讲个故事") == "<main>:讲个故事", prefix("讲个故事"))
    check("已带前缀 → 不动", prefix("<luna>:你好") == "<luna>:你好", prefix("<luna>:你好"))

    print("黑名单默认值")
    check("默认含 tool/reader", {"tool", "reader"} <= set(config.INTER_AGENT_MEMORY_SKIP_AGENTS),
          str(config.INTER_AGENT_MEMORY_SKIP_AGENTS))

    print()
    if FAILS:
        print(f"FAILED {len(FAILS)}: {FAILS}")
        return 1
    print(f"ALL OK — {CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
