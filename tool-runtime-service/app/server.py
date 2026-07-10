from __future__ import annotations

import mimetypes
import os
import shutil
import subprocess
import sys
import urllib.parse
import uuid
from concurrent import futures
from pathlib import Path
from typing import Any

import grpc
import requests
from bs4 import BeautifulSoup

from app import config
from app.logger import log, debug
from app.skill_runtime import skill_runtime
from app.workspace import delete_path, list_workspace, read_text, workspace_root, write_text

GENERATED_DIR = Path(__file__).parent / "generated"
if str(GENERATED_DIR) not in sys.path:
    sys.path.insert(0, str(GENERATED_DIR))

try:
    import tool_runtime_pb2
    import tool_runtime_pb2_grpc
except ImportError as exc:
    raise RuntimeError("gRPC generated files not found. Run bash scripts/gen_proto.sh first.") from exc


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
            output, artifacts = self._normalize_result(result)

            return tool_runtime_pb2.ExecuteToolResponse(
                ok=True,
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
            return tool_runtime_pb2.ExecuteToolResponse(
                ok=False,
                output="",
                logs="",
                error=str(exc),
            )

    @staticmethod
    def _normalize_result(result: Any) -> tuple[str, list[dict[str, str]]]:
        if isinstance(result, tuple) and len(result) == 2:
            output, artifacts = result
            return str(output or ""), list(artifacts or [])
        return str(result or ""), []

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

        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = root / candidate

        root_resolved = root.resolve()
        candidate_resolved = candidate.resolve()

        if candidate_resolved != root_resolved and root_resolved not in candidate_resolved.parents:
            raise ValueError(f"path escapes workspace: {path}")

        return candidate_resolved

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

        if name == "file-read":
            rel = kwargs.get("path") or (args[0] if args else "")
            return read_text(root, rel, config.MAX_READ_BYTES)

        if name == "file-write":
            rel = kwargs.get("path") or (args[0] if args else "")
            text = kwargs.get("text") or (args[1] if len(args) > 1 else "")
            return write_text(root, rel, text)

        # MSA 额外保留的文件删除工具，命名仍使用短横线风格。
        if name == "delete-file":
            rel = kwargs.get("path") or (args[0] if args else "")
            return delete_path(root, rel)

        # HTTP 请求工具，对齐提示词：fetch|url|method|data
        if name == "fetch":
            return self._fetch(args=args, kwargs=kwargs, timeout=timeout)

        # 网页搜索工具，对齐提示词：web-search|关键词
        if name == "web-search":
            query = kwargs.get("query") or kwargs.get("keyword") or (args[0] if args else "")
            return self._web_search(query=query, timeout=timeout)

        # 代码生成工具先保留占位，避免误以为 Codex CLI 已接入。
        if name == "codex":
            return "本方法未实现"

        # 图片 URL 工具
        if name == "get-image-url-from-local":
            local_path = self._first_arg(args, kwargs, "path", "local_path", default="")
            return self._get_image_url_from_local(local_path=local_path, root=root)

        if name == "send-image-by-url":
            image_url = self._first_arg(args, kwargs, "url", "image_url", default="")
            return self._send_image_by_url(image_url=image_url)

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

            return f"[status] {response.status_code}\n{response.text}"
        except Exception as exc:
            return f"请求失败：{exc}"

    def _web_search(self, query: str, timeout: int) -> str:
        query = (query or "").strip()
        if not query:
            return "搜索失败：关键词不能为空"

        headers = {
            "User-Agent": (
                "Mozilla/5.0 (X11; Linux x86_64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/130.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Connection": "close",
            "Upgrade-Insecure-Requests": "1",
        }

        try:
            response = requests.get(
                "https://www.baidu.com/s",
                params={"wd": query},
                headers=headers,
                timeout=timeout or 15,
            )
            response.raise_for_status()

            soup = BeautifulSoup(response.text, "html.parser")
            results = []

            # 兼容百度常见结果样式。
            for item in soup.find_all("div", class_=["result", "c-container", "result-op"]):
                if len(results) >= 4:
                    break

                try:
                    title_node = item.find("h3")
                    title = title_node.get_text(strip=True) if title_node else "无标题"

                    link_node = item.find("a")
                    url = link_node["href"] if link_node and link_node.has_attr("href") else "无链接"

                    content = item.get_text(" ", strip=True)[:160]
                    results.append(f"? {title}\n【网址】{url}\n{content}\n")
                except Exception:
                    continue

            if not results:
                return f"【百度搜索：{query}】未找到相关内容（或被百度拦截）"

            return f"【百度搜索：{query}】\n" + "\n".join(results)

        except Exception as exc:
            return f"搜索失败：{exc}"

    def _get_image_url_from_local(self, local_path: str, root: Path) -> str:
        if not local_path:
            return "本地文件不存在："

        try:
            source = self._safe_workspace_path(root, local_path)
        except Exception as exc:
            return f"本地文件不存在：{exc}"

        if not source.exists() or not source.is_file():
            return f"本地文件不存在：{source}"

        asset_dir = Path(os.getenv("IMAGE_ASSET_DIR", str(root / ".assets" / "images"))).resolve()
        asset_dir.mkdir(parents=True, exist_ok=True)

        suffix = source.suffix or mimetypes.guess_extension(mimetypes.guess_type(str(source))[0] or "") or ""
        target_name = f"{source.stem}-{uuid.uuid4().hex[:8]}{suffix}"
        target = asset_dir / target_name
        shutil.copyfile(source, target)

        base_url = os.getenv("IMAGE_BASE_URL", "").rstrip("/")
        if base_url:
            url = f"{base_url}/{urllib.parse.quote(target_name)}"
        else:
            url = target.as_uri()

        return f"此图片URL为：{url}"

    @staticmethod
    def _send_image_by_url(image_url: str):
        if not image_url:
            return "❌ 图片 URL 不能为空"

        return (
            f"URL为{image_url}的图片已发送",
            [{"type": "image", "local_path": "", "asset_url": image_url}],
        )

    def _help(self) -> str:
        return """tool-runtime-service tools:
- echo: args or kwargs.text
- shell: run system command (disabled by default)
- list-workspace: list all files in workspace
- fetch: url|method|data
- web-search: query
- file-read: kwargs.path or args[0]
- file-write: kwargs.path + kwargs.text, or args[0] + args[1]
- delete-file: kwargs.path or args[0], file or empty directory only
- codex: 本方法未实现
- get-image-url-from-local: local image path
- send-image-by-url: image url

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
