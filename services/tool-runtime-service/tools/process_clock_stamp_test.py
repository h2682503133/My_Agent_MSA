"""PROCESS 写入侧自检：created_at 落盘与覆写保留（tool-runtime，不依赖 gRPC）。"""

import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import process_runtime  # noqa: E402

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
    dirpath = tempfile.mkdtemp()
    try:
        print("追加时写 created_at")
        process_runtime.write(dirpath, "u", "a", "-1", "第一次", "内容一")
        path = Path(dirpath) / "u" / "a.json"
        item = json.loads(path.read_text(encoding="utf-8"))["items"][0]
        check("有 created_at", "created_at" in item, str(item))
        check("是 ISO8601 且带时区", "T" in item["created_at"] and "+" in item["created_at"], item["created_at"])

        print("覆写保留原 created_at")
        original = item["created_at"]
        process_runtime.write(dirpath, "u", "a", "1", "改过", "内容二")
        after = json.loads(path.read_text(encoding="utf-8"))["items"]
        check("仍 1 条", len(after) == 1, str(len(after)))
        check("created_at 未变", after[0]["created_at"] == original, str(after[0]))
        check("内容已更新", after[0]["content"] == "内容二")

        print("老数据覆写会补 created_at")
        old_path = Path(dirpath) / "u" / "old.json"
        old_path.write_text(json.dumps({"items": [{"title": "t", "content": "c"}]}), encoding="utf-8")
        process_runtime.write(dirpath, "u", "old", "1", "t2", "c2")
        old_item = json.loads(old_path.read_text(encoding="utf-8"))["items"][0]
        check("补上了 created_at", "created_at" in old_item, str(old_item))

        print("上限与时间戳互不影响")
        process_runtime.write(dirpath, "u", "b", "-1", "x", "y", max_items=1, max_chars=10)
        result = process_runtime.write(dirpath, "u", "b", "-1", "x2", "y2", max_items=1, max_chars=10)
        check("满额仍报错", result.startswith("错误：存储已满"), result)
    finally:
        shutil.rmtree(dirpath, ignore_errors=True)

    print()
    if FAILS:
        print(f"FAILED {len(FAILS)}: {FAILS}")
        return 1
    print(f"ALL OK — {CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
