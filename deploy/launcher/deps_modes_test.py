"""依赖与"当前模式"的对齐测试（不联网、不安装）。

覆盖本次发现的 bug：切模式（qq=onebot ↔ qq=satori）后服务集合变了，
但依赖不会自动补装 → 启动直接 ModuleNotFoundError。
"""
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


print("参数解析")
check("解析两个模式", deps._parse_modes("memory=local,qq=satori") == {"memory": "local", "qq": "satori"})
check("空串安全", deps._parse_modes("") == {})
check("忽略脏项", deps._parse_modes("qq=satori, ,bad,=x") == {"qq": "satori"})

print("服务集合随模式变化")
data_onebot = Path(tempfile.mkdtemp())
data_satori = Path(tempfile.mkdtemp())
plan_onebot = build(ROOT, data_onebot, ["core", "qq"], {"qq": "onebot"})
plan_satori = build(ROOT, data_satori, ["core", "qq"], {"qq": "satori"})
ids_onebot = {svc.id for svc in plan_onebot.services}
ids_satori = {svc.id for svc in plan_satori.services}
check("onebot 模式起 qq-onebot-service", "qq-onebot-service" in ids_onebot, str(sorted(ids_onebot)))
check("satori 模式起 qq-llbot-service", "qq-llbot-service" in ids_satori, str(sorted(ids_satori)))

req_onebot = deps.service_requirements(plan_onebot)
req_satori = deps.service_requirements(plan_satori)
pkg_onebot = set()
for pkgs in req_onebot.values():
    pkg_onebot |= set(pkgs)
pkg_satori = set()
for pkgs in req_satori.values():
    pkg_satori |= set(pkgs)
check("onebot 依赖含 websockets", "websockets" in pkg_onebot, str(sorted(pkg_onebot))[:120])
check("satori 依赖含 satori-python", "satori-python" in pkg_satori, str(sorted(pkg_satori))[:120])
check("satori 依赖含 aiohttp", "aiohttp" in pkg_satori)

print("stamp（决定要不要补装）")
resolved_a, _c, _o, _e = deps.build_lock(plan_onebot)
resolved_b, _c2, _o2, _e2 = deps.build_lock(plan_satori)
lock_a = deps.write_lock(plan_onebot, resolved_a)
lock_b = deps.write_lock(plan_satori, resolved_b)
stamp_a = deps.planned_stamp(data_onebot, lock_a, "shared")
stamp_b = deps.planned_stamp(data_satori, lock_b, "shared")
check("两种模式的 lock 指纹不同", stamp_a["lock_hash"] != stamp_b["lock_hash"])
check("未建 venv 时判定需要安装", deps.needs_install(data_onebot, lock_a, "shared") is True)

print()
if FAILS:
    print(f"FAILED {len(FAILS)}: {FAILS}")
    raise SystemExit(1)
print(f"ALL OK — {CHECK} checks passed")
