#!/usr/bin/env python3
"""提示词热重载的冒烟测试。

要解决的问题：system prompt 是**实例级**构建的 —— `AgentRuntime.__init__` 调一次
`build_system_prompt()`，实例再按 `(user_id, session_id, agent_id)` 缓存在
`_agent_instances`（LRU 20）里复用。所以在 dashboard / NFS 上改完提示词之后，
已经在聊的会话会一直用旧内容：要么等 LRU 把实例挤掉（**不同用户可能同时跑在两套
人设上**），要么重启 orchestrator（而重启会杀掉在飞任务）。

`refresh_system_prompt()`（`send()` 开头按 `prompt_revision()` 快照比对）把生效
时机收敛到「下一次模型调用」。本测试钉住的就是它，覆盖：

  1. 没变就不重建（快照稳定，不做无用功）
  2. 改 `.md` → 重建，内容真的换了
  3. 改 `@ref=` **指针行**（新语法 `@ref=人设甲.md` → `@ref=人设乙.md`）→ 重建
     —— 这是「几套 .md 快速切换」的核心动作
  4. 改**被引用的目标文件**本身（不改指针）→ 也要重建
  5. 改**两跳链的中间 hub**（角色存根 → hub → 人设文件）→ 重建，
     模拟「改一行，一堆角色跟着换」
  6. `.bak` / `.tmp` 不触发重建
  7. 重建失败不留半截状态，且下一次仍报错（不静默用回旧人设）
  8. 走真实实例缓存取回的仍是同一个实例，但内容已更新
  9. 结构断言：`send()` 里刷新发生在打断检查、消费输入、上下文构建、模型调用之前

与网络、模型完全解耦：只构造实例、只构建提示词。下游客户端里只有
timer_task_client 在 import 期直接 raise（缺 grpc 生成文件），塞空模块过去。

用法：python tools/prompt_hot_reload_smoke_test.py
"""

import inspect
import json
import os
import shutil
import sys
import tempfile
import types
from pathlib import Path
from types import SimpleNamespace

SERVICE_DIR = Path(__file__).resolve().parents[1]
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

for _name in ("timer_task_pb2", "timer_task_pb2_grpc"):
    sys.modules.setdefault(_name, types.ModuleType(_name))

TMP = Path(tempfile.mkdtemp(prefix="prompt_hot_reload_"))
PROMPTS = TMP / "prompts"
SETTINGS = TMP / "settings.json"
AGENT_LIST = TMP / "agent_list.json"

os.environ["SYSTEM_PROMPT_DIR"] = str(PROMPTS)
os.environ["AGENT_CONFIG_PATH"] = str(AGENT_LIST)
os.environ["SYSTEM_SETTINGS_PATH"] = str(SETTINGS)

from app import agent_runtime as ar  # noqa: E402
from app import config  # noqa: E402
from app import context_builder as cb  # noqa: E402
from app.agent_runtime import AgentRuntime  # noqa: E402
from app.prompt_refs import PromptRefError  # noqa: E402

# 日志是给生产排查用的，冒烟测试里只会淹没断言结果。
# context_builder 是 `from app.logger import debug_log` 直接绑定的函数名，
# 它那一份要单独 patch，改 agent_runtime.debug_log 影响不到它。
ar.debug_log = lambda *a, **k: None
ar.chat_log = lambda *a, **k: None
cb.debug_log = lambda *a, **k: None

# config 已在 import 期按环境变量定型；这里再显式指一次，防止将来默认值变动
config.SYSTEM_PROMPT_DIR = PROMPTS
config.AGENT_CONFIG_PATH = AGENT_LIST
config.SYSTEM_SETTINGS_PATH = SETTINGS

PASSED: list[str] = []
FAILED: list[str] = []


def check(label: str, ok: bool, extra: str = "") -> None:
    print(("  OK   " if ok else "  FAIL ") + label + (f"  {extra}" if extra else ""))
    if ok:
        PASSED.append(label)
    else:
        FAILED.append(label)


