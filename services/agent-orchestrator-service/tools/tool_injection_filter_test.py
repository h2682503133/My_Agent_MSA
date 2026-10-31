"""TOOL.json 按档位注入过滤的自检（离线，用假目录，不碰线上配置）。"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import context_builder as cb  # noqa: E402

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


CATALOG = {
    "title": "工具目录",
    "sections": [
        {"id": "说明", "title": "调用格式", "text": "格式说明（自由文本，不参与过滤）"},
        {
            "id": "内置工具",
            "title": "内置工具",
            "tools": [
                {"name": "file-read", "args": "路径", "desc": "读文件", "groups": ["文件", "L0"]},
                {"name": "file-write", "args": "路径|内容", "desc": "写文件", "groups": ["文件", "L1"]},
                {"name": "worldinfo-write", "args": "关键词|内容", "desc": "写世界书", "groups": ["记忆", "L2"]},
                {"name": "shell", "args": "命令", "desc": "执行命令", "groups": ["执行", "L3"]},
                {"name": "browser", "args": "动作", "desc": "操作浏览器", "groups": ["工具", "L4"]},
                {"name": "fetch", "args": "url", "desc": "读网页（GET）", "groups": ["网络", "L1"]},
                {"name": "fetch", "args": "url|method|data", "desc": "写请求", "groups": ["网络", "L3"]},
                {"name": "no-tag", "args": "", "desc": "没写标签", "groups": ["杂项"]},
            ],
        },
        {"id": "只有高档", "title": "高档专用", "tools": [
            {"name": "windows-file-copy", "args": "源|目标", "desc": "复制", "groups": ["宿主机", "L4"]},
        ]},
    ],
}


def main():
    print("tool_level：缺标签按 L4")
    check("L0", cb.tool_level({"groups": ["文件", "L0"]}) == 0)
    check("缺标签 → 4", cb.tool_level({"groups": ["文件"]}) == 4)
    check("多条标签取最严", cb.tool_level({"groups": ["L1", "L3"]}) == 3)

    print("L0+L1 用户（普通用户）")
    text = cb.render_tool_catalog(CATALOG, {"sections": ["*"]}, {0, 1})
    check("能看到 file-read", "file-read" in text, text[:120])
    check("能看到 file-write", "file-write" in text)
    check("fetch 只出现读那条", "读网页（GET）" in text and "写请求" not in text, text)
    check("看不到 worldinfo-write", "worldinfo-write" not in text)
    check("看不到 shell", "shell" not in text)
    check("看不到 browser", "browser" not in text)
    check("缺标签的工具不出现", "no-tag" not in text)
    check("整段被过滤时不留标题", "高档专用" not in text)
    check("自由文本段保留", "格式说明" in text)

    print("L0 用户（被限制）")
    only0 = cb.render_tool_catalog(CATALOG, {"sections": ["*"]}, {0})
    check("只有 L0 工具", "file-read" in only0 and "file-write" not in only0, only0[:120])
    check("fetch 两条都不出现", "fetch" not in only0)

    print("全档用户")
    full = cb.render_tool_catalog(CATALOG, {"sections": ["*"]}, {0, 1, 2, 3, 4})
    for name in ("file-read", "file-write", "worldinfo-write", "shell", "browser", "windows-file-copy"):
        check(f"能看到 {name}", name in full)
    check("fetch 两条都出现", full.count("- fetch") == 2, full)

    print("allowed_levels=None 时不过滤（向后兼容）")
    unfiltered = cb.render_tool_catalog(CATALOG, {"sections": ["*"]})
    check("包含全部工具", "browser" in unfiltered and "no-tag" in unfiltered)

    print("TOOL.md 选择器与档位过滤叠加")
    picked = cb.render_tool_catalog(CATALOG, {"sections": ["*"], "tools": ["L3"]}, {0, 1})
    check("选择器点名 L3 但用户无权 → 空", "shell" not in picked and "fetch" not in picked, picked[:80])

    print()
    if FAILS:
        print(f"FAILED {len(FAILS)}: {FAILS}")
        return 1
    print(f"ALL OK — {CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
