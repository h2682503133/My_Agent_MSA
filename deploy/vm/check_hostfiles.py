"""集群里验证「宿主文件」通道（windows-file-copy 双向）。

用法（kubectl exec 里跑）：
    cd /app && PYTHONPATH=/app python /tmp/check_hostfiles.py [远端源文件]
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, "/app")

from app import server as srv

src = sys.argv[1] if len(sys.argv) > 1 else "/mnt/d/2025-08-17.txt"
workspace = Path(os.getenv("WORKSPACE_DIR", "/app/workspace"))
local = workspace / "hostfile-check.txt"
back = "/mnt/d/__myagent_write_test.txt"

service = srv.ToolRuntimeService()


def run(tool, args):
    return service._dispatch(tool_name=tool, args=args, kwargs={}, root=workspace, timeout=60)


print("=== 环境 ===")
print("  CLAW_EXTERNAL_VM_HOST =", os.getenv("CLAW_EXTERNAL_VM_HOST"))
print("  workspace =", workspace)

print("\n=== 1) 宿主 -> 容器 ===")
print(run("windows-file-copy", [src, str(local)])[:400])
print("  本地文件存在:", local.is_file(), "| 大小:",
      local.stat().st_size if local.is_file() else "-")

print("\n=== 2) 容器 -> 宿主 ===")
print(run("windows-file-copy", [str(local), back])[:400])

print("\n=== 3) 校验宿主侧文件（再拉回一份比对）===")
again = workspace / "hostfile-roundtrip.txt"
print(run("windows-file-copy", [back, str(again)])[:300])
same = local.is_file() and again.is_file() and local.read_bytes() == again.read_bytes()
print("  往返内容一致:", same)

print("\n=== 4) 清理 ===")
print(run("windows-file-move", [back, "/mnt/d/__deleted.txt"])[:200])
