"""DSML 原生调用标记归一化测试（纯本地，不需要 gRPC / 网络）。

真实样本来自线上日志（注意是**双**竖线，且经常与一行正常的 `工具调用:` 同时出现）：

    <||DSML|| calls>
    <||DSML|| invoke name="shell">
    <||DSML|| parameter name="command" string="true">ls -la</||DSML|| parameter>
    </||DSML|| invoke>
    </||DSML|| calls>

要求：
  - 正文已有 `工具调用:` 行 → 只删标记（不能重复调用）；
  - 正文只有标记 → 转成 `工具调用:shell|…`（让调用真的发生）；
  - 孤立闭合标签 / 坏块 → 删掉，不留残渣（残渣会被多行参数吞进 shell 命令）。
"""

import sys
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVICE_DIR))

from app.dsml_text import adapt_text, has_dsml  # noqa: E402

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


BLOCK = (
    '<||DSML|| calls>\n'
    '<||DSML|| invoke name="shell">\n'
    '<||DSML|| parameter name="command" string="true">cd /app && ls -la</||DSML|| parameter>\n'
    '</||DSML|| invoke>\n'
    '</||DSML|| calls>'
)

print("── 识别 ──")
check("双竖线", has_dsml(BLOCK))
check("单竖线", has_dsml('<|DSML|invoke name="shell">x</|DSML|invoke>'))
check("孤立闭合标签也算", has_dsml("命令跑完了</||DSML|| parameter>"))
check("普通文本不算", not has_dsml("工具调用:shell|ls -la"))
check("空串", not has_dsml(""))

print("── 全角竖线（线上真实输出用的就是这个） ──")
FULLWIDTH = BLOCK.replace("||", "｜｜")
check("全角双竖线可识别", has_dsml(FULLWIDTH))
check("全角：只有标记 → 转协议行",
      adapt_text(FULLWIDTH) == "工具调用:shell|cd /app && ls -la", repr(adapt_text(FULLWIDTH)))
check("全角：协议行 + 标记 → 只删标记",
      adapt_text("工具调用:shell|ls -la\n" + FULLWIDTH) == "工具调用:shell|ls -la",
      repr(adapt_text("工具调用:shell|ls -la\n" + FULLWIDTH)))

print("── 只有标记 → 转成协议行（让调用真的发生） ──")
out = adapt_text(BLOCK)
check("产出 工具调用:shell|…", out == "工具调用:shell|cd /app && ls -la", repr(out))

out2 = adapt_text("我先看看目录。\n" + BLOCK)
check("保留说明文本 + 追加协议行",
      out2 == "我先看看目录。\n工具调用:shell|cd /app && ls -la", repr(out2))

multi = (
    '<|DSML|calls>\n<|DSML|invoke name="file-write">\n'
    '<|DSML|parameter name="path" string="true">a.txt</|DSML|parameter>\n'
    '<|DSML|parameter name="content" string="true">你好</|DSML|parameter>\n'
    '</|DSML|invoke>\n</|DSML|calls>'
)
check("多参数按声明顺序拼", adapt_text(multi) == "工具调用:file-write|a.txt|你好", repr(adapt_text(multi)))

print("── 正文已有协议行 → 只删标记（不重复调用） ──")
mixed = "工具调用:shell|cd /app && ls -la\n" + BLOCK
check("标记被删、协议行保留", adapt_text(mixed) == "工具调用:shell|cd /app && ls -la", repr(adapt_text(mixed)))

print("── 残留标记 / 坏块 ──")
tail_junk = "工具调用:shell|ls -la</||DSML|| parameter>\n</||DSML|| invoke>\n</||DSML|| calls>"
check("行尾孤立闭合标签被清（否则会被多行参数吞进命令）",
      adapt_text(tail_junk) == "工具调用:shell|ls -la", repr(adapt_text(tail_junk)))

broken = "<||DSML|| calls>没有 invoke 的垃圾</||DSML|| calls>"
check("坏块整块删除、不留残渣", adapt_text("前后文\n" + broken) == "前后文",
      repr(adapt_text("前后文\n" + broken)))

check("无标记时严格恒等（不 trim、不改空白）", adapt_text("  原文  \n\n") == "  原文  \n\n")

print("── 幂等 ──")
once = adapt_text("说明\n" + BLOCK)
check("再跑一次不再变化", adapt_text(once) == once, repr(adapt_text(once)))

print("── 安装器：包装 chat_completion 出口（不改 provider_client） ──")
from app import dsml_adapter  # noqa: E402
from app.provider_client import ProviderClient  # noqa: E402


class FakeClient(ProviderClient):
    """只替换"真正发给上游"的那一步，验证出口适配。"""

    def _call_openai_compatible(self, profile, messages, params):
        return {"ok": True, "text": BLOCK, "reasoning": "", "error": ""}

    def _call_ollama(self, profile, messages, params):
        return {"ok": True, "text": BLOCK, "reasoning": "", "error": ""}


out = FakeClient().chat_completion({"provider": "openai_compatible"}, [], {})
check("openai_compatible 出口已归一化", out["text"] == "工具调用:shell|cd /app && ls -la", repr(out["text"]))

out_ollama = FakeClient().chat_completion({"provider": "ollama"}, [], {})
check("ollama 出口也归一化", out_ollama["text"] == "工具调用:shell|cd /app && ls -la", repr(out_ollama["text"]))


class PlainClient(ProviderClient):
    def _call_openai_compatible(self, profile, messages, params):
        return {"ok": True, "text": "工具调用:shell|pwd", "reasoning": "", "error": ""}


check("无标记时原样透传", PlainClient().chat_completion({"provider": "openai_compatible"}, [], {})["text"] == "工具调用:shell|pwd")
check("install() 幂等（重复调用不叠加）",
      dsml_adapter.install() is None and getattr(ProviderClient.chat_completion, "_dsml_adapted", False) is True)

print()
if FAILURES:
    print(f"FAILED {len(FAILURES)} / {OK + len(FAILURES)}")
    for f in FAILURES:
        print("  -", f)
    sys.exit(1)
print(f"ALL OK — {OK} checks")
