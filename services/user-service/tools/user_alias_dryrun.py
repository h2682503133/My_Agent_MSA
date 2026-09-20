"""在真实生产数据的副本上预演启动对账，逐文件给出 before/after 差异。

用法：python tools/user_alias_dryrun.py <数据目录副本>
"""

import json
import os
import sys
from pathlib import Path

target = Path(sys.argv[1]).resolve()
os.environ["USER_DATA_DIR"] = str(target)

SERVICE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVICE_DIR))

before = {p.name: p.read_text(encoding="utf-8") for p in sorted(target.glob("*.json"))}

from app.user_store import user_store  # noqa: E402

stats = user_store.reconcile_all()

after = {p.name: p.read_text(encoding="utf-8") for p in sorted(target.glob("*.json"))}

print()
print("=" * 78)
print(f"数据目录: {target}")
print(f"用户文件: {len(before)} 个（前）→ {len(after)} 个（后）")
print(f"对账统计: {stats}")
print("=" * 78)

created = sorted(set(after) - set(before))
removed = sorted(set(before) - set(after))
changed = sorted(n for n in set(before) & set(after) if before[n] != after[n])

print(f"\n新增文件 {len(created)} 个: {created}")
print(f"删除文件 {len(removed)} 个: {removed}")
print(f"\n被修改的文件 {len(changed)} 个:")
for name in changed:
    b = json.loads(before[name])
    a = json.loads(after[name])
    keys = sorted(set(b) | set(a))
    diff = {k: (b.get(k), a.get(k)) for k in keys if b.get(k) != a.get(k)}
    print(f"\n  --- {name}")
    for k, (old, new) in diff.items():
        print(f"      {k}: {json.dumps(old, ensure_ascii=False)} -> {json.dumps(new, ensure_ascii=False)}")

print("\n" + "=" * 78)
print("解析验证：按渠道身份能否拿到全部渠道")
print("=" * 78)
for path in sorted(target.glob("*.json")):
    uid = path.stem
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        continue
    if data.get("alias_of"):
        got = user_store.get_user(uid)
        print(f"  {uid:<20} alias_of={data['alias_of']:<16} "
              f"→ 解析出 channels={sorted((got or {}).get('channels', {}))}")
