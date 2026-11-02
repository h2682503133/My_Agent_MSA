"""websockets 头参数名兼容性测试（不联网）。

覆盖两种真实环境：
  容器 websockets 13.1  → legacy connect，参数名 extra_headers
  单体 VM 新版          → asyncio connect，参数名 additional_headers
"""
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]   # services/qq-onebot-service
sys.path.insert(0, str(ROOT))

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


def load_module_with_fake_websockets(params):
    """用假的 websockets 模块重新加载 onebot_bridge，验证它选中的参数名。"""
    import importlib

    class FakeConnect:
        def __init__(self, *a, **kw):
            self.args = a
            self.kwargs = kw

    def connect(*args, **kwargs):
        return FakeConnect(*args, **kwargs)

    if params is None:
        connect_sig = connect
    else:
        # 伪造签名
        ns = {}
        args = ", ".join(params)
        exec(f"def connect(*args, {args}, **kwargs):\n    return None", ns)
        connect_sig = ns["connect"]

    fake = types.ModuleType("websockets")
    fake.connect = connect_sig
    fake.__version__ = "13.1" if "extra_headers" in (params or []) else "15.0"
    fake.ConnectionClosed = type("ConnectionClosed", (Exception,), {})
    sys.modules["websockets"] = fake

    # 清掉已有的 app 包缓存，逼它重新 import
    for mod in [m for m in list(sys.modules) if m.startswith("app")]:
        del sys.modules[mod]

    import app.onebot_bridge as bridge
    importlib.reload(bridge)
    return bridge


print("参数名选择")
bridge = load_module_with_fake_websockets(["additional_headers"])
check("新版签名 → additional_headers", bridge.HEADERS_KW == "additional_headers", bridge.HEADERS_KW)

# websockets 13 的 connect 签名里没有 headers 参数（legacy），回退看版本号
bridge = load_module_with_fake_websockets(["uri", "extra_headers"])
check("legacy 签名 → extra_headers", bridge.HEADERS_KW == "extra_headers", bridge.HEADERS_KW)

# 真实环境自检：当前解释器里 websockets 的签名必须能匹配其中一个
import importlib
for mod in [m for m in list(sys.modules) if m.startswith("app")]:
    del sys.modules[mod]
sys.modules.pop("websockets", None)
import websockets  # noqa: F401
import app.onebot_bridge as real_bridge
importlib.reload(real_bridge)
real_params = {}
try:
    import inspect
    real_params = inspect.signature(websockets.connect).parameters
except Exception:
    pass
expect = "additional_headers" if "additional_headers" in real_params else "extra_headers"
check(f"当前环境({websockets.__version__}) 选中正确参数名",
      real_bridge.HEADERS_KW == expect, f"{real_bridge.HEADERS_KW} != {expect}")

print()
if FAILS:
    print(f"FAILED {len(FAILS)}: {FAILS}")
    raise SystemExit(1)
print(f"ALL OK — {CHECK} checks passed")