def write_prompt(rel: str, text: str) -> None:
    path = PROMPTS / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def expect_error(label: str, fn) -> str:
    try:
        fn()
    except PromptRefError as exc:
        check(label, True, str(exc)[:100])
        return str(exc)
    except Exception as exc:
        check(label, False, f"抛了别的异常 {type(exc).__name__}: {exc}")
        return ""
    check(label, False, "没有报错")
    return ""


def text_of(agent) -> str:
    """实例当前生效的 system prompt（拼成一段，便于断言用的是哪一份）。"""
    return "\n".join(m["content"] for m in agent.system_prompt)


# ══ 造数据 ═══════════════════════════════════════════════════

AGENT_LIST.write_text(
    json.dumps(
        {
            "default": {
                "main": {"files": ["SOUL.md", "TOOL.md"]},
                "tool": {"files": ["SOUL.md"]},
            }
        },
        ensure_ascii=False,
        indent=2,
    ),
    encoding="utf-8",
)
SETTINGS.write_text(
    json.dumps({"image_receive_enabled": True}, ensure_ascii=False, indent=2),
    encoding="utf-8",
)

write_prompt("GLOBAL_SETTING.md", "全局设置标记: GLOBAL\n")
write_prompt("人设甲.md", "人设标记: 甲\n")
write_prompt("人设乙.md", "人设标记: 乙\n")
write_prompt("main/SOUL.md", "@ref=人设甲.md\n")   # 指针：换套 = 改这一行
write_prompt("main/TOOL.md", "工具说明标记: MAIN_TOOL\n")
write_prompt("tool/SOUL.md", "@ref=hub\n")          # 两跳：存根 → hub → 人设文件
write_prompt("hub/SOUL.md", "@ref=人设甲.md")

AgentRuntime._agent_instances.clear()
main_agent = AgentRuntime.get_agent("main", "sess-1", "u1")
tool_agent = AgentRuntime.get_agent("tool", "sess-1", "u1")

print("\n== [1] 初始状态 + 无变更不重建 ==")
check("main 用的是指针指向的甲", "人设标记: 甲" in text_of(main_agent))
check("tool 两跳也解析到甲", "人设标记: 甲" in text_of(tool_agent))
check("未变更时 refresh 返回 False", main_agent.refresh_system_prompt() is False)
check("再刷一次仍是 False（快照稳定）", main_agent.refresh_system_prompt() is False)

print("\n== [2] 改 .md 内容 → 重建 ==")
write_prompt("main/TOOL.md", "工具说明标记: MAIN_TOOL_EDITED\n")
check("refresh 返回 True", main_agent.refresh_system_prompt() is True)
check("内容真的换了", "MAIN_TOOL_EDITED" in text_of(main_agent))
check("再刷不再重建", main_agent.refresh_system_prompt() is False)

print("\n== [3] 改 @ref= 指针行 → 换套（核心动作）==")
write_prompt("main/SOUL.md", "@ref=人设乙.md\n")
check("refresh 返回 True", main_agent.refresh_system_prompt() is True)
check("人设换成乙", "人设标记: 乙" in text_of(main_agent))
check("甲已消失（是替换不是叠加）", "人设标记: 甲" not in text_of(main_agent))
check("记录串与段落同源（_soul_message_content 也换了）",
      "人设标记: 乙" in main_agent._soul_message_content)

print("\n== [4] 改被引用的目标文件本身（不动指针）→ 也要重建 ==")
write_prompt("人设乙.md", "人设标记: 乙_EDITED\n")
check("refresh 返回 True", main_agent.refresh_system_prompt() is True)
check("内容跟着目标文件变", "人设标记: 乙_EDITED" in text_of(main_agent))

print("\n== [5] 改两跳链的中间 hub → 下游全体跟随 ==")
write_prompt("hub/SOUL.md", "@ref=人设乙.md\n")
check("tool 的 refresh 返回 True", tool_agent.refresh_system_prompt() is True)
check("tool 现在也是乙（改 hub 一行即可）", "人设标记: 乙_EDITED" in text_of(tool_agent))
check("tool 的甲已消失", "人设标记: 甲" not in text_of(tool_agent))

