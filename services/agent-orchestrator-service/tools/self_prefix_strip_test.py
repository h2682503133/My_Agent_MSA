"""模型自写首段（`[时间] <自己id>:`）剥离自检。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.syntax_parser import strip_self_prefix  # noqa: E402

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
    print("剥离自己写的首段")
    check("时间戳 + 自己", strip_self_prefix("[10-28 23:09] <main>: 你好", "main") == "你好")
    check("只有自己", strip_self_prefix("<main>: 你好", "main") == "你好")
    check("全角冒号", strip_self_prefix("[23:09] <main>：你好", "main") == "你好")
    check("前导空白", strip_self_prefix("   [10-28 23:09] <main>: 你好", "main") == "你好")
    check("只剩前缀 → 空串", strip_self_prefix("[10-28 23:09] <main>:", "main") == "")

    print("不该动的")
    check("别人的发言保留", strip_self_prefix("[10-28 23:09] <luna>: 她说…", "main")
          == "[10-28 23:09] <luna>: 她说…")
    check("普通正文不动", strip_self_prefix("你好，我是凌伊", "main") == "你好，我是凌伊")
    check("正文中间的前缀不动", strip_self_prefix("我说：<main>: 你好", "main") == "我说：<main>: 你好")
    check("别的 agent 用自己的 id 剥自己的", strip_self_prefix("<luna>: 在呢", "luna") == "在呢")

    print("大小写/空白")
    check("id 大小写不敏感", strip_self_prefix("<MAIN>: 你好", "main") == "你好")
    check("空文本安全", strip_self_prefix("", "main") == "")
    check("空 id 不处理", strip_self_prefix("<main>: 你好", "") == "<main>: 你好")

    print()
    if FAILS:
        print(f"FAILED {len(FAILS)}: {FAILS}")
        return 1
    print(f"ALL OK — {CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
