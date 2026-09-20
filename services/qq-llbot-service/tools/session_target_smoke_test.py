"""会话 id → 推送目标 的解析冒烟测试（P3 的核心纯函数）。

会话 id 的两种形式是确定性编码的，所以推送目标可以直接从 session_id 重建 ——
这是"不需要用户先发一条消息就能推送"的前提（此前依赖进程内 `_sessions` 缓存，
服务重启后必然为空，所有被动推送都被静默丢弃）。

运行：python tools/session_target_smoke_test.py
"""

import sys
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVICE_DIR))

# 纯函数模块，不依赖 satori/aiohttp —— 本地与容器内都能跑
from app.session_target import parse_session_target  # noqa: E402

FAILURES = []
CHECKS = 0


def check(name, condition, extra=""):
    global CHECKS
    CHECKS += 1
    if not condition:
        FAILURES.append(name)
        print(f"  FAIL  {name} {extra}")
    else:
        print(f"  ok    {name}")


print("=== 私聊（qq_{QQ号}）===")
check("普通 QQ 号", parse_session_target("qq_2195239690") == ("private", "2195239690"))
check("另一位 QQ 用户", parse_session_target("qq_2682503133") == ("private", "2682503133"))
check("短 QQ 号", parse_session_target("qq_12345") == ("private", "12345"))
check("前后空白被容忍", parse_session_target("  qq_2195239690  ") == ("private", "2195239690"))

print("\n=== 群聊（qq_g_{群号}）===")
check("普通群号", parse_session_target("qq_g_851648664") == ("group", "851648664"))
check("测试群号", parse_session_target("qq_g_test_1787673564") == ("group", "qq_g_test_1787673564"[5:]))
check("群号先于私聊前缀匹配（qq_g_ 不被当成 qq_）",
      parse_session_target("qq_g_851648664")[0] == "group")

print("\n=== 非法 / 边界 ===")
check("空串", parse_session_target("") is None)
check("None 安全", parse_session_target(None) is None)
check("只有前缀 qq_", parse_session_target("qq_") is None)
check("只有前缀 qq_g_", parse_session_target("qq_g_") is None)
check("不认识的格式（web 会话）", parse_session_target("web_h268") is None)
check("不认识的格式（多角色群聊）", parse_session_target("grp_agents_chat_xiunv5") is None)
check("纯数字无前缀", parse_session_target("2195239690") is None)
check("前缀大小写敏感（QQ_ 不匹配）", parse_session_target("QQ_123") is None)

print("\n=== 与 _session_key 的一致性（往返）===")
# qq_bridge._session_key 的产出必须能被 parse_session_target 解析回同一目标
for qq_id in ("2195239690", "10001", "2682503133"):
    key = f"qq_{qq_id}"
    check(f"私聊往返 {qq_id}", parse_session_target(key) == ("private", qq_id))
for gid in ("851648664", "1065434062"):
    key = f"qq_g_{gid}"
    check(f"群聊往返 {gid}", parse_session_target(key) == ("group", gid))

print()
if FAILURES:
    print(f"FAILURES ({len(FAILURES)}):")
    for name in FAILURES:
        print(f"  - {name}")
    sys.exit(1)
print(f"ALL OK — {CHECKS} checks passed")
