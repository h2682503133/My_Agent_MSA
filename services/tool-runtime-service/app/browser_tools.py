"""浏览器操作工具：通过 CDP 连到**宿主已开启**的 Edge/Chromium，做页面级人机协作。

设计要点：
- 不启动浏览器、不用独立 profile：直接连接宿主真实浏览器（`--remote-debugging-port=9222`），
  因此登录态、插件、渲染都是用户真实的那个浏览器；
- 每次调用「连一次、做完断开」，不驻留状态（避免长连接把浏览器句柄耗死）；
- 所有失败都显式返回原因（连不上 / 元素找不到 / 超时），不静默；
- 权限在 orchestrator 侧复用「允许访问 Windows 宿主机的 user_id」白名单（shell_allowed_users）。

动作（`工具调用:browser|动作|参数1|参数2`）：
  open|url                    打开/复用当前标签页
  read                        读页面：标题、URL、正文摘要 + 可交互元素列表（带序号）
  click|#序号 / CSS / 文本     点击
  type|#序号 或 CSS|文本        输入（密码框内容不落日志）
  key|Enter                    按键
  back                         后退
  tabs                         列出标签页
  wait|毫秒                    显式等待
  shot|可选路径                截图（默认存工作区 outputs/browser/）
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from app.logger import log

# 读取页面：正文摘要 + 可交互元素（给模型看的"页面结构"，比裸 HTML 省得多）
_READ_JS = r"""
() => {
  const MAX_TEXT = 4000;
  const MAX_ELS = 60;
  const pick = (el) => (el.innerText || el.value || el.placeholder || el.getAttribute('aria-label') || el.getAttribute('title') || '').trim();
  const nodes = [...document.querySelectorAll('a, button, input, textarea, select, [role=button], [contenteditable=true]')];
  const els = nodes.slice(0, MAX_ELS).map((el, i) => ({
    i: i + 1,
    tag: el.tagName.toLowerCase(),
    type: (el.getAttribute('type') || ''),
    text: pick(el).slice(0, 80),
    disabled: !!(el.disabled || el.getAttribute('aria-disabled') === 'true'),
    href: el.getAttribute('href') || '',
  }));
  return {
    title: document.title,
    url: location.href,
    ready: document.readyState,
    text: (document.body ? document.body.innerText : '').slice(0, MAX_TEXT),
    elementCount: nodes.length,
    elements: els,
  };
}
"""

_CLICK_JS = r"""
(target) => {
  const pick = (el) => (el.innerText || el.value || el.placeholder || el.getAttribute('aria-label') || el.getAttribute('title') || '').trim();
  const nodes = [...document.querySelectorAll('a, button, input, textarea, select, [role=button], [contenteditable=true]')];
  let el = null;
  if (target.index) {
    el = nodes[target.index - 1] || null;
  } else if (target.selector) {
    el = document.querySelector(target.selector);
  } else if (target.text) {
    const needle = target.text.toLowerCase();
    el = nodes.find(n => pick(n).toLowerCase().includes(needle)) || null;
  }
  if (!el) return {ok: false, reason: '没找到目标元素'};
  el.scrollIntoView({block: 'center'});
  el.click();
  return {ok: true, text: pick(el).slice(0, 80), tag: el.tagName.toLowerCase()};
}
"""

_TYPE_JS = r"""
(payload) => {
  const pick = (el) => (el.innerText || el.value || el.placeholder || el.getAttribute('aria-label') || '').trim();
  const nodes = [...document.querySelectorAll('input, textarea, [contenteditable=true]')];
  let el = null;
  if (payload.index) el = nodes[payload.index - 1] || null;
  else if (payload.selector) el = document.querySelector(payload.selector);
  if (!el) return {ok: false, reason: '没找到输入框'};
  const isPassword = (el.getAttribute('type') || '').toLowerCase() === 'password';
  el.focus();
  if (el.isContentEditable) {
    el.textContent = payload.text;
  } else {
    el.value = payload.text;
  }
  el.dispatchEvent(new Event('input', {bubbles: true}));
  el.dispatchEvent(new Event('change', {bubbles: true}));
  return {ok: true, tag: el.tagName.toLowerCase(), isPassword, wasEmpty: !pick(el)};
}
"""


class BrowserError(RuntimeError):
    """带用户可读原因的浏览器错误。"""


def _target_of(raw: str) -> dict:
    """把 `#3` / `CSS 选择器` / `可见文本` 解析成 JS 需要的目标描述。"""
    text = str(raw or "").strip()
    if not text:
        return {}
    if text.startswith("#") and text[1:].isdigit():
        return {"index": int(text[1:])}
    if text.startswith(("css:", "selector:")):
        return {"selector": text.split(":", 1)[1].strip()}
    # 含 CSS 特征（. # > [ ）且不像自然语言 → 当选择器
    if any(ch in text for ch in (">", "[", "=")) or (text.startswith(".") and " " not in text):
        return {"selector": text}
    return {"text": text}


class BrowserTools:
    """页面上的一组同步动作；每次调用独立连接 CDP。"""

    def __init__(self, cdp_url: str | None = None, timeout_ms: int | None = None):
        self.cdp_url = (cdp_url or os.getenv("BROWSER_CDP_URL", "") or "http://host.docker.internal:9222").rstrip("/")
        self.timeout_ms = int(timeout_ms or int(os.getenv("BROWSER_TIMEOUT_MS", "20000")))

    # ---------- 连接 ----------
    @staticmethod
    def _default_gateway() -> str:
        """容器默认网关（Docker Desktop 下就是宿主可达的那一跳）。读不到返回空串。"""
        try:
            with open("/proc/net/route", "r", encoding="utf-8") as handle:
                for line in handle.readlines()[1:]:
                    fields = line.split()
                    if len(fields) > 2 and fields[1] == "00000000":
                        hexip = fields[2]
                        return ".".join(str(int(hexip[i:i + 2], 16)) for i in (6, 4, 2, 0))
        except Exception:  # noqa: BLE001
            return ""
        return ""

    def _candidates(self) -> list[str]:
        """候选 CDP 端点：显式配置优先，其次 host.docker.internal / 默认网关。"""
        items = [self.cdp_url]
        for extra in ("http://host.docker.internal:9222",):
            if extra not in items:
                items.append(extra)
        gateway = self._default_gateway()
        if gateway:
            candidate = f"http://{gateway}:9222"
            if candidate not in items:
                items.append(candidate)

        # Docker Desktop 的 kind 集群：pod 到宿主要走节点所在网桥的 .1
        # （default gateway 是 CNI 网关，到不了宿主；节点 IP 由 downward API 注入）
        node_ip = os.getenv("NODE_IP", "").strip()
        if node_ip.count(".") == 3:
            prefix = node_ip.rsplit(".", 1)[0]
            candidate = f"http://{prefix}.1:9222"
            if candidate not in items:
                items.append(candidate)
        return items

    @staticmethod
    def _probe(url: str, timeout: float = 1.5) -> bool:
        import urllib.request

        try:
            with urllib.request.urlopen(f"{url}/json/version", timeout=timeout) as resp:
                return resp.status == 200
        except Exception:  # noqa: BLE001
            return False

    def _connect(self):
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:  # noqa: BLE001
            raise BrowserError(f"playwright 不可用：{exc}")

        # 先挑一个真的能应答的端点：容器到宿主的路由在不同环境里不一样
        endpoint = next((item for item in self._candidates() if self._probe(item)), "")
        if not endpoint:
            tried = "、".join(self._candidates())
            raise BrowserError(
                "找不到可用的浏览器调试端口（已尝试 "
                f"{tried}）。请以 `--remote-debugging-port=9222 --remote-debugging-address=0.0.0.0`"
                " 启动 Edge，并确认该端口对容器可达。"
            )

        playwright = sync_playwright().start()
        try:
            browser = playwright.chromium.connect_over_cdp(endpoint, timeout=self.timeout_ms)
        except Exception as exc:  # noqa: BLE001
            playwright.stop()
            raise BrowserError(
                f"连上了 {endpoint} 但 CDP 握手失败：{str(exc).splitlines()[0][:160]}"
            )
        return playwright, browser

    @staticmethod
    def _page(browser):
        contexts = browser.contexts
        if not contexts:
            raise BrowserError("浏览器没有可用上下文（请先手动打开一个页面）")
        pages = contexts[0].pages
        if not pages:
            raise BrowserError("浏览器没有打开的标签页（请先手动打开一个页面）")
        return pages[-1]

    @staticmethod
    def _eval(page, expression_js: str, arg=None):
        return page.evaluate(expression_js, arg) if arg is not None else page.evaluate(expression_js)

    # ---------- 动作 ----------
    def run(self, action: str, arg1: str = "", arg2: str = "", root: Path | None = None) -> str:
        action = str(action or "").strip().lower()
        handlers = {
            "open": self._open,
            "read": self._read,
            "click": self._click,
            "type": self._type,
            "key": self._key,
            "back": self._back,
            "tabs": self._tabs,
            "wait": self._wait,
            "shot": self._shot,
        }
        handler = handlers.get(action)
        if handler is None:
            return (
                f"错误：未知的浏览器动作 {action!r}。可用：{', '.join(handlers)}\n"
                "用法 工具调用:browser|动作|参数1|参数2"
            )
        playwright = None
        try:
            playwright, browser = self._connect()
            return handler(browser, arg1, arg2, root)
        except BrowserError as exc:
            return f"错误：{exc}"
        except Exception as exc:  # noqa: BLE001
            return f"错误：浏览器操作失败（{type(exc).__name__}）：{exc}"
        finally:
            if playwright is not None:
                try:
                    playwright.stop()
                except Exception:  # noqa: BLE001
                    pass

    def _open(self, browser, url: str, _arg2: str, root) -> str:
        url = str(url or "").strip()
        if not url:
            return "错误：open 需要一个 URL"
        if not url.startswith(("http://", "https://")):
            url = "https://" + url
        page = self._page(browser)
        page.goto(url, timeout=self.timeout_ms, wait_until="domcontentloaded")
        return f"已打开 {page.url}（标题：{page.title()}）"

    def _read(self, browser, _arg1: str, _arg2: str, root) -> str:
        page = self._page(browser)
        info = self._eval(page, _READ_JS) or {}
        lines = [
            f"URL: {info.get('url', page.url)}",
            f"标题: {info.get('title', '')}",
            f"状态: {info.get('ready', '')}",
            "",
            "正文（截断）：",
            (info.get("text") or "").strip()[:3000] or "（无正文）",
            "",
            f"可交互元素（共 {info.get('elementCount', 0)} 个，列出前 {len(info.get('elements') or [])}）：",
        ]
        for element in info.get("elements") or []:
            flags = " [禁用]" if element.get("disabled") else ""
            kind = f"{element['tag']}" + (f"[{element['type']}]" if element.get("type") else "")
            lines.append(f"  #{element['i']} {kind} {element.get('text', '')[:60]}{flags}")
        return "\n".join(lines)

    def _click(self, browser, target: str, _arg2: str, root) -> str:
        page = self._page(browser)
        result = self._eval(page, _CLICK_JS, _target_of(target)) or {}
        if not result.get("ok"):
            return f"错误：点击失败（{result.get('reason', '未知原因')}）：{target!r}。可先 read 看元素序号"
        return f"已点击 {result.get('tag', '')}「{result.get('text', '')}」"

    def _type(self, browser, target: str, text: str, root) -> str:
        page = self._page(browser)
        spec = _target_of(target)
        spec["text"] = str(text or "")
        result = self._eval(page, _TYPE_JS, spec) or {}
        if not result.get("ok"):
            return f"错误：输入失败（{result.get('reason', '未知原因')}）：{target!r}"
        shown = "******" if result.get("isPassword") else str(text or "")[:40]
        return f"已向 {result.get('tag', '')} 输入：{shown}"

    def _key(self, browser, key: str, _arg2: str, root) -> str:
        page = self._page(browser)
        key = str(key or "Enter").strip() or "Enter"
        page.keyboard.press(key)
        return f"已按键 {key}"

    def _back(self, browser, _arg1: str, _arg2: str, root) -> str:
        page = self._page(browser)
        page.go_back(timeout=self.timeout_ms)
        return f"已后退到 {page.url}"

    def _tabs(self, browser, _arg1: str, _arg2: str, root) -> str:
        contexts = browser.contexts
        if not contexts:
            return "浏览器没有可用上下文"
        lines = ["标签页："]
        for index, page in enumerate(contexts[0].pages, 1):
            try:
                title = page.title()
            except Exception:  # noqa: BLE001
                title = "(无标题)"
            lines.append(f"  {index}. {title} — {page.url}")
        return "\n".join(lines)

    def _wait(self, browser, ms: str, _arg2: str, root) -> str:
        page = self._page(browser)
        try:
            millis = min(max(int(str(ms or "1000").strip() or 1000), 100), 30000)
        except ValueError:
            return "错误：wait 需要毫秒数，例如 wait|1500"
        page.wait_for_timeout(millis)
        return f"已等待 {millis} ms"

    def _shot(self, browser, path: str, _arg2: str, root) -> str:
        page = self._page(browser)
        base = Path(root) if root else Path(os.getenv("WORKSPACE_DIR", "/app/workspace"))
        rel = str(path or "").strip() or f"outputs/browser/shot-{int(time.time())}.png"
        target = (base / rel).resolve()
        try:
            target.relative_to(base.resolve())
        except ValueError:
            return f"错误：截图路径必须在工作区内：{rel}"
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            png = page.screenshot(full_page=False, timeout=self.timeout_ms)
        except Exception as exc:  # noqa: BLE001
            return (
                f"错误：截图失败（{type(exc).__name__}）：{str(exc).splitlines()[0][:120]}。"
                "页面可能在后台标签/最小化状态，先把它切到前台再试。"
            )
        target.write_bytes(png)
        log(f"[browser] screenshot -> {target}")
        return f"已截图：{rel}（{len(png)} 字节）"


browser_tools = BrowserTools()
