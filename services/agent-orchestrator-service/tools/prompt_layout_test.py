"""提示词分段布局自检：工具记录独立成段、免责框只管"回忆"、顺序正确。

覆盖：
1) split_tool_log_block：剥离工具日志块、保留【收到返回】/【用户插话】、无标记时不丢正文；
2) split_context_messages：profile/摘要/长期记忆/历史 四段拆分；
3) scope_summary_message：摘要带范围声明、正文不变；
4) AST 结构：build_context 的 base_head 顺序必须是
   system → profile → summary → tool_log → (免责框/记忆/历史) → task_memory → user_input。
"""
import ast
import sys
from pathlib import Path

SERVICE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE))

from app.context_builder import (  # noqa: E402
    PROFILE_MARKER,
    SUMMARY_MARKER,
    SUMMARY_SCOPE_NOTE,
    TOOL_LOG_HEADER,
    TOOL_LOG_MARKER,
    scope_summary_message,
    split_context_messages,
    split_tool_log_block,
)

FAILS = []
CHECKS = 0


def check(name, cond, extra=""):
    global CHECKS
    CHECKS += 1
    print(f"  {'ok  ' if cond else 'FAIL'}  {name} {extra if not cond else ''}")
    if not cond:
        FAILS.append(name)


print("split_tool_log_block")
log = f"{TOOL_LOG_MARKER}\n调用工具:browser|open\n结果: 页面已打开"
body, rest = split_tool_log_block(log + "\n\n【收到返回】\n页面标题：B站")
check("剥出日志块", body.startswith(TOOL_LOG_MARKER) and "结果: 页面已打开" in body)
check("其余正文保留", rest.startswith("【收到返回】") and "页面标题" in rest, rest[:40])
b2, r2 = split_tool_log_block(log + "\n\n【用户插话】\n先别弄了")
check("插话为界", "先别弄了" in r2 and "先别弄了" not in b2)
b3, r3 = split_tool_log_block(f"{TOOL_LOG_MARKER}\n调用工具:shell|ls")
check("只有日志无结果时不丢", b3 and r3 == "")
b4, r4 = split_tool_log_block("普通对话内容")
check("无标记原样返回", b4 == "" and r4 == "普通对话内容")
b5, r5 = split_tool_log_block("")
check("空串安全", b5 == "" and r5 == "")
b6, r6 = split_tool_log_block(f"\n\n  {TOOL_LOG_MARKER}\n调用工具:x")
check("前导空白容忍", b6.startswith(TOOL_LOG_MARKER))

print("split_context_messages")
msgs = [
    {"role": "system", "content": f"{PROFILE_MARKER}\n- 名字：蝶殇"},
    {"role": "system", "content": f"{SUMMARY_MARKER}\n早前聊了路由表"},
    {"role": "system", "content": "【长期记忆】偏好：喜欢简洁"},
    {"role": "system", "content": "【长期记忆】事件：昨天在写报告"},
    {"role": "user", "content": "<2195239690>: 继续"},
    {"role": "assistant", "content": "<main>: 好的"},
]
prof, summ, mem, hist = split_context_messages(msgs)
check("profile 拆出", len(prof) == 1 and prof[0]["content"].startswith(PROFILE_MARKER))
check("摘要拆出", len(summ) == 1 and summ[0]["content"].startswith(SUMMARY_MARKER))
check("长期记忆拆出", len(mem) == 2)
check("历史剩余 2 条", len(hist) == 2 and hist[0]["role"] == "user")
check("空输入安全", split_context_messages([]) == ([], [], [], []))
check("None 安全", split_context_messages(None) == ([], [], [], []))

print("scope_summary_message")
scoped = scope_summary_message({"role": "system", "content": f"{SUMMARY_MARKER}\n早前聊了路由表"})
check("带范围声明", SUMMARY_SCOPE_NOTE in scoped["content"])
check("正文不变", "早前聊了路由表" in scoped["content"])
check("role 仍为 system", scoped["role"] == "system")

print("工具记录段文案")
check("声明为事实且不受免责框限制", "事实记录" in TOOL_LOG_HEADER and "不适用下方历史免责框" in TOOL_LOG_HEADER)
check("声明优先采信", "优先采信" in TOOL_LOG_HEADER)

print("结构：base_head 顺序")
src = (SERVICE / "app" / "context_builder.py").read_text(encoding="utf-8")
tree = ast.parse(src)
fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "build_send_context")
stmts = [n for n in fn.body if isinstance(n, ast.Assign) and getattr(n.targets[0], "id", "") == "base_head"]
check("找到 base_head 赋值", bool(stmts))
if stmts:
    value = stmts[0].value
    names = []
    def walk(node):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            walk(node.left); walk(node.right)
        elif isinstance(node, ast.Name):
            names.append(node.id)
    walk(value)
    check("顺序 = system→profile→summary→task_memory→tool_log→记忆块→输入",
          names == ["system_prompt_messages", "profile_messages", "summary_messages",
                    "task_memory_messages", "tool_log_messages", "long_context_message",
                    "user_input_messages"], names)
check("免责框只贴在记忆+历史前", "[\"role\": \"system\", \"content\": HISTORY_DISCLAIMER}]" in src.replace("\n", "") or
      "[{\"role\": \"system\", \"content\": HISTORY_DISCLAIMER}]" in src)

print()
if FAILS:
    print("FAILED:", FAILS)
    raise SystemExit(1)
print(f"ALL OK — {CHECKS} checks")
