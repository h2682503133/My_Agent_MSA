"""把 openviking 服务器地址写进 <数据目录>/config/launcher.json。

等价于面板「单体模式 → openviking 记忆 → 保存」。用法：
    python set_launcher_ov.py http://10.244.37.37:1933
    python set_launcher_ov.py ""          # 清空 → 记忆回退到文件模式
"""

import json
import os
import sys
from pathlib import Path

DATA = Path(os.path.expandvars(r"%LOCALAPPDATA%\MyAgent\data"))
REPO_TEMPLATE = Path(r"C:\MyAgent\config\launcher.json")
TARGET = DATA / "config" / "launcher.json"


def main() -> int:
    url = (sys.argv[1] if len(sys.argv) > 1 else "").strip()

    TARGET.parent.mkdir(parents=True, exist_ok=True)
    if TARGET.exists():
        data = json.loads(TARGET.read_text(encoding="utf-8-sig"))  # 容忍记事本的 BOM
    elif REPO_TEMPLATE.exists():
        data = json.loads(REPO_TEMPLATE.read_text(encoding="utf-8-sig"))
    else:
        data = {}

    ov = data.setdefault("openviking", {})
    ov["server_url"] = url
    ov.setdefault("account", "my-agent")
    ov.setdefault("account_mode", "fixed")
    ov.setdefault("file_fallback", True)

    TARGET.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"已写入 {TARGET}")
    print(json.dumps({"openviking": ov}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
