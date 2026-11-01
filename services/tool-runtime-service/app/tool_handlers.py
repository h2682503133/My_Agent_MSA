"""原生工具实现（从 server._dispatch 的 if 链搬到独立 handler）。

约定
====
- 每个 handler 签名 `(ctx: ToolContext) -> str`，参数读取统一用 `ctx.first/positional/flag/int_arg`；
- 需要用到 ToolRuntimeService 上的方法（`_download` / `_image_gen` / `_run_shell` …）时走 `ctx.service`；
- 环境差异化只出现在 `file-copy` / `file-move`：由上层 `TOOL_PROVIDERS` 选 provider：
    * `local`     —— 直接本地文件系统（单体 Windows：盘符即本地）
    * `host-ssh`  —— 宿主在外部 VM/WSL，经 SSH 流式传输（k8s 默认）
- 技能相关工具不在这里（见 `skill_handlers.py`）。
"""

from __future__ import annotations

import base64

from app import config, fetch_tools, mnt_access
from app.process_runtime import init as process_init
from app.process_runtime import modify as process_modify
from app.process_runtime import remove as process_remove
from app.process_runtime import var_list as process_var_list
from app.process_runtime import var_remove as process_var_remove
from app.process_runtime import write as process_write
from app.tool_registry import ToolContext, register
from app.web_search import web_search
from app.worldinfo_runtime import write as worldinfo_write
from app.workspace import (
    append_text,
    copy_path,
    delete_path,
    extract_zip,
    list_dir,
    list_workspace,
    move_path,
    read_text,
    search_text,
    tail_text,
    write_bytes,
    write_text,
)


# ─── 基础 ──────────────────────────────────────────────────────────────

def _echo(ctx: ToolContext) -> str:
    return ctx.kwargs.get("text") or " ".join(ctx.args)


register("echo", _echo)


def _help(ctx: ToolContext) -> str:
    return ctx.service._help() if ctx.service is not None else ""


# 内部命令：不进 TOOL.json（模型不需要，但保留旧行为）
register("help", _help)


# ─── 工作区文件 ─────────────────────────────────────────────────────────

def _list_workspace(ctx: ToolContext) -> str:
    return list_workspace(ctx.root, config.MAX_LIST_FILES)


def _dir_list(ctx: ToolContext) -> str:
    rel = ctx.kwargs.get("path") or ctx.positional(0)
    pattern = ctx.kwargs.get("pattern") or ctx.kwargs.get("glob") or ctx.positional(1)
    recursive = str(ctx.kwargs.get("recursive", "true")).lower() not in {"0", "false", "no"}
    return list_dir(ctx.root, rel, pattern, recursive)


def _file_read(ctx: ToolContext) -> str:
    rel = ctx.kwargs.get("path") or ctx.positional(0)
    return read_text(ctx.root, rel, config.MAX_READ_BYTES)


def _file_write(ctx: ToolContext) -> str:
    rel = ctx.kwargs.get("path") or ctx.positional(0)
    text = ctx.kwargs.get("text") or ctx.positional(1)
    return write_text(ctx.root, rel, text)


def _file_append(ctx: ToolContext) -> str:
    rel = ctx.kwargs.get("path") or ctx.positional(0)
    text = ctx.kwargs.get("text") or ctx.positional(1)
    return append_text(ctx.root, rel, text)


def _file_tail(ctx: ToolContext) -> str:
    rel = ctx.kwargs.get("path") or ctx.positional(0)
    num_lines = ctx.int_arg(("lines", "num_lines"), 1, 50)
    return tail_text(ctx.root, rel, num_lines)


def _file_search(ctx: ToolContext) -> str:
    pattern = ctx.first("pattern", "keyword", "query", index=0)
    rel = ctx.kwargs.get("path") or ctx.positional(1)
    limit = ctx.int_arg(("limit", "max_results"), 2, 30)
    case_sensitive = str(ctx.kwargs.get("case_sensitive", "")).lower() in {"1", "true", "yes"}
    return search_text(ctx.root, pattern, rel, limit, case_sensitive)


