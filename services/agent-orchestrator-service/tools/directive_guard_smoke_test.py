"""伪指令清理冒烟测试（纯本地，不起服务）。

背景：`main_memory` 曾经把转交记录渲染成 `→tool: …`，main 每轮看到它，就把它当成
`对话:tool|…` 的替代写法——从那以后连续多轮"写了转交但其实没转交"（`_find_command_block`
不认箭头），而整段文字作为普通回复原样记进历史，历史又继续强化这个错误示范。

本测试覆盖三道防线：
  1. 记忆渲染不再用 `id:` 前缀（render_dispatch_memory / render_return_memory）
  2. 历史注入前剔除伪指令行（strip_misformatted_directives + shape_history_messages）
  3. 复发检测（has_misformatted_directive）
"""

import sys
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVICE_DIR))

from app.context_builder import shape_history_messages  # noqa: E402
from app.syntax_parser import (  # noqa: E402
    has_misformatted_directive,
    render_dispatch_memory,
    render_return_memory,
    strip_misformatted_directives,
)

FAILURES: list[str] = []
OK = 0


def ok(label: str) -> None:
    global OK
    OK += 1
    print(f"  OK   {label}")


def fail(label: str, detail: str = "") -> None:
    FAILURES.append(label)
    print(f"  FAIL {label}" + (f"\n       {detail}" if detail else ""))


def check(label: str, cond: bool, detail: str = "") -> None:
    ok(label) if cond else fail(label, detail)


print("── 记忆渲染：不再写成 `→id:` ──")
d = render_dispatch_memory("tool", "请先 pwd")
r = render_return_memory("tool", "执行结果")
check("转交记录带【已转交】标记", d == "【已转交 tool】请先 pwd", repr(d))
check("返回记录带【返回】标记", r == "【tool 返回】执行结果", repr(r))
check("转交记录不会被误判为伪指令", not has_misformatted_directive(d), repr(d))
check("转交记录不是任何协议指令", not d.lstrip().startswith(("对话:", "工具调用:", "切换:")), repr(d))

print("── 复发检测 has_misformatted_directive ──")
check("行首 →tool:", has_misformatted_directive("→tool: 请执行"))
check("前置空白", has_misformatted_directive("   →tool: 请执行"))
check("带说话人前缀（历史里的真实形态）", has_misformatted_directive("<main>: →tool: 请执行"))
check("行中位置（多行里的第二行）", has_misformatted_directive("先说明一句\n→alice: 你好"))
check("反方向 ←main:", has_misformatted_directive("←main: 结果"))
check("⇒ / ➔ 也算", has_misformatted_directive("⇒tool: x") and has_misformatted_directive("➔tool: x"))
check("全角冒号", has_misformatted_directive("→tool：x"))
check("行中箭头不算（自然文本）", not has_misformatted_directive("A → B: 表示映射"))
check("正文里的箭头不算", not has_misformatted_directive("然后 → 他走了"))
check("正常指令不算", not has_misformatted_directive("对话:tool|请执行"))
check("空文本", not has_misformatted_directive(""))

print("── 清洗 strip_misformatted_directives ──")
check("整行只有伪指令 → 空串", strip_misformatted_directives("→tool: 请执行") == "",
      repr(strip_misformatted_directives("→tool: 请执行")))
check("带说话人前缀的伪指令也清空",
      strip_misformatted_directives("<main>: →tool: 请执行") == "",
      repr(strip_misformatted_directives("<main>: →tool: 请执行")))
check("前缀行的说明文本保留、伪指令行删掉",
      strip_misformatted_directives("<main>: 收到，我去办\n→tool: 请执行") == "<main>: 收到，我去办",
      repr(strip_misformatted_directives("<main>: 收到，我去办\n→tool: 请执行")))
check("保留说明文本、删掉伪指令行",
      strip_misformatted_directives("收到，我去办\n→tool: 请执行") == "收到，我去办",
      repr(strip_misformatted_directives("收到，我去办\n→tool: 请执行")))
check("多条伪指令一次清掉",
      strip_misformatted_directives("→tool: a\n→alice: b") == "",
      repr(strip_misformatted_directives("→tool: a\n→alice: b")))
check("无伪指令时**原样返回**（不 trim、不改空白）",
      strip_misformatted_directives("  原文 \n\n") == "  原文 \n\n",
      repr(strip_misformatted_directives("  原文 \n\n")))
check("正常指令不被误删",
      strip_misformatted_directives("对话:tool|请执行") == "对话:tool|请执行")
check("自然文本里的箭头保留",
      strip_misformatted_directives("A → B: 表示映射") == "A → B: 表示映射")

print("── 注入期：shape_history_messages 清洗 assistant 侧 ──")
history = [
    {"role": "assistant", "content": "<main>: 收到，我去办\n→tool: 请执行"},
    {"role": "assistant", "content": "<main>: →tool: 只有伪指令"},
    {"role": "assistant", "content": "<main>: 正常的一句话"},
    {"role": "user", "content": "→tool: 用户手打的箭头，保留"},
]
shaped = shape_history_messages(history, self_id="main")
contents = [m["content"] for m in shaped]
check("伪指令行被删除，说明文本保留", contents[0] == "<main>: 收到，我去办", repr(contents[0]))
check("整条只有伪指令 → 该消息被丢弃", "<main>: →tool: 只有伪指令" not in contents, repr(contents))
check("正常消息不受影响", "<main>: 正常的一句话" in contents, repr(contents))
check("user 侧不清洗（那是另一回事）",
      "→tool: 用户手打的箭头，保留" in contents, repr(contents))
check("清洗后仍是合法的 role 序列",
      all(m["role"] in ("user", "assistant", "system") for m in shaped), repr(shaped))

print()
if FAILURES:
    print(f"FAILED {len(FAILURES)} / {OK + len(FAILURES)}")
    for f in FAILURES:
        print("  -", f)
    sys.exit(1)
print(f"ALL OK — {OK} checks")
