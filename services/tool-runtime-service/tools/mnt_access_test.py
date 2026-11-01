"""宿主文件通道（mnt_access）自检：路径换算 + 四种拷贝方向。

背景：早先 copy_or_move 把 `cp 源 目标` 整条丢给远端，目标在容器里时必然
`cp: cannot create regular file "/app/workspace/...": No such file or directory`。
现在按平台与两侧位置分流：
  - Windows 主机（单体）：盘符路径 = 本地路径，全本地操作；
  - Linux 容器：`/mnt/...` 是宿主，跨边界用 SSH 流式传输（cat / cat >）。
"""

from __future__ import annotations

import importlib
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "services" / "tool-runtime-service"))

FAILS = []
CHECKS = 0


def check(name, cond, extra=""):
    global CHECKS
    CHECKS += 1
    if cond:
        print("  ok    " + name)
    else:
        FAILS.append(name)
        print("  FAIL  " + name + " " + extra)


class FakeProc:
    def __init__(self, returncode=0, stdout=b"", stderr=b""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def fresh_module(container_mode, host="10.0.0.9"):
    if container_mode:
        os.environ["CLAW_EXTERNAL_VM_HOST"] = host
        os.environ.setdefault("CLAW_EXTERNAL_VM_USER", "tester")
        os.environ.setdefault("CLAW_EXTERNAL_VM_SSH_KEY", "/tmp/fake_key")
    # app.config 在导入时就把环境变量读进常量了，切平台前必须一起重载
    import app.config as cfg

    importlib.reload(cfg)
    import app.mnt_access as m

    importlib.reload(m)
    m.IS_WINDOWS_HOST = not container_mode
    return m


def main():
    saved_host = os.environ.get("CLAW_EXTERNAL_VM_HOST")
    try:
        m = fresh_module(False)

        print("路径换算")
        check("D:\\x\\y -> /mnt/d/x/y", m.to_mnt_path("D:\\x\\y") == "/mnt/d/x/y")
        check("/mnt/c/a 原样", m.to_mnt_path("/mnt/c/a") == "/mnt/c/a")
        check("/mnt/d/a -> D:\\a", m.from_mnt_path("/mnt/d/a") == "D:\\a")
        check("非 Windows 路径返回空", m.to_mnt_path("/app/workspace/x") == "")
        check("is_windows_path 认盘符", m.is_windows_path("E:\\data") is True)

        print("Windows 主机：本地拷贝（不碰 SSH）")
        tmp = Path(tempfile.mkdtemp())
        (tmp / "a.txt").write_text("hello", encoding="utf-8")
        out = m.copy_or_move("file-copy", [str(tmp / "a.txt"), str(tmp / "b.txt")], {}, 30)
        check("本地 copy 成功", (tmp / "b.txt").read_text(encoding="utf-8") == "hello", out)
        out = m.copy_or_move("file-move", [str(tmp / "b.txt"), str(tmp / "c.txt")], {}, 30)
        check("本地 move 成功", (tmp / "c.txt").is_file() and not (tmp / "b.txt").exists(), out)

        print("Linux 容器：宿主 -> 容器（cat 流式取回）")
        m = fresh_module(True)
        calls = []

        def fake_read(script, stdin_bytes=None, timeout=None):
            calls.append(("read", script, stdin_bytes))
            return FakeProc(0, b"from-host")

        m._ssh_popen = fake_read
        dst = Path("/tmp/myagent_from_host.txt")
        out = m.copy_or_move("file-copy", ["/mnt/d/2025-08-17.txt", str(dst)], {}, 30)
        check("用 cat 而不是 cp", bool(calls) and calls[0][0] == "read" and "cat " in calls[0][1], str(calls))
        check("字节真的落到本地", dst.is_file() and dst.read_bytes() == b"from-host", out)

        print("Linux 容器：容器 -> 宿主（cat > 流式写入）")
        calls.clear()

        def fake_write(script, stdin_bytes=None, timeout=None):
            calls.append(("write", script, stdin_bytes))
            return FakeProc(0, b"")

        m._ssh_popen = fake_write
        src = Path("/tmp/myagent_to_host.txt")
        src.write_text("payload", encoding="utf-8")
        m.copy_or_move("file-copy", [str(src), "/mnt/d/out.txt"], {}, 30)
        check("用 cat > 而不是 cp", bool(calls) and "cat >" in calls[0][1], str(calls))
        check("stdin 传的是文件内容", bool(calls) and calls[0][2] == b"payload", str(calls[:1]))

        print("Linux 容器：两端都在宿主（远端 cp）")
        scripts = []
        m._ssh = lambda script, timeout=None: (scripts.append(script) or "copied")
        m.copy_or_move("file-copy", ["/mnt/d/a", "/mnt/d/b"], {}, 30)
        check("走远端 cp -r", any(s.startswith("cp -r") for s in scripts), str(scripts))

        print("错误路径")
        check("缺参数报错", "缺少源路径或目标路径" in m.copy_or_move("file-copy", [], {}, 30))

        os.environ["CLAW_EXTERNAL_VM_HOST"] = ""
        import app.config as cfg

        importlib.reload(cfg)
        importlib.reload(m)
        m.IS_WINDOWS_HOST = False
        out = m.copy_or_move("file-copy", ["/mnt/d/a", "/app/workspace/b"], {}, 30)
        check("未配外部 VM 时明确报错", "无法访问 /mnt" in out, out)

        print()
        if FAILS:
            print(f"FAILED {len(FAILS)}: {FAILS}")
            return 1
        print(f"ALL OK - {CHECKS} checks passed")
        return 0
    finally:
        if saved_host is None:
            os.environ.pop("CLAW_EXTERNAL_VM_HOST", None)
        else:
            os.environ["CLAW_EXTERNAL_VM_HOST"] = saved_host


if __name__ == "__main__":
    raise SystemExit(main())