def _delete_file(ctx: ToolContext) -> str:
    rel = ctx.kwargs.get("path") or ctx.positional(0)
    return delete_path(ctx.root, rel)


def _unzip(ctx: ToolContext) -> str:
    zip_rel = ctx.first("path", "zip", index=0)
    target_rel = ctx.first("target", "dest", index=1)
    return extract_zip(ctx.root, zip_rel, target_rel)


def _file_upload(ctx: ToolContext) -> str:
    rel = ctx.kwargs.get("path") or ctx.positional(0)
    data_b64 = ctx.kwargs.get("data") or ctx.positional(1)
    if not data_b64:
        raise ValueError("missing data (base64)")
    data = base64.b64decode(data_b64)
    if len(data) > config.MAX_UPLOAD_BYTES:
        raise ValueError(f"file too large: {len(data)} bytes > max {config.MAX_UPLOAD_BYTES} bytes")
    return write_bytes(ctx.root, rel, data)


for _name, _fn in (
    ("list-workspace", _list_workspace),
    ("dir-list", _dir_list),
    ("file-read", _file_read),
    ("file-write", _file_write),
    ("file-append", _file_append),
    ("file-tail", _file_tail),
    ("file-search", _file_search),
    ("delete-file", _delete_file),
    ("unzip", _unzip),
    ("file-upload", _file_upload),
):
    register(_name, _fn)


# ─── 拷贝/移动：环境差异化（本地 vs 宿主 SSH）─────────────────────────────

def _copy_local(ctx: ToolContext) -> str:
    source = ctx.first("source", "src", index=0)
    target = ctx.first("target", "dest", index=1)
    return copy_path(ctx.root, source, target)


def _move_local(ctx: ToolContext) -> str:
    source = ctx.first("source", "src", index=0)
    target = ctx.first("target", "dest", index=1)
    return move_path(ctx.root, source, target)


def _copy_host_ssh(ctx: ToolContext) -> str:
    return mnt_access.copy_or_move("file-copy", ctx.args, ctx.kwargs, ctx.timeout)


def _move_host_ssh(ctx: ToolContext) -> str:
    return mnt_access.copy_or_move("file-move", ctx.args, ctx.kwargs, ctx.timeout)


def _windows_copy(ctx: ToolContext) -> str:
    """显式宿主工具：总是走宿主通道（单体下 mnt_access 自己就是本地实现）。"""
    return mnt_access.copy_or_move("file-copy", ctx.args, ctx.kwargs, ctx.timeout)


def _windows_move(ctx: ToolContext) -> str:
    return mnt_access.copy_or_move("file-move", ctx.args, ctx.kwargs, ctx.timeout)


COPY_PROVIDERS = {"local": _copy_local, "host-ssh": _copy_host_ssh}
MOVE_PROVIDERS = {"local": _move_local, "host-ssh": _move_host_ssh}

register("file-copy", providers=COPY_PROVIDERS)
register("file-move", providers=MOVE_PROVIDERS)
register("windows-file-copy", _windows_copy)
register("windows-file-move", _windows_move)


# ─── 网络 ──────────────────────────────────────────────────────────────

def _fetch(ctx: ToolContext) -> str:
    url = ctx.kwargs.get("url") or ctx.positional(0)
    method = (ctx.kwargs.get("method") or ctx.positional(1) or "GET").upper()
    data = ctx.kwargs.get("data") or ctx.positional(2)

    # 兼容「fetch|url|搜索词」：漏写 method 时把第 2 参当 data
    if (
        method not in fetch_tools.VALID_HTTP_METHODS
        and not data
        and len(ctx.args) > 1
        and not ctx.kwargs.get("method")
    ):
        data = ctx.args[1]
        method = "GET"

    return fetch_tools.fetch(url=url, method=method, data=data, timeout=ctx.timeout)


def _download(ctx: ToolContext) -> str:
    return ctx.service._download(args=ctx.args, kwargs=ctx.kwargs, root=ctx.root, timeout=ctx.timeout)


def _web_search(ctx: ToolContext) -> str:
    query = ctx.first("query", "keyword", index=0)
    limit = (
        ctx.kwargs.get("limit")
        or ctx.kwargs.get("count")
        or ctx.kwargs.get("max_results")
        or ctx.positional(1)
    )
    return web_search(query=query, limit=limit, timeout=ctx.timeout)


