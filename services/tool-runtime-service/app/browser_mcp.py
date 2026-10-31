"""Playwright MCP 客户端（Streamable HTTP，JSON-RPC 2.0 + SSE 响应）。

为什么不继续用 CDP 9222（参考《Edge_Playwright_MCP_经验汇总》）：
  - 调试端口参数会被 Edge 丢弃（默认 profile 被浏览器自身禁止开调试端口）；
  - 每次都要先退出用户所有 Edge 窗口；
  - 端口可能被系统回收。
Playwright MCP 由它自己托管浏览器（固定 profile），只暴露一个 HTTP 端口，
对模型更友好的是 browser_snapshot（无障碍树 + ref）和 browser_evaluate。

协议要点：
  - POST /mcp，header 带 Accept: application/json, text/event-stream；
  - initialize 的响应头里有 mcp-session-id，后续请求都要带上；
  - 服务重启后旧 session 失效（404 / Session not found）→ 自动重新 initialize 重试一次；
  - 响应体是 SSE：`event: message` + `data: {json}`。
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
from pathlib import Path
from typing import Any

from app.logger import log

_TIMEOUT = float(os.getenv("BROWSER_MCP_TIMEOUT", "40"))
_PROTOCOL_VERSION = "2025-06-18"


class McpError(RuntimeError):
    """MCP 调用失败（带用户可读原因）。"""


class McpClient:
    def __init__(self, url: str, timeout: float = _TIMEOUT):
        self.url = str(url or "").strip()
        self.timeout = timeout
        self.session_id = ""
        self._initialized_at = 0.0

    # ---------- HTTP / SSE ----------
    def _post(self, payload: dict, session_id: str = "") -> tuple[int, dict, str]:
        import urllib.error
        import urllib.request

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if session_id:
            headers["mcp-session-id"] = session_id
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(self.url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8", "replace")
                return resp.status, dict(resp.headers), body
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            return exc.code, dict(exc.headers or {}), body

    @staticmethod
    def _parse_body(body: str) -> dict:
        """SSE 里取第一条 data；纯 JSON 也兼容。"""
        text = (body or "").strip()
        if not text:
            return {}
        if text.startswith("{"):
            try:
                return json.loads(text)
            except Exception:  # noqa: BLE001
                return {}
        for line in text.splitlines():
            line = line.strip()
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if not payload:
                continue
            try:
                return json.loads(payload)
            except Exception:  # noqa: BLE001
                continue
        return {}

    # ---------- 会话 ----------
    def initialize(self) -> str:
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": _PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "my-agent-tool-runtime", "version": "1.0"},
            },
        }
        status, headers, body = self._post(payload)
        if status >= 400:
            raise McpError(f"initialize 失败（HTTP {status}）：{body[:200]}")
        parsed = self._parse_body(body)
        if parsed.get("error"):
            raise McpError(f"initialize 失败：{parsed['error']}")
        session = ""
        for key, value in headers.items():
            if key.lower() == "mcp-session-id":
                session = str(value)
                break
        if not session:
            raise McpError("initialize 没有返回 mcp-session-id，MCP 版本可能不兼容 Streamable HTTP")
        self.session_id = session
        self._initialized_at = time.time()
        # 协议要求：initialize 之后补一条 notifications/initialized（无 id，不等响应）
        try:
            self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, session)
        except Exception:  # noqa: BLE001
            pass
        return session

    def _ensure_session(self) -> None:
        if not self.session_id:
            self.initialize()

    def call(self, method: str, params: dict | None = None, retry: bool = True) -> dict:
        """调一次 JSON-RPC 方法；session 失效自动重建后重试一次。"""
        self._ensure_session()
        payload = {"jsonrpc": "2.0", "id": int(time.time() * 1000) % 2_000_000_000, "method": method}
        if params is not None:
            payload["params"] = params
        status, _headers, body = self._post(payload, self.session_id)
        if status in (400, 404) and retry:
            log(f"[browser-mcp] session 失效（HTTP {status}），重新 initialize 后重试")
            self.session_id = ""
            self.initialize()
            return self.call(method, params, retry=False)
        parsed = self._parse_body(body)
        if status >= 400 and not parsed:
            raise McpError(f"{method} 失败（HTTP {status}）：{body[:200]}")
        if parsed.get("error"):
            error = parsed["error"]
            message = error.get("message") if isinstance(error, dict) else str(error)
            raise McpError(f"{method} 失败：{message}")
        return parsed.get("result") or {}

    def call_tool(self, name: str, arguments: dict | None = None) -> dict:
        return self.call("tools/call", {"name": name, "arguments": arguments or {}})


# ───────────── 对外：动作 → MCP 工具 ─────────────


def _text_of(result: dict) -> str:
    """把 MCP 的 content 数组压成文本；图片单独提示（由调用方决定落盘）。"""
    parts: list[str] = []
    for item in result.get("content") or []:
        if not isinstance(item, dict):
            continue
        kind = item.get("type")
        if kind == "text":
            parts.append(str(item.get("text") or ""))
        elif kind == "image":
            parts.append("[图片返回，已忽略文本化]")
    text = "\n".join(part for part in parts if part).strip()
    if result.get("isError"):
        return f"错误：{text or '工具返回 isError'}"
    return text


def _first_image(result: dict) -> tuple[str, str]:
    for item in result.get("content") or []:
        if isinstance(item, dict) and item.get("type") == "image":
            return str(item.get("data") or ""), str(item.get("mimeType") or "image/png")
    return "", ""


# 同一个 MCP 端点复用同一个会话：Playwright MCP 是"一个会话一个浏览器"，
# 每次新建会话会在同一个 fixed profile 上再起一个浏览器，直接报
# `Browser is already in use for ...`。
_CLIENT_CACHE: dict[str, McpClient] = {}


def _client_for(url: str) -> McpClient:
    endpoint = str(url or "").strip()
    client = _CLIENT_CACHE.get(endpoint)
    if client is None:
        client = McpClient(endpoint)
        _CLIENT_CACHE[endpoint] = client
    return client


def run(action: str, arg1: str = "", arg2: str = "", root: Path | None = None, url: str = "") -> str:
    """统一入口：`工具调用:browser|动作|参数1|参数2`（MCP 版）。"""
    action = str(action or "").strip().lower()
    client = _client_for(url or os.getenv("BROWSER_MCP_URL", ""))

    try:
        if action == "open":
            page_url = str(arg1 or "").strip()
            if not page_url:
                return "错误：open 需要一个 URL"
            if not page_url.startswith(("http://", "https://")):
                page_url = "https://" + page_url
            result = client.call_tool("browser_navigate", {"url": page_url})
            return _text_of(result) or f"已打开 {page_url}"

        if action in {"read", "snapshot"}:
            result = client.call_tool("browser_snapshot", {})
            text = _text_of(result)
            return text[:6000] if text else "（页面没有可读内容）"

        if action == "find":
            result = client.call_tool("browser_find", {"text": str(arg1 or "")})
            return _text_of(result) or "没找到匹配内容"

        if action == "click":
            target = str(arg1 or "").strip()
            if not target:
                return "错误：click 需要一个元素（snapshot 里的 ref 如 e12，或可见文本）"
            if _looks_like_ref(target):
                ref = _normalize_ref(target)
                result = client.call_tool("browser_click", {"element": ref, "target": ref})
                return _text_of(result) or f"已点击 {ref}"
            # MCP 的 browser_click 必须给 target（ref），纯文本走 JS 兜底
            script = (
                "() => { const t = %s;"
                " const nodes = [...document.querySelectorAll('a,button,input,[role=button],summary,label')];"
                " const hit = nodes.find(n => ((n.innerText||n.value||n.getAttribute('aria-label')||'') + '').trim().toLowerCase().includes(t));"
                " if (!hit) return 'not-found';"
                " hit.scrollIntoView({block:'center'}); hit.click();"
                " return 'clicked: ' + ((hit.innerText||hit.value||'').trim().slice(0,40)); }"
            ) % json.dumps(target.lower(), ensure_ascii=False)
            result = client.call_tool("browser_evaluate", {"function": script})
            text = _text_of(result)
            if "not-found" in text:
                return (
                    f"错误：页面上没找到可点击的「{target}」。"
                    "先 read 看可交互元素的 ref（e12 这种），再用 click|e12"
                )
            return text or f"已点击 {target}"

        if action == "type":
            target = str(arg1 or "").strip()
            text = str(arg2 or "")
            if not target:
                return "错误：type 需要「元素|内容」（元素用 ref 如 e12，或输入框的 placeholder/标签文字）"
            if _looks_like_ref(target):
                ref = _normalize_ref(target)
                result = client.call_tool("browser_type", {"element": ref, "target": ref, "text": text})
                return _text_of(result) or f"已向 {ref} 输入"
            script = (
                "() => { const t = %s; const v = %s;"
                " const nodes = [...document.querySelectorAll('input,textarea,[contenteditable=true]')];"
                " const hit = nodes.find(n => ((n.getAttribute('placeholder')||'') + (n.getAttribute('aria-label')||'') + (n.name||'')).toLowerCase().includes(t));"
                " if (!hit) return 'not-found';"
                " hit.focus();"
                " if (hit.isContentEditable) hit.textContent = v; else hit.value = v;"
                " hit.dispatchEvent(new Event('input', {bubbles:true}));"
                " hit.dispatchEvent(new Event('change', {bubbles:true}));"
                " return 'typed'; }"
            ) % (json.dumps(target.lower(), ensure_ascii=False), json.dumps(text, ensure_ascii=False))
            result = client.call_tool("browser_evaluate", {"function": script})
            out = _text_of(result)
            if "not-found" in out:
                return (
                    f"错误：没找到匹配「{target}」的输入框。"
                    "先 read 看输入框的 ref，再用 type|e12|内容"
                )
            shown = "******" if _looks_password(target) else text[:40]
            return f"已输入到「{target}」：{shown}"

        if action == "key":
            key = str(arg1 or "Enter").strip() or "Enter"
            result = client.call_tool("browser_press_key", {"key": key})
            return _text_of(result) or f"已按键 {key}"

        if action == "back":
            result = client.call_tool("browser_navigate_back", {})
            return _text_of(result) or "已后退"

        if action == "tabs":
            result = client.call_tool("browser_tabs", {"action": "list"})
            return _text_of(result) or "（没有标签页信息）"

        if action == "wait":
            raw = str(arg1 or "2").strip()
            try:
                seconds = max(min(float(raw), 30.0), 0.2)
            except ValueError:
                return "错误：wait 需要秒数，例如 wait|2"
            result = client.call_tool("browser_wait_for", {"time": seconds})
            return _text_of(result) or f"已等待 {seconds}s"

        if action == "eval":
            code = str(arg1 or "").strip()
            if not code:
                return "错误：eval 需要一段 JS，例如 eval|() => document.title"
            result = client.call_tool("browser_evaluate", {"function": code})
            return _text_of(result) or "（无返回值）"

        if action == "select":
            target = str(arg1 or "").strip()
            values = [part.strip() for part in str(arg2 or "").split("|") if part.strip()]
            args: dict[str, Any] = {"element": target, "values": values}
            if _looks_like_ref(target):
                args["target"] = target
            result = client.call_tool("browser_select_option", args)
            return _text_of(result) or f"已选择 {target}"

        if action == "shot":
            result = client.call_tool("browser_take_screenshot", {"fullPage": False})
            data, mime = _first_image(result)
            if not data:
                return _text_of(result) or "错误：截图没有返回图片数据"
            return _save_image(data, mime, str(arg1 or ""), root)

        return (
            f"错误：未知的浏览器动作 {action!r}。可用：open/read/find/click/type/key/back/tabs/wait/shot/eval/select"
        )
    except McpError as exc:
        return f"错误：{exc}"
    except Exception as exc:  # noqa: BLE001
        return f"错误：浏览器（MCP）调用失败（{type(exc).__name__}）：{exc}"


def _normalize_ref(text: str) -> str:
    """去掉模型习惯的 `#`；`#12` 补成 `e12`，`#f2e13` 原样（frame 前缀要在）。"""
    value = str(text or "").strip()
    if value.startswith("#"):
        value = value[1:]
    if value.isdigit():
        return "e" + value
    return value


def _looks_password(target: str) -> bool:
    lowered = str(target or "").lower()
    return any(word in lowered for word in ("密码", "password", "passwd", "口令"))


_REF_RE = re.compile(r"[a-z]*\d*e\d+")


def _looks_like_ref(text: str) -> bool:
    """MCP 的 ref 形如 `e12`，带 frame 前缀时是 `f2e13`；也接受 `#12` / `#f2e13`。"""
    value = str(text or "").strip().lower()
    if value.startswith("#"):
        # 模型习惯写 #12：数字视为 ref 的简写，交给 _normalize_ref 补 e
        rest = value[1:]
        return bool(rest) and (rest.isdigit() or bool(_REF_RE.fullmatch(rest)))
    if not value:
        return False
    return bool(_REF_RE.fullmatch(value))


def _save_image(data_b64: str, mime: str, rel_path: str, root: Path | None) -> str:
    base = Path(root) if root else Path(os.getenv("WORKSPACE_DIR", "/app/workspace"))
    rel = rel_path.strip() or f"outputs/browser/shot-{int(time.time())}.png"
    if not rel.lower().endswith((".png", ".jpg", ".jpeg")):
        rel += ".png"
    target = (base / rel).resolve()
    try:
        target.relative_to(base.resolve())
    except ValueError:
        return f"错误：截图路径必须在工作区内：{rel}"
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        target.write_bytes(base64.b64decode(data_b64))
    except Exception as exc:  # noqa: BLE001
        return f"错误：截图保存失败：{exc}"
    return f"已截图：{rel}"
