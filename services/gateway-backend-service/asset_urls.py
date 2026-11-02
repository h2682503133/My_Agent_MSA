"""把"集群内图片绝对地址"改写成网页同源相对路径。

背景：工具/编排层产出的图片地址是 `http://image-assets-service/assets/x.png`
（给 QQ 适配器用，集群内可解析）。但网页浏览器在集群外，解析不了这个域名，
图片就变成碎图。gateway 本身有 `/api/assets/<file>`（读同一个 assets 卷），
所以在**推给网页 SSE 之前**把绝对地址改写成 `/api/assets/<file>` 即可：
网页同源、能直接显示；QQ 那条链路拿到的仍是原来的绝对地址，不受影响。
"""

from __future__ import annotations

import re

# 匹配 http(s)://任意主机/assets/（含 image-assets-service、NodePort、localhost 等）
_ASSET_PATTERN = re.compile(r"https?://[^/\s\"'<>]+/assets/")
_RELATIVE_PREFIX = "/api/assets/"


def rewrite_asset_urls(text: str) -> str:
    """把文本里的图片绝对地址换成 /api/assets/…；没有就原样返回。"""
    if not text:
        return text
    return _ASSET_PATTERN.sub(_RELATIVE_PREFIX, text)


def rewrite_images(images: list[str] | None) -> list[str]:
    """逐个改写图片地址列表。"""
    if not images:
        return []
    result = []
    for item in images:
        if not isinstance(item, str):
            result.append(item)
            continue
        result.append(rewrite_asset_urls(item))
    return result
