from __future__ import annotations

import fnmatch
import os
import shutil
from pathlib import Path

from app import config


def workspace_root(requested: str | None = None) -> Path:
    root = Path(requested or config.WORKSPACE_DIR).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _vm_to_container(path: Path) -> Path:
    """把外部 VM 的绝对路径映射回容器内 workspace 路径。

    codex/clawhub 跑在外部 VM 上，返回的路径形如
    /srv/nfs/my-agent/workspace/...（NFS 挂载点），容器内同一目录是
    /app/workspace/...。若 VM 根未配置则原样返回。
    """
    vm_root = (config.CODEX_EXTERNAL_VM_WORKSPACE or "").rstrip("/")
    if not vm_root:
        return path
    text = str(path)
    if text == vm_root:
        return Path(config.WORKSPACE_DIR)
    if text.startswith(vm_root + "/"):
        return Path(config.WORKSPACE_DIR) / text[len(vm_root) + 1:]
    return path


def safe_path(root: Path, relative_path: str) -> Path:
    if not relative_path:
        raise ValueError("missing path")

    raw = str(relative_path).strip()
    if raw.startswith("/"):
        # 绝对路径兼容：
        # 1) 容器内 /app/workspace/... 直接可用；
        # 2) VM 侧 /srv/nfs/my-agent/workspace/... 映射回容器路径；
        # 3) 共享 workspace 内的绝对路径（如 /app/workspace/skill/...）直接可用；
        # 4) 仍不在 workspace 内时，去掉开头 / 按 workspace 相对路径再试。
        candidate = _vm_to_container(Path(raw)).resolve()
        try:
            candidate.relative_to(root)
            return candidate
        except ValueError:
            pass
        workspace = Path(config.WORKSPACE_DIR).resolve()
        try:
            candidate.relative_to(workspace)
            return candidate
        except ValueError:
            pass
        raw = raw.lstrip("/")

    candidate = (root / raw).resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"path escapes workspace: {relative_path}") from exc
    return candidate


def list_workspace(root: Path, max_files: int) -> str:
    rows: list[str] = []

    for index, path in enumerate(sorted(root.rglob("*"))):
        if index >= max_files:
            rows.append(f"... truncated at {max_files} entries")
            break

        rel = path.relative_to(root)
        if path.is_dir():
            rows.append(f"[dir]  {rel}")
        else:
            size = path.stat().st_size
            rows.append(f"[file] {rel} ({size} bytes)")

    return "\n".join(rows) if rows else "(workspace is empty)"


def read_text(root: Path, relative_path: str, max_bytes: int) -> str:
    path = safe_path(root, relative_path)
    if not path.exists():
        raise FileNotFoundError(str(path))
    if not path.is_file():
        raise IsADirectoryError(str(path))

    data = path.read_bytes()[:max_bytes]
    return data.decode("utf-8", errors="replace")


