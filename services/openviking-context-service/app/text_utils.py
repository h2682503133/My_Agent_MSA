import re

def clean_text(text: str) -> str:
    if not text:
        return ""
    text = re.sub(r"</?think>", "", text)
    return text.strip()
