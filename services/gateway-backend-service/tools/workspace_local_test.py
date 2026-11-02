"""单机工作空间本地实现自检（gateway 在没有 tool-runtime 时用它）。"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

SERVICE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVICE))

import workspace_local as wl  # noqa: E402

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
    saved = os.environ.get("GATEWAY_WORKSPACE_LOCAL")
    tmp = Path(tempfile.mkdtemp())
    ws = tmp / "workspace" / "users" / "nh"
    ws.mkdir(parents=True)

    print("开关")
    os.environ.pop("GATEWAY_WORKSPACE_LOCAL", None)
    check("默认关闭（走 tool-runtime）", wl.enabled() is False)
    os.environ["GATEWAY_WORKSPACE_LOCAL"] = "1"
    check("=1 时启用", wl.enabled() is True)
    os.environ["GATEWAY_WORKSPACE_LOCAL"] = "true"
    check("=true 时启用", wl.enabled() is True)

    print("写入 / 读取 / 列表")
    r = wl.write_text(str(ws), "notes/a.txt", "hello 世界")
    check("写文本成功", r["ok"] is True, str(r))
    check("文件真的落盘", (ws / "notes" / "a.txt").read_text(encoding="utf-8") == "hello 世界")

    r = wl.read_file(str(ws), "notes/a.txt")
    check("读文本成功", r["ok"] and r["output"] == "hello 世界", str(r))

    r = wl.list_dir(str(ws), "")
    check("根目录列出 notes", "[dir] notes" in r["output"], r["output"])
    r = wl.list_dir(str(ws), "notes")
    check("子目录列出文件（tool-runtime 同款格式）",
          r["output"].startswith("[file] a.txt ("), r["output"])
    check("文件大小写在括号里", "bytes)" in r["output"], r["output"])

    print("二进制（图片）走 base64")
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 32
    r = wl.write_bytes(str(ws), "img/p.png", png)
    check("写二进制成功", r["ok"] is True, str(r))
    r = wl.read_file(str(ws), "img/p.png")
    check("二进制标记为 binary", r.get("binary") is True, str(r)[:80])
    import base64

    check("base64 可还原", base64.b64decode(r["output"]) == png)

    print("安全边界")
    r = wl.read_file(str(ws), "../escape.txt")
    check("越界读取被拒", r["ok"] is False and "越界" in r["error"], str(r))
    r = wl.write_text(str(ws), "a/../../escape.txt", "x")
    check("越界写入被拒", r["ok"] is False, str(r))
    r = wl.list_dir(str(ws), "not-exist")
    check("不存在的目录返回空而不是报错", r["ok"] is True and r["output"] == "", str(r))

    print("删除")
    r = wl.delete(str(ws), "notes/a.txt")
    check("删文件成功", r["ok"] is True and not (ws / "notes" / "a.txt").exists(), str(r))
    r = wl.delete(str(ws), "notes")
    check("空目录可删", r["ok"] is True)
    (ws / "d").mkdir()
    (ws / "d" / "x.txt").write_text("x", encoding="utf-8")
    r = wl.delete(str(ws), "d")
    check("非空目录拒绝删除", r["ok"] is False and "非空" in r["error"], str(r))

    if saved is None:
        os.environ.pop("GATEWAY_WORKSPACE_LOCAL", None)
    else:
        os.environ["GATEWAY_WORKSPACE_LOCAL"] = saved

    print()
    if FAILS:
        print(f"FAILED {len(FAILS)}: {FAILS}")
        return 1
    print(f"ALL OK — {CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
