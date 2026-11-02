"""本地记忆（①窗口滚动 / ②轮数压缩）自检。离线：压缩器用桩替换，不联网。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

SERVICE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVICE))

TMP = Path(tempfile.mkdtemp())
os.environ["PROCESS_DIR"] = str(TMP / "process")
os.environ.pop("OPENVIKING_CONTEXT_TARGET", None)

from app import config  # noqa: E402

config.PROCESS_DIR = TMP / "process"
(TMP / "process").mkdir(parents=True, exist_ok=True)

from app import local_context as lc  # noqa: E402

FAILS: list[str] = []
CHECKS = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global CHECKS
    CHECKS += 1
    if cond:
        print(f"  ok    {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL  {name} {extra}")


def main() -> int:
    real_summarize = lc._summarize
    try:
        print("① 窗口滚动（commit_limit=0）")
        for i in range(10):
            lc.append("u1", "s1", "main", f"Q{i}", f"A{i}", commit_limit=0, max_messages=6)
        msgs = lc.search("u1", "s1", "main")
        check("最多 6 条消息", len(msgs) == 6, str(len(msgs)))
        check("保留的是最近内容", msgs[-1]["content"] == "A9", msgs[-1]["content"])
        check("没有长期摘要", not any("长期记忆" in m["content"] for m in msgs))

        print("② 达到 commit_limit 才压缩")
        calls = []

        def fake_summarize(prompt: str) -> str:
            calls.append(prompt)
            return "用户喜欢简短回答；在测试本地记忆。"

        lc._summarize = fake_summarize
        # 语义：窗口 4 条不动，窗口外的历史攒够 commit_limit(3) 条才压一次
        for i in range(4):
            lc.append("u2", "s2", "main", f"Q{i}", f"A{i}", commit_limit=3, max_messages=4)
        check("还没攒够窗口外的 3 回合不压", not calls, str(len(calls)))
        lc.append("u2", "s2", "main", "Q4", "A4", commit_limit=3, max_messages=4)
        check("攒够后触发压缩", len(calls) == 1, str(len(calls)))
        check("提示词里带窗口外的回合", "Q0" in calls[0] and "A2" in calls[0], calls[0][:160])
        lc.append("u2", "s2", "main", "Q5", "A5", commit_limit=3, max_messages=4)
        lc.append("u2", "s2", "main", "Q6", "A6", commit_limit=3, max_messages=4)

        msgs2 = lc.search("u2", "s2", "main")
        check("摘要注入在最前", msgs2[0]["role"] == "system" and "【长期记忆】" in msgs2[0]["content"], str(msgs2[0]))
        check("摘要内容正确", "简短回答" in msgs2[0]["content"], msgs2[0]["content"])
        check("窗口仍是 4 条（2 回合）", len([m for m in msgs2 if m["role"] != "system"]) == 4,
          str([m["content"] for m in msgs2]))

        data = json.loads((TMP / "local_context" / "u2__s2__main.json").read_text(encoding="utf-8"))
        check("committed 有累计", data["committed"] > 0, str(data.get("committed")))
        check("已压缩的回合被移出窗口", all("Q0" != t.get("user") for t in data["turns"]), str(data["turns"]))

        print("压缩失败不丢数据")
        def boom(prompt: str) -> str:
            raise RuntimeError("reader 不可用")

        lc._summarize = boom
        before = json.loads((TMP / "local_context" / "u2__s2__main.json").read_text(encoding="utf-8"))
        lc.append("u2", "s2", "main", "Q9", "A9", commit_limit=1, max_messages=2)
        after = json.loads((TMP / "local_context" / "u2__s2__main.json").read_text(encoding="utf-8"))
        check("压缩失败保留原摘要", after["summary"] == before["summary"], str(after.get("summary"))[:40])
        check("新回合写进去了", any(t.get("user") == "Q9" for t in after["turns"]), str(after["turns"]))

        print("损坏文件不炸")
        bad = TMP / "local_context" / "u3__s3__main.json"
        bad.write_text("{not json", encoding="utf-8")
        check("读坏文件返回空", lc.search("u3", "s3", "main") == [])
        check("坏文件也能追加", lc.append("u3", "s3", "main", "Q", "A", commit_limit=0, max_messages=4) is True)
        check("追加后可读", lc.search("u3", "s3", "main")[-1]["content"] == "A")

        print("环境变量控制窗口")
        lc._summarize = fake_summarize
        os.environ["LOCAL_CONTEXT_MAX_MESSAGES"] = "2"
        for i in range(6):
            lc.append("u4", "s4", "main", f"X{i}", f"Y{i}", commit_limit=0, max_messages=0)
        check("按 env 限制条数", len(lc.search("u4", "s4", "main")) == 2, str(len(lc.search("u4", "s4", "main"))))
        os.environ.pop("LOCAL_CONTEXT_MAX_MESSAGES", None)

        print("默认参数")
        check("默认窗口条数 6", lc._max_messages() == 6, str(lc._max_messages()))
        check("默认 reader profile", lc._reader_profile() == "default-reader", lc._reader_profile())
    finally:
        lc._summarize = real_summarize

    print()
    if FAILS:
        print(f"FAILED {len(FAILS)}: {FAILS}")
        return 1
    print(f"ALL OK — {CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
