"""工具注册与分发：TOOL.json 管暴露，工具自声明能力，上层注入环境实现。

分工（定稿，见 docs/长期计划.md 第六章与 tool_provider.py 文件头）：

| 问题 | 归属 | 实现位置 |
|---|---|---|
| 能不能用 | 工具自己 | `ToolImpl.requires` / `ToolImpl.reason`（或 provider 的 `available()`） |
| 给不给用 | `TOOL.json` | `tool_catalog.exposed()` |
| 怎么差异化 | 上层注入 | `TOOL_PROVIDERS` → `tool_provider.select()` |

`dispatch()` 只做四件事：别名解析 → 查实现 → 过能力门禁 → 调 handler。
未登记的名字沿用旧行为：当作技能名交给 `skill_runtime.dispatch_unknown()`。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from app import config
from app import tool_catalog, tool_provider

Handler = Callable[["ToolContext"], str]


@dataclass
class ToolContext:
    """一次工具调用：参数读取 + 提供者选择所需的运行期信息。"""

    name: str
    args: list[str]
    kwargs: dict[str, str]
    root: Path
    timeout: int
    service: object | None = None
    skill: object | None = None

    # ── 参数读取（沿用"kwargs 优先，其次位置参数"的老约定）───────────────
    def first(self, *names: str, index: int = 0, default: str = "") -> str:
        for name in names:
            value = self.kwargs.get(name)
            if value is not None and str(value).strip():
                return str(value).strip()
        if index < len(self.args) and str(self.args[index]).strip():
            return str(self.args[index]).strip()
        return default

    def positional(self, index: int, default: str = "") -> str:
        if index < len(self.args) and str(self.args[index]).strip():
            return str(self.args[index]).strip()
        return default

    def flag(self, name: str, default: bool = False) -> bool:
        raw = str(self.kwargs.get(name, "")).strip().lower()
        if not raw:
            return default
        return raw in {"1", "true", "yes", "on"}

    def int_arg(self, names: tuple[str, ...], index: int, default: int) -> int:
        raw = self.first(*names, index=index, default="")
        try:
            return int(raw)
        except (TypeError, ValueError):
            return default


@dataclass
class ToolImpl:
    """工具实现声明。

    - `fn`：唯一实现；
    - `providers`：需要按环境差异化的工具，键 = provider 前缀（值见上层 `TOOL_PROVIDERS`）；
    - `requires`：能力开关名（对应 `config.ENABLE_*`）；不满足时返回 `reason`。
    """

    fn: Handler | None = None
    providers: dict[str, Handler] = field(default_factory=dict)
    requires: tuple[str, ...] = ()
    reason: str = ""


_REGISTRY: dict[str, ToolImpl] = {}
_ALIASES: dict[str, str] = {}


def register(
    name: str,
    fn: Handler | None = None,
    *,
    aliases: tuple[str, ...] = (),
    providers: dict[str, Handler] | None = None,
    requires: tuple[str, ...] = (),
    reason: str = "",
) -> None:
    if name in _REGISTRY:
        raise RuntimeError(f"工具重复注册：{name}")
    if not fn and not providers:
        raise RuntimeError(f"工具 {name} 既没有实现也没有 providers")
    _REGISTRY[name] = ToolImpl(fn=fn, providers=dict(providers or {}), requires=requires, reason=reason)
    for alias in aliases:
        register_alias(alias, name)


def register_alias(alias: str, name: str) -> None:
    existing = _ALIASES.get(alias)
    if existing and existing != name:
        raise RuntimeError(f"别名冲突：{alias} 同时指向 {existing} 与 {name}")
    if alias in _REGISTRY:
        raise RuntimeError(f"别名与工具名冲突：{alias}")
    _ALIASES[alias] = name


def registered() -> set[str]:
    return set(_REGISTRY)


def alias_map() -> dict[str, str]:
    return dict(_ALIASES)


def canonical(name: str) -> str:
    """解析别名：代码别名 + TOOL.json 里声明的 aliases。"""
    text = (name or "").strip()
    if text in _REGISTRY:
        return text
    if text in _ALIASES:
        return _ALIASES[text]
    try:
        for target, entry in tool_catalog.exposed().items():
            if text in entry.get("aliases", ()):
                return target
    except tool_catalog.CatalogError:
        pass
    return text


def _gate_error(impl: ToolImpl) -> str:
    for cap in impl.requires:
        env_key = {
            "clawhub": "ENABLE_CLAWHUB",
            "openviking": "ENABLE_OPENVIKING",
            "codex": "ENABLE_CODEX",
            "shell": "ENABLE_SHELL_TOOLS",
        }.get(cap, f"ENABLE_{cap.upper()}")
        if not getattr(config, env_key, True):
            return impl.reason or f"错误：{cap} 未启用（{env_key}=false）。"
    return ""


def validate_startup(*, strict: bool | None = None) -> list[str]:
    """启动期校验：目录 ↔ 实现双向一致 + provider 可选。返回问题列表（同时打印）。"""
    from app.logger import log

    problems: list[str] = []
    try:
        data = tool_catalog.load()
        problems += tool_catalog.validate(registered(), data)
    except tool_catalog.CatalogError as exc:
        problems.append(str(exc))
        data = {}

    provider_map = {name: impl.providers for name, impl in _REGISTRY.items() if impl.providers}
    problems += tool_provider.validate(provider_map)

    for problem in problems:
        log(f"[tool-registry] ⚠ {problem}")

    hard = [p for p in problems if not p.startswith("提示：")]
    if hard and (strict if strict is not None else _strict_default()):
        raise RuntimeError("工具目录/实现不一致：" + "；".join(hard))
    return problems


def _strict_default() -> bool:
    raw = str(getattr(config, "TOOL_CATALOG_STRICT", "") or "").strip().lower()
    if not raw:
        return False  # 迁移期默认宽松；k8s 挂载与 launcher 注入完成后改回 True
    return raw in {"1", "true", "yes", "on"}


def dispatch(
    name: str,
    args: list[str],
    kwargs: dict[str, str],
    root: Path,
    timeout: int,
    *,
    service: object | None = None,
    skill: object | None = None,
) -> str:
    text = (name or "").strip()
    target = canonical(text)
    impl = _REGISTRY.get(target)
    if impl is None:
        if skill is None:
            return f"错误：未知工具 {text!r}"
        return skill.dispatch_unknown(text, args, kwargs, root, timeout)  # type: ignore[attr-defined]

    denied = _gate_error(impl)
    if denied:
        return denied

    ctx = ToolContext(
        name=target,
        args=list(args),
        kwargs=dict(kwargs),
        root=Path(root),
        timeout=timeout,
        service=service,
        skill=skill,
    )

    handler = impl.fn
    if handler is None:
        prefix, selected = tool_provider.select(target, impl.providers)
        handler = selected  # type: ignore[assignment]
        del prefix

    return handler(ctx)
