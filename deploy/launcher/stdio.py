"""把 stdout / stderr 固定成 UTF-8。

为什么需要：Windows 上 python 的输出一旦被重定向到管道或文件（面板接子进程输出、`> log.txt`、
任务计划里跑），默认就用 locale 编码（常见 cp1252 / cp936），脚本里大量中文 `print` 会直接
`UnicodeEncodeError: 'charmap' codec can't encode characters` 崩掉。
2026-11-02 实测：admin panel 点「初始化数据目录 / 探测环境 / 停止」全部因为这个报错失败，
而在控制台里手敲同样的命令是好的（控制台走 WindowsConsoleIO，不受 locale 影响）。

用法：各入口文件 import 后立刻调一次 `use_utf8()`。
"""

from __future__ import annotations

import sys


def use_utf8() -> None:
    """把标准输出/错误重配成 UTF-8（失败就算了，不能因为这个再崩一次）。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except Exception:  # noqa: BLE001 - 老解释器或非文本流
            pass


def child_env(env: dict | None = None) -> dict:
    """给子进程用的环境：带上 PYTHONUTF8/PYTHONIOENCODING，让子进程也走 UTF-8。

    子进程 stdout 被重定向到日志文件时同理，不设就会有同样的编码崩。
    """
    import os

    merged = dict(os.environ if env is None else env)
    merged.setdefault("PYTHONUTF8", "1")
    merged.setdefault("PYTHONIOENCODING", "utf-8")
    return merged
