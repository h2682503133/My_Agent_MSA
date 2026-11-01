from __future__ import annotations

import base64
import mimetypes
import socket
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

from app import config
from app import fetch_tools
from app.logger import log, debug
from app import mnt_access
from app.process_runtime import init as process_init
from app.process_runtime import modify as process_modify
from app.process_runtime import remove as process_remove
from app.process_runtime import var_list as process_var_list
from app.process_runtime import var_remove as process_var_remove
from app.process_runtime import write as process_write
from app.worldinfo_runtime import write as worldinfo_write
from app.skill_runtime import skill_runtime
from app import tool_registry
from app import skill_handlers, tool_handlers  # noqa: F401  （import 即注册，顺序必须在 tool_registry 之后）
from app.image_tools import load_image
from app.web_search import web_search
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
    write_bytes,
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
            # `model_image` 是特例：不是给用户看的，而是给**模型**看的
            # （image-view 回填），orchestrator 会把它挂到下一轮 images 上。
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
                elif event_type == "model_image":
                    artifact = event.get("artifact") or {}
                    yield tool_runtime_pb2.ExecuteToolEvent(
                        event_type="model_image",
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

    def _port_expose(self, port_str: str) -> str:
        try:
            port = int(str(port_str or "").strip())
        except (TypeError, ValueError):
            raise ValueError(f"invalid port: {port_str}")
        low, _, high = config.PORT_PROXY_RANGE.partition("-")
        try:
            low_i, high_i = int(low), int(high or low)
        except (TypeError, ValueError):
            low_i, high_i = 5800, 5899
        if not (low_i <= port <= high_i):
            raise ValueError(f"port {port} out of allowed range {low_i}-{high_i}")

        listening = False
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=1):
                listening = True
        except OSError:
            listening = False

        warn = ""
        if not listening:
            warn = f"\n警告：端口 {port} 当前未检测到监听，请先启动服务再使用链接。"
        return (
            f"端口 {port} 已开放\n"
            f"用户访问链接：{config.PORT_PROXY_BASE_URL}/{port}/\n"
            f"容器内访问：http://tool-runtime-service:{port}/\n"
            f"若服务有子路径，请在链接后拼接。{warn}"
        )

    def _dispatch(self, tool_name: str, args: list[str], kwargs: dict[str, str], root: Path, timeout: int):
        """统一分发：TOOL.json 管暴露、工具自声明能力、上层注入环境实现（见 tool_registry）。"""
        # 延迟导入：注册发生在模块 import 时，放这里可避免启动顺序问题
        return tool_registry.dispatch(
            tool_name,
            args,
            kwargs,
            root,
            timeout,
            service=self,
            skill=skill_runtime,
        )

    def _codex(self, args: list[str], kwargs: dict[str, str], root: Path, timeout: int) -> str:
        if not config.ENABLE_CODEX:
            return (
                "错误：Codex 未启用。部署 tool-runtime 时未勾选「codex 代码生成」功能，无法调用 codex。"
                "如需启用，请重新运行 deploy-all.ps1 并勾选 codex，或设置环境变量 ENABLE_CODEX=true 后重启 tool-runtime。"
            )

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

    # ---- fetch 相关逻辑已拆到 app/fetch_tools.py；web-search 整体在 app/web_search.py ----

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

        asset_dir = Path(config.IMAGE_ASSET_DIR).resolve()
        asset_dir.mkdir(parents=True, exist_ok=True)

        suffix = source.suffix or mimetypes.guess_extension(mimetypes.guess_type(str(source))[0] or "") or ""
        target_name = f"{source.stem}-{uuid.uuid4().hex[:8]}{suffix}"
        target = asset_dir / target_name
        shutil.copyfile(source, target)

        base_url = str(config.IMAGE_BASE_URL).rstrip("/")
        if base_url:
            url = f"{base_url}/{urllib.parse.quote(target_name)}"
        else:
            url = target.as_uri()

        return f"此图片URL为：{url}"

    def _image_gen(self, prompt: str, size: str, model_profile: str, root: Path) -> ToolResult:
        """生图：提示词 → model-proxy → 落盘到工作区 → 推图给用户。"""
        prompt = (prompt or "").strip()
        if not prompt:
            return "错误：image-gen 需要提示词，用法 工具调用:image-gen|提示词|尺寸(可选)"
        from app.image_tools import promote_to_assets, save_generated_image
        from app.model_proxy_client import model_proxy_client

        try:
            result = model_proxy_client.image_generation(
                prompt=prompt, size=size, model_profile=model_profile
            )
        except Exception as exc:  # noqa: BLE001
            return f"生图失败：{exc}"
        if not result.get("ok"):
            return f"生图失败：{result.get('error') or '未知错误'}"

        refs: list[str] = []          # 已落盘的工作区路径
        failed_saves: list[str] = []  # 落盘失败的原因（不影响发图）
        events: list[dict] = []

        # 发图优先用**集群内图床地址**：远程签名 URL（火山 TOS）需要渠道自己出网下载，
        # 容器 DNS 抖一下就会"推送失败"，还会被调度器转投到官方渠道（那边更发不出去）。
        # 所以先把图复制进共享图床，再发 /assets/... 的内部地址。
        for index, url in enumerate(result.get("urls") or []):
            saved, _data_url, error = save_generated_image(root, url, index)
            if error:
                failed_saves.append(error.split("\n")[0])
            else:
                refs.append(saved)
            asset_url = ""
            if saved:
                asset_url = promote_to_assets(root, saved)
            # 走「发图」这条通道（与 工具调用:send-image 同一套事件），
            # 不把 URL 当文字发出去：text 只做说明，URL 放在 artifact 里由渠道取图。
            events.append({
                "type": "image",
                "text": "生成的图片",
                "artifact": {
                    "type": "image",
                    "local_path": saved,
                    "asset_url": asset_url or url,   # 图床复制失败才回退远程地址
                },
            })

        if not events:
            return "生图失败：接口没有返回图片"

        parts = [
            f"已生成 {len(events)} 张图片（模型 {result.get('model') or model_profile}"
            f"{'，' + size if size else ''}），图片已通过发图通道发给用户"
        ]
        if refs:
            parts.append("同时已存到工作区：" + "、".join(refs) + "（需要复看可再 image-view）")
        if failed_saves:
            parts.append("落盘失败（不影响发图）：" + failed_saves[0])
        # 完整原图链接也给模型（它可能需要引用/记录）。明确说明这是资料，
        # 不是让它把链接当文字复述给用户 —— 发图已经由事件通道完成。
        links = list(result.get("urls") or [])
        if links:
            parts.append(
                "原图链接（供你参考，图片已发出，不要把链接原文当消息发送）：\n"
                + "\n".join(f"- {link}" for link in links)
            )
        return ToolResult(output="；".join(parts), events=events)

    def _image_view(self, target: str, root: Path, hint: str = "") -> ToolResult:
        """image-view：读书/下载图片并交给模型（不发给用户）。

        事件类型用 `model_image` 而不是 `image`：`image` 会被 orchestrator 当成
        "推送给用户"，而这里的图是喂给模型看的，两者不能混。

        `hint` 是本次看图的**需求方向**（例如"看清表格第三列"），随图片一起交给模型；
        任何轮次都可以调用，不限制"只在新图/首次"。
        """
        try:
            payload = load_image(target, root, config.IMAGE_VIEW_MAX_BYTES)
        except Exception as exc:
            debug(f"image-view failed: {exc}")
            return f"图片读取失败：{exc}"

        hint = (hint or "").strip()
        if len(hint) > 200:
            hint = hint[:200] + "…"
        text = (
            f"已载入图片：{payload.relpath}"
            f"（{payload.dimension_text}，{payload.mime}，{payload.size_text}）。"
        )
        if hint:
            text += f"本次关注方向：{hint}。"
        text += "图片内容已附加到本轮上下文，据此回答。"
        return ToolResult(
            output=text,
            events=[
                {
                    "type": "model_image",
                    "text": text,
                    "artifact": {
                        "type": "image",
                        "local_path": payload.relpath,
                        "asset_url": payload.data_url,
                    },
                }
            ],
        )

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
- fetch: url|method|data (GET: data=搜索词(空格分隔)或章节序号/章节名，空则大页面返回带序号大纲；POST: data=请求体)
- download: url|目标路径 (保存到工作区)
- web-search: 关键词|条数 (条数可选，默认10，最多50)
- file-read: kwargs.path or args[0]
- file-write: kwargs.path + kwargs.text, or args[0] + args[1]
- file-append: kwargs.path + kwargs.text, or args[0] + args[1] (append to file end)
- file-upload: kwargs.path + kwargs.data (base64 内容，最大 48MB)
- port-expose: 端口 - 声明对外开放端口（范围 5800-5899），返回用户访问链接
- file-copy: 源路径|目标路径
- file-move: 源路径|目标路径
- file-tail: 文件路径|行数 (默认50)
- file-search: 关键词|路径|条数 (kwargs: pattern/path/limit/case_sensitive)
- delete-file: kwargs.path or args[0], file or empty directory only
- unzip: 压缩包路径|目标目录
- codex: 工作目录|需求 (在外部 VM 上调用 codex exec 生成代码，需 VM 安装 @openai/codex)
- get-image-url-from-local: local image path
- send-image-by-url: image url (执行时立即将图片推送给用户)
- image-view: 图片路径或 http(s) URL（把图片读取给模型看，用于回看历史消息里的图片；
  用户发送的图片会自动保存到 uploads/images/<日期>/，历史与提示词里带路径）
