"""web-search 工具：SearXNG 主通道 + 国内引擎回落通道。

为什么要两条通道
----------------
主通道是 Pod 内的 SearXNG sidecar，其引擎池（brave / duckduckgo / startpage /
wikipedia / google cse）全部依赖境外站点。实测宿主机出网代理抖动时整池不可达，
而 SearXNG 此时仍返回 HTTP 200 且 `results` 为空 —— 与「确实没有相关结果」在
响应上完全无法区分。若直接对外说「未找到相关内容」，模型会把基础设施故障当成
事实陈述出去。

因此命中 0 条时回落到不依赖境外站点的 Bing 中国站：RSS 接口优先（机器可读、
无需 API key），失败再试结果页 HTML。

（本文件整体对应 TOOL.json 里注册的 `web-search`，与 fetch 拆到 fetch_tools.py
同理：一个工具组一个模块。）

结构
----
- parse_rss / parse_html：纯解析函数，可脱离网络单测
- _search_searxng：主通道，异常向上抛由入口决定是否回落
- _search_cn：回落通道，内部再分 RSS / HTML 两级
- web_search：对外唯一入口，返回模型可见文本（server.py 只调用它）

注意：Bing RSS 的响应声明这些结果仅允许「出于个人的非商业用途在 RSS 聚合器中
呈现」，作为服务端搜索后端是否落在该授权范围内由部署方自行判断。
"""

from __future__ import annotations

import html as html_lib
import re
import time
from urllib.parse import quote
import xml.etree.ElementTree as ET

import requests
from bs4 import BeautifulSoup

from app import config
from app.logger import log

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

_TAG_RE = re.compile(r"<[^>]+>")


# --------------------------------------------------------------------------
# 纯解析函数
# --------------------------------------------------------------------------


def _clean_text(value: str) -> str:
    """去掉 RSS description 里偶发的内联标签并反转义实体。"""
    if not value:
        return ""
    return html_lib.unescape(_TAG_RE.sub("", value)).strip()


def parse_rss(payload: bytes | str, max_results: int) -> list[dict]:
    """解析 Bing RSS 为 [{title,url,snippet}]。

    非 XML / 结构不符时返回空列表（由调用方决定是否回落 HTML）。
    """
    # 上限判定放在最前：循环内是「先收集后判定」，max_results<=0 时会多收一条
    if max_results < 1:
        return []

    text = payload.decode("utf-8", errors="replace") if isinstance(payload, bytes) else payload
    try:
        root = ET.fromstring(text)
    except ET.ParseError:
        return []

    results: list[dict] = []
    seen: set[str] = set()
    for item in root.iter("item"):
        url = (item.findtext("link") or "").strip()
        if not url.startswith("http") or url in seen:
            continue
        seen.add(url)
        results.append(
            {
                "title": (item.findtext("title") or "").strip(),
                "url": url,
                "snippet": _clean_text(item.findtext("description") or ""),
            }
        )
        if len(results) >= max_results:
            break
    return results


def parse_html(text: str, max_results: int) -> list[dict]:
    """解析 Bing 结果页 HTML 为 [{title,url,snippet}]。"""
    if max_results < 1:
        return []

    soup = BeautifulSoup(text or "", "html.parser")
    results: list[dict] = []
    seen: set[str] = set()
    for block in soup.select("li.b_algo"):
        anchor = block.select_one("h2 a")
        if anchor is None:
            continue
        url = (anchor.get("href") or "").strip()
        if not url.startswith("http") or url in seen:
            continue
        seen.add(url)
        node = block.select_one("p") or block.select_one("div.b_caption")
        results.append(
            {
                "title": anchor.get_text(" ", strip=True),
                "url": url,
                "snippet": node.get_text(" ", strip=True) if node is not None else "",
            }
        )
        if len(results) >= max_results:
            break
    return results


# --------------------------------------------------------------------------
# 主通道：SearXNG
# --------------------------------------------------------------------------


