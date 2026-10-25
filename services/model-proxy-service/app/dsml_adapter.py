"""DSML 适配的**安装器**（新文件，不改 `provider_client.py` 内部逻辑）。

`app/dsml_text.py` 是纯函数（识别 / 归一化），本文件负责把它挂到模型调用出口上：
导入即安装 —— 用装饰式包装替换 `ProviderClient.chat_completion`，在**返回前**对
`result["text"]` 做一次归一化（正文里的 DSML 标记：有协议行就删、没有就补）。

挂载入口是 `app/bootstrap.py`（Dockerfile 的 CMD 指向它），`provider_client.py` 一个字符
都没动 —— 这样上游/原实现的解析逻辑保持原样，适配失败也只影响这一个包装层。

幂等：重复 import 不会重复包装。
"""

from __future__ import annotations

from typing import Any

from app import dsml_text
from app.logger import debug_log
from app.provider_client import ProviderClient

_MARK = "_dsml_adapted"


def adapt_result(result: Any) -> Any:
    """统一出口适配：把正文里混入的 DSML 原生调用标记归一化掉。"""
    if not isinstance(result, dict):
        return result
    text = result.get("text")
    if isinstance(text, str) and dsml_text.has_dsml(text):
        adapted = dsml_text.adapt_text(text)
        debug_log(
            f"[dsml] 正文含 DSML 原生调用标记，已归一化"
            f"（{len(text)} -> {len(adapted)} 字符）"
        )
        result["text"] = adapted
    return result


def install() -> None:
    """把 `chat_completion` 包一层（幂等）。"""
    original = ProviderClient.chat_completion
    if getattr(original, _MARK, False):
        return

    def chat_completion(self, profile, messages, params):
        return adapt_result(original(self, profile, messages, params))

    setattr(chat_completion, _MARK, True)
    chat_completion.__doc__ = (original.__doc__ or "") + "\n（已由 app.dsml_adapter 包装：出口归一化 DSML）"
    ProviderClient.chat_completion = chat_completion


install()
