"""fetch 工具：抓取、清洗、章节切块、大纲、reader 提取、语义召回。

统一协议：fetch|url|method|data
- GET:  data = 搜索词（多词空格分隔）或章节序号/章节名；空 → 大纲/全文
- POST: data = 请求体（JSON/原始数据），返回即结果不提取

行为矩阵（按清洗后文本长度 L，HTML）：
- L <= FETCH_MAX_RAW_CHARS          : 直接返回原始文本（不加工）
- L <= FETCH_MAX_FULL_TEXT_CHARS    : data 有 → reader 提取「从中提取出与下列相关的原文内容：{data}」
                                        data 空 → 完整清洗文本
- L >  上限 且 data 空              : 页面大纲（带序号章节）+ 下一步提示
- L >  上限 且 data=序号/标题词     : 返回该章节整块内容
- L >  上限 且 data=具体问题        : 语义召回（向量 → LLM 分块提取 → 关键词 降级链）

搜索词分隔：空格为主（搜索引擎习惯），兼容 `；` / `;` / `、` 防呆（不告知模型）。
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from app import config
from app.logger import log

VALID_HTTP_METHODS = {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"}

# 浏览器 UA：多数站点（Wikipedia/THBWiki 等）会拦截 python-requests 默认 UA
DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}

# ---------------------------------------------------------------------------
# 响应类型识别与格式化（自 server.py 原样搬移）
# ---------------------------------------------------------------------------


def format_json_response(status_line: str, response) -> str:
    try:
        parsed = response.json()
        import json
        formatted = json.dumps(parsed, ensure_ascii=False, indent=2)
        return f"{status_line}\n[JSON]\n{formatted}"
    except Exception:
        return f"{status_line}\n[JSON·解析失败]\n{response.text}"


def format_image_response(status_line: str, response) -> str:
    content_type = response.headers.get("Content-Type", "image/unknown")
    content_length = response.headers.get("Content-Length", "未知")
    return f"{status_line}\n[图片] 类型: {content_type} | 大小: {content_length} bytes（图片二进制数据未在文本中返回，请使用 send-image-by-url 发送）"


def format_text_response(status_line: str, response) -> str:
    return f"{status_line}\n[文本]\n{response.text}"


def format_binary_response(status_line: str, response, content_type: str) -> str:
    content_length = response.headers.get("Content-Length", "未知")
    return f"{status_line}\n[二进制] 类型: {content_type} | 大小: {content_length} bytes（二进制数据未在文本中返回）"


# ---------------------------------------------------------------------------
# HTML 解析与清洗
# ---------------------------------------------------------------------------


def parse_html(response):
    """解析 HTML，返回 (soup, img_urls)。img_urls 去重后最多 20 张。"""
    soup = BeautifulSoup(response.text, "html.parser")
    img_urls = []
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src") or ""
        if src and not src.startswith("data:"):
            img_urls.append(urljoin(response.url, src))
    seen = set()
    unique_imgs = []
    for u in img_urls:
        if u not in seen:
            seen.add(u)
            unique_imgs.append(u)
    return soup, unique_imgs[:20]


def extract_text(soup) -> str:
    """提取正文文本（调用前需先 clean_soup 移除无用标签）。"""
    text = soup.get_text(separator="\n", strip=True)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text


def build_text_result(status_line: str, cleaned: str, img_urls: list[str]) -> str:
    result = f"{status_line}\n[HTML→文本]\n{cleaned}"
    if img_urls:
        result += "\n\n[页面图片]\n" + "\n".join(f"- {u}" for u in img_urls)
    return result


# ---------------------------------------------------------------------------
# 策略一：章节切块 + 页面大纲 + 按章节取整块内容
# ---------------------------------------------------------------------------

_HEADING_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6")


def clean_soup(soup):
    """移除无用标签（脚本/样式/导航/页脚/页头），返回清洗后的 soup。"""
    for tag in soup(["script", "style", "nav", "footer", "header"]):
        tag.decompose()
    return soup


def split_sections(soup):
    """按 h1~h6 把正文切成章节块 → [(title, text)]。

    每块 = 标题 + 标题到下一个标题之间的全部文本（段落/列表/表格等）。
    """
    if soup is None:
        return []
    sections = []
    title = None
    parts = []
    for el in soup.find_all(["h1", "h2", "h3", "h4", "h5", "h6", "p", "li", "blockquote", "pre", "table"]):
        if el.name in _HEADING_TAGS:
            t = el.get_text(" ", strip=True)
            if not t:
                continue
            if title is not None:
                sections.append((title, "\n".join(parts)))
            title = t
            parts = [t]
        elif title is not None:
            t = el.get_text(" ", strip=True)
            if t:
                parts.append(t)
    if title is not None:
        sections.append((title, "\n".join(parts)))
    return sections


def build_outline(soup, img_urls, status_line: str, url: str) -> str:
    """策略一：标题 + 带序号章节大纲（【N】标题：首句）+ 图片列表 + 下一步提示。"""
    parts = [status_line, "[页面大纲]"]

    title = ""
    if soup is not None and soup.title is not None:
        title = soup.title.get_text(strip=True)
    if title:
        parts.append(f"标题：{title}")
    if url:
        parts.append(f"URL：{url}")

    sections = split_sections(soup)
    if sections:
        parts.append("章节：")
        for i, (sec_title, sec_text) in enumerate(sections[:50]):
            snippet = (
                sec_text.replace(sec_title, "", 1).strip()[:60]
                if sec_text.startswith(sec_title)
                else sec_text[:60]
            )
            line = f"【{i + 1}】{sec_title}"
            if snippet:
                line += f"：{snippet}"
            parts.append(line)
        if len(sections) > 50:
            parts.append(f"…（共 {len(sections)} 节，仅显示前 50 节）")
    else:
        text = extract_text(soup) if soup is not None else ""
        parts.append("（页面无明显章节结构）")
        parts.append("开头：\n" + text[:500])

    if img_urls:
        parts.append("图片：")
        parts.extend(f"- {u}" for u in img_urls[:10])

    parts.append("")
    parts.append(
        "提示：需要某章节完整内容时，请调用 fetch|url|GET|章节序号（如 3）或 章节名；"
        "需要全文检索时，请调用 fetch|url|GET|要查找的内容（多关键词用空格分隔）。"
    )
    out = "\n".join(parts)
    if len(out) > config.FETCH_OUTLINE_MAX_CHARS:
        out = out[: config.FETCH_OUTLINE_MAX_CHARS] + "\n…（大纲已截断）"
    return out


def fetch_section(soup, data: str, status_line: str, url: str):
    """data 匹配章节（序号或标题词）→ 返回该章节整块内容；无匹配返回 None。"""
    sections = split_sections(soup)
    if not sections:
        return None

    data_s = (data or "").strip()
    if not data_s:
        return None

    # 1) 纯数字 → 章节序号（【N】）
    if data_s.isdigit():
        idx = int(data_s) - 1
        if 0 <= idx < len(sections):
            sec_title, sec_text = sections[idx]
            return f"{status_line}\n[章节{idx + 1}] {sec_title}\n{sec_text}"

    # 2) 标题词匹配：搜索词全部出现在标题中（优先），否则标题含任一词
    terms = [t for t in split_queries(data_s) if t]
    if not terms:
        return None
    for i, (sec_title, sec_text) in enumerate(sections):
        t = sec_title.lower()
        if all(term in t for term in terms):
            return f"{status_line}\n[章节{i + 1}] {sec_title}\n{sec_text}"
    for i, (sec_title, sec_text) in enumerate(sections):
        t = sec_title.lower()
        if any(term in t for term in terms):
            return f"{status_line}\n[章节{i + 1}] {sec_title}\n{sec_text}"
    return None


# ---------------------------------------------------------------------------
# 分块 / 向量 / 关键词工具
# ---------------------------------------------------------------------------


def split_into_chunks(text: str, size: int | None = None, overlap: int | None = None) -> list[str]:
    """按字符分块（带重叠），保证语义召回时跨块信息不丢。"""
    size = size or config.FETCH_CHUNK_SIZE
    overlap = overlap if overlap is not None else config.FETCH_CHUNK_OVERLAP
    size = max(100, int(size))
    overlap = max(0, min(int(overlap), size // 2))
    if len(text) <= size:
        return [text]
    chunks = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        chunks.append(text[start:end])
        if end >= len(text):
            break
        start = end - overlap
    return chunks


def split_queries(query: str) -> list[str]:
    """搜索词分隔：空格为主（搜索引擎习惯），兼容 `；` / `;` / `、` 防呆。"""
    query = (query or "").strip()
    if not query:
        return []
    parts = [q.strip() for q in re.split(r"[；;、\s]+", query) if q.strip()]
    return parts or [query]


# 统一路由：data（第 4 参）的值决定返回方式
#   GET:  data 空           → 大纲（带序号章节） / ≤6000 全文
#   GET:  data=章节序号/名  → 返回该章节整块内容
#   GET:  data=具体问题     → 语义召回（向量 → LLM → 关键词 降级链）
#   POST: data=请求体       → 直接返回请求结果（不提取）
# 防呆：data 显式写"大纲/结构"等 → 大纲
_OUTLINE_MARKERS = {"大纲", "outline", "结构", "structure"}


def is_outline_marker(data: str | None) -> bool:
    return (data or "").strip().lower() in _OUTLINE_MARKERS


def extract_terms(query: str) -> list[str]:
    """提取查询关键词：2 字以上中文片段 + 2 字符以上英文/数字词。"""
    terms = []
    for q in split_queries(query):
        terms.extend(re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z0-9_]{2,}", q.lower()))
    seen = set()
    out = []
    for t in terms:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    import math
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


# ---------------------------------------------------------------------------
# 策略二：内存向量语义召回（多子问题各自召回后合并去重）
# ---------------------------------------------------------------------------


def semantic_recall(text: str, query: str) -> str:
    """分块 → 向量化 → query 余弦 Top-K。失败抛异常（由调用方降级）。"""
    from app.model_proxy_client import model_proxy_client

    sub_queries = split_queries(query)
    chunks = split_into_chunks(text)
    if not chunks:
        raise RuntimeError("no chunks to embed")

    resp = model_proxy_client.embedding(
        task_id="fetch",
        agent_id="tool",
        model_profile=config.FETCH_EMBEDDING_PROFILE,
        texts=sub_queries + chunks,
    )
    if len(resp) < len(sub_queries) + 1:
        raise RuntimeError(f"embedding response too short: {len(resp)} items")

    q_vecs = [resp[i]["vector"] for i in range(len(sub_queries))]
    # 输入顺序：sub_queries 在前，chunks 在后；按返回 index 还原块下标
    chunk_vecs: dict[int, list[float]] = {}
    for i in range(len(sub_queries), len(resp)):
        chunk_idx = resp[i]["index"] - len(sub_queries)
        chunk_vecs[chunk_idx] = resp[i]["vector"]

    per_query_k = max(1, config.FETCH_TOP_K // max(1, len(sub_queries)))
    selected: dict[int, float] = {}
    for q_vec in q_vecs:
        scored = [
            (idx, _cosine(q_vec, vec))
            for idx, vec in chunk_vecs.items()
        ]
        scored.sort(key=lambda x: x[1], reverse=True)
        for idx, score in scored[:per_query_k]:
            selected[idx] = max(selected.get(idx, 0.0), score)

    ordered = sorted(selected.items(), key=lambda x: x[1], reverse=True)[: config.FETCH_TOP_K]

    parts = [f"[语义检索] 页面共 {len(chunks)} 块，召回与「{query}」最相关的 {len(ordered)} 块："]
    for idx, score in ordered:
        parts.append(f"\n【片段{idx + 1}｜相关度 {score:.3f}】\n{chunks[idx]}")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# 策略三：LLM 按 query 逐块提取（嵌入不可用时的降级）
# ---------------------------------------------------------------------------


def extract_with_reader(text: str, data: str, status_line: str, img_urls=None) -> str:
    """1500~6000：单次调用 reader，按「从中提取出与下列相关的原文内容：{data}」提取。失败不抛异常。"""
    from app.model_proxy_client import model_proxy_client

    system_prompt = (
        "你是信息提取助手。从中提取出与下列相关的原文内容。"
        "只摘录与搜索词直接相关的原文（保留数字、名称、原话），不要改写、总结或添加无关内容。"
        "如果文本与搜索词完全无关，只输出：（无）"
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"从中提取出与下列相关的原文内容：{data}\n\n网页文本：\n{text}"},
    ]
    try:
        out = model_proxy_client.chat_completion(
            task_id="fetch",
            agent_id="tool",
            model_profile=config.FETCH_EXTRACT_PROFILE,
            messages=messages,
            params={"temperature": "0", "max_tokens": "800"},
        )
        out = (out or "").strip()
        if not out or out in {"（无）", "无", "无相关内容", "无关"}:
            return f"{status_line}\n[提取] 未找到与「{data}」相关的内容"
        result = f"{status_line}\n[提取] 与「{data}」相关的原文内容：\n{out}"
        if img_urls:
            result += "\n\n[页面图片]\n" + "\n".join(f"- {u}" for u in img_urls)
        return result
    except Exception as exc:
        log(f"fetch reader extract failed ({exc}); return raw text")
        return f"{status_line}\n[提取失败] 提取失败（{exc}），返回完整文本：\n{text}"


def extract_with_llm(text: str, query: str) -> str:
    """关键词预筛后取前 N 块，并行调 reader 模型提取。失败抛异常。"""
    from app.model_proxy_client import model_proxy_client

    chunks = split_into_chunks(text)
    terms = extract_terms(query)
    if terms:
        scored = []
        for idx, chunk in enumerate(chunks):
            low = chunk.lower()
            score = sum(low.count(t) for t in terms)
            if score > 0:
                scored.append((idx, score))
        scored.sort(key=lambda x: x[1], reverse=True)
        pick = [idx for idx, _ in scored[: config.FETCH_LLM_EXTRACT_CHUNKS]]
    if not pick:
        pick = list(range(min(len(chunks), config.FETCH_LLM_EXTRACT_CHUNKS)))

    system_prompt = (
        "你是信息提取助手。从中提取出与下列相关的原文内容。"
        "只摘录与搜索词直接相关的原文（保留数字、名称、原话），不要改写、总结或添加无关内容。"
        "如果片段与搜索词完全无关，只输出：（无）"
    )

    def _extract_one(idx: int) -> tuple[int, str]:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"从中提取出与下列相关的原文内容：{query}\n\n网页片段：\n{chunks[idx]}"},
        ]
        out = model_proxy_client.chat_completion(
            task_id="fetch",
            agent_id="tool",
            model_profile=config.FETCH_EXTRACT_PROFILE,
            messages=messages,
            params={"temperature": "0", "max_tokens": "400"},
        )
        return idx, (out or "").strip()

    results: list[tuple[int, str]] = []
    with ThreadPoolExecutor(max_workers=min(4, len(pick) or 1)) as pool:
        for idx, out in pool.map(_extract_one, pick):
            if out and out not in {"（无）", "无", "无相关内容", "无关"}:
                results.append((idx, out))

    if not results:
        raise RuntimeError("LLM 提取无有效结果")

    parts = [f"[LLM提取] 按「{query}」从网页中提取的信息："]
    for idx, out in results:
        parts.append(f"\n【片段{idx + 1}】\n{out}")
    out_text = "\n".join(parts)
    return out_text[: 4 * config.FETCH_TOP_K * config.FETCH_CHUNK_SIZE]


# ---------------------------------------------------------------------------
# 关键词召回（无 LLM、无嵌入依赖的最终降级）
# ---------------------------------------------------------------------------


def keyword_recall(text: str, query: str):
    """多子问题各自关键词打分取 Top-K，合并去重。无命中返回 None。"""
    sub_queries = split_queries(query)
    chunks = split_into_chunks(text)
    selected: dict[int, float] = {}
    for sub in sub_queries:
        terms = extract_terms(sub)
        if not terms:
            continue
        scored = []
        for idx, chunk in enumerate(chunks):
            low = chunk.lower()
            score = sum(low.count(t) for t in terms)
            if score > 0:
                # 同时包含全部关键词的块强加权（AND 偏好）
                if all(t in low for t in terms):
                    score += 100
                scored.append((idx, score))
        scored.sort(key=lambda x: x[1], reverse=True)
        for idx, score in scored[: max(1, config.FETCH_TOP_K // max(1, len(sub_queries)))]:
            selected[idx] = max(selected.get(idx, 0.0), score)

    if not selected:
        return None

    ordered = sorted(selected.items(), key=lambda x: x[1], reverse=True)[: config.FETCH_TOP_K]
    parts = [f"[关键词检索] 页面共 {len(chunks)} 块，命中「{query}」的 {len(ordered)} 块："]
    for idx, score in ordered:
        parts.append(f"\n【片段{idx + 1}｜得分 {score}】\n{chunks[idx]}")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# 提取总入口（降级链）
# ---------------------------------------------------------------------------


def extract_relevant(cleaned_text: str, query: str, soup, img_urls, status_line: str, url: str) -> str:
    """策略二为主，失败逐级降级：策略三 → 关键词召回 → 大纲/截断。永不抛异常。"""
    try:
        return semantic_recall(cleaned_text, query)
    except Exception as exc:
        log(f"fetch semantic recall unavailable ({exc}); fallback to LLM extract")

    try:
        return extract_with_llm(cleaned_text, query)
    except Exception as exc2:
        log(f"fetch LLM extract unavailable ({exc2}); fallback to keyword recall")

    kw = keyword_recall(cleaned_text, query)
    if kw:
        return kw

    if soup is not None:
        return build_outline(soup, img_urls, status_line, url)
    return (
        f"{status_line}\n[文本·截断] 内容较长（{len(cleaned_text)} 字符），返回开头：\n"
        f"{cleaned_text[:2000]}\n\n（如需特定信息，请用 fetch|url|GET|要查找的内容（空格分隔）语义提取）"
    )


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def process_fetch_output(response, data: str | None = None) -> str:
    """统一入口：按 data 路由（行为矩阵见文件头）。

    data（GET 搜索词 / 章节定位）：
      - 空             → ≤6000 全文；>6000 大纲
      - "大纲"等标记   → 大纲（防呆）
      - 章节序号/标题词 → 该章节整块内容（>6000）
      - 具体问题       → ≤6000 reader 提取；>6000 语义召回（降级链）
    """
    content_type = response.headers.get("Content-Type", "").lower()
    status_line = f"[status] {response.status_code}"

    # 图片（非 SVG）始终格式化（二进制对模型无意义；SVG 是文本，按普通文本处理）
    if content_type.startswith("image/") and "svg" not in content_type:
        return format_image_response(status_line, response)

    text = response.text
    # 未超过原始阈值，直接返回原始文本（信息已完整，不加工）
    if len(text) <= config.FETCH_MAX_RAW_CHARS:
        return f"{status_line}\n{text}"

    # 超过阈值，按类型智能格式化
    if "application/json" in content_type:
        return format_json_response(status_line, response)

    if "text/html" in content_type or "application/xhtml" in content_type:
        soup, img_urls = parse_html(response)
        clean_soup(soup)
        cleaned = extract_text(soup)
        data_s = (data or "").strip()

        # 显式大纲防呆（fetch|url|GET|大纲）
        if is_outline_marker(data_s):
            return build_outline(soup, img_urls, status_line, response.url)

        if len(cleaned) <= config.FETCH_MAX_FULL_TEXT_CHARS:
            # 1500~6000：data 有 → reader 提取；data 空 → 完整文本
            if data_s:
                return extract_with_reader(cleaned, data_s, status_line, img_urls)
            return build_text_result(status_line, cleaned, img_urls)

        # >6000：data 有 → 先试章节匹配（整块），无匹配 → 语义召回
        if data_s:
            section_result = fetch_section(soup, data_s, status_line, response.url)
            if section_result:
                return section_result
            return extract_relevant(cleaned, data_s, soup, img_urls, status_line, response.url)
        # data 空 → 大纲
        return build_outline(soup, img_urls, status_line, response.url)

    if "text/" in content_type:
        cleaned = text
        data_s = (data or "").strip()
        if len(cleaned) <= config.FETCH_MAX_FULL_TEXT_CHARS:
            if data_s:
                return extract_with_reader(cleaned, data_s, status_line, None)
            return format_text_response(status_line, response)
        if data_s:
            # 纯文本无章节结构，直接走语义召回（降级链）
            return extract_relevant(cleaned, data_s, None, [], status_line, response.url)
        return (
            f"{status_line}\n[文本·截断] 内容较长（{len(cleaned)} 字符），返回开头：\n"
            f"{cleaned[:2000]}\n\n（如需特定信息，请用 fetch|url|GET|要查找的内容（空格分隔）语义提取）"
        )

    return format_binary_response(status_line, response, content_type)


def fetch(url: str, method: str = "GET", data: str = "", timeout: int = 10) -> str:
    """fetch 工具主入口：url|method|data

    - GET:  data = 搜索词（多词用空格分隔）或章节序号/章节名；空 → 大纲/全文
    - POST: data = 请求体（JSON 或原始数据），返回即结果不提取
    """
    if not url:
        return "请求失败：url 不能为空"

    request_timeout = timeout or 10

    try:
        if method == "GET":
            response = requests.get(url, timeout=request_timeout, headers=DEFAULT_HEADERS)
            return process_fetch_output(response, data=data)
        else:
            json_data = None
            raw_data = None

            if data:
                try:
                    json_data = requests.compat.json.loads(data)
                except Exception:
                    raw_data = data

            response = requests.request(
                method,
                url,
                json=json_data,
                data=raw_data,
                timeout=request_timeout,
                headers=DEFAULT_HEADERS,
            )

            # POST 等：data 是请求体，返回即结果，不做提取
            return process_fetch_output(response, data=None)
    except Exception as exc:
        return f"请求失败：{exc}"
