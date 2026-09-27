"""prompt_plan（用户输入里的 RPG 结构化调用）解析冒烟测试。

纯函数、不依赖集群与下游服务。覆盖三类分支：
  - 不是结构化调用 → None（原始服务零影响）
  - 确认是结构化调用但不合法 → PromptPlanError（显式报错，不静默降级）
  - 合法 → 计划内容正确，且填充阶段一律是 {role, content}

用法：
    python tools/prompt_plan_smoke_test.py
"""

import json
import os
import sys
import tempfile
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVICE_DIR))

TMP = Path(tempfile.mkdtemp(prefix="prompt_plan_"))
os.environ["SYSTEM_PROMPT_DIR"] = str(TMP)
os.environ["AGENT_CONFIG_PATH"] = str(TMP / "agent_list.json")

from app.prompt_plan import (  # noqa: E402
    BLOCK_WHITELIST,
    INSERT_ANCHORS,
    PromptPlanError,
    insert_messages,
    parse_prompt_plan,
)

FAILURES: list[str] = []
OK = 0


def ok(label: str) -> None:
    global OK
    OK += 1
    print(f"  OK   {label}")


def fail(label: str, detail: str = "") -> None:
    FAILURES.append(label)
    print(f"  FAIL {label}" + (f"\n       {detail}" if detail else ""))


