"""clawhub 落盘 + Viking 索引核对。

在虚拟机里用共享 venv 跑：
    python check_skill_state.py [slug]
"""

import json
import os
import sys
import urllib.request
from pathlib import Path

SVC = Path(r"C:\MyAgent\services\tool-runtime-service")
sys.path.insert(0, str(SVC))

DATA = Path(os.path.expandvars(r"%LOCALAPPDATA%\MyAgent\data"))
WORKSPACE = DATA / "workspace"
SKILL_ROOT = WORKSPACE / "skills"

os.environ.setdefault("SKILL_ROOT_DIR", str(SKILL_ROOT))
os.environ.setdefault("CLAW_DOWNLOAD_MODE", "local")
os.environ.setdefault("CLAW_EXTERNAL_VM_SKILL_ROOT_DIR", str(SKILL_ROOT))
os.environ["PATH"] = r"C:\Program Files\nodejs" + os.pathsep + os.environ.get("PATH", "")

HOST_BASE = "http://10.244.37.37:8000"
slug = sys.argv[1] if len(sys.argv) > 1 else "planning-with-files"

report = {"skill_root": str(SKILL_ROOT)}

# 1) 磁盘上的技能目录
entries = []
if SKILL_ROOT.is_dir():
    for item in sorted(SKILL_ROOT.iterdir()):
        files = [p.name for p in item.rglob("*") if p.is_file()][:6] if item.is_dir() else []
        entries.append({"name": item.name, "files": files})
report["disk_skills"] = entries
report["installed_dir_exists"] = (SKILL_ROOT / slug).is_dir()
report["skill_md_exists"] = any((SKILL_ROOT / slug / n).is_file() for n in ("SKILL.md", "skill.md"))

# 2) 工具返回的 skill-list（走 OpenViking）
try:
    from app.skill_runtime import skill_runtime

    report["skill_list"] = str(
        skill_runtime.dispatch("skill-list", [], {}, WORKSPACE, 60)
    )[:800]
except Exception as exc:  # noqa: BLE001
    report["skill_list"] = f"失败: {exc}"

# 3) skill-overview / manual 能否读到内容
for tool in ("skill-overview", "skill-manual", "skill-abstract"):
    try:
        from app.skill_runtime import skill_runtime

        report[tool] = str(skill_runtime.dispatch(tool, [slug], {}, WORKSPACE, 60))[:400]
    except Exception as exc:  # noqa: BLE001
        report[tool] = f"失败: {exc}"

print(json.dumps(report, ensure_ascii=False, indent=2))
try:
    payload = json.dumps({"stage": "skill-state", "ok": True,
                          "detail": json.dumps(report, ensure_ascii=False),
                          "host": "vm"}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(f"{HOST_BASE}/report", data=payload, method="POST",
                                 headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=10).read()
except Exception as exc:  # noqa: BLE001
    print("回传失败:", exc)
