"""README 补一节：用户斜杠命令。"""

import io

PATH = "services/agent-orchestrator-service/README.md"
text = io.open(PATH, encoding="utf-8").read()

SECTION = """

## 用户斜杠命令（`/agent`、`/mode`）

小模型往往输出不了 `切换:` / `模式:` 这类协议行，用户敲斜杠命令更可靠。命令在**任务入口**
拦截：不进历史、不建实例、不调用模型，直接回执。

| 命令 | 作用 |
|---|---|
| `/agent` | 查看当前智能体与可用列表 |
| `/agent <名称>` / `/switch <名称>` | 切换智能体（**模式同时重置为 default**） |
| `/mode` | 查看当前模式与可用列表 |
| `/mode <名称>` | 切换模式（不动 agent_id → 会话上下文共用） |
| `/mode default` | 回到默认模式 |

- 全角 `／` 与中文别名（`/智能体`、`/模式`）都接受；大小写不敏感；
- 普通聊天里出现的斜杠（如“今天/mode 是啥”）不会被吞；
- 失败显式回执并列出可用项：`【切换失败】未知智能体 'x'，可用：main, talker, …`；
- 模式定义是**按智能体**的：切到别的智能体后，可用模式会变成那个智能体的模式（通常只有 `default`）。
- 命令作用于**当前会话**；群聊里即整个群会话，如需限制可加白名单（暂未实现）。

实现：`app/user_commands.py`（纯解析）+ `AgentRuntime._handle_user_command`（校验/写变量/回执）。
"""

if "## 用户斜杠命令（`/agent`、`/mode`）" in text:
    raise SystemExit("already documented")
io.open(PATH, "w", encoding="utf-8", newline="").write(text.rstrip() + SECTION)
print("README updated")
