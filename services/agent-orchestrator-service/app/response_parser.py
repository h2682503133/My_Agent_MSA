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

    if input_token > 0 and output_token > 0:
        chat_log(f"{agent_id} [输入]{input_token}token [输出]{output_token}")
        debug_log(f"{agent_id} [输入]{input_token}token [输出]{output_token}")

    return ParsedModelText(
        text=full_to_half(raw_response.strip()),
        prompt_tokens=input_token,
        completion_tokens=output_token,
    )
