"""session_target 自测：会话 id ↔ 投递目标（纯函数，无依赖）。

运行：python tools/session_target_smoke_test.py
"""
import os
import sys

SERVICE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, SERVICE_ROOT)

from app.session_target import parse_session_target  # noqa: E402

CASES = [
    ("qq_2195239690", ("private", "2195239690")),
    ("qq_OPENID_ABC", ("private", "OPENID_ABC")),
    ("qq_g_888888", ("group", "888888")),
    ("qq_g_GROUP_OPENID_X", ("group", "GROUP_OPENID_X")),
    ("", None),
    ("web_h268", None),
    ("qq_", None),
    ("qq_g_", None),
    ("  qq_123  ", ("private", "123")),
]

failed = 0
for session_id, expected in CASES:
    got = parse_session_target(session_id)
    ok = got == expected
    if not ok:
        failed += 1
        print(f"[FAIL] {session_id!r}: expected {expected}, got {got}")

print(f"done: {len(CASES) - failed}/{len(CASES)} passed")
sys.exit(1 if failed else 0)
