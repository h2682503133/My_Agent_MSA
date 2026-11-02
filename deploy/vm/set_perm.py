"""通过 dashboard 接口把用户加进 L2/L3/L4 白名单（等价于网页「工具权限」页保存）。

用法：python set_perm.py nh
"""

import json
import sys
import urllib.request

BASE = "http://127.0.0.1:5700"
user = (sys.argv[1] if len(sys.argv) > 1 else "nh").strip()

payload = {
    "L1_blacklist_users": [],
    "L2_whitelist_users": [user],
    "L3_whitelist_users": [user],
    "L4_whitelist_users": [user],
}
req = urllib.request.Request(
    f"{BASE}/api/tool_permissions",
    data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
    method="PUT",
    headers={"Content-Type": "application/json"},
)
try:
    with urllib.request.urlopen(req, timeout=15) as resp:
        print("HTTP", resp.status, resp.read().decode("utf-8", "replace")[:300])
except Exception as exc:  # noqa: BLE001
    print("失败:", exc)

# 回读确认
try:
    with urllib.request.urlopen(f"{BASE}/api/tool_permissions", timeout=15) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))
    print("L2 =", data.get("L2_whitelist_users"))
    print("L3 =", data.get("L3_whitelist_users"))
    print("L4 =", data.get("L4_whitelist_users"))
    print("配置文件 =", data.get("settings_path"))
except Exception as exc:  # noqa: BLE001
    print("回读失败:", exc)
