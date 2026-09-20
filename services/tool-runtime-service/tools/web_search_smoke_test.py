"""web-search 冒烟测试（app/web_search.py）。

覆盖三层：
  1. 纯解析函数 parse_rss / parse_html —— 不碰网络
  2. 通道选择与降级 —— 本地起假 SearXNG / 假 Bing，确定性复现真实故障形态
     （SearXNG 在「引擎池整体不可达」和「确实没有结果」时都是 HTTP 200 + 空
     results，这是本模块存在的理由，也是最需要钉死的分支）
  3. 真实网络往返 —— 需要能访问 cn.bing.com，不可用时只跳过该节

只依赖 requests / bs4，本机与容器内都能跑：
    python tools/web_search_smoke_test.py
"""

import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVICE_DIR))

from app import config  # noqa: E402
from app import web_search as ws  # noqa: E402

FAILURES = []
CHECKS = 0


def check(name, condition, extra=""):
    global CHECKS
    CHECKS += 1
    if not condition:
        FAILURES.append(name)
        print(f"  FAIL  {name} {extra}")
    else:
        print(f"  ok    {name}")


def stub_server(handler_cls, port):
    """起一个本地 stub HTTP 服务，返回 (server, url)。"""
    server = ThreadingHTTPServer(("127.0.0.1", port), handler_cls)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{port}"


def make_handler(routes):
    """routes: {路径前缀: (status, content_type, body_callable_or_str)}"""

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            for prefix, (status, ctype, body) in routes.items():
                if self.path.startswith(prefix):
                    payload = body(self.path) if callable(body) else body
                    if isinstance(payload, str):
                        payload = payload.encode("utf-8")
                    self.send_response(status)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *args):
            pass

    return _Handler


def rss_doc(items):
    parts = ['<?xml version="1.0" encoding="utf-8" ?><rss version="2.0"><channel><title>t</title>']
    for i, (title, url, desc) in enumerate(items):
        parts.append(
            f"<item><title>{title}</title><link>{url}</link><description>{desc}</description></item>"
        )
    parts.append("</channel></rss>")
    return "".join(parts)


RSS_FIXTURE = rss_doc([
    ("第一条 &amp; 实体", "https://example.com/a", "摘要 A"),
    ("第二条", "https://example.com/b", "&lt;b&gt;加粗&lt;/b&gt; 摘要 &amp; B"),
    ("重复 URL 应被去重", "https://example.com/a", "dup"),
    ("非 http 链接应被跳过", "ftp://example.com/c", "x"),
    ("第四条", "https://example.com/d", "摘要 D"),
])

HTML_FIXTURE = """<html><body><ol id="b_results">
<li class="b_algo"><h2><a href="https://example.com/1">标题一</a></h2>
<div class="b_caption"><p>正文一</p></div></li>
<li class="b_algo"><h2><a href="https://example.com/2">标题二</a></h2><p>正文二</p></li>
<li class="b_algo"><h2><a href="https://example.com/1">重复</a></h2><p>dup</p></li>
<li class="b_algo"><h2><a href="/relative">相对链接</a></h2><p>skip</p></li>
</ol></body></html>"""

# ---------------------------------------------------------------- 1. parse_rss
print("=== 1. parse_rss（纯函数）===")
rss = ws.parse_rss(RSS_FIXTURE, 10)
check("条目数（去重 + 跳过非 http 后应为 3）", len(rss) == 3, f"实际 {len(rss)}")
check("首条标题反转义", rss[0]["title"] == "第一条 & 实体", rss[0]["title"] if rss else "")
check("首条 URL", rss[0]["url"] == "https://example.com/a")
check("摘要去标签 + 反转义", rss[1]["snippet"] == "加粗 摘要 & B", rss[1]["snippet"] if len(rss) > 1 else "")
check("重复 URL 只保留一次", sum(1 for r in rss if r["url"] == "https://example.com/a") == 1)
check("非 http 链接被跳过", all(r["url"].startswith("http") for r in rss))
check("max_results 生效", len(ws.parse_rss(RSS_FIXTURE, 1)) == 1)
check("max_results=0 边界（不应返回条目）", ws.parse_rss(RSS_FIXTURE, 0) == [])
check("bytes 输入等价于 str", ws.parse_rss(RSS_FIXTURE.encode(), 10) == rss)
check("非法 XML 返回空表", ws.parse_rss("<not xml", 10) == [])
check("空 payload 返回空表", ws.parse_rss("", 10) == [])
check("合法 XML 但无 item 返回空表", ws.parse_rss("<rss><channel/></rss>", 10) == [])