register("fetch", _fetch)
register("download", _download)
register("web-search", _web_search)


def _codex(ctx: ToolContext) -> str:
    return ctx.service._codex(args=ctx.args, kwargs=ctx.kwargs, root=ctx.root, timeout=ctx.timeout)


register("codex", _codex, requires=("codex",),
         reason="错误：Codex 未启用。部署 tool-runtime 时未勾选「codex 代码生成」功能，无法调用 codex。")


def _port_expose(ctx: ToolContext) -> str:
    port = ctx.kwargs.get("port") or ctx.positional(0)
    return ctx.service._port_expose(port)


register("port-expose", _port_expose)


# ─── 图片 ──────────────────────────────────────────────────────────────

def _get_image_url_from_local(ctx: ToolContext) -> str:
    local_path = ctx.first("path", "local_path", index=0)
    return ctx.service._get_image_url_from_local(local_path=local_path, root=ctx.root)


def _send_image_by_url(ctx: ToolContext) -> str:
    image_url = ctx.first("url", "image_url", index=0)
    return ctx.service._send_image_by_url(image_url=image_url)


def _image_view(ctx: ToolContext) -> str:
    target = ctx.first("path", "url", "image", index=0)
    hint = str(ctx.kwargs.get("hint") or ctx.kwargs.get("focus") or ctx.kwargs.get("需求方向") or "").strip()
    if not hint:
        hint = ctx.positional(1)
    return ctx.service._image_view(target=target, root=ctx.root, hint=hint)


def _image_gen(ctx: ToolContext) -> str:
    prompt = ctx.first("prompt", "text", "提示词", index=0)
    size = str(ctx.kwargs.get("size") or ctx.positional(1)).strip()
    profile = str(ctx.kwargs.get("model") or ctx.kwargs.get("profile") or ctx.positional(2)).strip() or "image-gen"
    return ctx.service._image_gen(prompt=prompt, size=size, model_profile=profile, root=ctx.root)


register("get-image-url-from-local", _get_image_url_from_local)
register("send-image-by-url", _send_image_by_url)
register("image-view", _image_view, aliases=("view-image",))
register("image-gen", _image_gen, aliases=("text-to-image", "draw"))


# ─── 消息 / 表情 ────────────────────────────────────────────────────────

def _send_message(ctx: ToolContext) -> str:
    text = ctx.first("text", "message", index=0)
    return ctx.service._send_message(text)


register("send-message", _send_message, aliases=("notify",))


def _reply_image(ctx: ToolContext) -> str:
    from app.reply_image_tool import handle as reply_image_handle
    from pathlib import Path

    return reply_image_handle(
        Path(config.REPLY_IMAGES_DIR),
        str(ctx.kwargs.get("agent_id") or "main"),
        list(ctx.args),
        dict(ctx.kwargs),
        user_id=str(ctx.kwargs.get("user_id") or "default"),
        workspace_root=ctx.root,
        base_url=config.REPLY_IMAGE_BASE_URL,
        asset_dir=Path(config.IMAGE_ASSET_DIR),
    )


register("reply-image", _reply_image, aliases=("reply_image",))


# ─── 浏览器 ────────────────────────────────────────────────────────────

def _browser(ctx: ToolContext) -> str:
    action = str(ctx.positional(0) or ctx.kwargs.get("action") or "").strip()
    arg1 = str(ctx.positional(1) or ctx.kwargs.get("arg1") or "").strip()
    arg2 = str(ctx.positional(2) or ctx.kwargs.get("arg2") or "")
    # 允许 `browser-click|#3` 这种把动作写在工具名里的简写
    if ctx.name != "browser" and not action:
        action = ctx.name.split("-", 1)[1]

    mcp_url = str(getattr(config, "BROWSER_MCP_URL", "") or "").strip()
    if mcp_url:
        from app.browser_mcp import run as mcp_run

        return mcp_run(action, arg1, arg2, ctx.root, url=mcp_url)

    from app.browser_tools import browser_tools

    return browser_tools.run(action, arg1, arg2, ctx.root)