print("\n== [6] .bak / .tmp 不触发重建 ==")
# 快照是**全局**的（改任何提示词文件都会让所有实例在下一次 send 时重建），
# 所以先把上一节改 hub 引起的那次待重建吸收掉，再看 .bak/.tmp 有没有影响。
main_agent.refresh_system_prompt()
check("吸收待重建后再刷 → False", main_agent.refresh_system_prompt() is False)
write_prompt("main/SOUL.md.bak", "备份文件不该参与\n")
write_prompt("main/SOUL.md.tmp", "原子写中间态不该参与\n")
check(".bak / .tmp 不触发重建", main_agent.refresh_system_prompt() is False)

print("\n== [7] 重建失败：不静默用回旧人设 ==")
write_prompt("main/SOUL.md", "@ref=人设丙.md\n")
expect_error("refresh 抛 PromptRefError", lambda: main_agent.refresh_system_prompt())
check("失败后实例内容保持上一次（不是半截 / 清空）",
      "人设标记: 乙_EDITED" in text_of(main_agent))
expect_error("不记快照：下一次 refresh 仍报错（不会悄悄放行）",
             lambda: main_agent.refresh_system_prompt())
write_prompt("main/SOUL.md", "@ref=人设甲.md\n")
check("改正后立刻恢复", main_agent.refresh_system_prompt() is True
      and "人设标记: 甲" in text_of(main_agent))

print("\n== [8] 真实实例缓存：同一个实例，内容已更新 ==")
same = AgentRuntime.get_agent("main", "sess-1", "u1")
check("取回的是同一个实例（没被 LRU 淘汰、没重启）", same is main_agent)
check("缓存实例带的是纠正后的内容", "人设标记: 甲" in text_of(same))

print("\n== [9] 结构断言：send() 的调用顺序 ==")
src = inspect.getsource(AgentRuntime.send)
at_refresh = src.find("refresh_system_prompt()")
at_interrupt = src.find("interrupt_event.is_set")
at_input = src.find("consume_temp_dialog_input")
at_context = src.find("build_send_context")
at_model = src.find("chat_completion")
check("send() 里调用了 refresh_system_prompt", at_refresh != -1)
check("刷新在打断检查之前（代价只是一次 stat，先刷再用）",
      -1 < at_refresh < at_interrupt, f"refresh={at_refresh} interrupt={at_interrupt}")
check("刷新在消费本轮输入之前", -1 < at_refresh < at_input,
      f"refresh={at_refresh} input={at_input}")
check("刷新在上下文构建之前（否则重建等于没建）", -1 < at_refresh < at_context,
      f"refresh={at_refresh} context={at_context}")
check("刷新在模型调用之前", -1 < at_refresh < at_model,
      f"refresh={at_refresh} model={at_model}")

print("\n== [10] 刷新语义的两个不变量（结构断言）==")
refresh_src = inspect.getsource(AgentRuntime.refresh_system_prompt)
build_src = inspect.getsource(AgentRuntime.build_system_prompt)
check("refresh 里刻意不先记快照（失败时下一条消息仍会报错）",
      "_prompt_revision =" not in refresh_src)
check("build_system_prompt 里快照在读取之前取（宁可多建一次，不要漏）",
      -1 < build_src.find("stamp = prompt_revision()")
      < build_src.find("ContextBuilder(self).build_system_prompt()"))
check("刷新点是 send() 开头而不是任务入口（子智能体各有实例，只刷 main 不够）",
      "refresh_system_prompt()" in inspect.getsource(AgentRuntime.send))

shutil.rmtree(TMP, ignore_errors=True)
print()
print(f"PASSED: {len(PASSED)}   FAILED: {len(FAILED)}")
print("FAILURES:", FAILED if FAILED else "none")
sys.exit(1 if FAILED else 0)
