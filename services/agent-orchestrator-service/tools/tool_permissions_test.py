"""工具权限分级自检（离线：用临时 system_settings.json）。

覆盖：
 1. 分级表：L0–L4 关键工具落位、未知工具 fail-closed
 2. L1 黑名单：按用户屏蔽、黑名单工具自己永远可用
 3. L2/L3/L4 白名单：名单内放行、名单外拒绝且原因可读
 4. 参数升级：reply-image|global → L2；fetch 非 GET → L3
 5. 提示词说明：只列可用工具，不暴露 L2+ 与分级标准
 6. 黑名单写入/移除（set_blacklist）
"""

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TMP = Path(tempfile.mkdtemp())
SETTINGS = TMP / "system_settings.json"
os.environ["SYSTEM_SETTINGS_PATH"] = str(SETTINGS)

from app import config  # noqa: E402
config.SYSTEM_SETTINGS_PATH = SETTINGS  # 覆盖模块常量

from app import tool_permissions as tp  # noqa: E402

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


def write_settings(section):
    SETTINGS.write_text(json.dumps({"tool_permission": section}, ensure_ascii=False), encoding="utf-8")


def main():
    print("分级表")
    check("file-read → L0", tp.level_of("file-read") == 0)
    check("file-write → L1", tp.level_of("file-write") == 1)
    check("worldinfo-write → L2", tp.level_of("worldinfo-write") == 2)
    check("run-shell → L3", tp.level_of("run-shell") == 3)
    check("delete-file → L3", tp.level_of("delete-file") == 3)
    check("browser → L4", tp.level_of("browser") == 4)
    check("windows-file-copy → L4", tp.level_of("windows-file-copy") == 4)
    check("未知工具 fail-closed → L4", tp.level_of("some-new-tool") == 4)
    check("扩展名按前缀归位", tp.level_of("browser-open") == 4)


    print("档位来自 TOOL.json 的 groups 标签")
    check("fetch 有两条同名条目", len(tp._tool_entries("fetch")) == 2, str(tp._tool_entries("fetch")))
    check("fetch 基础档取最低（L1）", tp.level_of("fetch") == 1, str(tp.level_of("fetch")))
    check("run-shell 归到 shell（后缀匹配）", tp.level_of("run-shell") == 3, str(tp.level_of("run-shell")))
    check("command 同样归到 shell", tp.level_of("command") == 3, str(tp.level_of("command")))
    check("条目缺 L 标签 → L4", tp._entry_level({"groups": ["文件"]}) == 4)
    check("groups 里 L 大小写不敏感", tp._entry_level({"groups": ["l2"]}) == 2)

    print("形态判定（同名多条取档）")
    check("GET 无 body → L1", tp._effective_level("fetch", ["http://x", "GET"], {}) == 1)
    check("不给方法 → L1", tp._effective_level("fetch", ["http://x"], {}) == 1)
    check("POST → L3", tp._effective_level("fetch", ["http://x", "POST"], {}) == 3)
    check("带 body → L3", tp._effective_level("fetch", ["http://x", "GET", "data"], {}) == 3)
    check("kwargs 方法同样生效", tp._effective_level("fetch", ["http://x"], {"method": "PUT"}) == 3)

    print("空配置：L0/L1 放行，L2+ 拒绝")
    write_settings({})
    check("L0 放行", tp.check("u1", "file-read")[0] is True)
    check("L1 放行", tp.check("u1", "file-write")[0] is True)
    ok, reason = tp.check("u1", "worldinfo-write")
    check("L2 拒绝", ok is False)
    check("拒绝原因带档位", "L2" in reason and "白名单" in reason, reason)
    check("L3 拒绝", tp.check("u1", "run-shell")[0] is False)
    check("L4 拒绝", tp.check("u1", "browser")[0] is False)

    print("L1 黑名单（按用户）")
    write_settings({"L1_blacklist_users": ["bad"]})
    check("黑名单用户 L1 被拒", tp.check("bad", "file-write")[0] is False)
    check("黑名单用户 L0 不受影响", tp.check("bad", "file-read")[0] is True)
    check("其他人 L1 正常", tp.check("good", "file-write")[0] is True)
    check("黑名单工具自己永远可用", tp.check("bad", "tool-blacklist")[0] is True)

    print("白名单")
    write_settings({
        "L2_whitelist_users": ["boss"],
        "L3_whitelist_users": ["boss"],
        "L4_whitelist_users": [],
    })
    check("L2 名单内放行", tp.check("boss", "worldinfo-write")[0] is True)
    check("L3 名单内放行", tp.check("boss", "run-shell")[0] is True)
    check("L4 仍拒绝", tp.check("boss", "browser")[0] is False)
    check("名单外 L2 拒绝", tp.check("other", "worldinfo-write")[0] is False)

    print("按参数升级")
    write_settings({})
    check("reply-image 普通 → L1 放行", tp.check("u1", "reply-image", ["词", "url"])[0] is True)
    check("reply-image 第3参写 global 也按 L1（工具内部会拒绝写全局）",
          tp.check("u1", "reply-image", ["词", "url", "global"])[0] is True)
    check("fetch GET → L1 放行", tp.check("u1", "fetch", ["http://x", "GET"])[0] is True)
    check("fetch POST → 需 L3", tp.check("u1", "fetch", ["http://x", "POST", "data"])[0] is False)

    print("提示词说明")
    write_settings({})
    note = tp.describe_for_prompt("u1")
    check("列出可用工具", "file-read" in note and "file-write" in note, note[:80])
    check("不列 L2+ 工具", "worldinfo-write" not in note and "run-shell" not in note and "browser" not in note)
    check("不提分级标准", "L2" not in note and "L3" not in note and "L4" not in note, note[-80:])
    check("提到黑名单工具", tp.BLACKLIST_TOOL in note, note[-80:])
    check("说明只能加入", "解除需要管理员" in note, note[-80:])

    write_settings({"L1_blacklist_users": ["bad"]})
    note_bad = tp.describe_for_prompt("bad")
    check("被屏蔽时只列只读工具", "file-write" not in note_bad, note_bad[:120])
    check("被屏蔽时提示联系管理员", "联系管理员" in note_bad, note_bad[-80:])
    check("被屏蔽时不教自助移出", "移出黑名单" not in note_bad, note_bad[-80:])


    print("通配 * = 该档对所有用户开放")
    write_settings({"L3_whitelist_users": ["*"]})
    check("L3 通配放行任意用户", tp.check("anyone", "run-shell")[0] is True)
    check("L4 未通配仍拒绝", tp.check("anyone", "browser")[0] is False)

    print("从杂项迁移（shell → L3、宿主机 → L4）")
    SETTINGS.write_text(json.dumps({
        "shell_restriction_enabled": False,
        "shell_allowed_users": [],
        "mnt_write_restriction_enabled": True,
        "mnt_write_allowed_users": ["boss"],
    }, ensure_ascii=False), encoding="utf-8")
    tp.migrate_from_misc_settings()
    section = json.loads(SETTINGS.read_text(encoding="utf-8")).get("tool_permission") or {}
    check("shell 关闭限制 → L3 通配", section.get("L3_whitelist_users") == ["*"], str(section))
    check("宿主机限制开启 → L4 搬名单", section.get("L4_whitelist_users") == ["boss"], str(section))
    check("迁移标记已写", bool(json.loads(SETTINGS.read_text(encoding="utf-8")).get(tp.LEGACY_MIGRATED_KEY)))
    check("迁移后判定生效", tp.check("boss", "browser")[0] is True and tp.check("other", "browser")[0] is False)

    again = tp.migrate_from_misc_settings()
    check("重复迁移不再改动", again == "", again)


    write_settings({"L4_whitelist_users": ["boss"]})
    boss_note = tp.describe_for_prompt("boss")
    check("白名单工具也列出来", "browser" in boss_note, boss_note[:120])
    check("非白名单用户不列宿主工具", "browser" not in tp.describe_for_prompt("plain"))

    print("自定义注入说明（通用 + 按档）")
    write_settings({
        "level_notes": {
            "ALL": "通用要求：删除/付款前先确认。",
            "L0": "L0 说明：只读。",
            "L1": "L1 说明：可写自己的工作区。",
            "L3": "L3 说明：可执行 shell。",
        },
        "L3_whitelist_users": ["boss"],
    })
    plain = tp.describe_for_prompt("plain")
    check("通用说明对所有人注入", "通用要求" in plain, plain[-120:])
    check("只注入最高档（L1）", "L1 说明" in plain, plain[-120:])
    check("低档说明不叠加", "L0 说明" not in plain, plain[-120:])
    check("无权档位说明不注入", "L3 说明" not in plain, plain[-120:])
    boss = tp.describe_for_prompt("boss")
    check("白名单用户拿到最高档说明", "L3 说明" in boss, boss[-120:])
    check("中间档不叠加", "L0 说明" not in boss and "L1 说明" not in boss, boss[-160:])
    check("通用说明在前", boss.find("通用要求") < boss.find("L3 说明"), boss[-160:])

    write_settings({"level_notes": {"ALL": "通用X"}, "L1_blacklist_users": ["bad"]})
    bad_note = tp.describe_for_prompt("bad")
    check("被屏蔽仍有通用说明", "通用X" in bad_note, bad_note[-120:])

    write_settings({"level_notes": {"L2": "仅空档"}})
    empty_note = tp.describe_for_prompt("plain")
    check("无可注入说明时不追加空行", "仅空档" not in empty_note, empty_note[-80:])

    print("注入里不出现档位/等级字样")
    write_settings({
        "level_notes": {"ALL": "通用", "L0": "只读", "L1": "本地写", "L2": "全局写", "L4": "宿主"},
        "L4_whitelist_users": ["boss"],
    })
    for user in ("plain", "boss"):
        note = tp.describe_for_prompt(user)
        bad = [w for w in ("档", "等级", "L0", "L1", "L2", "L3", "L4", "白名单") if w in note]
        check(f"{user} 注入无档位字样", not bad, f"{bad} :: {note[-160:]}")

    print("黑名单写入")
    write_settings({})
    check("加入黑名单", "已把用户" in tp.set_blacklist("u2", True), tp.blacklist_users())
    check("列表里有 u2", tp.blacklist_users() == ["u2"], str(tp.blacklist_users()))
    check("重复加入有提示", "已在黑名单" in tp.set_blacklist("u2", True))
    check("移出黑名单", "已把用户" in tp.set_blacklist("u2", False))
    check("列表已空", tp.blacklist_users() == [], str(tp.blacklist_users()))
    check("缺用户 id 报错", tp.set_blacklist("", True).startswith("错误："))

    print()
    if FAILS:
        print(f"FAILED {len(FAILS)}: {FAILS}")
        return 1
    print(f"ALL OK — {CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
