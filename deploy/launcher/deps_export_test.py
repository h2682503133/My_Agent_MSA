"""--export 的测试：只导出统一依赖清单，不建 venv、不装包（不联网）。

给"自己有 conda / 想先看装什么"的用法兜底：
- 导出的必须是**合并 + 覆盖后**的结果（不是各服务 requirements 的简单拼接）；
- 文件要纯 ASCII（Windows 上 pip 用本地编码读 requirements，非 ASCII 会崩）；
- 不能偷偷创建 venv 或装任何东西。
"""
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deploy"))

from launcher import deps  # noqa: E402
from launcher.plan import build  # noqa: E402

FAILS = []
CHECK = 0


def check(name, cond, extra=""):
    global CHECK
    CHECK += 1
    if cond:
        print(f"  ok    {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL  {name} {extra}")


print("导出内容 = 合并 + 覆盖后的结果")
data = Path(tempfile.mkdtemp())
plan = build(ROOT, data, ["core", "memory"], {"memory": "local"})
resolved, conflicts, overrides, extras = deps.build_lock(plan)
out = data / "export" / "requirements.all.txt"
lock_path = deps.write_lock(plan, resolved, out)

check("写到指定路径", lock_path == out and out.is_file(), str(lock_path))
text = out.read_text(encoding="ascii")  # 非 ASCII 会直接抛
lines = [ln.strip() for ln in text.splitlines()]
pkgs = [ln for ln in lines if ln and not ln.startswith("#")]

check("带生成说明头", text.startswith("# GENERATED"), text[:40])
check("记录功能与服务", "# features: core,memory" in text and "# services:" in text)
check("包数与解析结果一致", len(pkgs) == len(resolved), f"{len(pkgs)} vs {len(resolved)}")
check("有覆盖项时按覆盖后的版本", "grpcio==1.76.0" in text or all(
    f"{name}{spec}" in text for name, spec in resolved.items()))
check("纯 ASCII", all(ord(ch) < 128 for ch in text))

print("导出不产生副作用")
check("不建 venv", not (data / "venv").exists())
check("不写 deps/requirements.lock.txt", not (data / "deps" / "requirements.lock.txt").exists())
check("默认路径仍是数据目录", deps.write_lock(plan, resolved) == data / "deps" / "requirements.lock.txt")

print("CLI 端到端（--export 不进安装分支）")
work = Path(tempfile.mkdtemp())
export = work / "req.txt"
proc = subprocess.run(
    [sys.executable, "-X", "utf8", str(ROOT / "deploy" / "launcher" / "deps.py"),
     "--data-dir", str(work / "data"), "--features", "core",
     "--export", str(export)],
    capture_output=True, text=True, encoding="utf-8", errors="replace",
)
check("退出码 0", proc.returncode == 0, proc.stderr[-300:])
check("文件已生成", export.is_file())
check("没有建 venv", not (work / "data" / "venv").exists())
check("输出里有导出提示", "已导出统一依赖" in proc.stdout, proc.stdout[-200:])

print("CLI：--export 不给路径 → 写到 <数据目录>/deps/requirements.txt")
work2 = Path(tempfile.mkdtemp())
proc2 = subprocess.run(
    [sys.executable, "-X", "utf8", str(ROOT / "deploy" / "launcher" / "deps.py"),
     "--data-dir", str(work2), "--features", "core,memory", "--export"],
    capture_output=True, text=True, encoding="utf-8", errors="replace",
)
default_req = work2 / "deps" / "requirements.txt"
check("退出码 0", proc2.returncode == 0, proc2.stderr[-300:])
check("默认路径已生成", default_req.is_file(), str(default_req))
check("没建 venv", not (work2 / "venv").exists())
check("清单打到屏幕", "--- 清单 ---" in proc2.stdout and "fastapi" in proc2.stdout, proc2.stdout[-200:])

print("CLI：--no-install 等价于只出清单")
work3 = Path(tempfile.mkdtemp())
proc3 = subprocess.run(
    [sys.executable, "-X", "utf8", str(ROOT / "deploy" / "launcher" / "deps.py"),
     "--data-dir", str(work3), "--features", "core", "--no-install"],
    capture_output=True, text=True, encoding="utf-8", errors="replace",
)
check("退出码 0", proc3.returncode == 0, proc3.stderr[-300:])
check("生成清单", (work3 / "deps" / "requirements.txt").is_file())
check("没建 venv", not (work3 / "venv").exists())

print()
if FAILS:
    print(f"FAILED {len(FAILS)}: {FAILS}")
    raise SystemExit(1)
print(f"ALL OK — {CHECK} checks passed")
