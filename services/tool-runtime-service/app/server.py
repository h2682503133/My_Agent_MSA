from __future__ import annotations

import mimetypes
import os
import shlex
import shutil
import subprocess
import sys
import urllib.parse
import uuid
from concurrent import futures
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import grpc
import requests
from bs4 import BeautifulSoup

from app import config
from app.logger import log, debug
from app.skill_runtime import skill_runtime
from app.workspace import (
    append_text,
    copy_path,
    delete_path,
    extract_zip,
    list_dir,
    list_workspace,
    move_path,
    read_text,
    safe_path,
    search_text,
    tail_text,
    workspace_root,
    write_text,
)

GENERATED_DIR = Path(__file__).parent / "generated"
if str(GENERATED_DIR) not in sys.path:
    sys.path.insert(0, str(GENERATED_DIR))

try:
    import tool_runtime_pb2
    import tool_runtime_pb2_grpc
except ImportError as exc:
    raise RuntimeError("gRPC generated files not found. Run bash scripts/gen_proto.sh first.") from exc


@dataclass
class ToolResult:
    """工具执行结果。

    output:    工具最终输出，会回传给模型。
    artifacts: 随 done 事件返回的产物（保持旧的最终送达语义）。
    events:    执行过程中要即时推送给用户的事件（message/image）。
    """

    output: str = ""
    artifacts: list[dict[str, str]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)