def write_text(root: Path, relative_path: str, text: str) -> str:
    path = safe_path(root, relative_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return f"wrote {path.relative_to(root)} ({len(text.encode('utf-8'))} bytes)"


def write_bytes(root: Path, relative_path: str, data: bytes) -> str:
    path = safe_path(root, relative_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return f"wrote {path.relative_to(root)} ({len(data)} bytes)"


def append_text(root: Path, relative_path: str, text: str) -> str:
    path = safe_path(root, relative_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    data = text.encode("utf-8")
    with path.open("ab") as fh:
        fh.write(data)
    return f"appended {path.relative_to(root)} (+{len(data)} bytes)"


def copy_path(root: Path, source: str, target: str) -> str:
    src = safe_path(root, source)
    dst = safe_path(root, target)
    if not src.exists():
        raise FileNotFoundError(str(src))
    if dst.exists():
        raise FileExistsError(str(dst))
    if src.is_dir() and (dst == src or src in dst.parents):
        raise ValueError("cannot copy directory into itself")
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(src, dst)
        return f"copied directory {src.relative_to(root)} -> {dst.relative_to(root)}"
    shutil.copy2(src, dst)
    return f"copied {src.relative_to(root)} -> {dst.relative_to(root)} ({src.stat().st_size} bytes)"


def move_path(root: Path, source: str, target: str) -> str:
    src = safe_path(root, source)
    dst = safe_path(root, target)
    if not src.exists():
        raise FileNotFoundError(str(src))
    if dst.exists():
        raise FileExistsError(str(dst))
    if src.is_dir() and (dst == src or src in dst.parents):
        raise ValueError("cannot move directory into itself")
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dst))
    return f"moved {src.relative_to(root)} -> {dst.relative_to(root)}"


def tail_text(root: Path, relative_path: str, num_lines: int) -> str:
    path = safe_path(root, relative_path)
    if not path.exists():
        raise FileNotFoundError(str(path))
    if not path.is_file():
        raise IsADirectoryError(str(path))

    num_lines = max(1, min(int(num_lines), 10000))
    with path.open("rb") as fh:
        fh.seek(0, os.SEEK_END)
        size = fh.tell()
        chunk = b""
        offset = size
        newlines = 0
        read_size = 8192
        while offset > 0 and newlines <= num_lines:
            offset = max(0, offset - read_size)
            fh.seek(offset)
            buf = fh.read(read_size)
            chunk = buf + chunk
            newlines += buf.count(b"\n")

    lines = chunk.decode("utf-8", errors="replace").splitlines()
    if len(lines) > num_lines:
        lines = lines[-num_lines:]
    return "\n".join(lines) if lines else "(empty file)"


def search_text(
    root: Path,
    pattern: str,
    relative_path: str,
    max_results: int,
    case_sensitive: bool,
) -> str:
    import re

    if not pattern:
        raise ValueError("missing search pattern")
    base = safe_path(root, relative_path) if relative_path else root
    if not base.exists():
        raise FileNotFoundError(str(base))
    try:
        regex = re.compile(pattern, 0 if case_sensitive else re.IGNORECASE)
    except re.error as exc:
        raise ValueError(f"invalid pattern: {exc}") from exc

    if base.is_file():
        targets = [base]
    else:
        targets = sorted(base.rglob("*"))

    max_results = max(1, min(int(max_results), 200))
    hits: list[str] = []
    for path in targets:
        if not path.is_file():
            continue
        try:
            if path.stat().st_size > config.MAX_READ_BYTES:
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            if regex.search(line):
                hits.append(f"{path.relative_to(root)}:{lineno}: {line.strip()[:200]}")
                if len(hits) >= max_results:
                    break
        if len(hits) >= max_results:
            break

    if not hits:
        return f"no matches for {pattern!r}"
    return "\n".join(hits) + f"\n[search done, {len(hits)} matches]"


def list_dir(root: Path, relative_path: str, pattern: str, recursive: bool) -> str:
    base = safe_path(root, relative_path) if relative_path else root
    if not base.exists():
        raise FileNotFoundError(str(base))
    if not base.is_dir():
        raise NotADirectoryError(str(base))

    iterator = sorted(base.rglob("*")) if recursive else sorted(base.iterdir())
    rows: list[str] = []
    for path in iterator:
        rel = path.relative_to(base)
        if pattern and not (fnmatch.fnmatch(str(rel), pattern) or fnmatch.fnmatch(path.name, pattern)):
            continue
        if path.is_dir():
            rows.append(f"[dir]  {rel}/")
        else:
            rows.append(f"[file] {rel} ({path.stat().st_size} bytes)")
    if not rows:
        rel_display = base.relative_to(root) if base != root else "."
        return f"(no entries in {rel_display})"
    return "\n".join(rows)


def delete_path(root: Path, relative_path: str) -> str:
    path = safe_path(root, relative_path)
    if not path.exists():
        return f"not found: {relative_path}"

    if path.is_dir():
        path.rmdir()
        return f"removed empty directory {path.relative_to(root)}"

    path.unlink()
    return f"removed file {path.relative_to(root)}"


def extract_zip(root: Path, zip_relative_path: str, target_relative_path: str) -> str:
    import zipfile

    src = safe_path(root, zip_relative_path)
    if not src.exists() or not src.is_file():
        raise FileNotFoundError(str(src))
    if not zipfile.is_zipfile(src):
        raise ValueError(f"not a zip file: {zip_relative_path}")

    if target_relative_path:
        dst = safe_path(root, target_relative_path)
    else:
        dst = safe_path(root, str((src.parent / src.stem).relative_to(root)))

    with zipfile.ZipFile(src) as zf:
        entries = zf.infolist()
        for member in entries:
            target = (dst / member.filename).resolve()
            if target != dst and dst not in target.parents:
                raise ValueError(f"zip entry escapes target directory: {member.filename}")
        zf.extractall(dst)
    return f"extracted {len(entries)} entries -> {dst.relative_to(root)}"
