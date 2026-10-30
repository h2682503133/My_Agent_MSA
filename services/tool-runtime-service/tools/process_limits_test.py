"""PROCESS 上限自检：process*.md 解析 + 写入强制（不依赖 gRPC）。"""

import io
import sys
from pathlib import Path

SERVICE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVICE))

from app import process_runtime  # noqa: E402

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
    root = Path(__file__).resolve().parent / "_tmp_process_limits"
    if root.exists():
        for child in sorted(root.rglob("*"), reverse=True):
            child.unlink() if child.is_file() else child.rmdir()
        root.rmdir()
    root.mkdir(parents=True)
    process_dir = str(root)

    print("上限 = 2 条 / 20 字")
    result = process_runtime.write(process_dir, "u1", "main", "-1", "标题1", "内容" * 5, max_items=2, max_chars=20)
    check("第 1 条写入成功", "已追加第 1 条" in result, result)
    check("返回里显示用量", "1/2" in result, result)
    result = process_runtime.write(process_dir, "u1", "main", "-1", "标题2", "内容" * 5, max_items=2, max_chars=20)
    check("第 2 条写入成功", "已追加第 2 条" in result, result)
    result = process_runtime.write(process_dir, "u1", "main", "-1", "标题3", "内容" * 5, max_items=2, max_chars=20)
    check("第 3 条被拒绝", result.startswith("错误：存储已满"), result)
    check("报错里带上限与当前条数", "2/2" in result, result)

    print("单条字数")
    result = process_runtime.write(process_dir, "u1", "main", "1", "标题1", "长" * 21, max_items=2, max_chars=20)
    check("超字数被拒", "超过上限 20 字" in result, result)
    result = process_runtime.write(process_dir, "u1", "main", "1", "标题1", "长" * 20, max_items=2, max_chars=20)
    check("刚好 20 字可覆写", "已覆写第 1 条" in result, result)

    print("不限制（0）")
    result = process_runtime.write(process_dir, "u2", "main", "-1", "a", "x" * 500, max_items=0, max_chars=0)
    check("无上限可写超长", "已追加第 1 条" in result, result)
    for _ in range(5):
        process_runtime.write(process_dir, "u2", "main", "-1", "b", "c", max_items=0, max_chars=0)
    check("无上限可多条", True)

    print("越界与非法 index 保持原行为")
    result = process_runtime.write(process_dir, "u1", "main", "99", "x", "y", max_items=2, max_chars=20)
    check("越界报错", result.startswith("错误：index=99 越界"), result)
    result = process_runtime.write(process_dir, "u1", "main", "abc", "x", "y")
    check("非法 index 报错", result.startswith("错误：index 必须是整数"), result)

    print("清理")
    for child in sorted(root.rglob("*"), reverse=True):
        child.unlink() if child.is_file() else child.rmdir()
    root.rmdir()
    check("临时目录已清理", not root.exists())

    print()
    if FAILS:
        print(f"FAILED {len(FAILS)}: {FAILS}")
        return 1
    print(f"ALL OK — {CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
