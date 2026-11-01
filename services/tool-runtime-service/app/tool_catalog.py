"""工具目录（TOOL.json）的读取与校验：它只负责"给不给用"。

分工（见 docs/长期计划.md 第六章与 tool_provider.py 文件头）：

| 问题 | 归属 |
|---|---|
| 能不能用（Node 没装 / openviking 连不上） | 工具实现自己声明 `requires` |
| 给不给用（暴露哪些工具、级别） | **本模块读到的 TOOL.json** |
| 怎么差异化（Windows / 容器 / 外部 VM） | 上层注入 `TOOL_PROVIDERS` |

本模块提供：
- `load()`：读目录（路径来自 `TOOL_CATALOG_PATH`，其次按仓库/容器常见位置探测）；
- `exposed()`：展开成 {name: {aliases, level, group, desc, args}}；
- `validate(implemented)`：双向校验（声明没实现 / 实现没声明），返回问题列表。
  启动期调用一次——**目录与实现对不上就不要假装能跑**。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

# 目录里"非工具"的分节（纯说明文字）
_NON_TOOL_SECTIONS_HINT = "调用格式"


class CatalogError(RuntimeError):
    """目录文件缺失/损坏/结构不对。"""


def _candidates() -> list[Path]:
    env = os.getenv("TOOL_CATALOG_PATH", "").strip()
    out: list[Path] = []
    if env:
        out.append(Path(env))
    data_root = os.getenv("MYAGENT_STORAGE_ROOT", "").strip()
    if data_root:
        out.append(Path(data_root) / "config" / "orchestrator" / "system_prompt" / "TOOL.json")
    out += [
        Path("/app/system_prompt/TOOL.json"),                      # k8s（挂载 orchestrator 的 system_prompt）
        Path("/app/config/orchestrator/system_prompt/TOOL.json"),  # 兜底旧布局
    ]
    # 仓库内（开发/自测）
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "config" / "orchestrator" / "system_prompt" / "TOOL.json"
        if candidate.is_file():
            out.append(candidate)
            break
    return out


def path() -> Path:
    """目录文件实际路径；不存在则抛 CatalogError（带修复提示）。"""
    tried = _candidates()
    for candidate in tried:
        if candidate.is_file():
            return candidate
    raise CatalogError(
        "找不到 TOOL.json；用 TOOL_CATALOG_PATH 指定，或把 orchestrator 的 "
        "system_prompt 目录挂载到 /app/system_prompt。已尝试："
        + "、".join(str(p) for p in tried)
    )


def load() -> dict[str, Any]:
    target = path()
    try:
        data = json.loads(target.read_text(encoding="utf-8-sig"))
    except Exception as exc:  # noqa: BLE001
        raise CatalogError(f"TOOL.json 解析失败：{target}（{exc}）") from exc
    if not isinstance(data, dict) or "sections" not in data:
        raise CatalogError(f"TOOL.json 结构不对（缺 sections）：{target}")
    return data


def _caps_of(groups: list[str]) -> list[str]:
    return [g[4:] for g in groups if isinstance(g, str) and g.startswith("cap:")]


def _level_of(groups: list[str]) -> str:
    for group in groups:
        if isinstance(group, str) and group.upper() in {"L0", "L1", "L2", "L3", "L4"}:
            return group.upper()
    return ""


def exposed(data: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    """展开成 {规范名: {...}}，别名同时登记指向规范名。"""
    payload = data if data is not None else load()
    result: dict[str, dict[str, Any]] = {}
    for section in payload.get("sections") or []:
        if not isinstance(section, dict):
            continue
        for tool in section.get("tools") or []:
            if not isinstance(tool, dict):
                continue
            name = str(tool.get("name") or "").strip()
            if not name:
                continue
            groups = [str(g) for g in (tool.get("groups") or [])]
            entry = {
                "name": name,
                "section": str(section.get("id") or ""),
                "args": str(tool.get("args") or ""),
                "desc": str(tool.get("desc") or ""),
                "level": _level_of(groups),
                "config_caps": _caps_of(groups),   # 历史遗留：目录里的 cap: 标签，仅用于迁移期告警
                "aliases": [str(a) for a in (tool.get("aliases") or [])],
            }
            # 同名重复条目（如 fetch 出现两次）保留第一份用于分发；展示仍由原文件决定
            result.setdefault(name, entry)
    return result


def validate(implemented: set[str], data: dict[str, Any] | None = None) -> list[str]:
    """双向校验：目录有但没实现 / 实现有但没登记。"""
    catalog = exposed(data)
    alias_map: dict[str, str] = {}
    for name, entry in catalog.items():
        for alias in entry["aliases"]:
            alias_map[alias] = name

    problems: list[str] = []

    missing_impl = [n for n in catalog if n not in implemented and n not in alias_map]
    if missing_impl:
        problems.append("TOOL.json 声明了但找不到实现：" + "、".join(sorted(missing_impl)))

    unimplemented_alias = [a for a, target in alias_map.items() if target not in implemented]
    if unimplemented_alias:
        problems.append("TOOL.json 别名指向未实现的工具：" + "、".join(sorted(unimplemented_alias)))

    # 实现了却没登记（除了内部命令）
    internal_ok = {"help", ""}
    unlisted = sorted(n for n in implemented if n not in catalog and n not in alias_map and n not in internal_ok)
    if unlisted:
        problems.append("实现存在但 TOOL.json 没登记：" + "、".join(unlisted))

    # 说明：cap: 标签的执行意义已经移到工具自声明 requires；目录里的标签仅剩
    # 「编排器提示词过滤」这一处消费方，因此这里只做提示，不算不一致。
    legacy_caps = sorted({c for entry in catalog.values() for c in entry["config_caps"]})
    if legacy_caps:
        problems.append(
            "提示：TOOL.json 仍有 cap: 标签（仅用于编排器提示词过滤，执行门禁以工具自声明为准）："
            + "、".join(legacy_caps)
        )
    return problems
