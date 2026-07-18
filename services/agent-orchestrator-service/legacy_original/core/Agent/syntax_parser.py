from core.logger import chat_log
from datetime import datetime
import re

def clean_ai_thinking(text: str) -> str:
    """彻底清洗 AI 思考内容，防止语法解析误触发"""
    if not text or not isinstance(text, str):
        return ""
    if "</think>" in text:
        text = text.split("</think>")[-1]
    return text.strip()

def to_timestamp(time_str: str) -> float:
    """时间字符串 2025-12-31 23:59:59 转时间戳"""
    dt = datetime.strptime(time_str, "%Y-%m-%d %H:%M:%S")
    return dt.timestamp()
