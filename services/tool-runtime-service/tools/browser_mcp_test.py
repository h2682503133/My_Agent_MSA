"""Playwright MCP 客户端自检（不连真服务：桩掉 HTTP 层）。

覆盖：
 1. SSE 响应解析（event/data 行）、纯 JSON 兼容
 2. initialize 取 mcp-session-id；缺 session 时报错
 3. session 失效（404）自动重新 initialize 并重试一次
 4. 动作 → MCP 工具的参数映射（open/click/type/wait/shot 等）
 5. MCP 返回 isError / content 文本提取
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import browser_mcp as mcp  # noqa: E402

FAILS = []
CHECKS = 0


def check(name, cond, extra=""):
    global CHECKS
    CHECKS += 1
    if cond:
        print(f"  ok    {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL  {name} {extra}")


class FakeClient(mcp.McpClient):
    """把 _post 换成脚本化响应，并记录调用。"""

    def __init__(self, script):
        super().__init__("http://fake/mcp", timeout=1)
        self.script = list(script)
        self.calls = []

    def _post(self, payload, session_id=""):  # type: ignore[override]
        self.calls.append({"method": payload.get("method"), "params": payload.get("params"), "session": session_id})
        # 通知类消息不等响应，不消耗脚本化的响应
        if payload.get("method") == "notifications/initialized":
            return 200, {}, ""
        if not self.script:
            return 200, {}, "event: message\ndata: {\"result\":{}}"
        status, headers, body = self.script.pop(0)
        return status, headers, body


def sse(result: dict, session: str = "") -> tuple[int, dict, str]:
    headers = {"mcp-session-id": session} if session else {}
    return 200, headers, "event: message\ndata: " + json.dumps({"jsonrpc": "2.0", "result": result})


def main():
    print("SSE 解析")
    check("从 data 行取 JSON", mcp.McpClient._parse_body('event: message\ndata: {"result":{"a":1}}') == {"result": {"a": 1}})
    check("纯 JSON 兼容", mcp.McpClient._parse_body('{"result":{"a":2}}') == {"result": {"a": 2}})
    check("空体返回 {}", mcp.McpClient._parse_body("") == {})
    check("多个 data 取第一个",
          mcp.McpClient._parse_body('data: {"x":1}\ndata: {"x":2}') == {"x": 1})

    print("initialize / session")
    client = FakeClient([sse({"serverInfo": {"name": "Playwright"}}, session="SID-1")])
    session = client.initialize()
    check("拿到 session", session == "SID-1", session)
    check("发了 notifications/initialized",
          any(call["method"] == "notifications/initialized" for call in client.calls), str(client.calls))

    client2 = FakeClient([(200, {}, 'event: message\ndata: {"result":{}}')])
    try:
        client2.initialize()
        check("缺 session 报错", False, "没有抛异常")
    except mcp.McpError as exc:
        check("缺 session 报错", "mcp-session-id" in str(exc), str(exc))

    print("session 失效自动重试")
    client3 = FakeClient([
        sse({}, session="SID-3"),           # initialize
        (404, {}, "Session not found"),     # 第一次调用：失效
        sse({}, session="SID-3b"),          # 重新 initialize
        sse({"content": [{"type": "text", "text": "ok"}]}),  # 重试成功
    ])
    result = client3.call_tool("browser_snapshot", {})
    check("重试后拿到结果", result.get("content", [{}])[0].get("text") == "ok", str(result))
    inits = [c for c in client3.calls if c["method"] == "initialize"]
    check("重新 initialize 过", len(inits) == 2, str([c["method"] for c in client3.calls]))
    check("重试带的是新 session",
          [c["session"] for c in client3.calls if c["method"] == "tools/call"] == ["SID-3", "SID-3b"],
          str([(c["method"], c["session"]) for c in client3.calls]))

    print("content 文本提取")
    check("拼多条文本", mcp._text_of({"content": [{"type": "text", "text": "a"}, {"type": "text", "text": "b"}]}) == "a\nb")
    check("isError 转错误串", mcp._text_of({"isError": True, "content": [{"type": "text", "text": "boom"}]}).startswith("错误："))
    check("空 content 返回空串", mcp._text_of({}) == "")

    print("ref 识别")
    check("e12 是 ref", mcp._looks_like_ref("e12") is True)
    check("f2e13（frame 前缀）也是 ref", mcp._looks_like_ref("f2e13") is True)
    check("#f2e13 也认", mcp._looks_like_ref("#f2e13") is True)
    check("纯数字不是 ref", mcp._looks_like_ref("123") is False)
    check("#12 也认", mcp._looks_like_ref("#12") is True)
    check("中文文本不是 ref", mcp._looks_like_ref("登录") is False)

    print("动作映射（用假 client 记录参数）")
    captured = {}

    def fake_run(action, arg1="", arg2="", root=None, url="http://fake/mcp"):
        # 直接复用 run()，但把 McpClient.call_tool 拦下来
        original = mcp.McpClient.call_tool

        def fake_call(self, name, arguments=None):
            captured.update({"tool": name, "args": arguments})
            if name == "browser_take_screenshot":
                return {"content": [{"type": "image", "data": "iVBORw0KGgo=", "mimeType": "image/png"}]}
            return {"content": [{"type": "text", "text": "done"}]}

        mcp.McpClient.call_tool = fake_call
        try:
            return mcp.run(action, arg1, arg2, root, url=url)
        finally:
            mcp.McpClient.call_tool = original

    fake_run("open", "example.com")
    check("open → browser_navigate", captured["tool"] == "browser_navigate", str(captured))
    check("自动补 https", captured["args"]["url"] == "https://example.com", str(captured["args"]))

    fake_run("click", "e12")
    check("click(ref) 带 target", captured["tool"] == "browser_click" and captured["args"].get("target") == "e12", str(captured))

    fake_run("click", "#7")
    check("#7 归一化成 e7", captured["args"].get("target") == "e7", str(captured))

    fake_run("click", "登录按钮")
    check("click(文本) 走 evaluate 兜底", captured["tool"] == "browser_evaluate", str(captured))
    check("脚本里带上目标文本", "登录按钮" in str(captured["args"].get("function")), str(captured["args"])[:120])

    fake_run("type", "e5", "hello")
    check("type(ref) 传 text", captured["args"].get("text") == "hello" and captured["args"].get("target") == "e5", str(captured))

    fake_run("type", "账号", "myname")
    check("type(文本) 走 evaluate", captured["tool"] == "browser_evaluate", str(captured))
    check("脚本里带上内容", "myname" in str(captured["args"].get("function")), str(captured["args"])[:120])

    fake_run("wait", "1500")
    check("wait 毫秒→秒上限 30", captured["args"]["time"] == 30.0, str(captured["args"]))

    fake_run("wait", "2")
    check("wait 秒直传", captured["args"]["time"] == 2.0, str(captured["args"]))

    import tempfile
    tmp = Path(tempfile.mkdtemp())
    try:
        msg = fake_run("shot", "outputs/browser/a", root=tmp)
        check("shot 保存图片", (tmp / "outputs/browser/a.png").exists(), msg)
        check("shot 返回相对路径", "outputs/browser/a.png" in msg, msg)
    finally:
        for child in sorted(tmp.rglob("*"), reverse=True):
            child.unlink() if child.is_file() else child.rmdir()
        tmp.rmdir()

    check("未知动作给出可用列表", fake_run("nonsense").startswith("错误：未知的浏览器动作"))
    check("open 缺 URL 报错", fake_run("open", "").startswith("错误：open 需要一个 URL"))

    print()
    if FAILS:
        print(f"FAILED {len(FAILS)}: {FAILS}")
        return 1
    print(f"ALL OK — {CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
