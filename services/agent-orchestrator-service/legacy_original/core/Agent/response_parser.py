import csv
import json
import time
from core.logger import debug_log,chat_log
def full_to_half(text: str) -> str:
    """
    全角转半角（万能版）
    字母、数字、空格、所有标点符号一次性转完
    """
    result = []
    for char in text:
        code = ord(char)
        if code == 0x3000:  # 全角空格
            result.append(chr(0x0020))
        elif 0xFF01 <= code <= 0xFF5E:  # 全角字符范围
            result.append(chr(code - 0xFEE0))
        else:
            result.append(char)
    return ''.join(result)
