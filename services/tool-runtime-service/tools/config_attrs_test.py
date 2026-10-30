"""tool-runtime 配置项自检：防"代码读了 config.X 但 config 里没有 X"这类必炸的漏配。

背景：reply-image 分支里写了 `config.IMAGE_ASSET_DIR`，而 config.py 里从没定义过，
于是每次调用都在参数校验之前抛 AttributeError（`module 'app.config' has no attribute ...`）。
这类问题应该在部署前被自检拦住。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config  # noqa: E402

FAILS = []
CHECKS = 0

# 代码里实际引用到的 config 属性（新增引用时一并加到这里）
REQUIRED = [
    "WORKSPACE_DIR",
    "PROCESS_DIR",
    "WORLD_INFO_DIR",
    "REPLY_IMAGES_DIR",
    "REPLY_IMAGE_BASE_URL",
    "IMAGE_ASSET_DIR",
    "IMAGE_BASE_URL",
    "IMAGE_VIEW_MAX_BYTES",
    "IMAGE_VIEW_TIMEOUT_SECONDS",
    "MODEL_PROXY_TARGET",
]


def check(name, cond, extra=""):
    global CHECKS
    CHECKS += 1
    if cond:
        print(f"  ok    {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL  {name} {extra}")


def main():
    print("config 属性存在")
    for name in REQUIRED:
        check(f"config.{name}", hasattr(config, name), "缺失")

    print("路径类不能为空")
    for name in ("REPLY_IMAGES_DIR", "REPLY_IMAGE_BASE_URL", "IMAGE_ASSET_DIR", "IMAGE_BASE_URL"):
        value = getattr(config, name, "")
        check(f"{name} 非空", bool(str(value).strip()), repr(value))

    print("回复配图目录应是隐藏目录（不污染 dashboard 配置管理）")
    check(
        Path(config.REPLY_IMAGES_DIR).name.startswith("."),
        Path(config.REPLY_IMAGES_DIR).name.startswith("."),
        config.REPLY_IMAGES_DIR,
    )

    print("图床地址必须是集群内可达（不能是 localhost）")
    check(
        "localhost" not in config.REPLY_IMAGE_BASE_URL,
        "localhost" not in config.REPLY_IMAGE_BASE_URL,
        config.REPLY_IMAGE_BASE_URL,
    )
    check(
        "localhost" not in str(config.IMAGE_BASE_URL),
        "localhost" not in str(config.IMAGE_BASE_URL),
        config.IMAGE_BASE_URL,
    )

    print()
    if FAILS:
        print(f"FAILED {len(FAILS)}: {FAILS}")
        return 1
    print(f"ALL OK — {CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
