"""reply-image 工具自检（tool-runtime 侧）：写入/删除/列出 + 与 orchestrator 读取格式一致。"""

import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import reply_image_tool  # noqa: E402

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
    root = Path(tempfile.mkdtemp())
    # 离线自检：默认放行 URL 校验（只有"远程 URL 校验"那一段临时换回严格版）
    _real_verify = reply_image_tool.verify_image_url
    reply_image_tool.verify_image_url = lambda url, timeout=6: ""
    try:
        print("三种调用形态")
        out = reply_image_tool.handle(root, "main", [], {})
        check("空参数 = 列出", "还没有回复配图规则" in out, out)

        out = reply_image_tool.handle(root, "main", ["跳起来,蹦", "https://img/a.png"], {})
        check("新增规则", "已添加规则" in out, out)

        out = reply_image_tool.handle(root, "main", [], {})
        check("列出包含关键词", "跳起来" in out and "https://img/a.png" in out, out)

        out = reply_image_tool.handle(root, "main", ["跳起来", ""], {})
        check("图片留空 = 删除", "已删除 1 条" in out, out)
        check("删除后为空", reply_image_tool.load_rules(root, "main") == [])

        print("校验")
        out = reply_image_tool.handle(root, "main", ["", "https://img/a.png"], {})
        check("只有图片没关键词 → 报错", out.startswith("错误：请给出关键词"), out)
        out = reply_image_tool.handle(root, "main", ["关键词", ""], {})
        check("只有关键词 → 走删除并提示没找到", "没有包含" in out, out)

        print("同图合并关键词")
        reply_image_tool.handle(root, "main", ["生气", "https://img/b.png"], {})
        out = reply_image_tool.handle(root, "main", ["愤怒", "https://img/b.png"], {})
        check("合并提示", "已更新规则" in out, out)
        rules = reply_image_tool.load_rules(root, "main")
        check("只有 1 条规则", len(rules) == 1, str(rules))
        check("两个关键词都在", set(rules[0]["keywords"]) == {"生气", "愤怒"}, str(rules[0]["keywords"]))

        print("文件格式与 orchestrator 一致")
        path = root / "default" / "main.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        check("顶层含 agent_id", payload.get("agent_id") == "main", str(payload.keys()))
        check("rules 是数组", isinstance(payload.get("rules"), list))
        check("每条含 keywords/image", {"keywords", "image"} <= set(payload["rules"][0].keys()), str(payload["rules"][0]))

        print("agent 隔离")
        reply_image_tool.handle(root, "alice", ["luna", "https://img/c.png"], {})
        check("main 不受影响", len(reply_image_tool.load_rules(root, "main")) == 1)
        check("alice 独立", len(reply_image_tool.load_rules(root, "alice")) == 1)

        print("用户隔离")
        reply_image_tool.handle(root, "main", ["u1关键词", "https://img/u1.png"], {}, user_id="user1")
        check("user1 有规则", len(reply_image_tool.load_rules(root, "main", "user1")) == 1)
        check("user2 看不到 user1 的规则", reply_image_tool.load_rules(root, "main", "user2") == [])
        check("落盘按 user/agent 两级",
              (root / "user1" / "main.json").exists(), str(list(root.rglob("*.json"))))

        print("本地图片转图床 URL")
        workspace = Path(tempfile.mkdtemp())
        try:
            img = workspace / "uploads" / "a.png"
            img.parent.mkdir(parents=True, exist_ok=True)
            img.write_bytes(b"\x89PNG\r\n\x1a\n")
            assets = Path(tempfile.mkdtemp())
            out = reply_image_tool.handle(
                root, "main", ["本地图", "uploads/a.png"], {},
                user_id="user3", workspace_root=workspace,
                base_url="http://image-assets-service/assets", asset_dir=assets,
            )
            check("本地路径可用", "已添加规则" in out, out)
            rules = reply_image_tool.load_rules(root, "main", "user3")
            check("存成集群 URL", rules[0]["image"].startswith("http://image-assets-service/assets/reply-"), rules[0]["image"])
            check("图已拷进图床目录", len(list(assets.glob("reply-*"))) == 1, str(list(assets.iterdir())))
            out = reply_image_tool.handle(
                root, "main", ["越界", "../../etc/passwd"], {},
                user_id="user3", workspace_root=workspace,
                base_url="http://x/assets", asset_dir=assets,
            )
            check("工作区外路径被拒", out.startswith("错误："), out)
        finally:
            shutil.rmtree(workspace, ignore_errors=True)
            shutil.rmtree(assets, ignore_errors=True)

        print("工具不允许写全局（全局只由 dashboard 维护）")
        out = reply_image_tool.handle(root, "main", ["全局脸", "https://img/g.png", "global"], {}, user_id="u1")
        check("写全局被拒绝", out.startswith("错误：本工具只能维护你自己的表情绑定规则"), out)
        check("没有生成全局规则文件", not (root / "global" / "main.json").exists())

        print("全局规则仍能被读取（由管理员在 dashboard 写入）")
        reply_image_tool.save_rules(
            root, "main", [{"keywords": ["全局脸"], "image": "https://img/g.png", "note": ""}],
            reply_image_tool.GLOBAL_SCOPE_DIR,
        )
        merged = reply_image_tool.load_rules(root, "main", "u9")
        check("任意用户都能读到全局", any(r["image"] == "https://img/g.png" for r in merged), str(merged))
        check("带 scope=global 标记", merged[0].get("scope") == "global", str(merged[0]))

        print("本人同关键词优先（全局那条不再拼进来）")
        reply_image_tool.handle(root, "main", ["全局脸", "https://img/mine.png"], {}, user_id="u9")
        mine = reply_image_tool.load_rules(root, "main", "u9")
        check("只剩本人的", len(mine) == 1 and mine[0]["image"] == "https://img/mine.png", str(mine))
        other = reply_image_tool.load_rules(root, "main", "u10")
        check("没设过的用户仍看到全局", len(other) == 1 and other[0]["scope"] == "global", str(other))
        check("@source 已不再生效",
              "已存在" not in reply_image_tool.handle(root, "main", ["@source", "boss"], {}, user_id="u1")
              and not (root / "_source.json").exists(),
              "仍有 @source 行为")

    finally:
        reply_image_tool.verify_image_url = _real_verify
        shutil.rmtree(root, ignore_errors=True)

    print()
    if FAILS:
        print(f"FAILED {len(FAILS)}: {FAILS}")
        return 1
    print(f"ALL OK — {CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
