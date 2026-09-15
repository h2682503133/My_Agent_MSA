"""prompt_refs / context_builder 的提示词引用与占位符冒烟测试。

不依赖集群与下游服务：用临时目录造几个假智能体，直接跑
`@ref=` 跳转解析与 `{agent_id}` 替换，覆盖全部报错分支。

用法：
    python tools/prompt_refs_smoke_test.py
"""

import os
import shutil
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

SERVICE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVICE_DIR))

TMP = Path(tempfile.mkdtemp(prefix="prompt_refs_"))
os.environ["SYSTEM_PROMPT_DIR"] = str(TMP)
os.environ["AGENT_CONFIG_PATH"] = str(TMP / "agent_list.json")

from app import config  # noqa: E402
from app import prompt_refs  # noqa: E402
from app.agent_config import load_agent_config  # noqa: E402
from app.context_builder import ContextBuilder  # noqa: E402
from app.prompt_refs import PromptRefError  # noqa: E402

FAILURES: list[str] = []


def check(label: str, ok: bool, extra: str = "") -> None:
    print(("  OK   " if ok else "  FAIL ") + label + (f"  {extra}" if extra else ""))
    if not ok:
        FAILURES.append(label)


def write(agent: str, filename: str, text: str) -> None:
    path = TMP / agent / filename
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def build(agent_id: str):
    """按 files 声明的假 agent_list 构建 system prompt。"""
    agent = SimpleNamespace(
        id=agent_id,
        user_id="default",
        config=load_agent_config(path=TMP / "agent_list.json", user_id="default", agent_id=agent_id),
        system_prompt=[],
        _identity_system_indexes=[],
        _soul_message_content="",
        _world_info_override_content="",
    )
    return ContextBuilder(agent).build_system_prompt()


def expect_error(label: str, fn) -> None:
    try:
        fn()
    except PromptRefError as exc:
        check(label, True, str(exc)[:110])
        return
    except Exception as exc:  # 非预期异常同样算失败
        check(label, False, f"抛了别的异常 {type(exc).__name__}: {exc}")
        return
    check(label, False, "没有报错")


# ── 造数据 ───────────────────────────────────────────────────
(TMP / "agent_list.json").write_text(
    """{
  "default": {
    "alice":  {"files": ["SOUL.md", "TOOL.md", "world_info.md"]},
    "lilith": {"files": ["SOUL.md", "TOOL.md", "world_info.md"]},
    "bob":    {"files": ["SOUL.md", "PROCESS.md"]},
    "carol":  {"files": ["SOUL.md"]},
    "dave":   {"files": ["SOUL.md"]},
    "erin":   {"files": ["SOUL.md"]},
    "frank":  {"files": ["SOUL.md"]},
    "grace":  {"files": ["SOUL.md"]}
  }
}
""",
    encoding="utf-8",
)

ALICE_SOUL = "# 角色设定\nalice 的 SOUL 正文\n"
write("alice", "SOUL.md", ALICE_SOUL)
write("alice", "world_info.md", "# 核心设定\n你的id是{agent_id}\n")
write("alice", "TOOL.md", "```tool-inject\n{\"sections\": [\"调用格式\"]}\n```\n\n我是{agent_id}，只用调用格式段\n")

# GLOBAL_SETTING.md（根目录，对所有 agent 注入）+ 最小 TOOL.json，让 TOOL 段可渲染
(TMP / "GLOBAL_SETTING.md").write_text("# 全局设置\n运行者：{agent_id}\n", encoding="utf-8")
(TMP / "TOOL.json").write_text(
    """{
  "version": 1,
  "title": "# 工具及技能调用设置",
  "sections": [
    {"id": "调用格式", "title": "## 调用格式", "text": "在回答末尾输出：工具调用:工具名|参数"},
    {"id": "内置工具", "title": "## 内置工具", "tools": [{"name": "shell", "args": "命令", "desc": "执行 shell 命令"}]}
  ]
}
""",
    encoding="utf-8",
)

write("lilith", "SOUL.md", "@ref=alice\n")  # 正常跳转
write("lilith", "world_info.md", "@ref=alice\n")  # world_info 也支持
write("lilith", "TOOL.md", "@ref=alice\n")  # TOOL.md 跳转