def j(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


def expect_none(label: str, content) -> None:
    try:
        result = parse_prompt_plan(content)
    except PromptPlanError as exc:
        return fail(label, f"不该报错，却报了：{exc}")
    if result is None:
        ok(label)
    else:
        fail(label, f"期望 None，实际 {result!r}")


def expect_error(label: str, content, contains: str | None = None) -> None:
    try:
        result = parse_prompt_plan(content)
    except PromptPlanError as exc:
        if contains and contains not in str(exc):
            return fail(label, f"报错信息里没有 {contains!r}：{exc}")
        return ok(label)
    fail(label, f"期望 PromptPlanError，实际 {result!r}")


def expect_plan(
    label: str,
    content,
    *,
    text=None,
    exclude_block=None,
    inserts=None,
    present=None,
) -> None:
    try:
        plan = parse_prompt_plan(content)
    except PromptPlanError as exc:
        return fail(label, f"不该报错，却报了：{exc}")
    if plan is None:
        return fail(label, "期望 PromptPlan，实际 None")
    problems = []
    if text is not None and plan.text != text:
        problems.append(f"text: {plan.text!r} != {text!r}")
    if exclude_block is not None and plan.exclude_block != exclude_block:
        problems.append(f"exclude_block: {plan.exclude_block!r} != {exclude_block!r}")
    if inserts is not None and plan.inserts != inserts:
        problems.append(f"inserts: {plan.inserts!r} != {inserts!r}")
    if present is not None and plan.present != present:
        problems.append(f"present: {plan.present!r} != {present!r}")
    if problems:
        return fail(label, "; ".join(problems))
    ok(label)


print("── 契约常量 ──")
assert BLOCK_WHITELIST == ("tool", "world_info", "memory"), BLOCK_WHITELIST
assert INSERT_ANCHORS == ("before_persona", "before_history", "before_input", "after_input"), INSERT_ANCHORS
ok("只开放三个可屏蔽块（tool / world_info / memory）")
ok("四个插入位置且顺序固定")
ok("屏蔽键名为 exclude_block（不再是 blocks）")


print("── 不是结构化调用：返回 None，该内容当普通内容原样处理 ──")
expect_none("普通中文聊天", "我们往北走吧")
expect_none("空字符串", "")
expect_none("纯空白", "   \n  ")
expect_none("坏 JSON（以 { 开头，解析不了 → 当普通内容）", '{"text": ')
expect_none("不是 { 开头", "[1, 2, 3]")
expect_none("JSON 字符串", j("hello"))
expect_none("JSON 数字", "123")
expect_none("JSON 布尔", "true")
expect_none("JSON null", "null")
expect_none("空对象（一个已知键都没有）", "{}")
expect_none("只有未知键的对象", j({"foo": 1}))
expect_none("content 是 None", None)
expect_none("content 是数字", 123)
expect_none("content 是列表", ["x"])

print("── 全段必须就是一个 JSON 对象（前后不许夹正文）──")
expect_none("JSON 后面还有文字", j({"text": "x"}) + " 后面还有字")
expect_none("JSON 前面还有文字", "前面有字 " + j({"text": "x"}))
expect_none("前后都有文字", "前 " + j({"text": "x"}) + " 后")
expect_plan("前后只有空白/换行 → 可以", "\n\n  " + j({"text": "x"}) + "  \n\n", text="x")


print("── 确认是结构化调用但不合法：必须显式报错 ──")
expect_error("有 exclude_block 但没有 text", j({"exclude_block": ["tool"]}), "缺少必填键 text")
expect_error("有 insert 但没有 text", j({"insert": []}), "缺少必填键 text")
expect_error("有 present 但没有 text", j({"present": ["a"]}), "缺少必填键 text")
expect_error("text 不是字符串", j({"text": 1}), "text 必须是字符串")
expect_error("exclude_block 不是列表（写成字符串）",
             j({"text": "x", "exclude_block": "tool"}), "exclude_block 必须是列表")
expect_error("exclude_block 含未知块名", j({"text": "x", "exclude_block": ["soul"]}), "不是可屏蔽的块")
expect_error("exclude_block 含空字符串", j({"text": "x", "exclude_block": [""]}), "空字符串")
expect_error("exclude_block 含非字符串", j({"text": "x", "exclude_block": [1]}), "每一项必须是字符串")
expect_error("insert 不是列表", j({"text": "x", "insert": {}}), "insert 必须是列表")
expect_error("insert 项不是对象", j({"text": "x", "insert": ["before_input"]}), "必须是对象")
expect_error("未知锚点", j({"text": "x", "insert": [{"at": "before_soul", "content": ["a"]}]}), "不是插入位置")
expect_error("缺 at", j({"text": "x", "insert": [{"content": ["a"]}]}), "不是插入位置")
expect_error("缺 content", j({"text": "x", "insert": [{"at": "before_input"}]}), "缺少 content")
expect_error("content 不是列表", j({"text": "x", "insert": [{"at": "before_input", "content": "a"}]}), "必须是列表")
expect_error("content 是空列表", j({"text": "x", "insert": [{"at": "before_input", "content": []}]}), "不能是空列表")
expect_error("content 项非字符串",
             j({"text": "x", "insert": [{"at": "before_input", "content": [1]}]}), "每一项必须是字符串")
expect_error("content 项为空字符串",
             j({"text": "x", "insert": [{"at": "before_input", "content": ["  "]}]}), "空字符串")
expect_error("present 不是列表", j({"text": "x", "present": "alice"}), "present 必须是列表")
expect_error("present 项非字符串", j({"text": "x", "present": [1]}), "每一项必须是字符串")
expect_error("present 项为空字符串", j({"text": "x", "present": [""]}), "空字符串")


print("── 合法调用：计划内容正确 ──")
expect_plan("最小调用（只有 text）", j({"text": "我们走吧"}),
            text="我们走吧", exclude_block=(), inserts={}, present=())
expect_plan("text 允许为空字符串", j({"text": ""}), text="")
expect_plan("exclude_block 去重且保持顺序",
            j({"text": "x", "exclude_block": ["world_info", "tool", "world_info"]}),
            exclude_block=("world_info", "tool"))
expect_plan("exclude_block 三个全给",
            j({"text": "x", "exclude_block": ["tool", "world_info", "memory"]}),
            exclude_block=("tool", "world_info", "memory"))
expect_plan("present 去重且保持顺序", j({"text": "x", "present": ["bob", "alice", "bob"]}),
            present=("bob", "alice"))
expect_plan("present 为空列表 → 空", j({"text": "x", "present": []}), present=())
expect_plan("未知顶层键被忽略（前向兼容）", j({"text": "x", "extra": {"a": 1}}),
            text="x", exclude_block=(), inserts={}, present=())
expect_plan("旧键名 blocks 不再生效（被当未知键忽略）",
            j({"text": "x", "blocks": ["tool"]}), text="x", exclude_block=())

expect_plan(
    "四个锚点各自插入，顺序保持",
    j({"text": "x", "insert": [
        {"at": "before_persona", "content": ["甲", "乙"]},
        {"at": "before_history", "content": ["丙"]},
        {"at": "before_input", "content": ["丁"]},
        {"at": "after_input", "content": ["戊"]},
    ]}),
    inserts={
        "before_persona": ["甲", "乙"],
        "before_history": ["丙"],
        "before_input": ["丁"],
        "after_input": ["戊"],
    },
)

expect_plan(
    "同一锚点跨条目累积（顺序 = 声明顺序）",
    j({"text": "x", "insert": [
        {"at": "before_input", "content": ["第一"]},
        {"at": "before_input", "content": ["第二", "第三"]},
    ]}),
    inserts={"before_input": ["第一", "第二", "第三"]},
)

expect_plan(
    "只给一个位置也可以（不必全有）",
    j({"text": "x", "insert": [{"at": "after_input", "content": ["只有这个"]}]}),
    inserts={"after_input": ["只有这个"]},
)

expect_plan(
    "完整调用",
    j({
        "exclude_block": ["tool", "world_info"],
        "insert": [{"at": "before_history", "content": ["本场情势"]}],
        "present": ["alice", "bob"],
        "text": "玩家推开了门",
    }),
    text="玩家推开了门",
    exclude_block=("tool", "world_info"),
    inserts={"before_history": ["本场情势"]},
    present=("alice", "bob"),
)


print("── 填充阶段：一律 {role, content}，不出现裸字符串 ──")
_plan = parse_prompt_plan(j({"text": "x", "insert": [
    {"at": "before_input", "content": ["甲", "乙"]},
]}))
msgs = insert_messages(_plan, "before_input")
if msgs == [{"role": "system", "content": "甲"}, {"role": "system", "content": "乙"}]:
    ok("渲染成 system 消息列表且保序")
else:
    fail("渲染成 system 消息列表且保序", f"{msgs!r}")
if insert_messages(_plan, "before_persona") == []:
    ok("没有内容的锚点 → 空列表")
else:
    fail("没有内容的锚点 → 空列表", f"{insert_messages(_plan, 'before_persona')!r}")
if all(isinstance(m, dict) and set(m) == {"role", "content"} for m in msgs):
    ok("每条都是 {role, content} 两个键")
else:
    fail("每条都是 {role, content} 两个键", f"{msgs!r}")
if _plan.has_inserts():
    ok("has_inserts 判定")
else:
    fail("has_inserts 判定")
if parse_prompt_plan(j({"text": "x"})).has_inserts() is False:
    ok("无插入时 has_inserts 为 False")
else:
    fail("无插入时 has_inserts 为 False")
if parse_prompt_plan(j({"text": "x", "exclude_block": ["tool"]})).excluded() == frozenset({"tool"}):
    ok("excluded 判定")
else:
    fail("excluded 判定")


print()
if FAILURES:
    print(f"FAILED {len(FAILURES)} / {OK + len(FAILURES)}")
    for name in FAILURES:
        print(f"  - {name}")
    sys.exit(1)
print(f"ALL OK — {OK} checks")