- send-message: text (执行过程中立即向用户推送文本消息)
- process-write: index|title|content (index=-1 追加到末尾，1..N 覆写对应条目)
- process-remove: index (按 1 起始删除对应条目)
- process-init: 重置 PROCESS（有模板 process_templates#/<agent>.json 则载入模板，无则清空条目/轮次归零）
- process-modify: key|op (修改 vars 变量，op=+N/-N/=N；严格匹配→别名匹配→脱靶新建，脱靶新建存符号数值)
- process-var-remove: key (删除 vars 变量，严格匹配→别名匹配)
- process-var-list: 列出全部 vars 变量

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
    server = grpc.server(
        futures.ThreadPoolExecutor(max_workers=16),
        options=[
            ("grpc.max_send_message_length", config.GRPC_MAX_MESSAGE_BYTES),
            ("grpc.max_receive_message_length", config.GRPC_MAX_MESSAGE_BYTES),
        ],
    )
    tool_runtime_pb2_grpc.add_ToolRuntimeServicer_to_server(ToolRuntimeService(), server)

    listen_addr = f"{config.TOOL_RUNTIME_HOST}:{config.TOOL_RUNTIME_PORT}"
    server.add_insecure_port(listen_addr)
    server.start()

    log(f"tool-runtime-service started on {listen_addr}")
    log(f"workspace dir: {config.WORKSPACE_DIR}")
    log(f"skill root dir: {config.SKILL_ROOT_DIR}")
    log(f"openviking server url: {config.OPENVIKING_SERVER_URL}")
    log(f"shell tools enabled: {config.ENABLE_SHELL_TOOLS}")

    # 工具目录与实现校验（TOOL.json 管暴露、工具自声明能力、上层注入 provider）
    from app import skill_handlers, tool_handlers  # noqa: F401  （import 即注册）
    from app import tool_registry, tool_provider

    log(f"tool catalog: {config.TOOL_CATALOG_PATH or '(自动探测)'}")
    log(f"tool providers: {tool_provider.parse() or '(按平台默认)'}")
    problems = tool_registry.validate_startup()
    log(f"tool registry: {len(tool_registry.registered())} 个实现，"
        f"{len(tool_registry.alias_map())} 个别名，{len(problems)} 条提示/问题")

    server.wait_for_termination()


if __name__ == "__main__":
    serve()
