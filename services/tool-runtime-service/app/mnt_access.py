"""/mnt 路径访问：Windows 宿主机目录（WSL 挂载）通过 SSH 在外部 VM 上执行。

设计要点
========
- `/mnt/c/...`、`/mnt/d/...` 这类路径**不在本容器内**，必须经 SSH 在外部 VM/WSL 上操作；
- 拷贝/移动要区分"两侧在哪"：
  * 两端都在宿主（`/mnt/...`）→ 远端 `cp`/`mv`；
  * 宿主 → 容器 → 远端 `cat` 读字节，本地落盘；
  * 容器 → 宿主 → 本地读字节，远�� `cat >` 写入；
  * 两端都在容器 → 本地 `shutil`。
  早先的实现把 `cp 源 目标` 整条丢给远端，一旦目标在容器里就必然
  `cp: cannot create regular file '/app/workspace/...': No such file or directory`，
  所以「宿主 ⇄ 工作区」这条最常用的路是**走不通**的。
- 写权限白名单由 agent-orchestrator-service 拦截（dashboard「Windows 宿主机访问权限」）。
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

from app import config
from app.skill_runtime import skill_runtime


def to_mnt_path(raw) -> str:
    """Windows 盘路径转 /mnt 路径；已是 /mnt/ 的原样返回；其余返回空串。"""
    text = str(raw or "").strip().replace("\\", "/")
    if not text:
        return ""
    if text.startswith("/mnt/"):
        return text
    match = re.match(r"^([A-Za-z]):(/.*)?$", text)
    if match:
        drive = match.group(1).lower()
        rest = (match.group(2) or "").lstrip("/")
        return f"/mnt/{drive}/{rest}" if rest else f"/mnt/{drive}"
    return ""


def from_mnt_path(raw) -> str:
    """`/mnt/<drive>/...` 转回 Windows 盘路径（`D:\\...`）；不是该形式返回空串。"""
    text = str(raw or "").strip()
    match = re.match(r"^/mnt/([A-Za-z])(?:/(.*))?$", text)
    if not match:
        return ""
    drive = match.group(1).upper()
    rest = (match.group(2) or "").replace("/", "\\")
    return f"{drive}:\\{rest}" if rest else f"{drive}:\\"


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


def _ssh_popen(script: str, stdin_bytes: bytes | None = None, timeout: int | None = None):
    """跑一条远端命令，可把本地字节喂给它的 stdin，并取回 stdout 字节。"""
    cmd = skill_runtime._ssh_base_command() + [script]
    return subprocess.run(
        cmd,
        input=stdin_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout or config.DEFAULT_TIMEOUT_SECONDS,
    )


# 平台语义：Linux 容器里 `/mnt/x` 才是"宿主目录"；Windows 主机上 `D:\x` 就是本地路径。
# 不加这条判断，单体（Windows）里 `D:\work\a.txt` 会被误当成宿主路径去 SSH。
IS_WINDOWS_HOST = os.name == "nt"


def _is_remote(path: str) -> bool:
    if IS_WINDOWS_HOST:
        return False
    return str(path or "").startswith("/mnt/")


def _remote_to_local(source: str, target: str, timeout: int) -> str:
    proc = _ssh_popen(f"cat {_q(source)}", timeout=timeout)
    if proc.returncode != 0:
        stderr = (proc.stderr or b"").decode("utf-8", "replace").strip()
        return f"[mnt] 读取宿主文件失败：{stderr or proc.returncode}"
    data = proc.stdout or b""
    dest = Path(target)
    if dest.is_dir():
        dest = dest / Path(source).name
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return f"[mnt] copied {source} -> {dest}（{len(data)} bytes）"


def _local_to_remote(source: str, target: str, timeout: int) -> str:
    src = Path(source)
    if not src.is_file():
        return f"[mnt] 源文件不存在或不是文件：{source}"
    data = src.read_bytes()
    proc = _ssh_popen(f"cat > {_q(target)}", stdin_bytes=data, timeout=timeout)
    if proc.returncode != 0:
        stderr = (proc.stderr or b"").decode("utf-8", "replace").strip()
        return f"[mnt] 写入宿主文件失败：{stderr or proc.returncode}"
    return f"[mnt] copied {src} -> {target}（{len(data)} bytes）"


def _remove_source(source: str, remote: bool, timeout: int) -> str:
    if remote:
        out = _ssh(f"rm -rf {_q(source)} && echo removed", timeout=timeout)
        return "" if "removed" in out else out
    try:
        path = Path(source)
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
    except Exception as exc:  # noqa: BLE001
        return f"[mnt] 删除源失败：{exc}"
    return ""



def _guard() -> str:
    """需要访问 /mnt 时的前置检查：未配置外部 VM 就返回一句明确错误。"""
    try:
        ensure_enabled()
    except RuntimeError as exc:
        return f"错误：{exc}"
    return ""


def copy_or_move(tool: str, args, kwargs, timeout: int) -> str:
    """拷贝/移动：按平台与两侧位置选本地 / 远端 / 流式传输。

    - Windows 主机（单体）：盘符路径就是本地路径，全部走本地文件操作，不碰 SSH；
    - Linux 容器：`/mnt/...` 是宿主（经 SSH），其余是容器内路径。
    """
    source_raw = _arg(args, kwargs, ("source", "src"), 0)
    target_raw = _arg(args, kwargs, ("target", "dest"), 1)
    if not source_raw or not target_raw:
        return "错误：缺少源路径或目标路径"

    label = "copied" if tool == "file-copy" else "moved"

    # 0) 单体（Windows）：盘符即本地
    if IS_WINDOWS_HOST:
        try:
            src, dst = Path(source_raw), Path(target_raw)
            dst.parent.mkdir(parents=True, exist_ok=True)
            if tool == "file-copy":
                if src.is_dir():
                    shutil.copytree(src, dst, dirs_exist_ok=True)
                else:
                    shutil.copy2(src, dst)
            else:
                shutil.move(str(src), str(dst))
        except Exception as exc:  # noqa: BLE001
            return f"错误：{exc}"
        return f"{label} {source_raw} -> {target_raw}"

    source = to_mnt_path(source_raw) or source_raw
    target = to_mnt_path(target_raw) or target_raw
    src_remote = _is_remote(source)
    dst_remote = _is_remote(target)

    # 1) 两端都在宿主：远端 cp/mv
    if src_remote and dst_remote:
        problem = _guard()
        if problem:
            return problem
        verb = "cp -r" if tool == "file-copy" else "mv"
        script = f"{verb} {_q(source)} {_q(target)} && echo '{label} {source} -> {target}'"
        return "[mnt] " + _ssh(script, timeout)

    # 2) 宿主 → 容器：流式取回
    if src_remote and not dst_remote:
        problem = _guard()
        if problem:
            return problem
        result = _remote_to_local(source, target, timeout)
        if result.startswith("[mnt] copied") and tool != "file-copy":
            err = _remove_source(source, True, timeout)
            return result + (f"（源删除失败：{err}）" if err else "（已删除源）")
        return result

    # 3) 容器 → 宿主：流式写入
    if dst_remote and not src_remote:
        problem = _guard()
        if problem:
            return problem
        result = _local_to_remote(source, target, timeout)
        if result.startswith("[mnt] copied") and tool != "file-copy":
            err = _remove_source(source, False, timeout)
            return result + (f"（源删除失败：{err}）" if err else "（已删除源）")
        return result

# 4) 两端都在容器：本地操作
    try:
        src, dst = Path(source), Path(target)
        dst.parent.mkdir(parents=True, exist_ok=True)
        if tool == "file-copy":
            if src.is_dir():
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                shutil.copy2(src, dst)
        else:
            shutil.move(str(src), str(dst))
    except Exception as exc:  # noqa: BLE001
        return f"错误：{exc}"
    return f"{label} {source} -> {target}"
