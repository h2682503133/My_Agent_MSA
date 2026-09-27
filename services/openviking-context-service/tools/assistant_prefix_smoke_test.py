"""assistant 正文前缀归属：单元级 smoke test（不依赖 gRPC / OpenViking）。

覆盖两种模式：
  - **默认**（无 metadata 开关）＝改造前的旧行为，必须逐条不变（防回归）
  - **authoritative_prefix=True**＝「以文本内容为准」，供多 agent 扇出写入使用

另含结构不变量（开关从 proto → gRPC handler → 门面 → 后端确实接通）与跨服务口径比对
（必须与 agent-orchestrator 的 context_builder.history_speaker 一致）。

使用：
  python tools/assistant_prefix_smoke_test.py
"""

import re
import sys
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parents[1]
if str(SERVICE_DIR) not in sys.path:
    sys.path.insert(0, str(SERVICE_DIR))

from app.text_utils import AUTHOR_PREFIX_RE, format_assistant_content

ok = 0
failed: list[str] = []


def check(label: str, actual, expected) -> None:
    global ok
    if actual == expected:
        ok += 1
        print(f"  OK   {label}")
    else:
        failed.append(label)
        print(f"  FAIL {label}\n       actual  : {actual!r}\n       expected: {expected!r}")


def run_cases(title: str, cases, *, authoritative: bool) -> None:
    print(title)
    for label, raw, agent_id, expected in cases:
        check(
            label,
            format_assistant_content(raw, agent_id, authoritative_prefix=authoritative),
            expected,
        )


run_cases(
    "── 默认模式：必须等于改造前的旧行为（防回归）──",
    [
        ("无前缀 → 补 agent_id", "你好", "alice", "<alice>: 你好"),
        ("自己的 `<id>:` → 剥掉再补", "<alice>: 你好", "alice", "<alice>: 你好"),
        ("自己的全角前缀 → 归一半角", "<alice>：你好", "alice", "<alice>: 你好"),
        ("自己的裸前缀（无尖括号）→ 仍能剥", "alice: 你好", "alice", "<alice>: 你好"),
        ("自己的裸全角前缀 → 仍能剥", "alice：你好", "alice", "<alice>: 你好"),
        ("剥掉后做 strip（空白归一）", "<alice>:  hi", "alice", "<alice>: hi"),
        ("冒充他人：外层真值保留", "<bob>: 你好", "alice", "<alice>: <bob>: 你好"),
        ("他人的裸前缀不剥，只加外层", "bob: 你好", "alice", "<alice>: bob: 你好"),
        ("空正文 → 只补前缀", "", "alice", "<alice>: "),
        ("agent_id 为空 → 兜底 main", "你好", "", "<main>: 你好"),
    ],
    authoritative=False,
)

run_cases(
    "── authoritative 模式：以文本内容为准（扇出用）──",
    [
        ("无前缀 → 补 agent_id", "你好", "alice", "<alice>: 你好"),
        ("已有自己的前缀 → 原样，不叠加", "<alice>: 你好", "alice", "<alice>: 你好"),
        ("已有他人前缀（收件人 alice / 发言者 bob）→ 原样，不叠加",
         "<bob>: 你好", "alice", "<bob>: 你好"),
        ("全角冒号 → 原样", "<bob>：你好", "alice", "<bob>：你好"),
        ("首部带空白 → 原样（保留原文）", "  <bob>: 你好", "alice", "  <bob>: 你好"),
        ("冒号后无空格 → 原样", "<bob>:你好", "alice", "<bob>:你好"),
        ("只有前缀没有正文 → 原样", "<bob>:", "alice", "<bob>:"),
        ("裸 `名字:` 不算权威前缀 → 仍补外层", "alice: 你好", "alice", "<alice>: alice: 你好"),
        ("有名字但无冒号 → 不算前缀", "<bob> 你好", "alice", "<alice>: <bob> 你好"),
        ("名字 64 字 → 算前缀（边界内）", "<" + "x" * 64 + ">: 你好", "alice", "<" + "x" * 64 + ">: 你好"),
        ("名字 65 字 → 不算前缀（超上限）", "<" + "x" * 65 + ">: 你好", "alice", "<alice>: <" + "x" * 65 + ">: 你好"),
        ("名字里含 `>` → 不算前缀", "<a>b>: 你好", "alice", "<alice>: <a>b>: 你好"),
        ("名字里含换行 → 不算前缀", "<bob\nx>: 你好", "alice", "<alice>: <bob\nx>: 你好"),
        ("空正文 → 只补前缀", "", "alice", "<alice>: "),
        ("agent_id 为空 → 兜底 main", "你好", "", "<main>: 你好"),
    ],
    authoritative=True,
)


print("── 结构不变量：开关从 proto 接到存储层 ──")
store_src = (SERVICE_DIR / "app" / "viking_store.py").read_text(encoding="utf-8")
server_src = (SERVICE_DIR / "app" / "server.py").read_text(encoding="utf-8")
client_path = (
    Path(__file__).resolve().parents[2]
    / "agent-orchestrator-service"
    / "app"
    / "context_client.py"
)

check("存储层定义 metadata 键常量",
      'AUTHOR_IN_CONTENT_KEY = "author_in_content"' in store_src, True)
check("后端按开关决定是否补前缀", "authoritative_prefix=_truthy(" in store_src, True)
check("门面与后端两个 append_turn 都收 metadata",
      store_src.count("metadata: dict[str, str] | None = None,"), 2)
check("门面把 metadata 透传给后端", "metadata=metadata," in store_src, True)
check("gRPC handler 把 request.metadata 传下去",
      "metadata=dict(request.metadata)" in server_src, True)
check("role_id 仍记真实产出者 agent_id",
      'role_id=agent_id or "main"' in store_src, True)
if client_path.exists():
    check("orchestrator 客户端把 metadata 透传进 AppendTurnRequest",
          "metadata=dict(metadata or {})" in client_path.read_text(encoding="utf-8"), True)
else:
    print(f"  SKIP 找不到 {client_path}（只跳过这条）")


print("── 跨服务口径比对：与 context_builder.history_speaker ──")
peer_path = (
    Path(__file__).resolve().parents[2]
    / "agent-orchestrator-service"
    / "app"
    / "context_builder.py"
)
if not peer_path.exists():
    print(f"  SKIP 找不到 {peer_path}（只跳过这条比对）")
else:
    matched = re.search(
        r'_HISTORY_SPEAKER_RE = re\.compile\(r"([^"]+)"\)',
        peer_path.read_text(encoding="utf-8"),
    )
    if not matched:
        failed.append("无法从 context_builder.py 提取 _HISTORY_SPEAKER_RE")
        print("  FAIL 无法从 context_builder.py 提取 _HISTORY_SPEAKER_RE")
    else:
        peer_re = re.compile(matched.group(1))
        samples = [
            "<alice>: 你好", "<alice>：你好", "  <alice>: 你好", "<alice>:你好",
            "<alice> 你好", "alice: 你好", "<a>b>: 你好", ": 你好", "<alice>:",
            "<" + "x" * 64 + ">: 你好", "<" + "x" * 65 + ">: 你好",
            "<alice\nb>: 你好", "", "你好",
        ]
        mismatch = [
            s for s in samples
            if bool(AUTHOR_PREFIX_RE.match(s)) != bool(peer_re.match(s))
        ]
        check("两边对同一批样本的判定完全一致", mismatch, [])

print()
if failed:
    print(f"FAILED {len(failed)} / {ok + len(failed)}")
    for name in failed:
        print(f"  - {name}")
    sys.exit(1)
print(f"ALL OK — {ok} checks")
