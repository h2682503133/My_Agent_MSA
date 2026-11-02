import io, json, shutil, time
p = "/app/system_prompts/orchestrator/system_prompt/TOOL.json"
shutil.copy2(p, p + ".bak-" + time.strftime("%Y%m%d%H%M"))
d = json.loads(io.open(p, encoding="utf-8-sig").read())
changed = False
for sec in d.get("sections", []):
    if sec.get("id") == "技能管理工具":
        names = [t.get("name") for t in sec.get("tools", [])]
        if "clawhub-list" not in names:
            sec.setdefault("tools", []).append({
                "name": "clawhub-list", "args": "",
                "desc": "列出本机已安装的 ClawHub 技能（读 lockfile）",
                "groups": ["技能", "L0"],
            })
            changed = True
io.open(p, "w", encoding="utf-8").write(json.dumps(d, ensure_ascii=False, indent=2) + "\n")
print("live TOOL.json 已补 clawhub-list" if changed else "已存在，未改动")
