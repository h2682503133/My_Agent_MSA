"""launcher：OpenViking 向量召回的服务地址（单体必须能配置）。

bug 现象：memory=openviking 但没起本地 embedding-service 时，
openviking-context-service 仍用默认 embedding-service:5303（集群 DNS），
每次召回都失败并静默降级。
"""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deploy"))

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


def make_data_dir(openviking=None) -> Path:
    d = Path(tempfile.mkdtemp())
    (d / "config").mkdir(parents=True, exist_ok=True)
    cfg = {"version": 1}
    if openviking is not None:
        cfg["openviking"] = openviking
    (d / "config" / "launcher.json").write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    return d


print("配置了 embedding_target → 注入")
d1 = make_data_dir({"server_url": "http://10.0.0.5:1933", "embedding_target": "10.0.0.5:5303"})
p1 = build(ROOT, d1, ["core", "memory"], {"memory": "openviking"})
check("使用配置值", p1.env.get("EMBEDDING_SERVICE_TARGET") == "10.0.0.5:5303",
      str(p1.env.get("EMBEDDING_SERVICE_TARGET")))

print("没配置 → 不注入 + 告警（而不是悄悄降级）")
d2 = make_data_dir({"server_url": "http://10.0.0.5:1933"})
p2 = build(ROOT, d2, ["core", "memory"], {"memory": "openviking"})
check("不注入该变量", "EMBEDDING_SERVICE_TARGET" not in p2.env)
check("给出告警", any("向量服务" in w for w in p2.warnings), str(p2.warnings))

print("memory.source=install（本地 embedding-service）→ 指向本机端口")
d3 = make_data_dir({"server_url": "http://127.0.0.1:1933"})
p3 = build(ROOT, d3, ["core", "memory"], {"memory": "openviking", "memory.source": "install"})
check("注入了本机 embedding 端点", str(p3.env.get("EMBEDDING_SERVICE_TARGET", "")).startswith("127.0.0.1:"),
      str(p3.env.get("EMBEDDING_SERVICE_TARGET")))

print("没开 memory → 不注入")
d4 = make_data_dir()
p4 = build(ROOT, d4, ["core"], {})
check("无该变量", "EMBEDDING_SERVICE_TARGET" not in p4.env)

print()
if FAILS:
    print(f"FAILED {len(FAILS)}: {FAILS}")
    raise SystemExit(1)
print(f"ALL OK — {CHECK} checks passed")
