"""run_skill 参数边界自检：用临时技能验证四种分支。

跑法：`python tools/run_skill_args_test.py`
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

SERVICE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE_ROOT))

from app.skill_runtime import SkillRuntime  # noqa: E402

PASSED = 0
FAILED: list[str] = []

DEMO_SCRIPT = '''import argparse

parser = argparse.ArgumentParser(description="demo skill")
sub = parser.add_subparsers(dest="cmd", required=True)
search = sub.add_parser("search")
search.add_argument("keyword")
sub.add_parser("boom")
args = parser.parse_args()
if args.cmd == "search":
    print("SEARCH:", args.keyword)
else:
    raise SystemExit(3)
'''


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASSED
    if condition:
        PASSED += 1
        print(f"  ok    {name}")
    else:
        FAILED.append(name)
        print(f"  FAIL  {name}" + (f"   [{detail}]" if detail else ""))


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        skill_root = Path(tmp) / "skill"
        skill_dir = skill_root / "demo" / "scripts"
        skill_dir.mkdir(parents=True)
        (skill_dir / "demo.py").write_text(DEMO_SCRIPT, encoding="utf-8")
        workspace = Path(tmp) / "ws"
        workspace.mkdir()

        runtime = SkillRuntime()
        runtime.skill_root = skill_root

        # 1) 空参数 → 缺少参数 + 用法，且不执行脚本本体
        out = runtime.run_skill("demo", args=[], user_workspace=workspace, timeout=30)
        check("空参数 → 【缺少参数】", "【缺少参数：demo】" in out, out[:60])
        check("空参数 → 附上 --help 用法", "usage: demo.py" in out, out[:120])
        check("空参数 → 不出现执行失败", "执行失败" not in out, out[:120])

        # 2) 全是占位说明 → 同样走缺参分支，并说明忽略了什么
        out = runtime.run_skill("demo", args=[",参数留空)"], user_workspace=workspace, timeout=30)
        check("散文参数 → 【缺少参数】", "【缺少参数：demo】" in out, out[:80])
        check("散文参数 → 说明被忽略", "已忽略无效参数" in out and "参数留空" in out, out[:120])

        # 3) 参数不正确（未知子命令）→ 【参数不正确】 + 用法
        out = runtime.run_skill("demo", args=["nope"], user_workspace=workspace, timeout=30)
        check("未知子命令 → 【参数不正确】", "【参数不正确：demo】" in out, out[:80])
        check("未知子命令 → 附用法", "usage: demo.py" in out, out[:160])
        check("未知子命令 → 保留原始报错", "invalid choice" in out, out[:200])

        # 4) 参数正确 → 正常执行
        # 用 ASCII 关键词：Windows 控制台是 GBK，中文可能被本地编码影响，测的是逻辑不是编码
        out = runtime.run_skill("demo", args=["search", "cat"], user_workspace=workspace, timeout=30)
        check("正确参数 → 【执行完成】", "【执行完成：demo】" in out, out[:60])
        check("正确参数 → 有输出", "SEARCH: cat" in out, out[:160])

        # 5) 真失败（技能内部报错）→ 【执行失败】 + --help 指引
        out = runtime.run_skill("demo", args=["boom"], user_workspace=workspace, timeout=30)
        check("技能内部失败 → 【执行失败】", "【执行失败：demo】" in out, out[:60])
        check("技能内部失败 → 指引 --help", "--help" in out, out[:160])

        # 6) 占位符判定单测
        for text, expected in (
            ("", True), ("<关键词>", True), ("【商品名】", True), ("(参数)", True),
            (",参数留空)", True), ("参数留空", True), ("search", False), ("cat", False),
        ):
            got = runtime._looks_like_placeholder(text)
            check(f"占位判定 {text!r} → {expected}", got is expected, str(got))

        # 7) 参数清洗：反引号/引号被去掉
        cleaned, dropped = runtime._sanitize_skill_args(["`search`", '"cat"', "  ", ",参数留空)"])
        check("清洗：去反引号与引号", cleaned == ["search", "cat"], str(cleaned))
        check("清洗：记录被丢弃项", dropped == [",参数留空)"], str(dropped))

    print()
    if FAILED:
        print(f"FAILED {len(FAILED)}: {FAILED}")
        return 1
    print(f"ALL OK — {PASSED} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
