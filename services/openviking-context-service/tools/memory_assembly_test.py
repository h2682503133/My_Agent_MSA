"""记忆组装（memory_assembly）自检：离线、不联网、不依赖 OpenViking。

覆盖：
- URI → 记忆类型推断（profile/identity/soul/preferences/entities/events/cases/trajectories/experiences/session）
- OpenViking 命中归一化（abstract 优先于 overview 优先于全文；metadata 里的 type/status/tags）
- 组装策略：阈值、废弃状态、状态白名单、内容去重、类型优先级、每类上限、条数/字符预算、兜底
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SERVICE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SERVICE))

from app import memory_assembly as ma  # noqa: E402

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


print("URI → 类型")
cases = {
    "viking://user/u1/memories/profile.md": ma.TYPE_PROFILE,
    "viking://user/u1/memories/identity.md": ma.TYPE_IDENTITY,
    "viking://user/u1/memories/soul.md": ma.TYPE_SOUL,
    "viking://user/u1/memories/preferences/沟通风格.md": ma.TYPE_PREFERENCES,
    "viking://user/u1/memories/entities/游戏角色": ma.TYPE_ENTITIES,
    "viking://user/u1/memories/events/x.md": ma.TYPE_EVENTS,
    "viking://user/u1/memories/cases/灯台仪式.md": ma.TYPE_CASES,
    "viking://user/u1/memories/trajectories/player_search_body.md": ma.TYPE_TRAJECTORIES,
    "viking://user/u1/memories/experiences/RPG推门.md": ma.TYPE_EXPERIENCES,
    "viking://user/u1/sessions/kp_qq_1/messages": ma.TYPE_SESSION,
}
for uri, want in cases.items():
    got = ma.classify_uri(uri)
    check(f"classify {want}", got == want, f"{uri} → {got}")
check("空 URI → unknown", ma.classify_uri("") == ma.TYPE_UNKNOWN)

print("命中归一化")
hit = {
    "abstract": "用户偏好简短回复",
    "content": "很长的正文……",
    "score": 0.82,
    "uri": "viking://user/u1/memories/preferences/a.md",
    "metadata": {"memory_type": "preferences", "status": "production", "tags": ["style"]},
    "token_count": 12,
}
item = ma.from_hit(hit, source="global")
check("abstract 优先（省 token）", item.level == "abstract" and item.content == "用户偏好简短回复")
check("metadata 里的 memory_type", item.memory_type == ma.TYPE_PREFERENCES, item.memory_type)
check("metadata 里的 status/tags", item.status == "production" and item.tags == ("style",))
check("source/token 带上", item.source == "global" and item.token_count == 12)
check("空内容 → None", ma.from_hit({"score": 0.9}) is None)
check("非 dict → None", ma.from_hit("not a dict") is None)
check("overview 次选", ma.from_hit({"overview": "L1 说明", "content": "全文"}).level == "overview")
check("没有 metadata 时按 URI 推断", ma.from_hit({"content": "x", "uri": "viking://user/u1/memories/cases/c.md"}).memory_type == ma.TYPE_CASES)

print("组装：阈值 / 状态 / 去重")
base = [
    ma.MemoryItem(content="A", score=0.9, memory_type=ma.TYPE_EXPERIENCES),
    ma.MemoryItem(content="B", score=0.1, memory_type=ma.TYPE_EXPERIENCES),
    ma.MemoryItem(content="C", score=0.8, memory_type=ma.TYPE_EVENTS, status="deprecated"),
    ma.MemoryItem(content="A", score=0.5, memory_type=ma.TYPE_EXPERIENCES),
]
res = ma.assemble(base, max_items=10, min_score=0.5)
check("低分丢弃", all(i.content != "B" for i in res.selected), str(res.dropped))
check("废弃状态丢弃", all(i.content != "C" for i in res.selected))
check("同内容去重（留高分）", [i.content for i in res.selected] == ["A"], [i.content for i in res.selected])
check("丢弃原因可查", "low_score" in res.drop_reasons and "dead_status" in res.drop_reasons, str(res.drop_reasons))

draft = [ma.MemoryItem(content="D", score=0.9, memory_type=ma.TYPE_EXPERIENCES, status="draft")]
check("draft 默认保留（当前集群全是 draft）", len(ma.assemble(draft).selected) == 1)
check("白名单可只取 production", ma.assemble(draft, keep_statuses={"production"}).selected == [])
check("deprecated 也能显式保留", len(ma.assemble(
    [ma.MemoryItem(content="E", score=0.9, status="deprecated")], drop_dead_statuses=False).selected) == 1)

print("组装：类型优先级 / 每类上限 / 预算")
mixed = [
    ma.MemoryItem(content="经验1", score=0.99, memory_type=ma.TYPE_EXPERIENCES),
    ma.MemoryItem(content="画像", score=0.10, memory_type=ma.TYPE_PROFILE),
    ma.MemoryItem(content="偏好", score=0.20, memory_type=ma.TYPE_PREFERENCES),
    ma.MemoryItem(content="实体", score=0.30, memory_type=ma.TYPE_ENTITIES),
]
ordered = [i.memory_type for i in ma.assemble(mixed, max_items=10).selected]
check("常驻画像排最前（不靠分数）", ordered[0] == ma.TYPE_PROFILE, str(ordered))
check("偏好优先于实体/经验", ordered.index(ma.TYPE_PREFERENCES) < ordered.index(ma.TYPE_ENTITIES))

capped = ma.assemble(
    [ma.MemoryItem(content=f"经验{i}", score=0.9 - i / 10, memory_type=ma.TYPE_EXPERIENCES) for i in range(5)]
    + [ma.MemoryItem(content=f"实体{i}", score=0.1, memory_type=ma.TYPE_ENTITIES) for i in range(3)],
    max_items=10,
    per_type_limit={ma.TYPE_EXPERIENCES: 2, ma.TYPE_ENTITIES: 1},
)
check("每类上限生效", sum(1 for i in capped.selected if i.memory_type == ma.TYPE_EXPERIENCES) == 2)
check("每类上限对实体生效", sum(1 for i in capped.selected if i.memory_type == ma.TYPE_ENTITIES) == 1)
check("被砍的记 type_limit", capped.drop_reasons.get("type_limit", 0) == 5, str(capped.drop_reasons))

budget = ma.assemble(
    [ma.MemoryItem(content="x" * 100, score=0.9), ma.MemoryItem(content="y" * 100, score=0.8)],
    max_items=10, max_chars=150,
)
check("字符预算裁剪", len(budget.selected) == 1 and budget.drop_reasons.get("max_chars") == 1, str(budget.drop_reasons))
check("max_items 裁剪", len(ma.assemble(mixed, max_items=2).selected) == 2)

print("单条上限（max_item_chars / MEMORY_ITEM_MAX_CHARS=1200）")
long_text = "很长的画像内容。" * 300
one = ma.assemble([ma.MemoryItem(content=long_text, score=0.9, memory_type=ma.TYPE_PROFILE)], max_item_chars=1200)
check("超长单条被截断", len(one.selected[0].content) <= 1220, str(len(one.selected[0].content)))
check("截断有标注", "已截断" in one.selected[0].content, one.selected[0].content[-40:])
check("保留开头（信息密度最高处）", one.selected[0].content.startswith("很长的画像内容。"))
check("max_item_chars=0 → 不截断", ma.assemble(
    [ma.MemoryItem(content=long_text, score=0.9)], max_item_chars=0).selected[0].content == long_text)
check("截断后才计入字符预算", len(ma.assemble(
    [ma.MemoryItem(content=long_text, score=0.9)], max_item_chars=1200, max_chars=1300).selected) == 1)
check("兜底那条不受单条上限影响", ma.assemble(
    [], max_item_chars=50, fallback=ma.MemoryItem(content="x" * 200, score=-1.0)).selected[0].content == "x" * 200)

print("兜底与渲染")
empty = ma.assemble([], fallback=ma.MemoryItem(content="最近一条用户消息", score=-1.0))
check("全空时兜底注入", empty.selected and empty.selected[0].content == "最近一条用户消息")
check("兜底不受阈值限制（score<0）", not any(r == "low_score" for _, r in empty.dropped))
text = ma.render(ma.assemble([
    ma.MemoryItem(content="画像内容", memory_type=ma.TYPE_PROFILE),
    ma.MemoryItem(content="草稿经验", memory_type=ma.TYPE_EXPERIENCES, status="draft"),
], max_items=5), header="长期记忆")
check("渲染带类型标注", "[profile]" in text and "[experiences/draft]" in text, text)
check("空结果渲染成空串", ma.render(ma.AssemblyResult()) == "")

print("兼容：无元数据的命中保持老行为")
legacy = ma.from_hits([{"content": "m1", "score": 0.9}, {"content": "m2", "score": 0.8}], source="session")
payload = ma.assemble(legacy, max_items=5).payloads()
check("输出仍是 memory_id/content/score/token_count", set(payload[0]) >= {"memory_id", "content", "score", "token_count"}, str(payload[0]))
check("unknown 类型不写进 payload", "memory_type" not in payload[0], str(payload[0]))

print("来源顺序：归档摘要 > 本会话 > 全库 > 本地召回（老数据的兜底顺序）")
mixed_sources = [
    ma.MemoryItem(content="recall", score=0.95, source="recall"),
    ma.MemoryItem(content="global", score=0.90, source="global"),
    ma.MemoryItem(content="session", score=0.85, source="session"),
    ma.MemoryItem(content="archive", score=0.0, source="archive"),
]
order = [i.content for i in ma.assemble(mixed_sources, max_items=4).selected]
check("archive 排第一（分数 0 也不影响）", order[0] == "archive", str(order))
check("session 先于 global/recall", order.index("session") < order.index("global") < order.index("recall"), str(order))
check("截断时留住高优先来源", [i.content for i in ma.assemble(mixed_sources, max_items=2).selected] == ["archive", "session"])

print("配置解析（MEMORY_ASSEMBLY_PER_TYPE）")
sys.path.insert(0, str(SERVICE))
from app import config  # noqa: E402

config.MEMORY_ASSEMBLY_PER_TYPE = "experiences=2, cases=1 ,bad=,x=0,y=-3"
limits = config.memory_per_type_limits()
check("正常项解析", limits.get("experiences") == 2 and limits.get("cases") == 1, str(limits))
check("非法项被忽略", "bad" not in limits and "x" not in limits and "y" not in limits, str(limits))
config.MEMORY_ASSEMBLY_PER_TYPE = ""
check("空字符串 → 不限制", config.memory_per_type_limits() == {})

print()
if FAILS:
    print(f"FAILED {len(FAILS)}: {FAILS}")
    raise SystemExit(1)
print(f"ALL OK — {CHECKS} checks passed")
