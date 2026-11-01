"""静态检查：类里定义的方法必须是实例方法（带 self）或显式 @staticmethod/@classmethod。

背景：skill_runtime._first_arg 曾经写成
    def _first_arg(args, kwargs, *names)      # 少了 self，也没有 @staticmethod
调用方写的是 `self._first_arg(args, kwargs, ...)`，于是 self 被当成 args、
真正的 args（list）被当成 kwargs —— 一调用就
    AttributeError: 'list' object has no attribute 'get'
clawhub-search / clawhub-install 等技能工具因此**全部不可用**，而且只在真正
调用时才炸，静态阅读很难发现。

本测试把所有服务的类方法扫一遍：既不是实例方法、又没加装饰器的，直接失败。
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SKIP_PARTS = ("generated", "tools", "node_modules", ".venv")

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


def scan() -> list[str]:
    bad: list[str] = []
    for path in ROOT.joinpath("services").rglob("*.py"):
        if any(part in SKIP_PARTS for part in path.parts):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            for item in node.body:
                if not isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                decorators = {
                    d.id if isinstance(d, ast.Name) else getattr(d, "attr", "")
                    for d in item.decorator_list
                }
                if decorators & {"staticmethod", "classmethod", "property", "cached_property",
                                 "abstractmethod", "validator", "root_validator", "field_validator"}:
                    continue
                params = [arg.arg for arg in item.args.args]
                if not params or params[0] in ("self", "cls"):
                    continue
                bad.append(f"{path.relative_to(ROOT)}:{item.lineno} {node.name}.{item.name}")
    return bad


def main() -> int:
    bad = scan()
    check("没有「类方法漏 self 也没装饰器」的定义", not bad, "; ".join(bad[:5]))

    # 具体回归：skill_runtime._first_arg 必须是 staticmethod
    src = (ROOT / "services" / "tool-runtime-service" / "app" / "skill_runtime.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    found = None
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == "_first_arg":
                    found = item
    check("skill_runtime._first_arg 存在", found is not None)
    if found is not None:
        decorators = {d.id if isinstance(d, ast.Name) else getattr(d, "attr", "")
                      for d in found.decorator_list}
        check("_first_arg 是 @staticmethod", "staticmethod" in decorators, str(decorators))
        params = [a.arg for a in found.args.args]
        check("_first_arg 首参是 args（说明 self 没被误绑）", params and params[0] == "args", str(params))

    print()
    if FAILS:
        print(f"FAILED {len(FAILS)}: {FAILS}")
        return 1
    print(f"ALL OK — {CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