class ToolRuntimeService(tool_runtime_pb2_grpc.ToolRuntimeServicer):
    def ExecuteTool(self, request, context):
        task_id = request.task_id or "-"
        tool_name = (request.tool_name or request.skill_name or "").strip()
        kwargs = dict(request.kwargs)
        args = list(request.args)
        timeout = request.timeout_seconds or config.DEFAULT_TIMEOUT_SECONDS

        try:
            root = workspace_root(request.workspace_dir or config.WORKSPACE_DIR)
            debug(f"ExecuteTool task_id={task_id} tool={tool_name} workspace={root}")

            result = self._dispatch(
                tool_name=tool_name,
                args=args,
                kwargs=kwargs,
                root=root,
                timeout=timeout,
            )
            output, artifacts, events = self._normalize_result(result)

            # 执行过程中产生的即时事件（message/image）先推送给调用方，
            # 调用方会立即转发给用户；最后的 done 事件携带最终结果。
            for event in events:
                event_type = str(event.get("type", "message"))
                if event_type == "image":
                    artifact = event.get("artifact") or {}
                    yield tool_runtime_pb2.ExecuteToolEvent(
                        event_type="image",
                        text=str(event.get("text", "")),
                        artifact=tool_runtime_pb2.Artifact(
                            type=str(artifact.get("type", "image")),
                            local_path=str(artifact.get("local_path", "")),
                            asset_url=str(artifact.get("asset_url", "")),
                        ),
                    )
                else:
                    yield tool_runtime_pb2.ExecuteToolEvent(
                        event_type="message",
                        text=str(event.get("text", "")),
                    )

            yield tool_runtime_pb2.ExecuteToolEvent(
                event_type="done",
                output=output,
                artifacts=[
                    tool_runtime_pb2.Artifact(
                        type=str(artifact.get("type", "")),
                        local_path=str(artifact.get("local_path", "")),
                        asset_url=str(artifact.get("asset_url", "")),
                    )
                    for artifact in artifacts
                ],
                logs=f"tool {tool_name} finished",
                error="",
            )

        except Exception as exc:
            log(f"ExecuteTool failed task_id={task_id} tool={tool_name}: {exc}")
            yield tool_runtime_pb2.ExecuteToolEvent(
                event_type="done",
                output="",
                logs="",
                error=str(exc),
            )

    @staticmethod
    def _normalize_result(result: Any) -> tuple[str, list[dict[str, str]], list[dict[str, Any]]]:
        if isinstance(result, ToolResult):
            return (
                str(result.output or ""),
                list(result.artifacts or []),
                list(result.events or []),
            )
        if isinstance(result, tuple) and len(result) == 2:
            output, artifacts = result
            return str(output or ""), list(artifacts or []), []
        return str(result or ""), [], []

    @staticmethod
    def _first_arg(args: list[str], kwargs: dict[str, str], *names: str, default: str = "") -> str:
        for name in names:
            value = kwargs.get(name)
            if value is not None and value != "":
                return str(value)
        return str(args[0]) if args else default

    @staticmethod
    def _safe_workspace_path(root: Path, path: str) -> Path:
        if not path:
            raise ValueError("path is required")
        # 与 workspace.safe_path 保持一致：支持容器内绝对路径、
        # VM 侧绝对路径映射、以及 / 开头的 workspace 相对路径兼容。
        return safe_path(root, path)

    def _dispatch(self, tool_name: str, args: list[str], kwargs: dict[str, str], root: Path, timeout: int):
        # 与原项目 core/Agent/Tool_manager.py 保持一致：
        # 工具名使用 OpenClaw/ClawHub 风格的短横线命名，并按原名精确分发。
        name = (tool_name or "").strip()

        if name in {"", "help"}:
            return self._help()

        if name == "echo":
            return kwargs.get("text") or " ".join(args)

        if name == "list-workspace":
            return list_workspace(root, config.MAX_LIST_FILES)

        if name == "dir-list":
            rel = kwargs.get("path") or (args[0] if args else "")
            pattern = kwargs.get("pattern") or kwargs.get("glob") or (args[1] if len(args) > 1 else "")
            # 默认递归列出；传 recursive=false 可只看当前目录
            recursive = str(kwargs.get("recursive", "true")).lower() not in {"0", "false", "no"}
            return list_dir(root, rel, pattern, recursive)

        if name == "file-read":
            rel = kwargs.get("path") or (args[0] if args else "")
            return read_text(root, rel, config.MAX_READ_BYTES)

        if name == "file-write":
            rel = kwargs.get("path") or (args[0] if args else "")
            text = kwargs.get("text") or (args[1] if len(args) > 1 else "")
            return write_text(root, rel, text)

        if name == "file-append":
            rel = kwargs.get("path") or (args[0] if args else "")
            text = kwargs.get("text") or (args[1] if len(args) > 1 else "")
            return append_text(root, rel, text)

        if name == "file-copy":
            source = kwargs.get("source") or kwargs.get("src") or (args[0] if args else "")
            target = kwargs.get("target") or kwargs.get("dest") or (args[1] if len(args) > 1 else "")
            return copy_path(root, source, target)

        if name == "file-move":
            source = kwargs.get("source") or kwargs.get("src") or (args[0] if args else "")
            target = kwargs.get("target") or kwargs.get("dest") or (args[1] if len(args) > 1 else "")
            return move_path(root, source, target)

        if name == "file-tail":
            rel = kwargs.get("path") or (args[0] if args else "")
            raw_lines = kwargs.get("lines") or kwargs.get("num_lines") or (args[1] if len(args) > 1 else "50")
            try:
                num_lines = int(raw_lines)
            except (TypeError, ValueError):
                num_lines = 50
            return tail_text(root, rel, num_lines)

        if name == "file-search":
            pattern = kwargs.get("pattern") or kwargs.get("keyword") or kwargs.get("query") or (args[0] if args else "")
            rel = kwargs.get("path") or (args[1] if len(args) > 1 else "")
            raw_limit = kwargs.get("limit") or kwargs.get("max_results") or (args[2] if len(args) > 2 else "30")
            try:
                limit = int(raw_limit)
            except (TypeError, ValueError):
                limit = 30
            case_sensitive = str(kwargs.get("case_sensitive", "")).lower() in {"1", "true", "yes"}
            return search_text(root, pattern, rel, limit, case_sensitive)

        # MSA 额外保留的文件删除工具，命名仍使用短横线风格。
        if name == "delete-file":
            rel = kwargs.get("path") or (args[0] if args else "")
            return delete_path(root, rel)

        if name == "unzip":
            zip_rel = kwargs.get("path") or kwargs.get("zip") or (args[0] if args else "")
            target_rel = kwargs.get("target") or kwargs.get("dest") or (args[1] if len(args) > 1 else "")
            return extract_zip(root, zip_rel, target_rel)

        # HTTP 请求工具，对齐提示词：fetch|url|method|data
        if name == "fetch":
            return self._fetch(args=args, kwargs=kwargs, timeout=timeout)

        if name == "download":
            return self._download(args=args, kwargs=kwargs, root=root, timeout=timeout)

        # 网页搜索工具，对齐提示词：web-search|关键词
        if name == "web-search":
            query = kwargs.get("query") or kwargs.get("keyword") or (args[0] if args else "")
            limit = (
                kwargs.get("limit")
                or kwargs.get("count")
                or kwargs.get("max_results")
                or (args[1] if len(args) > 1 else "")
            )
            return self._web_search(query=query, limit=limit, timeout=timeout)

        if name == "codex":
            return self._codex(args=args, kwargs=kwargs, root=root, timeout=timeout)

        # 图片 URL 工具
        if name == "get-image-url-from-local":
            local_path = self._first_arg(args, kwargs, "path", "local_path", default="")
            return self._get_image_url_from_local(local_path=local_path, root=root)

        if name == "send-image-by-url":
            image_url = self._first_arg(args, kwargs, "url", "image_url", default="")
            return self._send_image_by_url(image_url=image_url)

        if name in {"send-message", "notify"}:
            text = self._first_arg(args, kwargs, "text", "message", default="")
            return self._send_message(text)

        # Shell 执行
        if name in {"run-shell", "shell", "command"}:
            return self._run_shell(args=args, kwargs=kwargs, root=root, timeout=timeout)

        # Skill 相关管理工具和未知工具名都交给独立 skill_runtime。
        # 这对应原项目 ToolManager 的行为：原生工具未命中时，自动尝试按 skill 名执行。
        return skill_runtime.dispatch(
            tool_name=name,
            args=args,
            kwargs=kwargs,
            user_workspace=root,
            timeout=timeout,
        )

    def _codex(self, args: list[str], kwargs: dict[str, str], root: Path, timeout: int) -> str:
        working_dir = kwargs.get("working_dir") or (args[0] if args else str(root))
        requirement = kwargs.get("requirement") or (args[1] if len(args) > 1 else "")

        if not requirement:
            return "错误：请提供代码需求（codex|工作目录|需求）"

        # 解析容器内工作目录
        try:
            work_path = self._safe_workspace_path(root, working_dir)
        except ValueError:
            # 越界路径不安全，回退到用户工作区根目录
            work_path = root
        work_path.mkdir(parents=True, exist_ok=True)

        # 映射到外部 VM 路径（与 clawhub 共用 SSH 配置）
        vm_host = config.CLAW_EXTERNAL_VM_HOST
        if not vm_host:
            return "错误：未配置 CLAW_EXTERNAL_VM_HOST，无法调用外部 VM 上的 Codex CLI"

        vm_workspace = os.getenv("CODEX_EXTERNAL_VM_WORKSPACE", "/srv/nfs/my-agent/workspace").rstrip("/")
        container_workspace = str(root)
        vm_work_path = str(work_path).replace(container_workspace, vm_workspace)
        if not vm_work_path.startswith(vm_workspace):
            return f"错误：工作目录 {work_path} 无法映射到外部 VM 工作区 {vm_workspace}"

        codex_bin = self._resolve_codex_bin()
        if not codex_bin:
            return "错误：外部 VM 未安装 Codex CLI，请先安装（npm install -g @openai/codex）"

        script = (
            f"{shlex.quote(codex_bin)} exec "
            f"-C {shlex.quote(vm_work_path)} "
            f"--dangerously-bypass-approvals-and-sandbox "
            f"--skip-git-repo-check {shlex.quote(requirement)}"
        )
        return self._codex_via_ssh(script, requirement, vm_work_path, timeout)

    def _resolve_codex_bin(self) -> str:
        """在外部 VM 上解析可用的 codex 可执行文件路径。

        SSH 非交互 shell 通常不加载 nvm 等 PATH，直接用裸命令名 command -v 会失败，
        因此优先使用部署脚本生成的包装器（my-agent-codex），再回退到裸命令。
        """
        try:
            from app.skill_runtime import skill_runtime
        except Exception:
            return ""

        configured = (config.CODEX_BIN_PATH or "").strip() or "codex"
        candidates = [
            configured,
            f"/home/{config.CLAW_EXTERNAL_VM_USER}/.local/bin/my-agent-codex",
            os.path.expanduser("~/.local/bin/my-agent-codex"),
        ]
        probe = (
            "for c in %s; do "
            "if command -v \"$c\" >/dev/null 2>&1; then echo \"FOUND:$c\"; exit 0; fi; "
            "done; exit 1" % " ".join(shlex.quote(c) for c in candidates)
        )
        try:
            output = skill_runtime._run_external_vm_shell(probe, timeout=10)
        except Exception:
            return ""
        if "FOUND:" in output:
            return output.split("FOUND:", 1)[1].splitlines()[0].strip()
        return ""

    def _codex_via_ssh(self, script: str, requirement: str, vm_work_path: str, timeout: int) -> str:
        try:
            from app.skill_runtime import skill_runtime
            output = skill_runtime._run_external_vm_shell(script, timeout=timeout or 120)
            return f"【Codex 执行完成】\n工作目录: {vm_work_path}\n需求: {requirement}\n\n{output}"
        except subprocess.TimeoutExpired:
            return f"Codex 执行超时（{timeout or 120}秒），可适当加大 timeout_seconds"
        except Exception as exc:
            return f"Codex 执行失败: {exc}"

    def _run_shell(self, args: list[str], kwargs: dict[str, str], root: Path, timeout: int) -> str:
        if not config.ENABLE_SHELL_TOOLS:
            raise PermissionError("shell is disabled. Set ENABLE_SHELL_TOOLS=true to enable it.")

        command = kwargs.get("command") or " ".join(args)
        if not command:
            raise ValueError("missing command")

        # Shell 的相对路径必须以用户 workspace 为起点。
        # 注意：这不是完整沙箱；绝对路径仍可能访问容器内其它位置。
        # 真正生产环境还需要配合非 root、只读 rootfs、网络/权限隔离。
        env = {
            **os.environ,
            "MY_AGENT_WORKSPACE": str(root),
            "PWD": str(root),
        }

        proc = subprocess.run(
            command,
            shell=True,
            cwd=str(root),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            env=env,
        )

        output = proc.stdout
        if proc.stderr:
            output += "\n[stderr]\n" + proc.stderr
        output += f"\n[exit_code] {proc.returncode}"
        return output

    # ---- fetch 响应类型识别与格式化 ----

    @classmethod
    def _format_json_response(cls, status_line: str, response) -> str:
        try:
            parsed = response.json()
            import json
            formatted = json.dumps(parsed, ensure_ascii=False, indent=2)
            return f"{status_line}\n[JSON]\n{formatted}"
        except Exception:
            return f"{status_line}\n[JSON·解析失败]\n{response.text}"

    @classmethod
    def _format_html_response(cls, status_line: str, response) -> str:
        try:
            soup = BeautifulSoup(response.text, "html.parser")
            # 提取页面中的图片 URL
            img_urls = []
            for img in soup.find_all("img"):
                src = img.get("src") or img.get("data-src") or ""
                if src and not src.startswith("data:"):
                    from urllib.parse import urljoin
                    img_urls.append(urljoin(response.url, src))
            # 去重，最多 20 张
            seen = set()
            unique_imgs = []
            for u in img_urls:
                if u not in seen:
                    seen.add(u)
                    unique_imgs.append(u)
            unique_imgs = unique_imgs[:20]

            # 移除无用标签
            for tag in soup(["script", "style", "nav", "footer", "header"]):
                tag.decompose()
            text = soup.get_text(separator="\n", strip=True)
            import re
            text = re.sub(r"\n{3,}", "\n\n", text)

            result = f"{status_line}\n[HTML→文本]\n{text}"
            if unique_imgs:
                result += "\n\n[页面图片]\n" + "\n".join(f"- {u}" for u in unique_imgs)
            return result
        except Exception:
            return f"{status_line}\n[HTML→文本·解析失败]\n{response.text}"

    @staticmethod
    def _format_image_response(status_line: str, response) -> str:
        content_type = response.headers.get("Content-Type", "image/unknown")
        content_length = response.headers.get("Content-Length", "未知")
        return f"{status_line}\n[图片] 类型: {content_type} | 大小: {content_length} bytes（图片二进制数据未在文本中返回，请使用 send-image-by-url 发送）"

    @classmethod
    def _format_text_response(cls, status_line: str, response) -> str:
        return f"{status_line}\n[文本]\n{response.text}"

    @staticmethod
    def _format_binary_response(status_line: str, response, content_type: str) -> str:
        content_length = response.headers.get("Content-Length", "未知")
        return f"{status_line}\n[二进制] 类型: {content_type} | 大小: {content_length} bytes（二进制数据未在文本中返回）"

    _MAX_RAW_CHARS = 8000

    @classmethod
    def _process_fetch_response(cls, response) -> str:
        content_type = response.headers.get("Content-Type", "").lower()
        status_line = f"[status] {response.status_code}"

        # 图片（非 SVG）始终格式化（二进制对模型无意义；SVG 是文本，按普通文本处理）
        if content_type.startswith("image/") and "svg" not in content_type:
            return cls._format_image_response(status_line, response)

        text = response.text
        # 未超过阈值，直接返回原始文本
        if len(text) <= cls._MAX_RAW_CHARS:
            return f"{status_line}\n{text}"

        # 超过阈值，按类型智能格式化
        if "application/json" in content_type:
            return cls._format_json_response(status_line, response)
        if "text/html" in content_type:
            return cls._format_html_response(status_line, response)
        if "text/" in content_type:
            return cls._format_text_response(status_line, response)
        return cls._format_binary_response(status_line, response, content_type)

    def _fetch(self, args: list[str], kwargs: dict[str, str], timeout: int) -> str:
        url = kwargs.get("url") or (args[0] if args else "")
        method = (kwargs.get("method") or (args[1] if len(args) > 1 else "GET") or "GET").upper()
        data = kwargs.get("data") or (args[2] if len(args) > 2 else "")

        if not url:
            return "请求失败：url 不能为空"

        request_timeout = timeout or 10

        try:
            if method == "GET":
                response = requests.get(url, timeout=request_timeout)
            else:
                json_data = None
                raw_data = None

                if data:
                    try:
                        json_data = requests.compat.json.loads(data)
                    except Exception:
                        raw_data = data

                response = requests.request(
                    method,
                    url,
                    json=json_data,
                    data=raw_data,
                    timeout=request_timeout,
                )

            return self._process_fetch_response(response)
        except Exception as exc:
            return f"请求失败：{exc}"

    def _web_search(self, query: str, limit: int | str | None = None, timeout: int = 0) -> str:
        """通过 SearXNG 元搜索引擎执行网页搜索。"""
        query = (query or "").strip()
        if not query:
            return "搜索失败：关键词不能为空"

        try:
            max_results = int(limit or config.WEB_SEARCH_MAX_RESULTS)
        except (TypeError, ValueError):
            max_results = config.WEB_SEARCH_MAX_RESULTS
        max_results = max(1, min(max_results, 50))

        searxng_url = os.getenv("SEARXNG_URL", "http://localhost:8080")

        def _do_search(pageno: int = 1):
            response = requests.get(
                f"{searxng_url}/search",
                params={"q": query, "format": "json", "categories": "general", "pageno": pageno},
                timeout=timeout or 15,
            )
            response.raise_for_status()
            return response.json()

        def _collect(pageno: int = 1) -> tuple[list[dict], dict]:
            """抓取一页并按 URL 去重后追加结果。"""
            page_data = _do_search(pageno)
            page_results = []
            for item in page_data.get("results", []):
                url = item.get("url", "")
                if url and url in seen:
                    continue
                if url:
                    seen.add(url)
                page_results.append(item)
                if len(results) + len(page_results) >= max_results:
                    break
            return page_results, page_data

        results: list[dict] = []
        seen: set[str] = set()
        try:
            # SearXNG JSON 每页约 10 条，超过一页时按页抓取
            pages = max(1, (max_results + 9) // 10)
            data = {}
            for pageno in range(1, pages + 1):
                page_results, data = _collect(pageno)
                results.extend(page_results)
                if len(results) >= max_results:
                    break

            # 空结果时等待 1 秒后重试一次（搜索引擎偶发限流）
            if not results:
                import time
                time.sleep(1)
                results = []
                seen = set()
                for pageno in range(1, pages + 1):
                    page_results, data = _collect(pageno)
                    results.extend(page_results)
                    if len(results) >= max_results:
                        break

            if not results:
                return f"【搜索：{query}】未找到相关内容\n[搜索完成，共 0 条结果]"

            results = results[:max_results]
            formatted = []
            for item in results:
                title = item.get("title", "无标题")
                url = item.get("url", "无链接")
                snippet = (item.get("content") or item.get("snippet") or "")[:160]
                formatted.append(f"? {title}\n【网址】{url}\n{snippet}\n")

            engine = data.get("engines", [])
            engine_str = f"（聚合引擎：{', '.join(engine[:3])}）" if engine else ""
            count = len(formatted)
            return f"【搜索：{query}】{engine_str}\n" + "\n".join(formatted) + f"\n[搜索完成，共 {count} 条结果]"

        except Exception as exc:
            return f"搜索失败：{exc}"

    def _download(self, args: list[str], kwargs: dict[str, str], root: Path, timeout: int) -> str:
        url = kwargs.get("url") or (args[0] if args else "")
        rel = kwargs.get("path") or kwargs.get("file") or (args[1] if len(args) > 1 else "")
        if not url:
            return "错误：请提供 URL（download|url|目标路径）"

        try:
            if rel:
                dest = self._safe_workspace_path(root, rel)
            else:
                name = urllib.parse.urlparse(url).path.split("/")[-1] or "download"
                dest = self._safe_workspace_path(root, name)
            dest.parent.mkdir(parents=True, exist_ok=True)

            response = requests.get(url, stream=True, timeout=timeout or 60, allow_redirects=True)
            response.raise_for_status()
            size = 0
            with dest.open("wb") as fh:
                for chunk in response.iter_content(chunk_size=64 * 1024):
                    fh.write(chunk)
                    size += len(chunk)
            return f"downloaded {url}\n保存到: {dest.relative_to(root)}\n大小: {size} bytes"
        except Exception as exc:
            return f"下载失败：{exc}"

    def _get_image_url_from_local(self, local_path: str, root: Path) -> str:
        if not local_path:
            return "本地文件不存在："

        try:
            source = self._safe_workspace_path(root, local_path)
        except Exception as exc:
            return f"本地文件不存在：{exc}"

        if not source.exists() or not source.is_file():
            return f"本地文件不存在：{source}"

        asset_dir = Path(os.getenv("IMAGE_ASSET_DIR", "/app/assets/images")).resolve()
        asset_dir.mkdir(parents=True, exist_ok=True)

        suffix = source.suffix or mimetypes.guess_extension(mimetypes.guess_type(str(source))[0] or "") or ""
        target_name = f"{source.stem}-{uuid.uuid4().hex[:8]}{suffix}"
        target = asset_dir / target_name
        shutil.copyfile(source, target)

        base_url = os.getenv("IMAGE_BASE_URL", "http://localhost:5102/assets").rstrip("/")
        if base_url:
            url = f"{base_url}/{urllib.parse.quote(target_name)}"
        else:
            url = target.as_uri()

        return f"此图片URL为：{url}"

    @staticmethod
    def _send_image_by_url(image_url: str):
        if not image_url:
            return "❌ 图片 URL 不能为空"

        return ToolResult(
            output=f"URL为{image_url}的图片已发送",
            events=[
                {
                    "type": "image",
                    "text": f"图片：{image_url}",
                    "artifact": {"type": "image", "local_path": "", "asset_url": image_url},
                }
            ],
        )

    @staticmethod
    def _send_message(text: str):
        if not text:
            return "❌ 消息内容不能为空"

        return ToolResult(
            output=f"已向用户发送消息：{text}",
            events=[{"type": "message", "text": text}],
        )

    def _help(self) -> str:
        return """tool-runtime-service tools:
- echo: args or kwargs.text
- shell: run system command (disabled by default)
- list-workspace: list all files in workspace
- dir-list: 目录|通配符 (kwargs: path/pattern/recursive)
- fetch: url|method|data
- download: url|目标路径 (保存到工作区)
- web-search: 关键词|条数 (条数可选，默认10，最多50)
- file-read: kwargs.path or args[0]
- file-write: kwargs.path + kwargs.text, or args[0] + args[1]
- file-append: kwargs.path + kwargs.text, or args[0] + args[1] (append to file end)
- file-copy: 源路径|目标路径
- file-move: 源路径|目标路径
- file-tail: 文件路径|行数 (默认50)
- file-search: 关键词|路径|条数 (kwargs: pattern/path/limit/case_sensitive)
- delete-file: kwargs.path or args[0], file or empty directory only
- unzip: 压缩包路径|目标目录
- codex: 工作目录|需求 (在外部 VM 上调用 codex exec 生成代码，需 VM 安装 @openai/codex)
- get-image-url-from-local: local image path
- send-image-by-url: image url (执行时立即将图片推送给用户)
- send-message: text (执行过程中立即向用户推送文本消息)

skill tools:
- clawhub-search: keyword
- clawhub-install: skill_slug; installs into shared /app/workspace/skill and imports to OpenViking
- clawhub-list
- skill-list / skill-list-simple
- skill-delete: skill_slug
- skill-abstract / skill-overview / skill-manual: skill_name
- add-skill-to-viking: skill_slug
- any other tool name: treated as installed skill name
"""


def serve():
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=16))
    tool_runtime_pb2_grpc.add_ToolRuntimeServicer_to_server(ToolRuntimeService(), server)

    listen_addr = f"{config.TOOL_RUNTIME_HOST}:{config.TOOL_RUNTIME_PORT}"
    server.add_insecure_port(listen_addr)
    server.start()

    log(f"tool-runtime-service started on {listen_addr}")
    log(f"workspace dir: {config.WORKSPACE_DIR}")
    log(f"skill root dir: {config.SKILL_ROOT_DIR}")
    log(f"openviking server url: {config.OPENVIKING_SERVER_URL}")
    log(f"shell tools enabled: {config.ENABLE_SHELL_TOOLS}")

    server.wait_for_termination()


if __name__ == "__main__":
    serve()
