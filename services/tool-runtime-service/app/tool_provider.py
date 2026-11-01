"""环境差异化：由上層注入 provider，工具本体不做平台判断。

口径（与长期计划第六章一致）
============================
- **能不能用**：工具自己声明（`requires` / `available()`），不在 TOOL.json 里；
- **给不给用**：TOOL.json（暴露哪些工具、级别、说明）；
- **怎么差异化**：上层（launcher / k8s 清单）通过 `TOOL_PROVIDERS` 指定 provider 前缀，
  工具内部只在"确实不同"的几个点上分实现，共用部分留在 core 里。

示例（上层注入）：

    单体 Windows : TOOL_PROVIDERS=file-copy=local,shell=windows,clawhub=local
    容器 k8s     : TOOL_PROVIDERS=file-copy=host-ssh,shell=posix,clawhub=external-vm

没配置时按平台给默认值（Windows → local/windows；其它 → host-ssh/posix），
但**显式配置优先**；provider 不存在时启动期就报错，不静默退回。
"""

from __future__ import annotations

import os

IS_WINDOWS = os.name == "nt"

# 平台默认 provider（只在没显式配置时使用）
PLATFORM_DEFAULTS = {
    "file-copy": "local" if IS_WINDOWS else "host-ssh",
    "file-move": "local" if IS_WINDOWS else "host-ssh",
    "shell": "windows" if IS_WINDOWS else "posix",
    "clawhub": "local" if IS_WINDOWS else "external-vm",
}


def parse(raw: str | None = None) -> dict[str, str]:
    """解析 `TOOL_PROVIDERS`（"tool=prefix,tool2=prefix2"）。"""
    text = (raw if raw is not None else os.getenv("TOOL_PROVIDERS", "")) or ""
    result: dict[str, str] = {}
    for item in text.split(","):
        chunk = item.strip()
        if not chunk:
            continue
        if "=" not in chunk:
            raise ValueError(f"TOOL_PROVIDERS 写法不对：{chunk!r}（应为 tool=prefix）")
        key, value = chunk.split("=", 1)
        key, value = key.strip(), value.strip()
        if not key or not value:
            raise ValueError(f"TOOL_PROVIDERS 写法不对：{chunk!r}")
        result[key] = value
    return result


def select(tool: str, providers: dict[str, object], *, mapping: dict[str, str] | None = None) -> tuple[str, object]:
    """按上层配置选一个 provider，返回 (prefix, implementation)。

    - 上层显式配置 > 平台默认；
    - provider 不存在 → 抛 ValueError（启动期就该暴露，而不是运行期 500）。
    """
    configured = (mapping if mapping is not None else parse()).get(tool, "")
    prefix = configured or PLATFORM_DEFAULTS.get(tool, "")
    if not prefix:
        raise ValueError(f"工具 {tool} 需要环境实现，但既没有 TOOL_PROVIDERS 配置也没有平台默认值")
    if prefix not in providers:
        raise ValueError(
            f"工具 {tool} 配置的 provider={prefix!r} 不存在；可选：{sorted(providers)}"
        )
    return prefix, providers[prefix]


def validate(registry: dict[str, dict[str, object]], *, mapping: dict[str, str] | None = None) -> list[str]:
    """启动期校验：每个多实现工具都得能选中一个 provider。返回问题列表。"""
    problems: list[str] = []
    for tool, providers in registry.items():
        try:
            select(tool, providers, mapping=mapping)
        except ValueError as exc:
            problems.append(str(exc))
    return problems
