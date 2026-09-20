"""user-service 别名（`alias_of`）与对账逻辑冒烟测试。

覆盖：指针生成/移除、改绑、整体替换、冲突拒绝、透明跟随、只一跳、空记录就地转换、
非空记录拒写、指针路径写入重定向、删除清理、启动回填、以及短路不能吃掉对账。

运行：python tools/user_alias_smoke_test.py
"""

import json
import os
import sys
import tempfile
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="user_alias_test_")
os.environ["USER_DATA_DIR"] = _TMP

SERVICE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVICE_DIR))

# app.logger 用**相对路径** logs/<type>/<date>.log 写日志，会把文件落在当前工作目录。
# 切到临时目录，避免本地跑测试时在仓库里留下 logs/。
os.chdir(_TMP)

from app.user_store import _desired_aliases, user_store  # noqa: E402  (单例已绑定临时目录)

FAILURES = []
CHECKS = 0


def check(name, condition):
    global CHECKS
    CHECKS += 1
    if not condition:
        FAILURES.append(name)
        print(f"  FAIL  {name}")
    else:
        print(f"  ok    {name}")


def write(user_id, data):
    (Path(_TMP) / f"{user_id}.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def read(user_id):
    p = Path(_TMP) / f"{user_id}.json"
    if not p.exists():
        return None
    return json.loads(p.read_text(encoding="utf-8"))


def reset():
    for p in Path(_TMP).glob("*.json"):
        p.unlink()


print("=== 1. _desired_aliases：自映射不产生指针 ===")
check("web 自映射被排除", _desired_aliases("h268", {"web": {"channel_user_id": "h268"}}) == set())
check("异构 id 产生指针", _desired_aliases("h268", {"qq": {"channel_user_id": "2195239690"}}) == {"2195239690"})
check("混合时只留异构", _desired_aliases(
    "h268", {"web": {"channel_user_id": "h268"}, "qq": {"channel_user_id": "2195"}}
) == {"2195"})
check("空 channels", _desired_aliases("h268", {}) == set())
check("channels 非 dict 安全", _desired_aliases("h268", None) == set())
check("channel_user_id 缺失/空安全", _desired_aliases("h268", {"qq": {"channel_user_id": ""}}) == set())

print("\n=== 2. 绑定：异构 id 生成指针，自映射不生成 ===")
reset()
user_store.bind_channel("h268", "web", "h268", 0)
check("web 自映射不写指针", read("h268") is not None and read("h268").get("alias_of") is None)
check("web 绑定本身成功落盘", read("h268")["channels"]["web"]["channel_user_id"] == "h268")

user_store.bind_channel("h268", "qq", "2195239690", 0)
check("qq 绑定生成指针文件", read("2195239690") is not None)
check("指针内容正确", read("2195239690").get("alias_of") == "h268")
check("指针自身不带渠道", read("2195239690").get("channels") == {})
check("规范记录持有渠道", set(read("h268")["channels"]) == {"web", "qq"})

print("\n=== 3. 短路不能吃掉对账（我修掉的那个 bug）===")
reset()
# 先绑一个自映射（desired 前后都是空集 → 短路），再绑异构
user_store.bind_channel("h268", "web", "h268", 0)
user_store.bind_channel("h268", "qq", "2195239690", 0)
check("自映射先绑不阻止后续指针生成", read("2195239690").get("alias_of") == "h268")
# 重复绑同一个自映射（幂等）后，已有指针不能被删
user_store.bind_channel("h268", "web", "h268", 0)
check("幂等重绑不删除已有指针", read("2195239690").get("alias_of") == "h268")

print("\n=== 4. 改绑：旧指针移除、新指针生成 ===")
reset()
user_store.bind_channel("h268", "qq", "2195239690", 0)
check("改绑前旧指针存在", read("2195239690").get("alias_of") == "h268")
user_store.bind_channel("h268", "qq", "2999999999", 0)
check("改绑后新指针存在", read("2999999999").get("alias_of") == "h268")
check("改绑后旧指针字段被移除", read("2195239690").get("alias_of") is None)
check("改绑后旧文件仍存在（非破坏性）", read("2195239690") is not None)
check("规范记录只剩新 id", read("h268")["channels"]["qq"]["channel_user_id"] == "2999999999")

print("\n=== 5. 解绑：指针移除 ===")
reset()
user_store.bind_channel("h268", "qq", "2195239690", 0)
check("解绑返回 ok", user_store.unbind_channel("h268", "qq") == "ok")
check("解绑后指针字段移除", read("2195239690").get("alias_of") is None)
check("解绑后文件保留", read("2195239690") is not None)
check("规范记录渠道已空", read("h268")["channels"] == {})
check("重复解绑报未绑定", user_store.unbind_channel("h268", "qq") == "渠道未绑定")

print("\n=== 6. upsert 整体替换 channels（绕过 bind_channel 的入口）===")
reset()
user_store.bind_channel("h268", "qq", "2195239690", 0)
# 整体替换为完全不同的渠道集合
user_store.upsert_user("h268", json.dumps({"channels": {"tg": {"channel": "tg", "channel_user_id": "abc"}}}))
check("整体替换后旧指针被清理", read("2195239690").get("alias_of") is None)
check("整体替换后新指针生成", read("abc").get("alias_of") == "h268")
check("整体替换生效", set(read("h268")["channels"]) == {"tg"})
# 整体清空 channels
user_store.upsert_user("h268", json.dumps({"channels": {}}))
check("channels 清空后指针全部清理", read("abc").get("alias_of") is None)

print("\n=== 7. 冲突拒绝（你选的策略）===")
reset()
user_store.bind_channel("h268", "qq", "2195239690", 0)
result = user_store.bind_channel("other", "qq", "2195239690", 0)
check("第二个用户绑定同一渠道身份被拒", result != "ok")
check("拒绝信息含现有持有者", "h268" in result)
check("被拒后未创建 other 的指针", (read("other") or {}).get("alias_of") is None)
check("被拒后指针仍指向原主", read("2195239690").get("alias_of") == "h268")
# upsert 路径也要拒绝
result2 = user_store.upsert_user("other2", json.dumps(
    {"channels": {"qq": {"channel": "qq", "channel_user_id": "2195239690"}}}
))
check("upsert 同样拒绝冲突渠道身份", result2 != "other2")
# 同一用户重复绑同一身份不算冲突
check("同一用户重复绑不算冲突", user_store.bind_channel("h268", "qq", "2195239690", 5) == "ok")

print("\n=== 8. 透明跟随（GetUser 语义）===")
reset()
user_store.bind_channel("h268", "web", "h268", 0)
user_store.bind_channel("h268", "qq", "2195239690", 0)
got = user_store.get_user("2195239690")
check("按 QQ 号读拿到规范记录", got.get("user_id") == "h268")
check("拿到的 channels 是 h268 的", set(got.get("channels", {})) == {"web", "qq"})
check("规范用户读自己不变", user_store.get_user("h268").get("user_id") == "h268")
check("无别名用户读自己不变", (lambda: (write("solo", {"user_id": "solo", "channels": {}}),
      user_store.get_user("solo").get("user_id") == "solo")[-1])())

print("\n=== 9. 只一跳，不追链 ===")
reset()
write("a", {"user_id": "a", "alias_of": "b", "channels": {}})
write("b", {"user_id": "b", "alias_of": "c", "channels": {}})
write("c", {"user_id": "c", "channels": {"web": {"channel_user_id": "c"}}})
check("链式别名解析退回自身", user_store.resolve_alias("a") == "a")
check("链式别名读取退回自身记录", user_store.get_user("a").get("user_id") == "a")
write("d", {"user_id": "d", "alias_of": "missing", "channels": {}})
check("目标缺失时退回自身", user_store.resolve_alias("d") == "d")

print("\n=== 10. 空记录就地转换 vs 非空记录拒写 ===")
reset()
write("2195239690", {"user_id": "2195239690", "channels": {}, "created_at": "2026-08-12T05:36:00"})
user_store.bind_channel("h268", "qq", "2195239690", 0)
check("空记录被就地转成指针", read("2195239690").get("alias_of") == "h268")
check("就地转换保留原 created_at", read("2195239690").get("created_at") == "2026-08-12T05:36:00")

reset()
write("occupied", {"user_id": "occupied", "channels": {"web": {"channel_user_id": "occupied"}}})
user_store.bind_channel("h268", "qq", "occupied", 0)
check("非空记录不被降级为指针", read("occupied").get("alias_of") is None)
check("非空记录仍是独立用户", read("occupied")["channels"]["web"]["channel_user_id"] == "occupied")
check("规范记录仍记下了该绑定", read("h268")["channels"]["qq"]["channel_user_id"] == "occupied")

print("\n=== 11. 指针路径上的写入重定向到规范记录 ===")
reset()
user_store.bind_channel("h268", "qq", "2195239690", 0)
user_store.upsert_user("2195239690", json.dumps({"nickname": "凌伊"}))
check("upsert 指针路径写到规范记录", read("h268").get("nickname") == "凌伊")
check("指针未被写入 nickname", read("2195239690").get("nickname") is None)
user_store.set_openviking_key("2195239690", "qq-own-key")
user_store.set_openviking_key("h268", "web-own-key")
check("OpenViking key 不重定向（各自身份独立）",
      read("2195239690").get("openviking_api_key") == "qq-own-key")
check("规范用户的 key 未被覆盖", read("h268").get("openviking_api_key") == "web-own-key")
check("按指针路径读到自己那份 key", user_store.get_openviking_key("2195239690") == "qq-own-key")
check("按规范 id 读到规范那份 key", user_store.get_openviking_key("h268") == "web-own-key")
check("按指针路径绑定仍重定向", user_store.bind_channel("2195239690", "tg", "tg-1", 0) == "ok")
check("tg 指针挂在规范用户名下", read("tg-1").get("alias_of") == "h268")
check("重定向后指针的 key 未被覆盖", read("2195239690").get("openviking_api_key") == "qq-own-key")
check("重定向后指针仍不带渠道", read("2195239690").get("channels") == {})

print("\n=== 12. 删除清理 ===")
reset()
user_store.bind_channel("h268", "qq", "2195239690", 0)
check("删规范用户返回 True", user_store.delete_user("h268") is True)
check("规范用户文件已删除", read("h268") is None)
check("指向它的指针字段被清理", read("2195239690").get("alias_of") is None)

reset()
user_store.bind_channel("h268", "qq", "2195239690", 0)
user_store.delete_user("2195239690")
check("删指针路径只删该文件", read("2195239690") is None)
check("删指针路径不动规范用户", read("h268") is not None)
check("删不存在的用户返回 False", user_store.delete_user("nobody") is False)

print("\n=== 13. 启动回填：模拟线上 h268 <-> 2195239690 ===")
reset()
write("h268", {"user_id": "h268", "created_at": "2026-08-12T01:40:00", "channels": {
    "qq": {"channel": "qq", "channel_user_id": "2195239690", "priority": 0},
    "web": {"channel": "web", "channel_user_id": "h268", "priority": 0},
}})
write("2195239690", {"user_id": "2195239690", "created_at": "2026-08-12T05:36:00", "channels": {}})
write("dsh_fwd_001", {"user_id": "dsh_fwd_001", "channels": {
    "web": {"channel": "web", "channel_user_id": "dsh_fwd_001", "priority": 0},
    "qq": {"channel": "qq", "channel_user_id": "999000111", "priority": 0},
}})
write("zhangricheng", {"user_id": "zhangricheng", "channels": {
    "web": {"channel": "web", "channel_user_id": "zhangricheng", "priority": 0},
}})
stats = user_store.reconcile_all()
check("回填写出 2 个指针（h268 + dsh_fwd_001）", stats["written"] == 2)
check("h268 的 QQ 指针回填成功", read("2195239690").get("alias_of") == "h268")
check("dsh_fwd_001 的 QQ 指针回填成功", read("999000111").get("alias_of") == "dsh_fwd_001")
check("web-only 用户不产生指针", read("zhangricheng").get("alias_of") is None)
check("回填产生 0 冲突", stats["conflicts"] == 0)
stats2 = user_store.reconcile_all()
check("对账幂等（第二次无写入无移除）",
      stats2["written"] == 0 and stats2["removed"] == 0)
check("对账后按 QQ 号能解析出渠道", set(user_store.get_user("2195239690")["channels"]) == {"qq", "web"})

print("\n=== 14. 自愈历史漂移（无主指针被清理）===")
reset()
write("h268", {"user_id": "h268", "channels": {}})
write("ghost", {"user_id": "ghost", "alias_of": "h268", "channels": {}})
check("无主指针初始存在", read("ghost").get("alias_of") == "h268")
user_store.reconcile_all()
check("全量对账清掉无主指针", read("ghost").get("alias_of") is None)

print("\n=== 15. 多用户渠道计数（回归：线上 4 个 qq 绑定各唯一）===")
reset()
for uid, qq in (("h268", "2195239690"), ("dsh_fwd_001", "999000111"),
                ("dsh_fwd_002", "999000222"), ("dsh_fwd_003", "999000333")):
    write(uid, {"user_id": uid, "channels": {
        "web": {"channel": "web", "channel_user_id": uid, "priority": 0},
        "qq": {"channel": "qq", "channel_user_id": qq, "priority": 0},
    }})
stats = user_store.reconcile_all()
check("4 个用户各生成 1 个指针", stats["written"] == 4 and stats["conflicts"] == 0)
for uid, qq in (("h268", "2195239690"), ("dsh_fwd_001", "999000111"),
                ("dsh_fwd_002", "999000222"), ("dsh_fwd_003", "999000333")):
    if read(qq).get("alias_of") != uid:
        check(f"{qq} 指针正确", False)
check("4 个 qq 指针全部正确", all(
    read(qq).get("alias_of") == uid
    for uid, qq in (("h268", "2195239690"), ("dsh_fwd_001", "999000111"),
                    ("dsh_fwd_002", "999000222"), ("dsh_fwd_003", "999000333"))
))

print()
if FAILURES:
    print(f"FAILURES ({len(FAILURES)}):")
    for name in FAILURES:
        print(f"  - {name}")
    sys.exit(1)
print(f"ALL OK — {CHECKS} checks passed")
