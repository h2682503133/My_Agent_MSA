"""单机（无 tool-runtime）时的工作空间文件操作本地实现。

为什么需要：网页的「工作空间」上传/列目录/读文件/删除全部走 tool-runtime 的
gRPC 接口。最小部署档（core + memory，不启动 tool-runtime）里没有这个服务，
上传就会失败（DNS 解析 k8s 名字）。

所以当 `GATEWAY_WORKSPACE_LOCAL=1`（由单机启动器在不含 tool-runtime 时注入）时，
gateway 直接用 pathlib 在 `WORKSPACE_USERS_BASE/<user_id>` 下做同样的操作，
返回结构与 tool_client 保持一致：{"ok", "output", "error"}。
"""

from __future__ import annotations

import base64
import os
from pathlib import Path

MAX_TEXT_BYTES = 2 * 1024 * 1024          # 文本预览上限
MAX_RAW_BYTES = 32 * 1024 * 1024          # 原始文件上限（图片等）


def enabled() -> bool:
    return (os.getenv("GATEWAY_WORKSPACE_LOCAL") or "").strip().lower() in {"1", "true", "yes", "on"}


def _root(workspace_dir: str) -> Path:
    return Path(workspace_dir).resolve()


def _target(workspace_dir: str, rel: str) -> tuple[Path | None, str]:
    root = _root(workspace_dir)
    cleaned = (rel or "").replace("\\", "/").strip("/")
    candidate = (root / cleaned).resolve() if cleaned else root
    try:
        candidate.relative_to(root)
    except ValueError:
        return None, "路径越界"
    return candidate, ""


def _human_size(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size}B"


def list_dir(workspace_dir: str, rel: str = "") -> dict:
    """按 tool-runtime 的输出格式返回：`[dir] name` / `[file] name size`。"""
    target, err = _target(workspace_dir, rel)
    if target is None:
        return {"ok": False, "output": "", "error": err}
    if not target.exists():
        return {"ok": True, "output": "", "error": ""}
    if not target.is_dir():
        return {"ok": False, "output": "", "error": "不是目录"}

    lines: list[str] = []
    try:
        entries = sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name.lower()))
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "output": "", "error": str(exc)}
    for entry in entries:
        if entry.name.startswith("."):
            continue
        if entry.is_dir():
            lines.append(f"[dir] {entry.name}")
        else:
            try:
                size = entry.stat().st_size
            except Exception:  # noqa: BLE001
                size = 0
            # 必须与 tool-runtime 的输出格式一致：gateway 用 "[file] 名字 (N bytes)" 解析
            lines.append(f"[file] {entry.name} ({size} bytes)")
    return {"ok": True, "output": "\n".join(lines), "error": ""}


def read_file(workspace_dir: str, rel: str, limit: int = MAX_TEXT_BYTES) -> dict:
    target, err = _target(workspace_dir, rel)
    if target is None:
        return {"ok": False, "output": "", "error": err}
    if not target.is_file():
        return {"ok": False, "output": "", "error": "文件不存在"}
    try:
        data = target.read_bytes()
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "output": "", "error": str(exc)}
    if len(data) > limit:
        return {"ok": False, "output": "", "error": f"文件过大（{_human_size(len(data))}）"}
    try:
        return {"ok": True, "output": data.decode("utf-8"), "error": ""}
    except UnicodeDecodeError:
        # 二进制：按 base64 回传，保证 /raw 能还原图片
        return {"ok": True, "output": base64.b64encode(data).decode("ascii"), "error": "", "binary": True}


def write_text(workspace_dir: str, rel: str, text: str) -> dict:
    target, err = _target(workspace_dir, rel)
    if target is None:
        return {"ok": False, "output": "", "error": err}
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text or "", encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "output": "", "error": str(exc)}
    return {"ok": True, "output": f"已写入 {rel}（{len(text or '')} 字符）", "error": ""}


def write_bytes(workspace_dir: str, rel: str, data: bytes) -> dict:
    target, err = _target(workspace_dir, rel)
    if target is None:
        return {"ok": False, "output": "", "error": err}
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "output": "", "error": str(exc)}
    return {"ok": True, "output": f"已上传 {rel}（{len(data)} bytes）", "error": ""}


def delete(workspace_dir: str, rel: str) -> dict:
    target, err = _target(workspace_dir, rel)
    if target is None:
        return {"ok": False, "output": "", "error": err}
    try:
        if target.is_dir():
            if any(target.iterdir()):
                return {"ok": False, "output": "", "error": "目录非空"}
            target.rmdir()
        elif target.is_file():
            target.unlink()
        else:
            return {"ok": False, "output": "", "error": "不存在"}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "output": "", "error": str(exc)}
    return {"ok": True, "output": f"已删除 {rel}", "error": ""}
