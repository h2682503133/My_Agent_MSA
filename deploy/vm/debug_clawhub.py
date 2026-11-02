"""直接调用 tool-runtime 的 skill_runtime.dispatch，复现 clawhub 工具报错。

在虚拟机里用共享 venv 跑：
    python debug_clawhub.py [tool_name] [arg]
默认：clawhub-search markdown
"""

import os
import sys
import traceback
from pathlib import Path

SVC = Path(r"C:\MyAgent\services\tool-runtime-service")
sys.path.insert(0, str(SVC))

DATA = Path(os.path.expandvars(r"%LOCALAPPDATA%\MyAgent\data"))
WORKSPACE = DATA / "workspace"

# 与 launcher 注入的一致（模拟单体环境）
os.environ.setdefault("SKILL_ROOT_DIR", str(WORKSPACE / "skills"))
os.environ.setdefault("CLAW_DOWNLOAD_MODE", "local")
os.environ.setdefault("CLAW_EXTERNAL_VM_SKILL_ROOT_DIR", str(WORKSPACE / "skills"))
node_dir = Path(r"C:\Program Files\nodejs")
os.environ.setdefault("CLAW_EXTERNAL_VM_CLAWHUB_BIN", str(Path(os.environ["APPDATA"]) / "npm" / "clawhub.cmd"))
os.environ["PATH"] = str(node_dir) + os.pathsep + os.environ.get("PATH", "")

print("=== 环境 ===")
for key in ("SKILL_ROOT_DIR", "CLAW_DOWNLOAD_MODE", "CLAW_EXTERNAL_VM_CLAWHUB_BIN",
            "CLAW_EXTERNAL_VM_SKILL_ROOT_DIR", "ENABLE_CLAWHUB", "ENABLE_OPENVIKING"):
    print(f"  {key} = {os.environ.get(key)}")

tool = sys.argv[1] if len(sys.argv) > 1 else "clawhub-search"
arg = sys.argv[2] if len(sys.argv) > 2 else "markdown"

print(f"\n=== 调用 {tool} | {arg} ===")
try:
    from app.skill_runtime import skill_runtime

    result = skill_runtime.dispatch(
        tool_name=tool,
        args=[arg],
        kwargs={},
        user_workspace=WORKSPACE,
        timeout=90,
    )
    print("=== 结果 ===")
    print(result[:2000])
except Exception:  # noqa: BLE001
    print("=== 异常 ===")
    traceback.print_exc()
