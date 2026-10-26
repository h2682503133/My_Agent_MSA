"""统一的斜杠命令解析（`/stop`、`/agent`、`/mode` 一个入口）。

为什么要收口：原先每个命令各有一条正则 + 一个 `is_xxx()` + 一个 handler，
新增命令要改三处、还容易漏（`/stop` 与 `/agent` 各匹配各的，谁先谁后靠代码顺序）。
现在统一成 `parse()` → `Command(name, arg)`，任务入口只判断一次：

    command = commands.parse(task.content)
    if command is not None:
        return _handle_command(task, command)

别名：`/stop`｜`/停止`、`/agent`｜`/switch`｜`/智能体`｜`/切换`、`/mode`｜`/模式`。
全角斜杠 `／` 一并接受（中文输入法常见）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass

STOP = "stop"
AGENT = "agent"
MODE = "mode"

_ALIASES = {
    "stop": STOP,
    "停止": STOP,
    "agent": AGENT,
    "switch": AGENT,
    "智能体": AGENT,
    "切换": AGENT,
    "mode": MODE,
    "模式": MODE,
}

# 命令行：斜杠（半角/全角）+ 命令名 + 其余参数
_COMMAND_RE = re.compile(
    r"^[/／]\s*([A-Za-z_\u4e00-\u9fff][\w\u4e00-\u9fff-]*)\s*(.*)$",
    re.DOTALL,
)


@dataclass(frozen=True)
class Command:
    """一条已识别的命令。"""

    name: str
    arg: str = ""

    @property
    def is_stop(self) -> bool:
        return self.name == STOP

    def __str__(self) -> str:  # 日志友好
        return f"/{self.name} {self.arg}".strip()


def parse(content) -> Command | None:
    """解析用户输入；不是已注册的命令 → None（普通聊天一个都不误吞）。"""
    text = str(content or "").strip()
    match = _COMMAND_RE.match(text)
    if not match:
        return None
    name = _ALIASES.get((match.group(1) or "").strip().lower())
    if not name:
        return None
    arg = (match.group(2) or "").strip()
    # /stop 只做精确匹配，不接受参数（避免 "/stop 顺便聊聊" 被当成打断）
    if name == STOP and arg:
        return None
    return Command(name=name, arg=arg)


def is_command(content) -> bool:
    """内容是否是已注册的斜杠命令。"""
    return parse(content) is not None
