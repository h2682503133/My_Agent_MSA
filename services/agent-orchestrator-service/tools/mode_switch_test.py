"""模式开关自检：纯逻辑 + 解析 + 合并规则。

跑法：`python tools/mode_switch_test.py`
"""

from __future__ import annotations

import sys
from pathlib import Path

SERVICE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE_ROOT))

from app import mode  # noqa: E402

PASSED = 0
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    global PASSED
    if condition:
        PASSED += 1
        print(f"  ok    {name}")
    else:
        FAILED.append(name)
        print(f"  FAIL  {name}" + (f"   [{detail}]" if detail else ""))


AGENT_CONFIG = {
    "model_profile": "default-main",
    "temperature": 0.7,
    "max_tokens": 2048,
    "files": ["SOUL.md", "TOOL.md", "IDENTITY.md"],
    "modes": {
        "special": {
            "files": ["IDENTITY.md"],
            "model_profile": "default-talker",
            "temperature": 1.2,
        },
        "broken": {"files": ["NOT_EXIST.md"]},
    },
}


def test_normalize() -> None:
    check("空 → default", mode.normalize_mode("") == "default")
    check("None → default", mode.normalize_mode(None) == "default")
    check("空白 → default", mode.normalize_mode("   ") == "default")
    check("保留名字", mode.normalize_mode(" special ") == "special")


def test_case_insensitive() -> None:
    check("Default → default", mode.canonical_mode(AGENT_CONFIG, "Default") == "default")
    check("DEFAULT → default", mode.canonical_mode(AGENT_CONFIG, "DEFAULT") == "default")
    check("SPECIAL → special", mode.canonical_mode(AGENT_CONFIG, "SPECIAL") == "special")
    check("未知名原样返回", mode.canonical_mode(AGENT_CONFIG, "nope") == "nope")
    eff = mode.effective_config(AGENT_CONFIG, "SPECIAL")
    check("大写模式也覆盖模型", eff["model_profile"] == "default-talker", str(eff.get("model_profile")))
    check("大写 default 不改配置", mode.effective_config(AGENT_CONFIG, "DEFAULT")["files"] == AGENT_CONFIG["files"])


def test_available() -> None:
    names = mode.available_modes(AGENT_CONFIG)
    check("default 排第一", names[0] == "default", str(names))
    check("包含已定义模式", "special" in names, str(names))
    check("无 modes 时只有 default", mode.available_modes({}) == ["default"])


def test_effective_config() -> None:
    base = mode.effective_config(AGENT_CONFIG, "default")
    check("default 不改动配置", base["model_profile"] == "default-main", str(base.get("model_profile")))
    check("default 保留 files", base["files"] == AGENT_CONFIG["files"])

    special = mode.effective_config(AGENT_CONFIG, "special")
    check("模式覆盖模型", special["model_profile"] == "default-talker", str(special.get("model_profile")))
    check("模式覆盖温度", special["temperature"] == 1.2, str(special.get("temperature")))
    check("模式覆盖 files", special["files"] == ["IDENTITY.md"], str(special.get("files")))
    check("未覆盖的键沿用 agent", special["max_tokens"] == 2048, str(special.get("max_tokens")))

    unknown = mode.effective_config(AGENT_CONFIG, "nope")
    check("未知模式回落 agent 配置", unknown["model_profile"] == "default-main")


def test_validate() -> None:
    tmp = Path(__file__).parent / "_mode_probe"
    tmp.mkdir(exist_ok=True)
    (tmp / "IDENTITY.md").write_text("x", encoding="utf-8")
    try:
        check("default 永远合法", mode.validate_mode(AGENT_CONFIG, tmp, "default") == "")
        check("已定义且文件齐 → 合法", mode.validate_mode(AGENT_CONFIG, tmp, "special") == "")
        err = mode.validate_mode(AGENT_CONFIG, tmp, "nope")
        check("未知模式 → 报错并列出可用", "未知模式" in err and "special" in err, err)
        err2 = mode.validate_mode(AGENT_CONFIG, tmp, "broken")
        check("文件缺失 → 显式报错", "NOT_EXIST.md" in err2, err2)
    finally:
        for f in tmp.glob("*"):
            f.unlink()
        tmp.rmdir()


def test_parser() -> None:
    """`模式:` 指令解析（不依赖模型）。"""
    from app.syntax_parser import parse_syntax

    class FakeTask:
        def __init__(self, text: str) -> None:
            self._out = text
            self.content = text
            self.tool_log = []
            self.metadata = {}
            self.prompt_plan = None
            self.caller = None

        def consume_temp_dialog_output(self):
            value = self._out
            self._out = None
            return value

        def set_temp_dialog_output(self, value):
            self._out = value

        def begin_work_episode(self):
            pass

        def push_context(self, *args, **kwargs):
            pass

    class FakeAgent:
        id = "main"

        def __init__(self) -> None:
            self.switched = []

        def set_default_agent(self, agent_id):
            self.switched.append(agent_id)

    def parse(text: str) -> dict:
        agent = FakeAgent()
        task = FakeTask(text)
        parse_syntax(agent, task)
        return task.consume_temp_dialog_output() or {}

    result = parse("模式:special")
    check("解析出 mode_switch", result.get("mode_switch") == "special", str(result.get("mode_switch")))
    result = parse("模式:")
    check("空模式 = default", result.get("mode_switch") == "default", str(result.get("mode_switch")))
    result = parse("普通聊天，没有指令")
    check("无指令时为空", result.get("mode_switch") == "", str(result.get("mode_switch")))
    result = parse("切换:talker")
    check("切换 agent 仍照旧解析", result.get("mode_switch") == "", str(result.get("mode_switch")))


def main() -> int:
    print("归一化")
    test_normalize()
    print("大小写不敏感")
    test_case_insensitive()
    print("可用模式")
    test_available()
    print("配置合并")
    test_effective_config()
    print("校验")
    test_validate()
    print("指令解析")
    test_parser()

    print()
    if FAILED:
        print(f"FAILED {len(FAILED)}: {FAILED}")
        return 1
    print(f"ALL OK — {PASSED} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