write("bob", "SOUL.md", "bob 自己写的\n")
write("bob", "PROCESS.md", "@ref=alice\n")  # PROCESS 里写了指令 → 报错

write("carol", "SOUL.md", "@ref=alice\n多余的一行\n")  # 指令+正文 → 报错
write("dave", "SOUL.md", "@ref=nobody\n")  # 目标目录不存在 → 报错
write("erin", "SOUL.md", "@ref=alice IDENTITY.md\n")  # 行内多余内容 → 报错
write("frank", "SOUL.md", "@ref=frank\n")  # 自跳 → 报错
write("grace", "SOUL.md", "@ref=lilith\n")  # 目标也是跳转 → 只允许一层 → 报错

print("== 正常路径 ==")
alice = build("alice")
lilith = build("lilith")

# 消息顺序：0=GLOBAL_SETTING.md，1=SOUL.md，2=TOOL 段（world_info 不进 messages）
alice_soul = alice.messages[1]["content"]
check("alice: SOUL 原文注入", alice_soul == ALICE_SOUL.strip(), repr(alice_soul[:24]))
check("alice: GLOBAL_SETTING 的 {agent_id} 也替换", "运行者：alice" in alice.messages[0]["content"])
check("alice: {agent_id} 已替换", "你的id是alice" in alice.world_info_override, alice.world_info_override.strip().replace("\n", " / "))
check("alice: TOOL 段含替换后的 id", "我是alice" in alice.messages[2]["content"])

check("lilith: SOUL 整文件替换 == alice 的字节", lilith.messages[1]["content"] == ALICE_SOUL.strip())
check("lilith: soul_content 同步", lilith.soul_content == lilith.messages[1]["content"])
check("lilith: world_info 跳转生效", "你的id是lilith" in lilith.world_info_override, lilith.world_info_override.strip().replace("\n", " / "))
check("lilith: TOOL.md 跳转生效且用请求方 id", "我是lilith" in lilith.messages[2]["content"] and "调用格式" in lilith.messages[2]["content"])
check("lilith: 目标内容里没有残留模板", "{agent_id}" not in lilith.world_info_override)
check("lilith: GLOBAL_SETTING 用请求方 id", "运行者：lilith" in lilith.messages[0]["content"])

print("\n== 阶段1 剔除 SOUL 的前提（内容必须与记录串一致）==")
msg_contents = [m["content"] for m in lilith.messages]
check("SOUL 内容在 head 里恰好一条", sum(1 for c in msg_contents if c == lilith.soul_content) == 1)

print("\n== 报错分支 ==")
expect_error("PROCESS 声明文件写了 @ref= → 报错", lambda: build("bob"))
expect_error("指令行之外还有正文 → 报错", lambda: build("carol"))
expect_error("目标 agent 目录不存在 → 报错", lambda: build("dave"))
expect_error("指令行内有多余内容 → 报错", lambda: build("erin"))
expect_error("自跳 → 报错", lambda: build("frank"))
expect_error("目标也是跳转（两层）→ 报错", lambda: build("grace"))

print("\n== 纯函数层面 ==")
check("未命中原样返回（同一对象）", prompt_refs.resolve_text(
    "普通正文", agent_id="x", filename="SOUL.md", system_prompt_dir=TMP) == "普通正文")
text = "好感度：{好感度：65} 占位：{agent_id} 未知：{user_id} 空：{}"
result = prompt_refs.substitute_placeholders(text, agent_id="lilith")
check("只替换白名单键", result == "好感度：{好感度：65} 占位：lilith 未知：{user_id} 空：{}", result)
check("无花括号时零改动", prompt_refs.substitute_placeholders("无占位", agent_id="x") == "无占位")
check("大小写不敏感找文件", prompt_refs.resolve_named_file(TMP / "alice", "soul.md") is not None)

print("\n== 目标文件为空 ==")
write("alice", "SOUL.md", "   \n")
expect_error("空目标文件 → 报错", lambda: build("lilith"))
write("alice", "SOUL.md", ALICE_SOUL)
check("恢复后又能正常构建", build("lilith").messages[1]["content"] == ALICE_SOUL.strip())

shutil.rmtree(TMP, ignore_errors=True)
print()
print("FAILURES:", FAILURES if FAILURES else "none")
sys.exit(1 if FAILURES else 0)
