"""回复配图自检（orchestrator 侧）：匹配口径 + 存储往返。

重点验证「最先被命中的」语义：
  - 多条规则命中，取正文里出现位置最靠前的
  - 位置相同按规则顺序
  - ASCII 大小写不敏感；中文子串匹配；空正文不匹配
"""

import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

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


def main():
    tmp = Path(tempfile.mkdtemp())
    try:
        from app import reply_images

        original_dir = reply_images.rules_dir
        reply_images.rules_dir = lambda: tmp  # 隔离到临时目录
        try:
            print("增删查")
            result = reply_images.upsert_rule("main", ["跳起来", "蹦"], "https://img/a.png")
            check("新增成功", "已添加规则" in result, result)
            check("读到 1 条", len(reply_images.load_rules("main")) == 1)
            result = reply_images.upsert_rule("main", ["生气"], "https://img/b.png")
            check("新增第 2 条", "共 2 条" in result, result)
            result = reply_images.upsert_rule("main", ["暴怒"], "https://img/b.png")
            check("同图合并关键词", "已更新规则" in result and "生气" in result, result)
            listing = reply_images.list_rules("main")
            check("列表含两条规则", listing.count("https://img/") == 2, listing)
            result = reply_images.remove_rule("main", "跳起来")
            check("删除指定关键词", "已删除 1 条" in result, result)
            check("删除后剩 1 条", len(reply_images.load_rules("main")) == 1)
            result = reply_images.remove_rule("main", "不存在")
            check("删不存在的关键词有提示", "没有找到" in result, result)

            print("匹配规则：最先出现")
            rules = [
                {"keywords": ["A图片"], "image": "u1"},
                {"keywords": ["B图片"], "image": "u2"},
                {"keywords": ["紧急"], "image": "u3"},
            ]
            hit = reply_images.match_first("这里先说 B图片，然后才 A图片", rules, agent_id="x")
            check("取正文里先出现的 B", hit["image"] == "u2", str(hit))
            hit = reply_images.match_first("开头就有紧急，后面 A图片", rules, agent_id="x")
            check("取正文里先出现的 紧急", hit["image"] == "u3", str(hit))
            hit = reply_images.match_first("没有任何关键词", rules, agent_id="x")
            check("无命中返回 None", hit is None, str(hit))
            hit = reply_images.match_first("", rules, agent_id="x")
            check("空正文 None", hit is None)

            print("同一位置按规则顺序")
            rules_tie = [
                {"keywords": ["重复"], "image": "first"},
                {"keywords": ["重复"], "image": "second"},
            ]
            hit = reply_images.match_first("重复出现", rules_tie, agent_id="x")
            check("取顺序靠前的规则", hit["image"] == "first", str(hit))

            print("ASCII 大小写不敏感")
            hit = reply_images.match_first("用 LSD 打开", [{"keywords": ["lsd"], "image": "u4"}], agent_id="x")
            check("小写关键词命中大写文本", hit is not None, str(hit))

            print("一条规则多关键词只发一张")
            multi = [{"keywords": ["甲", "乙"], "image": "u5"}]
            hit = reply_images.match_first("先乙后甲", multi, agent_id="x")
            check("命中即可", hit["image"] == "u5", str(hit))


            print("用户隔离（同一智能体）")
            reply_images.upsert_rule("main", ["甲"], "https://img/u1.png", user_id="user1")
            reply_images.upsert_rule("main", ["乙"], "https://img/u2.png", user_id="user2")
            check("user1 只看到自己的", [r["image"] for r in reply_images.load_rules("main", "user1")] == ["https://img/u1.png"],
                  str(reply_images.load_rules("main", "user1")))
            check("user2 只看到自己的", [r["image"] for r in reply_images.load_rules("main", "user2")] == ["https://img/u2.png"])
            hit_u1 = reply_images.match_first("这里说乙", agent_id="main", user_id="user1")
            check("user1 匹配不到 user2 的关键词", hit_u1 is None, str(hit_u1))


            print("全局规则 + 按用户规则拼接")
            reply_images.upsert_rule("main", ["全局词"], "https://img/global.png", user_id=reply_images.GLOBAL_SCOPE_DIR)
            reply_images.upsert_rule("main", ["私有词"], "https://img/mine.png", user_id="userA")
            merged = reply_images.load_rules("main", "userA")
            check("拼接后 2 条", len(merged) == 2, str(merged))
            check("用户规则在前", merged[0]["image"] == "https://img/mine.png", str(merged[0]))
            check("全局规则在后且标记 scope", merged[1].get("scope") == "global", str(merged[1]))
            hit_global = reply_images.match_first("这里说全局词", agent_id="main", user_id="userA")
            check("用户能用全局规则", hit_global and hit_global["image"] == "https://img/global.png", str(hit_global))
            hit_other = reply_images.match_first("这里说私有词", agent_id="main", user_id="userB")
            check("别的用户看不到私有规则", hit_other is None, str(hit_other))
            # 删除只动自己的文件：删全局词不影响全局
            out = reply_images.remove_rule("main", "私有词", user_id="userA")
            check("删除自己的规则", "已删除" in out, out)
            still = reply_images.load_rules("main", "userA")
            check("全局仍保留", any(r["image"] == "https://img/global.png" for r in still), str(still))
            check("没把全局写进用户文件",
                  all(r.get("scope") != "global" for r in reply_images.load_rules("main", "userA", include_global=False)),
                  str(reply_images.load_rules("main", "userA", include_global=False)))


            print("全局规则：按关键词去重 + 本人优先")
            reply_images.save_rules("main", [], reply_images.GLOBAL_SCOPE_DIR)  # 清掉上一段
            reply_images.upsert_rule("main", ["全局词"], "https://img/g.png", user_id=reply_images.GLOBAL_SCOPE_DIR)
            reply_images.upsert_rule("main", ["私有词"], "https://img/mine.png", user_id="userA")

            merged = reply_images.load_rules("main", "userA")
            check("本人 + 全局拼接", len(merged) == 2, str(merged))
            check("本人规则在前", merged[0]["image"] == "https://img/mine.png", str(merged[0]))
            check("全局规则标记 scope", merged[1].get("scope") == "global", str(merged[1]))

            # 本人写同名关键词 → 全局那条不再拼进来（关键词去重，本人优先）
            reply_images.upsert_rule("main", ["全局词"], "https://img/mine2.png", user_id="userA")
            dedup = reply_images.load_rules("main", "userA")
            check("本人同名关键词挤掉全局", len(dedup) == 2 and all(m["image"] != "https://img/g.png" for m in dedup), str(dedup))

            hit = reply_images.match_first("这里说全局词", agent_id="main", user_id="userC")
            check("没设过的用户能用全局", hit and hit["image"] == "https://img/g.png", str(hit))

            print("promote_to_global：把某用户规则并入全局（关键词去重）")
            reply_images.save_rules("main", [], reply_images.GLOBAL_SCOPE_DIR)
            reply_images.upsert_rule("main", ["甲", "乙"], "https://img/both.png", user_id="boss")
            out = reply_images.promote_to_global("main", "boss", by="dashboard")
            check("提升成功", "已加入全局" in out, out)
            check("全局有 1 条", len(reply_images._load_file("main", reply_images.GLOBAL_SCOPE_DIR)) == 1)
            out2 = reply_images.promote_to_global("main", "boss", by="dashboard")
            check("重复提升被拦", "没有需要新增" in out2, out2)

            reply_images.upsert_rule("main", ["丙"], "https://img/c.png", user_id="boss")
            out3 = reply_images.promote_to_global("main", "boss", keyword="丙", by="dashboard")
            check("按关键词单条提升", "已加入全局 1 条" in out3, out3)
            globals_now = reply_images._load_file("main", reply_images.GLOBAL_SCOPE_DIR)
            check("全局 2 条", len(globals_now) == 2, str(globals_now))

            print("旧 _source.json 迁移")
            reply_images.save_rules("main", [], reply_images.GLOBAL_SCOPE_DIR)
            reply_images.upsert_rule("main", ["旧词"], "https://img/old.png", user_id="legacy")
            (reply_images.rules_dir() / "_source.json").write_text(
                json.dumps({"source_user": "legacy"}), encoding="utf-8")
            note = reply_images.migrate_legacy_source()
            check("迁移有结果", "已迁移" in note, note)
            check("_source.json 已删除", not (reply_images.rules_dir() / "_source.json").exists())
            migrated = reply_images._load_file("main", reply_images.GLOBAL_SCOPE_DIR)
            check("旧用户的规则进了全局",
                  any(r["image"] == "https://img/old.png" for r in migrated), str(migrated))

            print("损坏文件不炸")
            (tmp / "broken.json").write_text("{not json", encoding="utf-8")
            check("损坏文件返回空规则", reply_images.load_rules("broken") == [])
        finally:
            reply_images.rules_dir = original_dir
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print()
    if FAILS:
        print(f"FAILED {len(FAILS)}: {FAILS}")
        return 1
    print(f"ALL OK — {CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
