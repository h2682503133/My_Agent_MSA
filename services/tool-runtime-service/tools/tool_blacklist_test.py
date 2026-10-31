"""tool-blacklist 工具自检（离线，用临时 system_settings.json）。"""

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TMP = Path(tempfile.mkdtemp())
SETTINGS = TMP / "system_settings.json"
SETTINGS.write_text("{}", encoding="utf-8")

from app import config  # noqa: E402
config.SYSTEM_SETTINGS_PATH = SETTINGS

from app.tool_blacklist import handle  # noqa: E402

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


def blacklist():
    data = json.loads(SETTINGS.read_text(encoding="utf-8"))
    return (data.get("tool_permission") or {}).get("L1_blacklist_users") or []


def main():
    print("加入黑名单（默认自己）")
    out = handle([], {}, "u1")
    check("返回成功", "已把用户 u1 加入" in out, out)
    check("落盘正确", blacklist() == ["u1"], str(blacklist()))

    print("重复加入")
    out = handle(["u1"], {}, "u1")
    check("提示已在名单", "已在黑名单内" in out, out)

    print("移出被拒绝（只能管理员操作）")
    out = handle(["u1", "-"], {}, "u1")
    check("返回错误", out.startswith("错误：本工具只能把用户加入黑名单"), out)
    check("名单未被改动", blacklist() == ["u1"], str(blacklist()))
    out = handle([], {}, "u1")
    check("已在名单时提示联系管理员", "联系管理员" in out, out)

    print("只能改自己")
    out = handle(["someone-else"], {}, "u1")
    check("拒绝操作别人", out.startswith("错误：只能把自己"), out)
    check("没写进名单", blacklist() == ["u1"], str(blacklist()))

    print("缺省目标 = 调用者")
    out = handle([""], {}, "u9")
    check("u9 被加入", "u9" in blacklist() and "u1" in blacklist(), str(blacklist()))
    check("u9 提示联系管理员", "联系管理员" in handle([], {}, "u9"))

    print("保留其它配置键")
    SETTINGS.write_text(json.dumps({"image_receive_enabled": False, "tool_permission": {}}), encoding="utf-8")
    handle([], {}, "u3")
    data = json.loads(SETTINGS.read_text(encoding="utf-8"))
    check("其它键未被覆盖", data.get("image_receive_enabled") is False, str(data))
    check("黑名单已写入", (data.get("tool_permission") or {}).get("L1_blacklist_users") == ["u3"], str(data))

    print()
    if FAILS:
        print(f"FAILED {len(FAILS)}: {FAILS}")
        return 1
    print(f"ALL OK — {CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