register("browser", _browser, aliases=("browser-open", "browser-click", "browser-type"))


# ─── 黑名单自助 ─────────────────────────────────────────────────────────

def _tool_blacklist(ctx: ToolContext) -> str:
    from app.tool_blacklist import handle as blacklist_handle

    return blacklist_handle(list(ctx.args), dict(ctx.kwargs), str(ctx.kwargs.get("user_id") or "default"))


register("tool-blacklist", _tool_blacklist, aliases=("blacklist",))


# ─── PROCESS（长期事件记录）─────────────────────────────────────────────

def _process_write(ctx: ToolContext) -> str:
    return process_write(
        config.PROCESS_DIR,
        ctx.kwargs.get("user_id") or "default",
        ctx.kwargs.get("agent_id") or "main",
        ctx.kwargs.get("index") or ctx.positional(0),
        ctx.kwargs.get("title") or ctx.positional(1),
        ctx.kwargs.get("content") or ctx.kwargs.get("text") or ctx.positional(2),
        max_items=int(ctx.kwargs.get("process_max_items") or 0),
        max_chars=int(ctx.kwargs.get("process_max_chars") or 0),
    )


def _process_remove(ctx: ToolContext) -> str:
    return process_remove(
        config.PROCESS_DIR,
        ctx.kwargs.get("user_id") or "default",
        ctx.kwargs.get("agent_id") or "main",
        ctx.kwargs.get("index") or ctx.positional(0),
    )


def _process_init(ctx: ToolContext) -> str:
    return process_init(
        config.PROCESS_DIR,
        ctx.kwargs.get("user_id") or "default",
        ctx.kwargs.get("agent_id") or "main",
    )


def _process_modify(ctx: ToolContext) -> str:
    return process_modify(
        config.PROCESS_DIR,
        ctx.kwargs.get("user_id") or "default",
        ctx.kwargs.get("agent_id") or "main",
        ctx.kwargs.get("key") or ctx.positional(0),
        ctx.kwargs.get("op") or ctx.kwargs.get("value") or ctx.positional(1),
    )


def _process_var_remove(ctx: ToolContext) -> str:
    return process_var_remove(
        config.PROCESS_DIR,
        ctx.kwargs.get("user_id") or "default",
        ctx.kwargs.get("agent_id") or "main",
        ctx.kwargs.get("key") or ctx.positional(0),
    )


def _process_var_list(ctx: ToolContext) -> str:
    return process_var_list(
        config.PROCESS_DIR,
        ctx.kwargs.get("user_id") or "default",
        ctx.kwargs.get("agent_id") or "main",
    )


register("process-write", _process_write)
register("process-remove", _process_remove)
register("process-init", _process_init)
register("process-modify", _process_modify)
register("process-var-remove", _process_var_remove)
register("process-var-list", _process_var_list)


# ─── 世界书 ────────────────────────────────────────────────────────────

def _worldinfo_write(ctx: ToolContext) -> str:
    return worldinfo_write(
        config.WORLD_INFO_PATH,
        ctx.kwargs.get("agent_id") or "main",
        ctx.kwargs.get("keys") or ctx.positional(0),
        ctx.kwargs.get("content") or ctx.kwargs.get("text") or ctx.positional(1),
        ctx.kwargs.get("priority") or ctx.positional(2, "0"),
        ctx.kwargs.get("scope") or ctx.positional(3),
        ctx.kwargs.get("constant") or ctx.positional(4, "false"),
        ctx.kwargs.get("regex") or ctx.positional(5, "false"),
        ctx.kwargs.get("match_mode") or ctx.positional(6, "or"),
        ctx.kwargs.get("exclude") or ctx.positional(7),
    )


register("worldinfo-write", _worldinfo_write)


# ─── Shell ─────────────────────────────────────────────────────────────

def _shell(ctx: ToolContext) -> str:
    return ctx.service._run_shell(args=ctx.args, kwargs=ctx.kwargs, root=ctx.root, timeout=ctx.timeout)


register("shell", _shell, aliases=("run-shell", "command"), requires=("shell",))
