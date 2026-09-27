"""prompt_refs / context_builder 的提示词指令与占位符冒烟测试。

不依赖集群与下游服务：用临时目录造几个假智能体，直接跑
`@ref=` 跳转、`@when=` 注入条件与 `{agent_id}` 替换，覆盖全部报错分支。

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
from app.context_builder import ContextBuilder, is_group_session  # noqa: E402
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


def filtered(parts, *, is_group: bool):
    """在假实例上跑发送期过滤，模拟一次群聊 / 非群聊请求。"""
    agent = SimpleNamespace(
        id="henry",
        user_id="default",
        session_id="qq_g_1" if is_group else "qq_1",
        config={},
        system_prompt=list(parts.messages),
        _identity_system_indexes=[],
        _when_system_rules=list(parts.when_rules),
    )
    return ContextBuilder(agent).effective_system_prompt(is_group=is_group)


def expect_error(label: str, fn) -> str:
    """断言 fn 抛 PromptRefError；返回报错信息（空串 = 没报错 / 抛了别的异常）。"""
    try:
        fn()
    except PromptRefError as exc:
        check(label, True, str(exc)[:110])
        return str(exc)
    except Exception as exc:  # 非预期异常同样算失败
        check(label, False, f"抛了别的异常 {type(exc).__name__}: {exc}")
        return ""
    check(label, False, "没有报错")
    return ""


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
    "grace":  {"files": ["SOUL.md"]},
    "henry":  {"files": ["SOUL.md", "USER.md"]},
    "ivan":   {"files": ["SOUL.md"]},
    "jane":   {"files": ["SOUL.md"]},
    "judy":   {"files": ["SOUL.md"]},
    "kate":   {"files": ["SOUL.md"]},
    "leo":    {"files": ["SOUL.md"]},
    "mona":   {"files": ["SOUL.md", "PROCESS.md"]},
    "nick":   {"files": ["SOUL.md", "world_info.md"]},
    "olga":   {"files": ["SOUL.md", "TOOL.md"]},
    "quinn":  {"files": ["USER.md"]},
    "rita":   {"files": ["SOUL.md"]},
    "p1":     {"files": ["SOUL.md"]},
    "p2":     {"files": ["SOUL.md"]},
    "p3":     {"files": ["SOUL.md"]},
    "d1":     {"files": ["SOUL.md"]},
    "d2":     {"files": ["SOUL.md"]},
    "d3":     {"files": ["SOUL.md"]},
    "d4":     {"files": ["SOUL.md"]},
    "c1":     {"files": ["SOUL.md"]},
    "c2":     {"files": ["SOUL.md"]},
    "h1":     {"files": ["SOUL.md"]},
    "h2":     {"files": ["SOUL.md"]},
    "x1":     {"files": ["SOUL.md"]},
    "x2":     {"files": ["SOUL.md"]},
    "x3":     {"files": ["SOUL.md"]},
    "x4":     {"files": ["SOUL.md"]},
    "x5":     {"files": ["SOUL.md"]},
    "x6":     {"files": ["SOUL.md"]},
    "x7":     {"files": ["SOUL.md"]},
    "x8":     {"files": ["SOUL.md"]},
    "x9":     {"files": ["SOUL.md"]}
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
write("grace", "SOUL.md", "@ref=lilith\n")  # 两跳：grace → lilith → alice（现在合法）

# ── @ref= 新写法：任意文件路径（`目录/文件名`）───────────────
# 中文目录 + 任意文件名（老实现被 [A-Za-z0-9_.@-] 白名单挡住）
write("人设合集", "凌伊.md", "# 凌伊\n这是凌伊的人设正文\n")
write("人设合集", "带id.md", "人设：{agent_id}\n")
write("p1", "SOUL.md", "@ref=人设合集/凌伊.md\n")  # 路径形式
write("p2", "SOUL.md", "@ref=alice/world_info.md\n")  # 指向别的智能体的**非同名**文件
write("p3", "SOUL.md", "@ref=alice/soul.md\n")  # 末段大小写不敏感
write("h1", "SOUL.md", "@ref=人设合集/带id.md\n")  # 占位符链上的中间跳
write("h2", "SOUL.md", "@ref=h1\n")  # 两跳 + 占位符取最外层请求方
# 深度：3 跳仍可，4 跳报错
write("d1", "SOUL.md", "@ref=d2\n")
write("d2", "SOUL.md", "@ref=d3\n")
write("d3", "SOUL.md", "@ref=d4\n")
write("d4", "SOUL.md", "# 深度链终点\nd4 正文\n")
# 成环：不专门检测，靠深度上限拦下
write("c1", "SOUL.md", "@ref=c2\n")
write("c2", "SOUL.md", "@ref=c1\n")
# 非法目标
write("x1", "SOUL.md", "@ref=../secret.md\n")  # 穿越
write("x2", "SOUL.md", "@ref=/etc/passwd\n")  # 绝对路径
write("x3", "SOUL.md", "@ref=人设合集/\n")  # 空段（结尾多写 `/`）
write("x4", "SOUL.md", "@ref=人设合集//凌伊.md\n")  # 空段（双斜杠）
write("x5", "SOUL.md", "@ref={agent_id}\n")  # 占位符不能当目标
write("x6", "SOUL.md", "@ref=人设合集/空文件.md\n")  # 目标为空
write("x7", "SOUL.md", "@ref=人设合集/带when.md\n")  # 目标里带 @when → 仍报错
write("x8", "SOUL.md", "@ref=人设合集/带正文.md\n")  # 目标里 @ref= + 正文 → 报错
write("x9", "SOUL.md", "@ref=人设合集/nobody.md\n")  # 目标文件不存在
write("人设合集", "空文件.md", "   \n")
write("人设合集", "带when.md", "@when=group\n只群聊\n")
write("人设合集", "带正文.md", "@ref=凌伊.md\n多余的正文\n")

# ── @when= 注入条件 ──────────────────────────────────────────
HENRY_SOUL = "# 角色设定\nhenry 的 SOUL 正文\n"
HENRY_USER = "# 用户信息\nhenry 的 USER 正文\n"
write("henry", "SOUL.md", "@when=group\n" + HENRY_SOUL)  # 仅群聊
write("henry", "USER.md", "@when=single\n" + HENRY_USER)  # 仅非群聊

write("ivan", "SOUL.md", "@when=group\n@ref=alice\n")  # 与 @ref= 共存（条件在前）
write("jane", "SOUL.md", "@ref=alice\n@when=single\n")  # 与 @ref= 共存（条件在后）
write("judy", "SOUL.md", "@when=always\n")  # 取值非法 → 报错
write("kate", "SOUL.md", "@when=group\n@when=single\n正文\n")  # 重复 → 报错
write("leo", "SOUL.md", "正文\n@when=group\n")  # 指令不在开头 → 报错
write("mona", "PROCESS.md", "@when=group\n")  # PROCESS 内容不读 → 报错
write("nick", "world_info.md", "@when=group\n")  # world_info 不是普通段 → 报错
write("olga", "TOOL.md", "@when=group\n")  # TOOL 暂不支持 → 报错
write("quinn", "USER.md", "@ref=henry\n")  # 目标文件含 @when → 报错
write("rita", "SOUL.md", "@when = GROUP\nrita 的 SOUL\n")  # 容错：空格 + 大写

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

print("\n== @ref= 新写法：目录/文件名（任意文件）==")
grace = build("grace")
check("两跳：grace → lilith → alice 现在合法",
      grace.messages[1]["content"] == ALICE_SOUL.strip(), repr(grace.messages[1]["content"][:24]))

p1 = build("p1")
check("路径形式：中文目录 + 任意文件名",
      p1.messages[1]["content"] == "# 凌伊\n这是凌伊的人设正文", repr(p1.messages[1]["content"][:24]))

p2 = build("p2")
check("路径形式：可指向别的智能体的非同名文件（且替换成请求方 id）",
      p2.messages[1]["content"] == "# 核心设定\n你的id是p2", repr(p2.messages[1]["content"][:40]))

p3 = build("p3")
check("路径形式：末段大小写不敏感", p3.messages[1]["content"] == ALICE_SOUL.strip())

h2 = build("h2")
check("链上 {agent_id} 取最外层请求方", h2.messages[1]["content"] == "人设：h2", repr(h2.messages[1]["content"]))
check("中间跳的指针内容不进入结果", "@ref" not in h2.messages[1]["content"])

d1 = build("d1")
check("3 跳仍可解析", d1.messages[1]["content"] == "# 深度链终点\nd4 正文", repr(d1.messages[1]["content"][:24]))
write("d4", "SOUL.md", "@ref=d5\n")
write("d5", "SOUL.md", "d5 正文\n")
deep_msg = expect_error("超过深度上限 → 报错", lambda: build("d1"))
check("深度报错里带跳数与完整链",
      str(prompt_refs.MAX_REF_DEPTH) in deep_msg and "→" in deep_msg, deep_msg[:110])
write("d4", "SOUL.md", "# 深度链终点\nd4 正文\n")
cycle_msg = expect_error("成环 → 被深度上限拦下（无环检测）", lambda: build("c1"))
check("成环报错里带完整链（看得出是环）",
      "→" in cycle_msg and "c1/SOUL.md" in cycle_msg, cycle_msg[:110])

print("\n== @ref= 非法目标 ==")
expect_error("目标穿越 .. → 报错", lambda: build("x1"))
expect_error("绝对路径 → 报错", lambda: build("x2"))
expect_error("目标结尾多写 / → 报错", lambda: build("x3"))
expect_error("目标含双斜杠 → 报错", lambda: build("x4"))
expect_error("不能用 {占位符} 当目标 → 报错", lambda: build("x5"))
expect_error("目标文件为空 → 报错", lambda: build("x6"))
expect_error("目标里带 @when=（任何跳数下都禁止）→ 报错", lambda: build("x7"))
expect_error("目标里 @ref= 与正文并存 → 报错", lambda: build("x8"))
expect_error("目标文件不存在 → 报错", lambda: build("x9"))

print("\n== 纯函数层面 ==")
check("未命中原样返回（同一对象）", prompt_refs.resolve_text(
    "普通正文", agent_id="x", filename="SOUL.md", system_prompt_dir=TMP) == "普通正文")
text = "好感度：{好感度：65} 占位：{agent_id} 未知：{user_id} 空：{}"
result = prompt_refs.substitute_placeholders(text, agent_id="lilith")
check("只替换白名单键", result == "好感度：{好感度：65} 占位：lilith 未知：{user_id} 空：{}", result)
check("无花括号时零改动", prompt_refs.substitute_placeholders("无占位", agent_id="x") == "无占位")
check("大小写不敏感找文件", prompt_refs.resolve_named_file(TMP / "alice", "soul.md") is not None)

print("\n== @when= 注入条件：解析与登记 ==")
henry = build("henry")
check("指令行不进入内容（SOUL）", "@when" not in henry.messages[1]["content"], repr(henry.messages[1]["content"][:20]))
check("指令行不进入内容（USER）", "@when" not in henry.messages[2]["content"], repr(henry.messages[2]["content"][:20]))
check("SOUL 正文完整（首行未被吃掉）", henry.messages[1]["content"] == HENRY_SOUL.strip())
check("USER 正文完整", henry.messages[2]["content"] == HENRY_USER.strip())
check("条件已按段下标登记", henry.when_rules == [(1, "group"), (2, "single")], str(henry.when_rules))

print("\n== @when= 注入条件：发送期过滤 ==")
group_msgs = [m["content"] for m in filtered(henry, is_group=True)]
single_msgs = [m["content"] for m in filtered(henry, is_group=False)]
check("群聊：注入 group 段、剔除 single 段（USER.md 不注入）",
      HENRY_SOUL.strip() in group_msgs and HENRY_USER.strip() not in group_msgs,
      f"段数={len(group_msgs)}")
check("非群聊：注入 single 段、剔除 group 段",
      HENRY_USER.strip() in single_msgs and HENRY_SOUL.strip() not in single_msgs,
      f"段数={len(single_msgs)}")
check("无条件段不受影响（GLOBAL_SETTING 两边都在）",
      len(group_msgs) == 2 and len(single_msgs) == 2)

print("\n== @when= 与 @ref= 共存 ==")
ivan = build("ivan")
jane = build("jane")
check("条件在前：@ref= 仍整文件替换", ivan.messages[1]["content"] == ALICE_SOUL.strip())
check("条件在前：条件已登记", ivan.when_rules == [(1, "group")], str(ivan.when_rules))
check("条件在后：@ref= 仍整文件替换", jane.messages[1]["content"] == ALICE_SOUL.strip())
check("条件在后：条件已登记", jane.when_rules == [(1, "single")], str(jane.when_rules))
check("共存时正文里没有残留指令",
      not prompt_refs.has_any_directive(ivan.messages[1]["content"]))
rita = build("rita")
check("容错：`@when = GROUP` 归一为 group", rita.when_rules == [(1, "group")], str(rita.when_rules))
check("容错：大小写与空格不影响正文", rita.messages[1]["content"] == "rita 的 SOUL")

print("\n== @when= 报错分支 ==")
expect_error("@when= 取值非法 → 报错", lambda: build("judy"))
expect_error("@when= 写两行 → 报错", lambda: build("kate"))
expect_error("指令行在正文之后 → 报错", lambda: build("leo"))
expect_error("PROCESS 声明文件写 @when= → 报错", lambda: build("mona"))
expect_error("world_info.md 写 @when= → 报错", lambda: build("nick"))
expect_error("TOOL.md 写 @when= → 报错", lambda: build("olga"))
expect_error("目标文件内部含 @when= → 报错", lambda: build("quinn"))

print("\n== 群聊判定 is_group_session ==")
check("qq_g_ 前缀（群）", is_group_session("qq_g_1065434062", "1234567") is True)
check("grp_ 前缀（群）", is_group_session("grp_abc", None) is True)
check("私聊 session 不算群", is_group_session("qq_1234567", "1234567") is False)
check("user_id 带前缀也算群（兜底渠道）", is_group_session("", "qq_g_1") is True)
check("两者都空不算群", is_group_session(None, None) is False)
check("前后空格不影响判定", is_group_session("  qq_g_1  ", "") is True)

print("\n== resolve_segment 纯函数层面 ==")
seg = prompt_refs.resolve_segment(
    "普通正文", agent_id="x", filename="SOUL.md", system_prompt_dir=TMP)
check("无指令：原样返回且零拷贝", seg.text == "普通正文" and seg.when == "")
seg2 = prompt_refs.resolve_segment(
    "@when=group\n\n正文\n", agent_id="x", filename="SOUL.md", system_prompt_dir=TMP)
check("摘掉指令与其后空行", seg2.text == "正文" and seg2.when == "group", repr(seg2.text))
try:
    prompt_refs.resolve_segment(
        "@when=group\n正文\n", agent_id="x", filename="TOOL.md",
        system_prompt_dir=TMP, allow_when=False)
    check("allow_when=False 遇 @when= → 报错", False, "没有报错")
except PromptRefError as exc:
    check("allow_when=False 遇 @when= → 报错", True, str(exc)[:90])

print("\n== 目标文件为空 ==")
write("alice", "SOUL.md", "   \n")
expect_error("空目标文件 → 报错", lambda: build("lilith"))
write("alice", "SOUL.md", ALICE_SOUL)
check("恢复后又能正常构建", build("lilith").messages[1]["content"] == ALICE_SOUL.strip())

shutil.rmtree(TMP, ignore_errors=True)
print()
print("FAILURES:", FAILURES if FAILURES else "none")
sys.exit(1 if FAILURES else 0)