# --------------------------------------------------------------- 2. parse_html
print("\n=== 2. parse_html（纯函数）===")
html = ws.parse_html(HTML_FIXTURE, 10)
check("条目数（去重 + 跳过相对链接后应为 2）", len(html) == 2, f"实际 {len(html)}")
check("标题提取", html[0]["title"] == "标题一", html[0]["title"] if html else "")
check("摘要优先取 <p>", html[1]["snippet"] == "正文二", html[1]["snippet"] if len(html) > 1 else "")
check("相对链接被跳过", all(r["url"].startswith("http") for r in html))
check("空 HTML 返回空表", ws.parse_html("", 10) == [])
check("无关 HTML 返回空表", ws.parse_html("<html><body>x</body></html>", 10) == [])
check("max_results=0 边界（不应返回条目）", ws.parse_html(HTML_FIXTURE, 0) == [])

# --------------------------------------------------------- stub 服务与保存现场
empty_searx, EMPTY_SEARX_URL = stub_server(make_handler({
    "/search": (200, "application/json",
                json.dumps({"results": [], "unresponsive_engines": [["google cse", "timeout"]]})),
}), 18101)

ok_searx, OK_SEARX_URL = stub_server(make_handler({
    "/search": (200, "application/json", json.dumps({"results": [
        {"url": f"https://searx.example/{i}", "title": f"SearXNG 结果 {i}", "content": f"snippet {i}"}
        for i in range(1, 61)
    ]})),
}), 18102)

many_cn, MANY_CN_URL = stub_server(make_handler({
    "/search": (200, "application/xml", lambda path: rss_doc(
        [(f"CN 结果 {i}", f"https://cn.example/{i}", f"desc {i}") for i in range(1, 61)]
    )),
}), 18103)

# 回落通道自身两级：RSS 给空文档（0 条），HTML 给可用结果
html_only_cn, HTML_ONLY_CN_URL = stub_server(make_handler({
    "/search": (200, "text/html", HTML_FIXTURE),
}), 18104)

dead_cn, DEAD_CN_URL = stub_server(make_handler({}), 18105)