def _search_searxng(query: str, max_results: int, timeout: int) -> tuple[list[dict], dict]:
    """SearXNG 元搜索。返回 (results, 最后一页原始响应)。

    网络异常原样上抛，由 web_search 决定是否回落到国内通道。
    """
    seen: set[str] = set()
    results: list[dict] = []

    def _do_search(pageno: int = 1) -> dict:
        response = requests.get(
            f"{config.SEARXNG_URL}/search",
            params={"q": query, "format": "json", "categories": "general", "pageno": pageno},
            timeout=timeout or 15,
        )
        response.raise_for_status()
        return response.json()

    def _collect(pageno: int = 1) -> tuple[list[dict], dict]:
        """抓取一页并按 URL 去重后追加结果。"""
        page_data = _do_search(pageno)
        page_results = []
        for item in page_data.get("results", []):
            url = item.get("url", "")
            if url and url in seen:
                continue
            if url:
                seen.add(url)
            page_results.append(item)
            if len(results) + len(page_results) >= max_results:
                break
        return page_results, page_data

    # SearXNG JSON 每页约 10 条，超过一页时按页抓取
    pages = max(1, (max_results + 9) // 10)
    data: dict = {}
    for pageno in range(1, pages + 1):
        page_results, data = _collect(pageno)
        results.extend(page_results)
        if len(results) >= max_results:
            break

    # 空结果时等待 1 秒后重试一次（搜索引擎偶发限流）
    if not results:
        time.sleep(1)
        seen = set()
        for pageno in range(1, pages + 1):
            page_results, data = _collect(pageno)
            results.extend(page_results)
            if len(results) >= max_results:
                break

    return results[:max_results], data


# --------------------------------------------------------------------------
# 回落通道：Bing 中国站
# --------------------------------------------------------------------------


def _fetch(url: str, timeout: int) -> requests.Response:
    response = requests.get(url, headers=_HEADERS, timeout=timeout)
    response.raise_for_status()
    return response


def _search_cn_rss(base: str, query: str, max_results: int, timeout: int) -> list[dict]:
    response = _fetch(f"{base}/search?q={quote(query)}&format=rss&count={max_results}", timeout)
    return parse_rss(response.content, max_results)


def _search_cn_html(base: str, query: str, max_results: int, timeout: int) -> list[dict]:
    response = _fetch(f"{base}/search?q={quote(query)}&count={max_results}", timeout)
    response.encoding = response.encoding or "utf-8"
    return parse_html(response.text, max_results)


def _search_cn(
    query: str,
    max_results: int,
    timeout: int | None = None,
) -> tuple[list[dict], str, str]:
    """国内通道检索。

    返回 (results, channel, diagnostics)：成功时 channel 为命中的通道名，
    失败时 results 为空且 diagnostics 汇总各通道失败原因。
    """
    wait = int(timeout or config.CN_SEARCH_TIMEOUT_SECONDS)
    base = (config.CN_SEARCH_BASE_URL or "").rstrip("/")
    if not base:
        return [], "", "未配置 CN_SEARCH_BASE_URL"

    errors: list[str] = []
    for label, fetcher in (("Bing CN RSS", _search_cn_rss), ("Bing CN HTML", _search_cn_html)):
        try:
            results = fetcher(base, query, max_results, wait)
        except Exception as exc:  # 单通道失败不致命，继续试下一通道
            log(f"web-search 国内通道 {label} 失败 query={query!r}: {exc}")
            errors.append(f"{label}: {type(exc).__name__} {exc}")
            continue
        if results:
            return results[:max_results], label, ""
        # 成功时 errors 不返回给调用方（通道名已说明走了哪条），所以要在日志里留痕
        log(f"web-search 国内通道 {label} 返回 0 条 query={query!r}")
        errors.append(f"{label}: 0 条")

    return [], "", "; ".join(errors)


# --------------------------------------------------------------------------
# 输出渲染与入口
# --------------------------------------------------------------------------


def _render(query: str, results: list[dict], header_note: str = "") -> str:
    """统一渲染，两条通道共用，保证模型看到的格式一致。"""
    blocks = []
    for item in results:
        title = item.get("title", "无标题")
        url = item.get("url", "无链接")
        snippet = (item.get("snippet") or item.get("content") or "")[:160]
        blocks.append(f"- {title}\n【网址】{url}\n{snippet}\n")
    count = len(blocks)
    return (
        f"【搜索：{query}】{header_note}\n"
        + "\n".join(blocks)
        + f"\n[搜索完成，共 {count} 条结果]"
    )


def _cn_fallback(query: str, max_results: int, reason: str) -> str:
    """SearXNG 无结果时的回落。

    reason 是给模型看的短语；各通道的完整失败信息只落日志，避免把 host/port
    和嵌套异常链写进模型可见输出。
    """
    if not config.WEB_SEARCH_CN_FALLBACK:
        log(f"web-search 国内回落已关闭（WEB_SEARCH_CN_FALLBACK=false）query={query!r}")
        return (
            f"【搜索：{query}】未找到相关内容\n"
            f"[搜索完成，共 0 条结果]\n[诊断] {reason}（国内回落已关闭）"
        )

    try:
        results, channel, errors = _search_cn(query, max_results)
    except Exception as exc:
        log(f"web-search 国内回落异常 query={query!r}: {exc}")
        return (
            f"【搜索：{query}】未找到相关内容\n"
            f"[搜索完成，共 0 条结果]\n[诊断] {reason}；国内回落异常：{str(exc)[:200]}"
        )

    if not results:
        # 两条通道都空：如实标注是检索链路问题，避免模型把基础设施故障当事实
        return (
            f"【搜索：{query}】未找到相关内容\n"
            f"[搜索完成，共 0 条结果]\n[诊断] {reason}；国内回落亦无结果：{errors[:300]}"
        )

    log(f"web-search 国内回落命中 channel={channel} query={query!r} 共 {len(results)} 条")
    return _render(query, results, f"（国内回落：{channel}；{reason}）")


def web_search(query: str, limit: int | str | None = None, timeout: int = 0) -> str:
    """执行网页搜索：SearXNG 主通道，0 条结果时回落国内通道。"""
    query = (query or "").strip()
    if not query:
        return "搜索失败：关键词不能为空"

    try:
        max_results = int(limit or config.WEB_SEARCH_MAX_RESULTS)
    except (TypeError, ValueError):
        max_results = config.WEB_SEARCH_MAX_RESULTS
    max_results = max(1, min(max_results, 50))

    try:
        results, data = _search_searxng(query, max_results, timeout)
    except Exception as exc:
        # 完整异常只进日志：它是模型可见输出，不该塞进 host/port 和嵌套异常链
        log(f"web-search SearXNG 异常，转国内回落 query={query!r}: {exc}")
        return _cn_fallback(query, max_results, "SearXNG 不可用")

    if results:
        engines = data.get("engines", [])
        engine_str = f"（聚合引擎：{', '.join(engines[:3])}）" if engines else ""
        return _render(query, results, engine_str)

    # SearXNG 返回 0 条：既可能是确实无结果，也可能是引擎池整体不可达
    # （两种情况 SearXNG 都是 HTTP 200 + 空 results，无法从响应区分）。
    # 此处不直接对外说「未找到相关内容」，改走国内引擎回落通道。
    log(
        f"web-search SearXNG 返回 0 条，转国内回落 query={query!r} "
        f"unresponsive={data.get('unresponsive_engines')}"
    )
    return _cn_fallback(query, max_results, "SearXNG 返回 0 条")
