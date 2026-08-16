"""/mnt 路径访问：Windows 宿主机目录（WSL 挂载）通过 SSH 在外部 VM 上执行。

当前仅支持 file-copy / file-move（windows-file-copy / windows-file-move）。
写权限白名单由 agent-orchestrator-service 拦截（dashboard「Windows 宿主机访问权限」）。
"""

from __future__ import annotations

import re
import shlex

from app import config
from app.skill_runtime import skill_runtime


def to_mnt_path(raw) -> str:
    """Windows 盘路径转 /mnt 路径；已是 /mnt/ 的原样返回；其余返回空串。"""
    text = str(raw or "").strip().replace("\\", "/")
    if not text:
        return ""
    if text.startswith("/mnt/"):
        return text
    match = re.match(r"^([A-Za-z]):/(.*)$", text, re.DOTALL)
    if match:
        drive = match.group(1).lower()
        rest = match.group(2).strip("/")
        return f"/mnt/{drive}/{rest}"
    return ""


def is_windows_path(raw) -> bool:
    return bool(to_mnt_path(raw))


def ensure_enabled() -> None:
    if not config.CLAW_EXTERNAL_VM_HOST:
        raise RuntimeError("未配置 CLAW_EXTERNAL_VM_HOST，无法访问 /mnt（需外部 VM/WSL）")


def _arg(args, kwargs, names, index: int) -> str:
    if isinstance(names, str):
        names = (names,)
    for name in names:
        value = kwargs.get(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return str(args[index]).strip() if index < len(args) and str(args[index]).strip() else ""


def _q(value: str) -> str:
    return shlex.quote(value)


def _ssh(script: str, timeout: int | None = None) -> str:
    return skill_runtime._run_external_vm_shell(script, timeout=timeout or config.DEFAULT_TIMEOUT_SECONDS)


def copy_or_move(tool: str, args, kwargs, timeout: int) -> str:
    """在外部 VM（WSL）上执行 cp/mv：源或目标任一是 Windows 目录即走 SSH，另一侧 WSL 路径原样透传。"""
    source_raw = _arg(args, kwargs, ("source", "src"), 0)
    target_raw = _arg(args, kwargs, ("target", "dest"), 1)
    if not source_raw or not target_raw:
        return "错误：缺少源路径或目标路径"
    if not (is_windows_path(source_raw) or is_windows_path(target_raw)):
        return "错误：源和目标都不是 Windows 目录（应为 D:\\... 或 /mnt/...）"
    source = to_mnt_path(source_raw) or str(source_raw).strip()
    target = to_mnt_path(target_raw) or str(target_raw).strip()
    verb = "cp -r" if tool == "file-copy" else "mv"
    label = "copied" if tool == "file-copy" else "moved"
    script = f"{verb} {_q(source)} {_q(target)} && echo '{label} {source} -> {target}'"
    return "[mnt] " + _ssh(script, timeout)
