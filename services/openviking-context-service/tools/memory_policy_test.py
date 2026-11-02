"""会话记忆策略（memory_policy）自检：离线、不联网。

背景：OpenViking 每次 commit 默认生成 7 段 Working Memory（约 45k tokens、142KB 快照），
我们从没读过它 → 纯成本。关掉 WM 不影响记忆抽取（memories/peers 照常产出）。
覆盖：
- build_memory_policy() 的形状（只出现 OpenViking 认识的 4 个键）
- 类型串解析（中英文逗号/空格、大小写、空值）
- memory_policy() 读环境变量 + JSON 逃生口（非法 JSON / 非对象 / 未知键）
- viking_store.ensure_session：新会话带 policy、存量会话不重复建、SDK 不支持时回退
"""
import asyncio
import json
import os
import sys
from pathlib import Path

SERVICE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE))

from app import config  # noqa: E402

FAILS = []
CHECKS = 0


def check(name, cond, extra=""):
    global CHECKS
    CHECKS += 1
    if cond:
        print(f"  ok    {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL  {name} {extra}")


print("build_memory_policy：形状与解析")
p = config.build_memory_policy(
    working_memory=False,
    memory_types="profile,preferences,entities",
    self_enabled=True,
    peer_enabled=False,
)
check("只有 OpenViking 认识的 4 个键", set(p) <= {"self", "peer", "memory_types", "working_memory"}, str(p))
check("working_memory 关", p["working_memory"] == {"enabled": False})
check("self 开 / peer 关", p["self"] == {"enabled": True} and p["peer"] == {"enabled": False})
check("类型按顺序解析", p["memory_types"] == ["profile", "preferences", "entities"], str(p.get("memory_types")))

p2 = config.build_memory_policy(memory_types="Profile，EXPERIENCES、cases  events")
check("中英文逗号/顿号/空格都切", p2["memory_types"] == ["profile", "experiences", "cases", "events"], str(p2.get("memory_types")))

p3 = config.build_memory_policy(memory_types="")
check("空类型不写该键（交回默认）", "memory_types" not in p3, str(p3))

print("memory_policy()：环境变量驱动")
config.OPENVIKING_MEMORY_POLICY = ""
config.OPENVIKING_WM_ENABLED = False
config.OPENVIKING_MEMORY_TYPES = "profile,preferences,entities,events,experiences"
got = config.memory_policy()
check("默认：WM 关", got["working_memory"] == {"enabled": False}, str(got))
check("默认：5 类抽取", got["memory_types"] == ["profile", "preferences", "entities", "events", "experiences"], str(got))

config.OPENVIKING_WM_ENABLED = True
check("打开 WM 也生效", config.memory_policy()["working_memory"] == {"enabled": True})
config.OPENVIKING_WM_ENABLED = False

print("memory_policy()：JSON 逃生口")
config.OPENVIKING_MEMORY_POLICY = json.dumps(
    {"working_memory": {"enabled": True}, "memory_types": ["profile"]}
)
override = config.memory_policy()
check("JSON 原样透传", override["working_memory"] == {"enabled": True} and override["memory_types"] == ["profile"], str(override))

config.OPENVIKING_MEMORY_POLICY = json.dumps({"working_memory": {"enabled": True}, "bogus": 1})
cleaned = config.memory_policy()
check("未知键被剔除（否则 OpenViking 会报错）", "bogus" not in cleaned, str(cleaned))
check("认识的键保留", cleaned.get("working_memory") == {"enabled": True}, str(cleaned))

config.OPENVIKING_MEMORY_POLICY = "{not json"
check("非法 JSON 退回默认拼装", "memory_types" in config.memory_policy())
config.OPENVIKING_MEMORY_POLICY = "[1,2]"
check("非对象也退回默认", isinstance(config.memory_policy(), dict) and "working_memory" in config.memory_policy())
config.OPENVIKING_MEMORY_POLICY = ""

print("viking_store.ensure_session：建会话带上策略")
from app import viking_store  # noqa: E402


class FakeClient:
    def __init__(self, existing=False, support_create=True, raise_on_get=None):
        self.created = []
        self.existing = existing
        self.support_create = support_create
        self.raise_on_get = raise_on_get
        self.auto_created = 0

    async def get_session(self, session_id, auto_create=False):
        if self.raise_on_get:
            raise RuntimeError(self.raise_on_get)
        if auto_create:
            self.auto_created += 1
            return {"session_id": session_id, "auto_created": True}
        if self.existing:
            return {"session_id": session_id, "existing": True}
        raise RuntimeError("404 not found")

    async def create_session(self, session_id=None, memory_policy=None, telemetry=False):
        if not self.support_create:
            raise TypeError("unexpected keyword argument 'memory_policy'")
        self.created.append((session_id, memory_policy))
        return {"session_id": session_id, "memory_policy": memory_policy}


backend_cls = getattr(viking_store, "OpenVikingServerBackend", None)
store = backend_cls(url="http://127.0.0.1:1933") if backend_cls is not None else None
check("找到 backend 实例", store is not None and hasattr(store, "ensure_session"))

client = FakeClient(existing=False)
result = asyncio.run(store.ensure_session(client, "main_qq_1"))
check("新会话走 create_session", len(client.created) == 1 and client.created[0][0] == "main_qq_1", str(client.created))
check("policy 被带上", client.created[0][1]["working_memory"] == {"enabled": False}, str(client.created[0][1]))
check("不会再 auto_create", client.auto_created == 0)
check("返回 create 结果", result.get("session_id") == "main_qq_1")

client2 = FakeClient(existing=True)
asyncio.run(store.ensure_session(client2, "main_qq_2"))
check("存量会话不重建", client2.created == [] and client2.auto_created == 0)

client3 = FakeClient(existing=False, support_create=False)
asyncio.run(store.ensure_session(client3, "main_qq_3"))
check("SDK 不支持 policy 时回退 auto_create", client3.auto_created >= 1, str(client3.auto_created))

print("peer_id：用户级记忆空间下取 agent，非法值宁可不传")
from app.viking_store import peer_id_for_agent  # noqa: E402

check("agent_id 直接作 peer_id", peer_id_for_agent("main") == "main" and peer_id_for_agent("alice") == "alice")
check("空值不传", peer_id_for_agent("") == "" and peer_id_for_agent("   ") == "")
check("带斜杠（OpenViking 会判非法）不传", peer_id_for_agent("a/b") == "")
check("中文/非法字符不传", peer_id_for_agent("凌伊") == "")
check("两个 @ 不传", peer_id_for_agent("a@b@c") == "")
check("mode=off 关闭", peer_id_for_agent("main", "off") == "")
check("@ 与下划线允许", peer_id_for_agent("main_bot@v1") == "main_bot@v1")

print()
if FAILS:
    print(f"FAILED {len(FAILS)}: {FAILS}")
    raise SystemExit(1)
print(f"ALL OK — {CHECKS} checks passed")
