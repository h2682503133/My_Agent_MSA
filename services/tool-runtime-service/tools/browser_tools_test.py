"""browser 工具自检（不连浏览器：只测参数解析 / 目标解析 / 错误语义）。

真实连通性靠部署后手工验证（Edge 需以 --remote-debugging-port=9222 启动）。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.browser_tools import BrowserError, BrowserTools, _target_of  # noqa: E402

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


def main():
    print("目标解析（#序号 / CSS / 可见文本）")
    check("#3 → index", _target_of("#3") == {"index": 3}, str(_target_of("#3")))
    check("css: 前缀", _target_of("css:button.primary") == {"selector": "button.primary"})
    check("# 开头非数字 → 文本", _target_of("#话题") == {"text": "#话题"}, str(_target_of("#话题")))
    check(".cls → 选择器", _target_of(".login-btn") == {"selector": ".login-btn"}, str(_target_of(".login-btn")))
    check("带 > 当选择器", _target_of("div > a") == {"selector": "div > a"}, str(_target_of("div > a")))
    check("中文文本", _target_of("登录") == {"text": "登录"}, str(_target_of("登录")))
    check("空 → {}", _target_of("") == {})

    print("未知动作")
    tools = BrowserTools(cdp_url="http://127.0.0.1:1", timeout_ms=500)
    out = tools.run("nonsense")
    check("返回错误而不是抛异常", out.startswith("错误：未知的浏览器动作"), out[:60])
    check("列出可用动作", "open" in out and "shot" in out, out[:120])

    try:
        import playwright  # noqa: F401

        has_playwright = True
    except Exception:  # noqa: BLE001
        has_playwright = False

    if not has_playwright:
        print("本机没有 playwright：只验证「优雅报错」，连接相关用例跳过")
        out = tools.run("open", "https://example.com")
        check("没有依赖时给出明确原因", "playwright 不可用" in out, out[:120])
    else:
        print("连不上时错误可读（不静默）")
        out = tools.run("open", "https://example.com")
        check("提示启动参数", "remote-debugging-port=9222" in out, out[:160])
        check("带上 CDP 地址", "127.0.0.1:1" in out, out[:160])

        print("缺参数校验（需要先连上浏览器才会走到，故仅在可达时验证）")

    print("截图路径限制（不连浏览器时先看路径校验顺序）")
    # _shot 需要浏览器；这里只验证 BrowserTools 暴露的动作集合
    for action in ("open", "read", "click", "type", "key", "back", "tabs", "wait", "shot"):
        check(f"动作存在：{action}", hasattr(tools, f"_{action}"))


    print("CDP 端点候选（自动探测）")
    tools2 = BrowserTools(cdp_url="http://127.0.0.1:9222")
    candidates = tools2._candidates()
    check("显式配置在首位", candidates[0] == "http://127.0.0.1:9222", str(candidates))
    check("包含 host.docker.internal", any("host.docker.internal" in c for c in candidates), str(candidates))
    check("默认网关可读或为空", all(c.startswith("http://") for c in candidates), str(candidates))
    check("探测不存在的端点返回 False", tools2._probe("http://127.0.0.1:1", timeout=0.5) is False)

    print()
    if FAILS:
        print(f"FAILED {len(FAILS)}: {FAILS}")
        return 1
    print(f"ALL OK — {CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
