"""工具注册表 / 目录 / provider 自检（不依赖真实 gRPC、不依赖网络）。

覆盖：
- TOOL.json（暴露）与实现的双向一致性；
- 分发：直调、别名、未知回落技能；
- 能力门禁：工具自声明 requires（clawhub 关闭时的文案）；
- 环境差异化：TOOL_PROVIDERS 选 provider，缺失/非法时明确报错。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SERVICE = ROOT / "services" / "tool-runtime-service"
sys.path.insert(0, str(SERVICE))

FAILS: list[str] = []
CHECKS = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global CHECKS
    CHECKS += 1
    if cond:
        print(f"  ok    {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL  {name} {extra}")


def main() -> int:
    os.environ["TOOL_CATALOG_PATH"] = str(ROOT / "config" / "orchestrator" / "system_prompt" / "TOOL.json")

    from app import tool_catalog, tool_provider, tool_registry
    from app import skill_handlers, tool_handlers  # noqa: F401  （import 即注册）

    print("目录加载")
    data = tool_catalog.load()
    check("TOOL.json 能读到", bool(data.get("sections")))
    exposed = tool_catalog.exposed(data)
    check("目录条目 48+", len(exposed) >= 48, str(len(exposed)))
    check("clawhub-list 已登记", "clawhub-list" in exposed)

    print("注册与别名")
    registered = tool_registry.registered()
    check("实现数 49", len(registered) == 49, str(len(registered)))
    check("别名 20 个", len(tool_registry.alias_map()) == 20, str(len(tool_registry.alias_map())))
    check("run-shell → shell", tool_registry.canonical("run-shell") == "shell")
    check("view-image → image-view", tool_registry.canonical("view-image") == "image-view")
    check("clawhub-list 自身即规范名", tool_registry.canonical("clawhub-list") == "clawhub-list")

    print("双向一致性校验")
    problems = tool_registry.validate_startup()
    hard = [p for p in problems if not p.startswith("提示：")]
    check("没有硬性问题", not hard, str(hard))

    print("分发")
    tmp = Path(tempfile.mkdtemp())
    out = tool_registry.dispatch("echo", ["hi", "there"], {}, tmp, 10)
    check("echo 走注册表", out == "hi there", out)
    tool_registry.dispatch("file-write", ["x.txt", "hello"], {}, tmp, 10)
    out = tool_registry.dispatch("file-read", ["x.txt"], {}, tmp, 10)
    check("file-write/file-read", out == "hello", out)

    class FakeSkill:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def dispatch_unknown(self, name, args, kwargs, root, timeout):
            self.calls.append(name)
            return f"skill:{name}"

        def clawhub_search(self, keyword, timeout):
            return f"search:{keyword}"

    fake = FakeSkill()
    out = tool_registry.dispatch("no-such-tool", [], {}, tmp, 5, skill=fake)
    check("未知名字回落技能执行", out == "skill:no-such-tool" and fake.calls == ["no-such-tool"], out)
    out = tool_registry.dispatch("clawhub-search", ["markdown"], {}, tmp, 5, skill=fake)
    check("技能类工具走实现（非回落）", out == "search:markdown", out)

    print("能力门禁（工具自声明 requires）")
    from app import config

    saved = config.ENABLE_CLAWHUB
    config.ENABLE_CLAWHUB = False
    try:
        out = tool_registry.dispatch("clawhub-search", ["x"], {}, tmp, 5, skill=fake)
        check("clawhub 关闭时给出显式文案", "ClawHub 未启用" in out, out[:60])
    finally:
        config.ENABLE_CLAWHUB = saved

    print("环境差异化（TOOL_PROVIDERS）")
    saved_env = os.environ.get("TOOL_PROVIDERS")
    try:
        local_providers = {"local": lambda ctx: "local-impl", "host-ssh": lambda ctx: "ssh-impl"}
        os.environ["TOOL_PROVIDERS"] = "file-copy=local"
        prefix, _ = tool_provider.select("file-copy", local_providers)
        check("显式配置优先", prefix == "local", prefix)

        os.environ["TOOL_PROVIDERS"] = "file-copy=host-ssh"
        out = tool_registry.dispatch("file-copy", ["a", "b"], {}, tmp, 5)
        check("file-copy 选了 host-ssh（走 mnt_access）", "mnt" in out or "错误" in out, out[:80])

        os.environ["TOOL_PROVIDERS"] = "file-copy=nope"
        problems = tool_provider.validate({"file-copy": local_providers})
        check("非法 provider 被启动校验拦下", any("不存在" in p for p in problems), str(problems))
        try:
            tool_provider.select("file-copy", local_providers)
            check("直接 select 非法 provider 应抛错", False)
        except ValueError as exc:
            check("直接 select 非法 provider 应抛错", "不存在" in str(exc), str(exc))
    finally:
        if saved_env is None:
            os.environ.pop("TOOL_PROVIDERS", None)
        else:
            os.environ["TOOL_PROVIDERS"] = saved_env

    print("文案回归（这些句子模型/用户都见过）")
    src = (SERVICE / "app" / "skill_handlers.py").read_text(encoding="utf-8")
    check("ClawHub 未启用文案在实现里", "错误：ClawHub 未启用" in src)
    check("OpenViking 未启用文案在实现里", "错误：OpenViking 未启用" in src)

    print()
    if FAILS:
        print(f"FAILED {len(FAILS)}: {FAILS}")
        return 1
    print(f"ALL OK — {CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
