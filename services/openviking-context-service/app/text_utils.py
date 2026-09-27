import re

def clean_text(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"</?think>", "", text)
    return text.strip()


# 正文首部的说话人前缀：`<名字>:` / `<名字>：`（名字 1-64 字，不含 `>` 与换行）。
#
# ⚠️ 口径必须与 agent-orchestrator-service 的 `context_builder.history_speaker()`
# （`^[ \t]*<([^>\n]{1,64})>[ \t]*[:：][ \t]*`）保持一致：
#   - 这边在「正文首部已带权威归属」模式下用它判断是否已有前缀（有就不再补）
#   - 那边用它解析"这句话是谁说的"
# 两边若漂移，会出现「存的时候跳过了自动前缀、读的时候解析不出说话人」的静默错配，
# 而不是报错。tools/assistant_prefix_smoke_test.py 里有跨服务口径比对。
AUTHOR_PREFIX_RE = re.compile(r"^[ \t]*<[^>\n]{1,64}>[ \t]*[:：][ \t]*")


def format_assistant_content(
    assistant_message: str,
    agent_id: str = "main",
    *,
    authoritative_prefix: bool = False,
) -> str:
    """给 assistant 正文定归属。两种模式：

    **默认（`authoritative_prefix=False`）＝改造前的旧行为**：先剥掉"自己"的前缀，再统一补一次。
    识别 4 种写法：`<id>:` / `id:` / `<id>：` / `id：`；剥掉后 `.strip()`，再拼 `<id>: `。
    正文里出现**别人**的前缀（模型冒充其他角色）时保留原样，且被外层 `<id>:` 包住
    —— 外层是真值，内层是模型自己的表达。

    **`authoritative_prefix=True`＝以文本内容为准**（调用方声明正文首部已带权威归属）：
    首部已有 `<名字>:` → 原样返回；否则才补 `<agent_id>: `。

    为什么需要第二种：群聊共享 session / 多 agent 扇出写入时，调用方（orchestrator）
    会先把**真实发言者**写进正文首部，而此时传给存储层的 `agent_id` 是**收件人**
    （它要拿去拼 session key）。若无脑再补一次，正文会变成 `<收件人>: <发言者>: …`，
    而读取侧（`history_speaker`）只取最外层前缀 → 每个收件人都把别人的话当成自己说的。

    注意：开启第二种时，正文首部若写着别的角色名就会**以它为准**（放弃"外层真值"那层保护），
    所以只应由确实知道发言者的调用方开启。
    """
    raw = str(assistant_message or "")
    owner = agent_id or "main"

    if authoritative_prefix:
        if AUTHOR_PREFIX_RE.match(raw):
            return raw
        return f"<{owner}>: {raw}"

    body = raw
    for prefix in (f"<{owner}>:", f"{owner}:", f"<{owner}>：", f"{owner}："):
        if raw.startswith(prefix):
            body = raw[len(prefix):].strip()
            break
    return f"<{owner}>: {body}"
