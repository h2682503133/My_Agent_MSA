"""运行环境能力自检（离线：临时 capability.json + env 覆盖，不碰任何部署）。"""

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TMP = Path(tempfile.mkdtemp())
CAP = TMP / "capability.json"
os.environ["CAPABILITY_PATH"] = str(CAP)

from app import config  # noqa: E402
config.CAPABILITY_PATH = CAP

from app import capabilities as caps  # noqa: E402

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


def write_cap(features, degraded=None, tier="T0"):
    CAP.write_text(json.dumps({
        "schema": 1, "os": "windows", "runtime_tier": tier,
        "features": features, "degraded": degraded or [],
    }, ensure_ascii=False), encoding="utf-8")


def clear_env():
    for key in list(os.environ):
        if key.startswith("MYAGENT_CAP_"):
            del os.environ[key]


def main():
    print("没有文件时按默认（全部开启，行为不变）")
    if CAP.exists():
        CAP.unlink()
    clear_env()
    check("tool_runtime 默认开", caps.enabled("tool_runtime") is True)
    check("browser 默认开", caps.enabled("browser") is True)
    check("search 默认可用", caps.enabled("search") is True)
    check("tier 未知", caps.tier() == "unknown", caps.tier())
    check("工具无 cap 标签 → 可见", caps.tool_allowed([]) is True)

    print("文件里的布尔/枚举")
    write_cap({"tool_runtime": False, "browser": False, "search": "builtin",
               "memory_mode": "local", "clawhub": "off", "skill_exec": "partial"})
    check("tool_runtime 关闭", caps.enabled("tool_runtime") is False)
    check("browser 关闭", caps.enabled("browser") is False)
    check("search=builtin 视为可用", caps.enabled("search") is True)
    check("search 取值", caps.value("search") == "builtin", str(caps.value("search")))
    check("clawhub=off 视为关闭", caps.enabled("clawhub") is False)
    check("skill_exec=partial 视为关闭", caps.enabled("skill_exec") is False)
    check("tier 读取", caps.tier() == "T0")

    print("cap: 标签过滤")
    check("cap:browser 被拦", caps.tool_allowed(["cap:browser"]) is False)
    check("cap:tool_runtime 被拦", caps.tool_allowed(["cap:tool_runtime"]) is False)
    check("多个标签任一关闭即拦", caps.tool_allowed(["cap:search", "cap:browser"]) is False)
    check("非 cap 标签忽略", caps.tool_allowed(["文件", "L0"]) is True)
    check("cap 名称大小写不敏感", caps.tool_allowed(["CAP:search"]) is True)

    print("环境变量覆盖")
    write_cap({"browser": False})
    os.environ["MYAGENT_CAP_browser"] = "on"
    check("env 覆盖文件", caps.enabled("browser") is True)
    os.environ["MYAGENT_CAP_search"] = "off"
    check("env 关闭 search", caps.enabled("search") is False)
    clear_env()

    print("降级清单与摘要")
    write_cap({"tool_runtime": True}, degraded=[
        {"feature": "memory", "reason": "未检测到 OpenViking"},
        {"feature": "search", "reason": "未检测到 SearXNG"},
    ])
    deg = caps.degraded()
    check("降级项数量", len(deg) == 2, str(deg))
    check("降级项字段", deg[0]["feature"] == "memory" and "OpenViking" in deg[0]["reason"], str(deg[0]))
    info = caps.summary()
    check("summary 含 tier/os/features", info["tier"] == "T0" and info["os"] == "windows" and "tool_runtime" in info["features"], str(info)[:120])

    print("文件损坏时不炸")
    CAP.write_text("{ not json", encoding="utf-8")
    check("坏文件 → 按默认开", caps.enabled("browser") is True)
    check("坏文件 → 降级清单空", caps.degraded() == [])

    print()
    if FAILS:
        print(f"FAILED {len(FAILS)}: {FAILS}")
        return 1
    print(f"ALL OK — {CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
