"""步数上限中断的收尾摘要自检（纯函数 + 结构断言，不联网）。"""
import ast
import sys
from pathlib import Path

SERVICE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE))

from app import interrupt_summary as I  # noqa: E402

FAILS = []
CHECKS = 0


def check(name, cond, extra=""):
    global CHECKS
    CHECKS += 1
    print(f"  {'ok  ' if cond else 'FAIL'}  {name} {extra if not cond else ''}")
    if not cond:
        FAILS.append(name)


print("build_transcript：两本账合并 / 截断 / 空值")
main_log = [f"步骤{i} main: 产出第 {i} 段" for i in range(1, 6)]
tool_log = ["调用工具:shell|ls", "结果:" + ("x" * 900), "结果: 完成 A"]
t = I.build_transcript(main_log, tool_log, per_entry_chars=100, max_chars=5000)
check("先主日志后工具日志", t.index("主日志") < t.index("工具执行日志"), t[:60])
check("长条目被截断并标注", "…" in t and "x" * 200 not in t, t[-80:])
check("空列表安全", I.build_transcript([], []) == "")
check("含 None 不产生 None 文本", "None" not in I.build_transcript([None, "", " ok "], [None]))
check("总长受控", len(I.build_transcript(main_log * 50, tool_log * 50, max_chars=800)) <= 900)
check("超长提示存在", "已截断" in I.build_transcript(main_log * 50, tool_log * 50, max_chars=800))

print("reader_messages")
msgs = I.reader_messages(main_log, tool_log)
check("两条消息（system+user）", len(msgs) == 2 and msgs[0]["role"] == "system", len(msgs))
check("提示词要求三段式", all(k in msgs[0]["content"] for k in ("已完成", "关键数据", "未完成")))
check("空日志 → 不给 reader 空消息", I.reader_messages([], []) == [])
check("user 消息含转录", "主日志" in msgs[1]["content"])

print("render_final_text")
text = I.render_final_text("已完成：下载了字幕\n未完成：抽帧", steps=31, limit=30)
check("带步数说明", "31/30" in text and "已中止" in text, text[:60])
check("带摘要正文", "已完成：下载了字幕" in text)
check("空摘要退回原文案", I.render_final_text("", steps=31, limit=30) == "任务执行步数过多（31/30 步），已中止。")

print("fallback_summary")
fb = I.fallback_summary(main_log, tool_log, keep=2)
check("兜底含最后记录", "步骤5" in fb and "完成 A" in fb, fb[:80])
check("兜底标明 reader 不可用", "reader 不可用" in fb)
check("全空 → 空串", I.fallback_summary([], []) == "")

print("结构：agent_runtime 接线（防回退到直接 return）")
src = (SERVICE / "app" / "agent_runtime.py").read_text(encoding="utf-8")
tree = ast.parse(src)
names = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
check("存在 _finalize_step_limit", "_finalize_step_limit" in names)
fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_finalize_step_limit")
calls = {
    getattr(c.func, "attr", getattr(c.func, "id", ""))
    for c in ast.walk(fn)
    if isinstance(c, ast.Call)
}
check("收尾里调用 reader_messages", "reader_messages" in calls, calls)
check("收尾里写会话（_append_turn_for_recipients）", "_append_turn_for_recipients" in calls, calls)
check("收尾里持久化工具摘要", "_persist_tool_summary" in calls, calls)
cont = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_continue_task")
cont_calls = {
    getattr(c.func, "attr", getattr(c.func, "id", ""))
    for c in ast.walk(cont)
    if isinstance(c, ast.Call)
}
check("_continue_task 步数超限走 _finalize_step_limit", "_finalize_step_limit" in cont_calls, cont_calls)
check("main_log 派发时写对话指令", 'main_log.append(f"对话:{target_agent_id}|' in src)
check("main_log 弹栈时只写回复", 'main_log.append(f"收到回复:{returning_agent_id}|' in src)

print()
if FAILS:
    print("FAILED:", FAILS)
    raise SystemExit(1)
print(f"ALL OK — {CHECKS} checks")
