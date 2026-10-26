"""从 LLBot 的 llbot.js 里抽出 QQ 系统表情表（QSid → 中文名），生成给桥接用的 JSON。"""

import io
import json
import re

SRC = r"D:\DuanKou\tools\LLBot8.3\bin\llbot\llbot.js"
OUT = r"E:\github\My_Agent_MSA\services\qq-llbot-service\app\face_names.json"

data = io.open(SRC, encoding="utf-8", errors="replace").read()

# 形如 {"QSid":"14","QDes":"/微笑","IQLid":"23","AQLid":"23","EMCode":"100"}
pattern = re.compile(
    r'"QSid"\s*:\s*"(\d+)"\s*,\s*"QDes"\s*:\s*"([^"]*)"',
)
pairs = pattern.findall(data)
print("raw pairs:", len(pairs))

table: dict[str, str] = {}
for sid, desc in pairs:
    name = desc.strip().lstrip("/").strip()
    if not name:
        continue
    table.setdefault(sid, name)

print("unique:", len(table))
print("264 ->", table.get("264"))
print("14  ->", table.get("14"))
print("1   ->", table.get("1"))
print("first 8:", list(table.items())[:8])

io.open(OUT, "w", encoding="utf-8", newline="").write(
    json.dumps(table, ensure_ascii=False, indent=0, sort_keys=True)
)
print("written", OUT)
