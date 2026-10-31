"""dashboard 前端顶层符号冲突自检。

背景：dashboard 的 JS 都是**同一全局作用域**下的普通脚本（没有 ESM 模块），
两个文件里出现同名 `let/const/var/function` 时，后加载那份会整份 SyntaxError 不执行，
页面表现就是"点了没反应 / 一片空白"，而且浏览器控制台之外很难发现。

本测试扫描 static/assets/js 下所有脚本的顶层声明，出现重名即失败。
"""

from __future__ import annotations

import re
import sys
from collections import defaultdict
from pathlib import Path

JS_DIR = Path(__file__).resolve().parent.parent / "static" / "assets" / "js"
DECL = re.compile(r"^(?:let|const|var|function|async function)\s+([A-Za-z_$][\w$]*)", re.M)

FAILS: list[str] = []
CHECKS = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global CHECKS
    CHECKS += 1
    if cond:
        print(f"  ok    {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL  {name} {extra}")


def main() -> int:
    files = sorted(JS_DIR.glob("*.js"))
    check("找到前端脚本", len(files) >= 10, f"{len(files)} 个")

    owner: dict[str, list[str]] = defaultdict(list)
    for path in files:
        text = path.read_text(encoding="utf-8")
        for symbol in set(DECL.findall(text)):
            owner[symbol].append(path.name)

    dups = {sym: names for sym, names in owner.items() if len(names) > 1}
    check("没有跨文件重复的顶层符号", not dups, str(dups))

    # 已知必须唯一的关键符号（防止有人再引入同名变量）
    for symbol in ("tpTools", "tpPermTools", "tpRefresh", "refreshPods", "switchTab"):
        holders = owner.get(symbol, [])
        if symbol == "tpPermTools":
            check(f"{symbol} 只属于 toolperm.js", holders == ["toolperm.js"], str(holders))
        elif symbol == "tpTools":
            check("tpTools 只属于 tool_panel.js", holders == ["tool_panel.js"], str(holders))
        elif symbol == "tpRefresh":
            check("tpRefresh 只定义一次", holders == ["toolperm.js"], str(holders))

    # toolperm.js 不能再声明 tpTools（历史 bug：整份脚本不执行）
    js = (JS_DIR / "toolperm.js").read_text(encoding="utf-8")
    check("toolperm.js 不再声明 tpTools", not re.search(r"^let tpTools\b", js, re.M))
    check("toolperm.js 定义了 tpRefresh", "function tpRefresh" in js or "tpRefresh =" in js)

    print()
    if FAILS:
        print(f"FAILED {len(FAILS)}: {FAILS}")
        return 1
    print(f"ALL OK — {CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
