"""PROCESS 设置解析自检（orchestrator 侧，process*.md → max_items / max_chars）。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.context_builder import parse_process_settings  # noqa: E402
from app import prompt_refs  # noqa: E402

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


def main():
    print("正常写法")
    check("两项都配", parse_process_settings("max_items: 20\nmax_chars: 300") == (20, 300))
    check("全角冒号", parse_process_settings("max_items：15\nmax_chars：200") == (15, 200))
    check("等号写法", parse_process_settings("max_items=8") == (8, 0))
    check("带行尾注释", parse_process_settings("max_items: 5  # 最多 5 条") == (5, 0))
    check("大小写不敏感", parse_process_settings("MAX_ITEMS: 7") == (7, 0))
    check("夹在说明文字里也能解析",
          parse_process_settings("# 长期记录\n上面是说明\nmax_items: 12\n更多说明\n") == (12, 0))
    check("只写字数", parse_process_settings("max_chars: 100") == (0, 100))
    check("都不写 = 不限制", parse_process_settings("随便写点说明，没有配置") == (0, 0))
    check("空文件", parse_process_settings("") == (0, 0))

    print("非法写法显式报错")
    for bad in ("max_items: abc", "max_chars 300", "max_items:", "max_chars: -5"):
        try:
            parse_process_settings(bad)
            check(f"应报错：{bad!r}", False, "未抛异常")
        except prompt_refs.PromptRefError as exc:
            check(f"报错：{bad!r}", "写法不对" in str(exc), str(exc)[:60])
        except Exception as exc:  # noqa: BLE001
            check(f"报错类型：{bad!r}", False, type(exc).__name__)


    print("取文件：声明的优先，其次同目录其它 process*.md（不合并）")
    import shutil
    import tempfile
    from pathlib import Path
    from app.context_builder import _process_settings_for

    root = Path(tempfile.mkdtemp())
    try:
        agent_dir = root / "a"
        agent_dir.mkdir()

        # 1) 声明 PROCESS_clock.md 但文件不存在 → 回退到 PROCESS.md
        (agent_dir / "PROCESS.md").write_text("max_items: 7\nmax_chars: 70", encoding="utf-8")
        check("声明缺失时回退",
              _process_settings_for(agent_dir, "PROCESS_clock.md", agent_id="a") == (7, 70),
              str(_process_settings_for(agent_dir, "PROCESS_clock.md", agent_id="a")))

        # 2) 声明的文件有设置 → 用它，不被 PROCESS.md 影响
        (agent_dir / "PROCESS_clock.md").write_text("max_items: 3", encoding="utf-8")
        check("声明文件优先",
              _process_settings_for(agent_dir, "PROCESS_clock.md", agent_id="a") == (3, 0),
              str(_process_settings_for(agent_dir, "PROCESS_clock.md", agent_id="a")))

        # 3) 不合并：声明文件只写一半，不会去别的文件补另一半
        check("不合并其它文件", _process_settings_for(agent_dir, "PROCESS_clock.md", agent_id="a")[1] == 0)

        # 4) 声明文件是说明文字（无设置）→ 回退
        (agent_dir / "PROCESS_clock.md").write_text("# 只有说明，没有配置", encoding="utf-8")
        check("声明文件无设置时回退",
              _process_settings_for(agent_dir, "PROCESS_clock.md", agent_id="a") == (7, 70),
              str(_process_settings_for(agent_dir, "PROCESS_clock.md", agent_id="a")))

        # 5) 全都没有 → 不限
        empty_dir = root / "b"
        empty_dir.mkdir()
        check("都没有 = 不限", _process_settings_for(empty_dir, "PROCESS.md", agent_id="b") == (0, 0))
    finally:
        shutil.rmtree(root, ignore_errors=True)

    print()
    if FAILS:
        print(f"FAILED {len(FAILS)}: {FAILS}")
        return 1
    print(f"ALL OK — {CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