_saved = (config.SEARXNG_URL, config.CN_SEARCH_BASE_URL, config.WEB_SEARCH_CN_FALLBACK)
try:
    # ------------------------------------------- 3. 参数处理与关键词校验
    print("\n=== 3. 参数处理与关键词校验 ===")
    check("空关键词", ws.web_search("") == "搜索失败：关键词不能为空")
    check("纯空白关键词", ws.web_search("   ") == "搜索失败：关键词不能为空")
    check("None 关键词", ws.web_search(None) == "搜索失败：关键词不能为空")

    config.SEARXNG_URL = EMPTY_SEARX_URL
    config.CN_SEARCH_BASE_URL = MANY_CN_URL
    # limit 一律用字符串：生产路径上它来自 gRPC 的 args/kwargs（repeated string /
    # map<string,string>），int 只可能由服务内直接调用产生。
    check("limit='0' 钳制到 1", ws.web_search("x", "0").count("【网址】") == 1)
    check("limit='-5' 钳制到 1", ws.web_search("x", "-5").count("【网址】") == 1)
    check("limit='9999' 钳制到 50", ws.web_search("x", "9999").count("【网址】") == 50)
    check("limit='abc' 回落默认 10", ws.web_search("x", "abc").count("【网址】") == 10)
    check("limit 缺省 = WEB_SEARCH_MAX_RESULTS",
          ws.web_search("x").count("【网址】") == config.WEB_SEARCH_MAX_RESULTS)
    # int 0 是假值，会落到默认值而非钳到 1（与字符串 "0" 不一致）。生产不可达，
    # 这里把它钉成已知行为，避免日后误以为是回归。
    check("limit=0（int，不可达输入）落到默认值",
          ws.web_search("x", 0).count("【网址】") == config.WEB_SEARCH_MAX_RESULTS)

    # ---------------------------- 4. SearXNG 返回 0 条 -> 国内回落（核心分支）
    print("\n=== 4. SearXNG 返回 0 条 -> 国内回落 ===")
    config.SEARXNG_URL = EMPTY_SEARX_URL
    config.CN_SEARCH_BASE_URL = MANY_CN_URL
    out = ws.web_search("量子计算 基本原理", 3)
    print(f"  --- 首行：{out.splitlines()[0]}")
    check("标注了国内回落", "国内回落" in out, out[:160])
    check("声明了触发原因", "SearXNG 返回 0 条" in out, out[:160])
    check("没有直接说未找到相关内容", "未找到相关内容" not in out, out[:160])
    check("拿回了结果", "[搜索完成，共 3 条结果]" in out, out[-80:])
    check("通道名为 Bing CN RSS", "Bing CN RSS" in out)
    check("条数不超过 limit", out.count("【网址】") == 3, str(out.count("【网址】")))

    # -------------------------------- 5. SearXNG 不可达 -> 同样回落，且不泄漏异常
    print("\n=== 5. SearXNG 不可达（异常路径）===")
    config.SEARXNG_URL = "http://127.0.0.1:1"
    out = ws.web_search("kubernetes", 2)
    print(f"  --- 首行：{out.splitlines()[0]}")
    check("标注了国内回落", "国内回落" in out, out[:160])
    check("原因是简短短语", "SearXNG 不可用" in out, out[:160])
    check("拿回了结果", "[搜索完成，共 2 条结果]" in out, out[-80:])
    check("未泄漏异常的 host/port", "127.0.0.1:1" not in out, out[:160])
    check("未泄漏嵌套异常链", "Max retries exceeded" not in out, out[:160])
    check("未泄漏 Connection refused", "Connection refused" not in out, out[:160])

    # ------------------------------------- 6. 回落通道自身降级：RSS 空 -> HTML
    print("\n=== 6. 回落通道两级（RSS 失败 -> HTML）===")
    config.SEARXNG_URL = EMPTY_SEARX_URL
    config.CN_SEARCH_BASE_URL = HTML_ONLY_CN_URL
    results, channel, errors = ws._search_cn("测试", 5)
    # RSS 请求拿到的是 HTML，parse_rss 抛 ParseError -> 0 条 -> 换 HTML 通道
    check("回落到 HTML 通道（说明 RSS 已试过）", channel == "Bing CN HTML", channel)
    check("HTML 通道拿到结果", len(results) == 2, str(len(results)))
    # 成功时 diagnostics 为空是设计如此：通道名已说明走了哪条，失败原因只落日志
    check("成功时不返回 diagnostics", errors == "", errors)

    # --------------------------------------- 7. 两条通道都空 -> 注明是链路故障
    print("\n=== 7. 两条通道都空 -> 注明链路故障 ===")
    config.SEARXNG_URL = EMPTY_SEARX_URL
    config.CN_SEARCH_BASE_URL = DEAD_CN_URL
    out = ws.web_search("测试", 3)
    check("说明未找到", "未找到相关内容" in out)
    check("带 [诊断] 行", "[诊断]" in out, out[:200])
    check("诊断含 SearXNG 侧原因", "SearXNG 返回 0 条" in out, out[:200])
    check("诊断含回落侧原因", "国内回落亦无结果" in out, out[:200])

    # ------------------------------------------------- 8. 回落关闭 -> 原行为
    print("\n=== 8. WEB_SEARCH_CN_FALLBACK=false ===")
    config.WEB_SEARCH_CN_FALLBACK = False
    config.SEARXNG_URL = EMPTY_SEARX_URL
    config.CN_SEARCH_BASE_URL = MANY_CN_URL
    out = ws.web_search("测试", 3)
    check("不再回落", "国内回落" not in out.replace("国内回落已关闭", ""), out[:200])
    check("标注回落已关闭", "国内回落已关闭" in out, out[:200])
    config.WEB_SEARCH_CN_FALLBACK = True

    # --------------------------------- 9. SearXNG 正常 -> 不应触发回落
    print("\n=== 9. SearXNG 有结果 -> 不触发回落 ===")
    config.SEARXNG_URL = OK_SEARX_URL
    config.CN_SEARCH_BASE_URL = MANY_CN_URL
    out = ws.web_search("任意查询", 5)
    check("未触发国内回落", "国内回落" not in out, out[:160])
    check("拿到 SearXNG 结果", "SearXNG 结果 1" in out, out[:160])
    check("条数不超过 limit", out.count("【网址】") == 5, str(out.count("【网址】")))
    check("首行无多余标注", out.splitlines()[0] == "【搜索：任意查询】", out.splitlines()[0])

    # --------------------------------------------------- 10. 真实网络往返
    print("\n=== 10. 真实网络往返（需要可访问 cn.bing.com）===")
    config.SEARXNG_URL = "http://127.0.0.1:1"  # 强制走回落
    config.CN_SEARCH_BASE_URL = "https://cn.bing.com"
    out = ws.web_search("量子计算 基本原理", 3)
    if "国内回落命中" in out or "国内回落：" in out:
        check("真实回落拿到结果", "【网址】" in out, out[:160])
        check("末尾统计行", "[搜索完成，共" in out, out[-80:])
        print(f"        （首行={out.splitlines()[0][:70]!r}）")
    else:
        print(f"  SKIP  真实网络不可用，跳过本节：{out.splitlines()[-1][:120]}")

finally:
    config.SEARXNG_URL, config.CN_SEARCH_BASE_URL, config.WEB_SEARCH_CN_FALLBACK = _saved
    for srv in (empty_searx, ok_searx, many_cn, html_only_cn, dead_cn):
        srv.shutdown()

print(f"\n{'=' * 62}")
print(f"结果：{CHECKS - len(FAILURES)}/{CHECKS} 通过")
if FAILURES:
    print("失败项：")
    for name in FAILURES:
        print(f"  - {name}")
    sys.exit(1)
print("全部通过")
