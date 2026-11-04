"""结构自检：定时任务扫描循环必须"先执行、再重排；重复任务永不删除"。

这条测试是为了防止一次真实事故重演：
    · 修复"循环任务被误删"时改错了缩进区间，把 `if in_window_now: execute_timer_task(task)`
      一起替换掉了 → 任务时间照常递增（重排生效），但**从不投递**。
    · 另一次（老代码）：`catch_up_in_window(now, ...)` 返回 now → `nxt > now` 不成立 →
      循环任务被 `os.remove` 删掉。
纯 AST 断言，不跑服务、不依赖网络。
"""
import ast
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "app" / "timer_task.py"
FAILS = []
CHECKS = 0


def check(name, cond, extra=""):
    global CHECKS
    CHECKS += 1
    print(f"  {'ok  ' if cond else 'FAIL'}  {name} {extra if not cond else ''}")
    if not cond:
        FAILS.append(name)


tree = ast.parse(SRC.read_text(encoding="utf-8"))
scan = next(
    (n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and "scan" in n.name), None
)
check("找到扫描循环函数", scan is not None, "timer_scan_loop 不存在")
if scan is not None:
    exec_calls = [
        n
        for n in ast.walk(scan)
        if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "execute_timer_task"
    ]
    check("扫描循环里仍然调用 execute_timer_task（投递入口）", len(exec_calls) == 1, f"count={len(exec_calls)}")

    removes = [
        n
        for n in ast.walk(scan)
        if isinstance(n, ast.Call) and getattr(n.func, "attr", "") == "remove"
    ]
    check("删除点只有 2 个（一次性任务丢弃 + 一次性任务执行完）", len(removes) == 2, f"count={len(removes)}")

    # execute 必须在所有 os.remove 之前（先投递，再决定删不删）
    if exec_calls and removes:
        check("execute 早于重复分支兜底 remove", exec_calls[0].lineno < max(r.lineno for r in removes),
              (exec_calls[0].lineno, [r.lineno for r in removes]))

    # next_occurrence 必须被用（保证严格递增）
    used = [
        n
        for n in ast.walk(scan)
        if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "next_occurrence"
    ]
    check("重排使用 next_occurrence（严格递增）", len(used) == 1, f"count={len(used)}")

module_names = {n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
src_text = SRC.read_text(encoding="utf-8")
check("next_occurrence 已从 time_parser 导入", "    next_occurrence," in src_text)
check("重复分支有永不删除注释与 continue", "永不删除" in src_text and "continue" in src_text)

print()
if FAILS:
    print("FAILED:", FAILS)
    sys.exit(1)
print(f"ALL OK — {CHECKS} checks passed")
