import json, io
p = "/app/system_prompt/TOOL.json"
d = json.loads(io.open(p, encoding="utf-8-sig").read())
changed = []
for sec in d.get("sections", []):
    for t in sec.get("tools", []):
        if not isinstance(t, dict):
            continue
        name = str(t.get("name", ""))
        if name == "windows-file-copy":
            t["desc"] = ("与 windows 宿主机复制文件/目录（唯一正确的宿主文件通道）。"
                         "路径写 Windows 形式（D:\\\\x\\\\y）或 /mnt/d/x/y；两端任意一侧是宿主即可，"
                         "容器内一侧用工作区路径。注意：容器里的 /mnt 是空的，"
                         "不要用 shell 的 ls/cp 直接访问宿主目录，必须用本工具。")
            changed.append(name)
        elif name == "windows-file-move":
            t["desc"] = ("与 windows 宿主机移动或重命名文件/目录（同上：宿主侧走 SSH，"
                         "不要用 shell 直接操作 /mnt）。")
            changed.append(name)
io.open(p, "w", encoding="utf-8").write(json.dumps(d, ensure_ascii=False, indent=2) + "\n")
print("已更新:", changed)
