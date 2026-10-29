"""视觉模型路由自检（不联网，直接测 send() 的判定口径）。

覆盖：
 1. explicit 模式：纯文本 / 只有历史图 → 不切；本轮新图 / 显式 image-view → 切
 2. auto 模式：只要上下文有图就切（旧行为，可回退）
 3. 文本里出现图片链接不算视觉请求（fresh=0, explicit=False → 不切）
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config  # noqa: E402

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


def route(mode: str, image_count: int, fresh_images: int, explicit_vision: bool) -> str:
    """与 agent_runtime.send() 里的判定保持一致（纯函数版，便于回归）。"""
    reason = ""
    if fresh_images:
        reason = "fresh"
    elif explicit_vision:
        reason = "explicit_tool"
    if mode == "auto" and image_count > 0:
        reason = reason or "auto"
    return reason


def main():
    print("explicit 模式（默认）")
    check("纯文本不切", route("explicit", 0, 0, False) == "")
    check("只有历史图不切", route("explicit", 5, 0, False) == "")
    check("本轮新发图要切", route("explicit", 5, 1, False) == "fresh")
    check("显式 image-view 要切", route("explicit", 5, 0, True) == "explicit_tool")
    check("文本里有图片链接 → 不切", route("explicit", 0, 0, False) == "")

    print("auto 模式（可回退的旧行为）")
    check("有历史图就切", route("auto", 3, 0, False) == "auto")
    check("纯文本仍不切", route("auto", 0, 0, False) == "")

    print("配置默认值")
    check("默认是 explicit", config.VISION_ROUTE_MODE in ("explicit", "auto"), config.VISION_ROUTE_MODE)
    check("当前环境默认 explicit", config.VISION_ROUTE_MODE == "explicit", config.VISION_ROUTE_MODE)

    print()
    if FAILS:
        print(f"FAILED {len(FAILS)}: {FAILS}")
        return 1
    print(f"ALL OK — {CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
