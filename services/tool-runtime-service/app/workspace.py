from __future__ import annotations

from pathlib import Path

from app import config


def workspace_root(requested: str | None = None) -> Path:
    root = Path(requested or config.WORKSPACE_DIR).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def safe_path(root: Path, relative_path: str) -> Path:
    if not relative_path:
        raise ValueError("missing path")

    candidate = (root / relative_path).resolve()
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


def delete_path(root: Path, relative_path: str) -> str:
    path = safe_path(root, relative_path)
    if not path.exists():
        return f"not found: {relative_path}"

    if path.is_dir():
        path.rmdir()
        return f"removed empty directory {path.relative_to(root)}"

    path.unlink()
    return f"removed file {path.relative_to(root)}"
