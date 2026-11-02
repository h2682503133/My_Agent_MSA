"""
从原 core/Agent/response_parser.py 拆出并适配。

保留：
- full_to_half 全角转半角
- 兼容 OpenAI-compatible / Ollama-like 返回结构
- token usage 提取思路

变化：
- 不再依赖 requests.Response 对象
- 不再直接写 logs/token.csv
"""

from dataclasses import dataclass
from typing import Any
from app.logger import chat_log, debug_log


@dataclass
class ParsedModelText:
    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    # 前缀缓存命中/未命中（DeepSeek/OpenAI 计费口径不同，日志要能看出来）
    cached_tokens: int = 0
    cache_miss_tokens: int = 0


def full_to_half(text: str) -> str:
    """
    全角转半角（万能版）
    字母、数字、空格、所有标点符号一次性转完
    """
    result = []
    for char in text:
        code = ord(char)
        if code == 0x3000:
            result.append(chr(0x0020))
        elif 0xFF01 <= code <= 0xFF5E:
            result.append(chr(code - 0xFEE0))
        else:
            result.append(char)
    return "".join(result)


def parse_model_response(agent_id: str, data: dict[str, Any]) -> ParsedModelText:
    raw_response = ""
    input_token = 0
    output_token = 0
    cached_token = 0
    miss_token = 0

    if "choices" in data:
        raw_response = data["choices"][0]["message"]["content"]
        usage = data.get("usage", {})
        input_token = int(usage.get("prompt_tokens", 0) or 0)
        output_token = int(usage.get("completion_tokens", 0) or 0)
    elif "message" in data:
        raw_response = data["message"].get("content", "")
        input_token = int(data.get("prompt_eval_count", 0) or 0)
        output_token = int(data.get("eval_count", 0) or 0)
    elif "text" in data:
        raw_response = data.get("text", "")
        usage = data.get("usage", {})
        input_token = int(usage.get("prompt_tokens", 0) or 0)
        output_token = int(usage.get("completion_tokens", 0) or 0)
    else:
        raw_response = str(data)

    # 缓存命中：DeepSeek 直给 hit/miss；OpenAI 的 prompt_tokens_details.cached_tokens；
    # Anthropic 的 cache_read_input_tokens。都没有 → cached=0、miss=prompt。
    _u = data.get("usage") if isinstance(data.get("usage"), dict) else {}
    if _u.get("prompt_cache_hit_tokens") is not None:
        cached_token = int(_u.get("prompt_cache_hit_tokens") or 0)
        miss_token = int(_u.get("prompt_cache_miss_tokens") or max(input_token - cached_token, 0))
    elif _u.get("cache_read_input_tokens") is not None:
        cached_token = int(_u.get("cache_read_input_tokens") or 0)
        miss_token = max(input_token - cached_token, 0)
    elif isinstance(_u.get("prompt_tokens_details"), dict):
        cached_token = int((_u.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
        miss_token = max(input_token - cached_token, 0)
    else:
        cached_token = 0
        miss_token = input_token
    cached_token = max(0, min(cached_token, input_token))

    if input_token > 0 and output_token > 0:
        detail = f"[输入]{input_token}token [输出]{output_token}"
        if cached_token:
            rate = cached_token / input_token * 100 if input_token else 0.0
            detail += f" [缓存命中]{cached_token}token({rate:.1f}%) [未命中]{miss_token}token"
        chat_log(f"{agent_id} {detail}")
        debug_log(f"{agent_id} {detail}")

    return ParsedModelText(
        text=full_to_half(raw_response.strip()),
        prompt_tokens=input_token,
        completion_tokens=output_token,
        cached_tokens=cached_token,
        cache_miss_tokens=miss_token,
    )
