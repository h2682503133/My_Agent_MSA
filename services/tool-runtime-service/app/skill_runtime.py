from __future__ import annotations

import asyncio
import inspect
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Iterable

from app import config
from app.dsh_client import dsh_client


def _run_coro_sync(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    box = {"result": None, "error": None}

    def runner():
        try:
            box["result"] = asyncio.run(coro)
        except Exception as exc:
            box["error"] = exc

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join()
    if box["error"] is not None:
        raise box["error"]
    return box["result"]


class SkillRuntime:
    """
    OpenClaw / ClawHub skill runtime for My_Agent_MSA.

    - ClawHub commands run on an external VM or WSL by default.
    - Skill indexing/querying uses the OpenViking service.
    - OpenViking HTTP access mirrors openviking-context-service:
      root-key calls include account, user, and agent headers.
    - Skills are shared by all users under SKILL_ROOT_DIR.
    """

    def __init__(self) -> None:
        self.skill_root = Path(config.SKILL_ROOT_DIR)

    def _ensure_dirs(self) -> None:
        self.skill_root.mkdir(parents=True, exist_ok=True)

    async def _maybe_await(self, value):
        if inspect.isawaitable(value):
            return await value
        return value

    def _openviking_identity(self) -> tuple[str, str]:
        user_id = (getattr(config, "OPENVIKING_USER", "") or "agent-service").strip()
        agent_id = (getattr(config, "OPENVIKING_AGENT", "") or "skills").strip()
        return user_id, agent_id

    def _client_kwargs(self, client_cls, user_id: str, agent_id: str) -> dict[str, Any]:
        kwargs: dict[str, Any] = {}
        try:
            params = inspect.signature(client_cls).parameters
        except Exception:
            params = {}

        url = config.OPENVIKING_SERVER_URL
        if "url" in params:
            kwargs["url"] = url
        elif "base_url" in params:
            kwargs["base_url"] = url
        elif "endpoint" in params:
            kwargs["endpoint"] = url
        else:
            # openviking >=0.4 uses *args/**kwargs; always pass url
            kwargs["url"] = url
            kwargs["api_key"] = config.OPENVIKING_API_KEY

        if config.OPENVIKING_API_KEY:
            for key_name in ("api_key", "root_api_key", "token", "auth_token"):
                if key_name in params:
                    kwargs[key_name] = config.OPENVIKING_API_KEY
                    break

        if config.OPENVIKING_ACCOUNT:
            if "account" in params:
                kwargs["account"] = config.OPENVIKING_ACCOUNT
            elif "account_id" in params:
                kwargs["account_id"] = config.OPENVIKING_ACCOUNT

        if user_id:
            if "user_id" in params:
                kwargs["user_id"] = user_id
            elif "user" in params:
                kwargs["user"] = user_id

        if agent_id:
            if "agent_id" in params:
                kwargs["agent_id"] = agent_id
            elif "agent" in params:
                kwargs["agent"] = agent_id

        return kwargs

    def _apply_openviking_headers(self, client, user_id: str, agent_id: str) -> None:
        for attr, value in (
            ("api_key", config.OPENVIKING_API_KEY),
            ("account", config.OPENVIKING_ACCOUNT),
            ("account_id", config.OPENVIKING_ACCOUNT),
            ("user_id", user_id),
            ("user", user_id),
            ("agent_id", agent_id),
            ("agent", agent_id),
        ):
            if value and hasattr(client, attr):
                try:
                    setattr(client, attr, value)
                except Exception:
                    pass

        extra_headers = {}
        if config.OPENVIKING_API_KEY:
            extra_headers["X-API-Key"] = config.OPENVIKING_API_KEY
            extra_headers["Authorization"] = f"Bearer {config.OPENVIKING_API_KEY}"
            extra_headers["X-OpenViking-API-Key"] = config.OPENVIKING_API_KEY
        if config.OPENVIKING_ACCOUNT:
            extra_headers["X-OpenViking-Account"] = config.OPENVIKING_ACCOUNT
        if user_id:
            extra_headers["X-OpenViking-User"] = user_id
        if agent_id:
            extra_headers["X-OpenViking-Agent"] = agent_id

        for holder in (
            client,
            getattr(client, "_http", None),
            getattr(client, "client", None),
            getattr(client, "_client", None),
            getattr(client, "http_client", None),
            getattr(client, "_http_client", None),
        ):
            headers = getattr(holder, "headers", None) if holder is not None else None
            if headers is None:
                continue
            try:
                for key, value in extra_headers.items():
                    headers.setdefault(key, value)
            except Exception:
                pass

    async def _new_openviking_client(self):
        if not config.OPENVIKING_SERVER_URL:
            raise RuntimeError("OPENVIKING_SERVER_URL is empty")

        import openviking as ov

        client_cls = getattr(ov, "AsyncHTTPClient", None)
        if client_cls is None:
            raise RuntimeError("openviking.AsyncHTTPClient is not available")

        user_id, agent_id = self._openviking_identity()

        try:
            client = client_cls(**self._client_kwargs(client_cls, user_id=user_id, agent_id=agent_id))
        except TypeError:
            client = client_cls(config.OPENVIKING_SERVER_URL)

        self._apply_openviking_headers(client, user_id=user_id, agent_id=agent_id)
        initialize = getattr(client, "initialize", None)
        if initialize is not None:
            await self._maybe_await(initialize())
        # initialize() may create the actual HTTP client, so patch headers after it too.
        self._apply_openviking_headers(client, user_id=user_id, agent_id=agent_id)
        return client

    async def _close_openviking_client(self, client) -> None:
        for name in ("aclose", "close"):
            method = getattr(client, name, None)
            if method is None:
                continue
            try:
                await self._maybe_await(method())
                return
            except Exception:
                return

    def _openviking_add_skill(self, skill_md_path: str):
        async def run():
            client = await self._new_openviking_client()
            try:
                add_skill = getattr(client, "add_skill", None)
                if add_skill is None:
                    raise RuntimeError("OpenViking HTTP client has no add_skill API")
                try:
                    return await self._maybe_await(add_skill(skill_md_path, wait=True))
                except TypeError:
                    return await self._maybe_await(add_skill(skill_md_path))
            finally:
                await self._close_openviking_client(client)

        return _run_coro_sync(run())

    def _openviking_ls(self, uri: str, simple: bool = False):
        async def run():
            client = await self._new_openviking_client()
            try:
                ls = getattr(client, "ls", None)
                if ls is None:
                    raise RuntimeError("OpenViking HTTP client has no ls API")
                try:
                    return await self._maybe_await(ls(uri, simple=simple))
                except TypeError:
                    return await self._maybe_await(ls(uri))
            finally:
                await self._close_openviking_client(client)

        return _run_coro_sync(run())

    def _openviking_read(self, uri: str):
        async def run():
            client = await self._new_openviking_client()
            try:
                read = getattr(client, "read", None)
                if read is None:
                    raise RuntimeError("OpenViking HTTP client has no read API")
                return await self._maybe_await(read(uri))
            finally:
                await self._close_openviking_client(client)

        return _run_coro_sync(run())

    def _openviking_rm(self, uri: str):
        async def run():
            client = await self._new_openviking_client()
            try:
                rm = getattr(client, "rm", None)
                if rm is None:
                    raise RuntimeError("OpenViking HTTP client has no rm API")
                return await self._maybe_await(rm(uri))
            finally:
                await self._close_openviking_client(client)

        return _run_coro_sync(run())

    @staticmethod
    def _format_completed_process(proc: subprocess.CompletedProcess) -> str:
        stdout = (proc.stdout or "").strip()
        stderr = (proc.stderr or "").strip()

        if proc.returncode == 0:
            return stdout or "执行成功（无输出）"

        output = f"执行失败，退出码：{proc.returncode}"
        if stdout:
            output += f"\n[stdout]\n{stdout}"
        if stderr:
            output += f"\n[stderr]\n{stderr}"
        return output

    def _run_local_command(self, command: list[str], cwd: Path | None = None, timeout: int | None = None) -> str:
        self._ensure_dirs()
        proc = subprocess.run(
            command,
            cwd=str(cwd or self.skill_root),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout or config.DEFAULT_TIMEOUT_SECONDS,
        )
        return self._format_completed_process(proc)

    def _external_vm_target(self) -> str:
        host = config.CLAW_EXTERNAL_VM_HOST.strip()
        user = config.CLAW_EXTERNAL_VM_USER.strip()
        if not host:
            raise RuntimeError("CLAW_EXTERNAL_VM_HOST is required when CLAW_DOWNLOAD_MODE=external-vm")
        return f"{user}@{host}" if user else host

    def _ssh_base_command(self) -> list[str]:
        command = ["ssh", "-p", str(config.CLAW_EXTERNAL_VM_PORT)]
        if not config.CLAW_EXTERNAL_VM_STRICT_HOST_KEY_CHECKING:
            command.extend([
                "-o", "StrictHostKeyChecking=no",
                "-o", "UserKnownHostsFile=/dev/null",
            ])
        if config.CLAW_EXTERNAL_VM_SSH_KEY:
            command.extend(["-i", config.CLAW_EXTERNAL_VM_SSH_KEY])
        command.append(self._external_vm_target())
        return command

    def _run_external_vm_shell(self, script: str, timeout: int | None = None) -> str:
        proc = subprocess.run(
            self._ssh_base_command() + [script],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout or config.DEFAULT_TIMEOUT_SECONDS,
        )
        return self._format_completed_process(proc)

    def _run_clawhub(self, args: list[str], timeout: int | None = None) -> str:
        clawhub_bin = config.CLAW_EXTERNAL_VM_CLAWHUB_BIN or "clawhub"

        if config.CLAW_DOWNLOAD_MODE in {"local", "container"}:
            return self._run_local_command([clawhub_bin, *args], timeout=timeout)

        if config.CLAW_DOWNLOAD_MODE not in {"external-vm", "external_vm", "ssh"}:
            raise RuntimeError(f"unsupported CLAW_DOWNLOAD_MODE={config.CLAW_DOWNLOAD_MODE!r}")

        command = " ".join(shlex.quote(str(part)) for part in [clawhub_bin, *args])
        return self._run_external_vm_shell(command, timeout=timeout)

    @staticmethod
    @staticmethod
    @staticmethod
    @staticmethod
    def _looks_like_dsh_path(token: str) -> bool:
        """是不是"路径样"的参数：`.`、含分隔符或盘符，且**纯 ASCII 无空格**。

        纯 ASCII 这条是为了防止把中文提示词里带 `/` 的句子误判成路径。
        """
        text = str(token or "").strip()
        if not text or len(text) > 260:
            return False
        if text in {".", "./", ".\\", ".."}:
            return True
        if " " in text or "\t" in text:
            return False
        try:
            text.encode("ascii")
        except UnicodeEncodeError:
            return False
        return (
            "/" in text
            or "\\" in text
            or (len(text) > 2 and text[1] == ":")
        )

    @classmethod
    @staticmethod
    def _dsh_args_are_placeholders(parts: list[str]) -> bool:
        """参数是不是没替换的占位符（`<工作区>`、`<任务>`、`{task}` 之类）。"""
        for raw in parts or []:
            token = str(raw or "").strip()
            if not token:
                continue
            if token.startswith("<") and token.endswith(">"):
                return True
            if token.startswith("{") and token.endswith("}"):
                return True
            if "<工作区>" in token or "<任务>" in token or "<workspace>" in token.lower():
                return True
        return False

    # 参数里一旦混进这些字样，说明是把上一轮的报错/用法文本当参数传了
    _DSH_DIRTY_MARKERS = (
        "工具调用:", "【dsh ", "工作区要写在", "用法 工具调用",
    )

    @classmethod
    def _dsh_args_are_dirty(cls, parts: list[str]) -> bool:
        """参数里是否混进了报错/用法文本（会把 dsh 那边污染成垃圾回合）。"""
        tokens = [str(raw or "") for raw in parts or []]
        for token in tokens:
            for marker in cls._DSH_DIRTY_MARKERS:
                if marker in token:
                    return True
        # 只有"报错三件套"齐全才当脏（正常任务里可能只出现其中一个词）
        joined = "\n".join(tokens)
        if "错误:" in joined and "用法" in joined and "工具调用" in joined:
            return True
        # 多段参数里出现"工作区路径"/"任务"这种说明词，基本是把用法抄进来了
        stripped = [token.strip() for token in tokens if token.strip()]
        if len(stripped) >= 3 and any(token in {"工作区路径", "任务"} for token in stripped):
            return True
        return False

    @classmethod
    def _split_dsh_args(cls, parts: list[str]) -> tuple[str, str, str]:
        """把 `工具调用:dsh|...` 的参数拆成 (工作区, 权限预设, 要求)。

        约定（与 TOOL.json 里的 `args: 工作区路径|任务内容` 一致，不做位置猜测）：
        - 第一段位置参数 = 工作区（`.\` 或路径）；不给就写 `.` 或留空；
        - 其余位置参数拼成任务内容；
        - `ws=<路径>` / `perm=<预设>` 是显式声明，可放在任意位置。
        """
        workspace = ""
        preset = ""
        prompt_parts: list[str] = []

        for raw in parts or []:
            part = str(raw).strip()
            if not part:
                continue
            lowered = part.lower()
            head = lowered.split("=", 1)[0]
            if "=" in part and head in {
                "ws", "workspace", "cwd", "dir", "perm", "permission"
            }:
                key, value = part.split("=", 1)
                value = value.strip()
                if head in {"perm", "permission"}:
                    preset = value
                elif value:
                    workspace = value
                continue
            prompt_parts.append(part)

        # 位置写法：只有**第一段**才算工作区
        if not workspace and prompt_parts and cls._looks_like_dsh_path(prompt_parts[0]):
            workspace = prompt_parts.pop(0)

        return workspace, preset, "\n".join(prompt_parts)

    @classmethod
    def _dsh_args_misordered(cls, parts: list[str]) -> bool:
        """第一段不是路径、但后面某段像路径 → 说明顺序写反了。"""
        positional = [
            str(raw).strip() for raw in parts or []
            if str(raw).strip() and "=" not in str(raw)
        ]
        if len(positional) < 2:
            return False
        if cls._looks_like_dsh_path(positional[0]):
            return False
        return any(cls._looks_like_dsh_path(token) for token in positional[1:])

    def _first_arg(args: list[str], kwargs: dict[str, str], *names: str) -> str:
        for name in names:
            value = kwargs.get(name)
            if value:
                return str(value)
        return str(args[0]) if args else ""

    def _skill_md_path(self, skill_slug: str) -> Path:
         # 1. 先尝试原始 slug（保留 @），例如 @steipete/weather
        original_dir = self.skill_root / skill_slug
        for filename in ("SKILL.md", "skill.md"):
            candidate = original_dir / filename
            if candidate.exists():
                return candidate

        # 2. 若失败，去掉 @ 再试，或加上 @ 再试
        clean_slug = skill_slug.lstrip("@")
        clean_dir = self.skill_root / clean_slug
        for filename in ("SKILL.md", "skill.md"):
            candidate = clean_dir / filename
            if candidate.exists():
                return candidate

        # 2b. 若 slug 不带 @，尝试在第一段前加 @（steipete/weather -> @steipete/weather）
        if not skill_slug.startswith("@") and "/" in clean_slug:
            at_slug = "@" + clean_slug
            at_dir = self.skill_root / at_slug
            for filename in ("SKILL.md", "skill.md"):
                candidate = at_dir / filename
                if candidate.exists():
                    return candidate
        # 3. 若还是失败，取最后一段（不带 @）进行模糊匹配
        short_slug = clean_slug.rsplit("/", 1)[-1]
        if short_slug != clean_slug:
            short_dir = self.skill_root / short_slug
            for filename in ("SKILL.md", "skill.md"):
                candidate = short_dir / filename
                if candidate.exists():
                    return candidate

        # 4. 目录名可能以 short_slug 开头（如 weather-aditya），遍历查找
        if self.skill_root.exists():
            for entry in sorted(self.skill_root.iterdir()):
                if not entry.is_dir():
                    continue
                name = entry.name.lower()
                slug_lower = short_slug.lower()
                if name == slug_lower or name.startswith(slug_lower + "-") or name.startswith(slug_lower + "_"):
                    for filename in ("SKILL.md", "skill.md"):
                        candidate = entry / filename
                        if candidate.exists():
                            return candidate

        # 5. 都没找到，返回原始 clean_dir 下的 SKILL.md（用于报错提示）
        return clean_dir / "SKILL.md"

    def dispatch(
        self,
        tool_name: str,
        args: list[str],
        kwargs: dict[str, str],
        user_workspace: Path,
        timeout: int,
    ) -> str:
        name = (tool_name or "").strip()

        # ClawHub 相关工具：部署时未勾选 clawhub 功能则给出明确提示
        clawhub_tools = {
            "clawhub-search", "search_skill", "skill-search", "search-skill",
            "clawhub-install", "download_skill", "install_skill", "skill-install", "install-skill",
            "clawhub-list",
            "skill-delete",
        }
        if name in clawhub_tools and not config.ENABLE_CLAWHUB:
            return (
                "错误：ClawHub 未启用。部署 tool-runtime 时未勾选「clawhub 技能执行」功能，"
                "无法搜索/安装/卸载技能。如需启用，请重新运行 deploy-all.ps1 并勾选 clawhub，"
                "或设置环境变量 ENABLE_CLAWHUB=true 后重启 tool-runtime。"
            )

        # OpenViking（技能知识库）相关工具：部署时未勾选 openviking-server 则提示未启用
        openviking_tools = {
            "skill-list", "skill-list-simple",
            "skill-abstract", "skill-overview", "skill-manual",
            "add-skill-to-viking",
        }
        if name in openviking_tools and not config.ENABLE_OPENVIKING:
            return (
                "错误：OpenViking 未启用。部署时未勾选 openviking-server（技能知识库），"
                "无法查询或导入技能。如需启用，请重新运行 deploy-all.ps1 并勾选 openviking-server，"
                "或设置环境变量 ENABLE_OPENVIKING=true 后重启 tool-runtime。"
            )

        if name in {"clawhub-search", "search_skill", "skill-search", "search-skill"}:
            keyword = self._first_arg(args, kwargs, "keyword", "query", "name")
            return self.clawhub_search(keyword, timeout=timeout)

        if name in {"clawhub-install", "download_skill", "install_skill", "skill-install", "install-skill"}:
            skill_slug = self._first_arg(args, kwargs, "skill_slug", "skill", "name")
            return self.clawhub_install(skill_slug, timeout=timeout)

        if name == "clawhub-list":
            return self.clawhub_list(timeout=timeout)

        if name == "skill-list":
            return self.skill_list()

        if name == "skill-list-simple":
            return self.skill_list_simple()

        if name == "skill-delete":
            skill_slug = self._first_arg(args, kwargs, "skill_slug", "skill", "name")
            return self.skill_delete(skill_slug, timeout=timeout)

        if name == "skill-abstract":
            skill_name = self._first_arg(args, kwargs, "skill_name", "skill", "name")
            return self.skill_abstract(skill_name)

        if name == "skill-overview":
            skill_name = self._first_arg(args, kwargs, "skill_name", "skill", "name")
            return self.skill_overview(skill_name)

        if name == "skill-manual":
            skill_name = self._first_arg(args, kwargs, "skill_name", "skill", "name")
            return self.skill_manual(skill_name)

        if name == "add-skill-to-viking":
            skill_slug = self._first_arg(args, kwargs, "skill_slug", "skill", "name")
            return self.add_skill_to_viking(skill_slug)

        # dsh（DeepSeek Harness）外部大脑：`工具调用:dsh|工作区|要求`
        # 工作区可留空（用调用者的工作区，服务端按 DSH_PATH_MAP 翻译）；
        # 只给一个参数且不像路径时，当作“要求”。会话由工作区决定（同一工作区=同一 dsh 会话）。
        if name in {"dsh", "dsh-chat"}:
            parts = [str(a).strip() for a in args]
            if self._dsh_args_are_dirty(parts):
                return (
                    "错误：dsh 的参数里混进了上次的报错/用法文本，已拒绝发送。\n"
                    "请重新组织成两段：工具调用:dsh|工作区|任务，例如 "
                    "工具调用:dsh|.|用 pwsh 打印当前目录"
                )
            if self._dsh_args_are_placeholders(parts):
                return (
                    "错误：dsh 的参数是占位符，没有替换成真实值。\n"
                    "用法 工具调用:dsh|工作区路径|任务，例如："
                    "工具调用:dsh|.|用 pwsh 打印当前目录"
                )
            if self._dsh_args_misordered(parts):
                return (
                    "错误：工作区要写在**最前面**。用法 工具调用:dsh|工作区路径|任务\n"
                    "默认工作区写 `.`，例如：工具调用:dsh|.|打印当前目录"
                )
            workspace, preset, prompt = self._split_dsh_args(parts)
            if not prompt:
                return (
                    "错误：dsh 需要任务内容。用法 工具调用:dsh|工作区路径|任务"
                    "（默认工作区写 `.`）"
                )
            return dsh_client.ask(
                prompt,
                workspace=workspace,
                preset=preset,
                session_id=str(kwargs.get("session_id") or ""),
                user_id=str(kwargs.get("user_id") or ""),
                user_workspace=user_workspace,
                timeout=timeout,
            )

        return self.run_skill(name, args=args, user_workspace=user_workspace, timeout=timeout)

    def clawhub_search(self, keyword: str, timeout: int) -> str:
        if not keyword:
            return "错误：搜索关键词不能为空"
        return self._run_clawhub(["search", keyword], timeout=timeout)

    def clawhub_install(self, skill_slug: str, timeout: int) -> str:
        if not skill_slug:
            return "错误：技能名称不能为空"

        self._ensure_dirs()
        result = self._run_clawhub(
            ["install", skill_slug, "--dir", config.CLAW_EXTERNAL_VM_SKILL_ROOT_DIR, "--force"],
            timeout=timeout,
        )
        # 安装失败时跳过 Viking 导入
        if "执行失败" in result:
            return f"❌ 安装失败：{result}"

        add_result = self.add_skill_to_viking(skill_slug)

        if add_result.startswith("✅"):
            return f"✅ 安装并导入 Viking 知识库：{skill_slug}\n{add_result}\n{result}"
        return f"⚠️ 安装命令已执行，但导入技能失败：{add_result}\n{result}"

    def add_skill_to_viking(self, skill_slug: str) -> str:
        if not skill_slug:
            return "❌ 技能名称不能为空"

        try:
            skill_md_path = self._skill_md_path(skill_slug)
            if not skill_md_path.exists():
                return f"❌ 技能 {skill_slug} 不存在，缺少 SKILL.md"

            # 导入前先规范化 SKILL.md（幂等：正常文件无改动）。
            # ClawHub 分发的技能常见「过度转义」+ CRLF：首行是 \---（而不是 ---）、
            # 正文里 \## \- \* 与 HTML 实体 &#x20;，
            # 会让 OpenViking 直接报 "SKILL.md must have YAML frontmatter"，
            # 于是出现「安装命令已执行，但导入技能失败」——安装好了却用不了。
            if self._normalize_skill_md(skill_md_path):
                print(f"[skill] 已规范化 SKILL.md（去转义/CRLF/HTML实体）: {skill_md_path}", flush=True)

            try:
                add_result = self._openviking_add_skill(str(skill_md_path))
            except Exception:
                # 兜底：规范化后仍导入失败时，再规范一次并重试；仍失败才向上抛。
                if not self._normalize_skill_md(skill_md_path):
                    raise
                print(f"[skill] SKILL.md 二次规范化后重试导入: {skill_md_path}", flush=True)
                add_result = self._openviking_add_skill(str(skill_md_path))

            uri = add_result.get("uri", "") if isinstance(add_result, dict) else ""
            return f"✅ 技能导入成功：{skill_slug} | URI: {uri}"
        except Exception as exc:
            return f"❌ 导入技能失败：{exc}"

    # ClawHub 技能里常见的过度转义 → 原文替换表。
    # 顺序有讲究：长模式在前（\\--- 必须先于 \\-；\\## 必须先于 \\#），
    # 否则会被短模式先吃掉一半、留下不合法的 frontmatter。
    _SKILL_MD_FIXES: tuple[tuple[str, str], ...] = (
        ("\\---", "---"),
        ("\\##", "##"),
        ("\\#", "#"),
        ("\\-", "-"),
        ("\\*", "*"),
        ("&#x20;", " "),
    )

    @classmethod
    def _normalize_skill_md(cls, path: Path) -> bool:
        """把被转义/CRLF 污染的 SKILL.md 还原成可被解析的原文；返回是否发生了改动。

        只做「安装即失败」这几类确定性修复，不改动正常技能文件：
        1. CRLF / CR → LF
        2. \\--- \\## \\# \\- \\* → 去掉反斜杠
        3. HTML 实体 &#x20; → 空格
        """
        try:
            original = path.read_bytes().decode("utf-8", errors="replace")
        except Exception:
            return False

        fixed = original.replace("\r\n", "\n").replace("\r", "\n")
        for src, dst in cls._SKILL_MD_FIXES:
            fixed = fixed.replace(src, dst)

        if fixed == original:
            return False

        # 修复完仍不是 YAML frontmatter 开头时不做无意义写入
        if not fixed.lstrip().startswith("---"):
            return False

        try:
            path.write_text(fixed, encoding="utf-8")
        except Exception as exc:
            print(f"[skill] 规范化 SKILL.md 写入失败 {path}: {exc}", flush=True)
            return False
        return True

    def clawhub_list(self, timeout: int) -> str:
        return self._run_clawhub(["list"], timeout=timeout)

    def skill_delete(self, skill_slug: str, timeout: int) -> str:
        if not skill_slug:
            return "错误：技能名称不能为空"

        try:
            self._openviking_rm(f"viking://user/agent-service/skills/{skill_slug}")
        except Exception:
            pass

        uninstall_result = self._run_clawhub(["uninstall", "--yes", skill_slug], timeout=timeout)

        skill_dir = self.skill_root / skill_slug
        if skill_dir.exists():
            shutil.rmtree(skill_dir)

        return f"✅ 技能已完全卸载（Viking 已移除 + 文件夹已删除）：{skill_slug}\n{uninstall_result}"

    def skill_list(self) -> str:
        try:
            skills = self._openviking_ls("viking://user/agent-service/skills/")
            if not skills:
                return "📭 Viking 知识库中暂无任何技能"

            output = "📚 已安装技能列表：\n"
            for index, skill in enumerate(skills, 1):
                if isinstance(skill, dict):
                    name = skill.get("name", "未命名")
                    desc = skill.get("abstract", "无描述")
                else:
                    name = str(skill).rstrip("/").rsplit("/", 1)[-1]
                    desc = ""
                output += f"{index}. {name} - {desc}\n"
            return output.strip()
        except Exception as exc:
            return f"❌ 获取技能列表失败：{exc}"

    def skill_list_simple(self) -> str:
        try:
            names = self._openviking_ls("viking://user/agent-service/skills/", simple=True)
            if not names:
                return "📭 暂无技能"
            names = [n.rstrip("/").rsplit("/", 1)[-1] if "/" in str(n) else str(n) for n in names]
            return "已安装技能：\n" + "\n".join(f"- {name}" for name in names)
        except Exception as exc:
            return f"❌ 获取失败：{exc}"

    def skill_abstract(self, skill_name: str) -> str:
        if not skill_name:
            return "读取 abstract 失败，请提供技能名"
        try:
            return self._openviking_read(f"viking://user/agent-service/skills/{skill_name}/.abstract.md") or "无简介"
        except Exception:
            return f"读取 abstract 失败,请检查知识库中是否有名为{skill_name}的技能"

    def skill_overview(self, skill_name: str) -> str:
        if not skill_name:
            return "读取 overview 失败，请提供技能名"
        try:
            return self._openviking_read(f"viking://user/agent-service/skills/{skill_name}/.overview.md") or "无使用说明"
        except Exception:
            return f"读取 overview 失败,请检查知识库中是否有名为{skill_name}的技能"

    def skill_manual(self, skill_name: str) -> str:
        if not skill_name:
            return "读取 SKILL.md 失败，请提供技能名"
        try:
            return self._openviking_read(f"viking://user/agent-service/skills/{skill_name}/SKILL.md") or "无执行文档"
        except Exception:
            return f"读取 SKILL.md 失败,请检查知识库中是否有名为{skill_name}的技能"

    # ── 技能脚本调用（参数边界）──────────────────────────────────

    # 明显是“占位说明”而不是真参数的标记
    _PLACEHOLDER_MARKERS = ("参数留空", "占位符", "请填写", "请输入", "待填", "示例参数")

    @classmethod
    def _looks_like_placeholder(cls, arg: str) -> bool:
        """判断一个参数是不是“占位说明”而不是真参数。

        真实模型会写出 `,参数留空)`、`<关键词>`、`【商品名】` 这类东西 ——
        直接塞进 argv 只会换来 argparse 的 `invalid choice`，对模型毫无指向性。
        """
        text = str(arg or "").strip()
        if not text:
            return True
        # <...>、【...】、[...]、（...）
        if re.fullmatch(r"[<【\[（(].*[>】\]）)]", text):
            return True
        if any(marker in text for marker in cls._PLACEHOLDER_MARKERS):
            return True
        # 形如 ",参数留空)" 的残片
        if text[0] in ",，" and ("参数" in text or "留空" in text):
            return True
        return False

    @classmethod
    def _sanitize_skill_args(cls, args: Iterable[str]) -> tuple[list[str], list[str]]:
        """清洗参数：去引号/反引号/空白，剔除占位说明。返回 (有效参数, 被丢弃的)。"""
        cleaned: list[str] = []
        dropped: list[str] = []
        for raw in args or []:
            text = str(raw).strip().strip("`").strip()
            text = text.strip('"').strip("'").strip()
            if not text:
                continue
            if cls._looks_like_placeholder(text):
                dropped.append(text)
                continue
            cleaned.append(text)
        return cleaned, dropped

    def _run_skill_capture(
        self,
        command: list[str],
        skill_dir: Path,
        user_workspace: Path,
        timeout: int,
    ) -> subprocess.CompletedProcess:
        """执行技能脚本（与 run_skill 同一套 env/cwd）。"""
        return subprocess.run(
            command,
            cwd=str(skill_dir),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            env={
                **os.environ,
                "MY_AGENT_WORKSPACE": str(user_workspace),
                "MY_AGENT_SKILL_ROOT": str(self.skill_root),
                "MY_AGENT_SKILL_DIR": str(skill_dir),
            },
        )

    def _skill_usage(
        self,
        script: list[str],
        skill_dir: Path,
        user_workspace: Path,
        timeout: int,
    ) -> tuple[str, bool]:
        """跑一次 `<script> --help`，返回 (输出, 是否成功)。"""
        try:
            proc = self._run_skill_capture(script + ["--help"], skill_dir, user_workspace, timeout)
        except Exception as exc:
            return f"{type(exc).__name__}: {exc}", False
        text = ((proc.stdout or "") + (proc.stderr or "")).strip()
        return text or "（无输出）", proc.returncode == 0

    @staticmethod
    def _looks_like_usage_error(proc: subprocess.CompletedProcess) -> bool:
        """argparse 的参数错误：退出码 2 且带 usage:/error:。"""
        if proc.returncode != 2:
            return False
        stderr = proc.stderr or ""
        return ("usage:" in stderr) or ("error:" in stderr)

    def run_skill(self, skill_name: str, args: Iterable[str], user_workspace: Path, timeout: int) -> str:
        if not skill_name:
            return "错误：技能名称不能为空"

        skill_dir = self.skill_root / skill_name
        script_file = skill_dir / "scripts" / f"{skill_name}.py"

        if not script_file.exists():
            return (
                "本技能无法通过该方式调用，请阅读下面文档学习该任务的执行流程\n"
                + self.skill_overview(skill_name)
            )

        script = [sys.executable, str(script_file)]
        cleaned, dropped = self._sanitize_skill_args(args)
        dropped_note = (
            f"（已忽略无效参数：{'、'.join(dropped)}）\n" if dropped else ""
        )

        # 1) 没有有效参数 → 不执行脚本本体，先给用法（让模型下一轮能写对）
        if not cleaned:
            usage, ok = self._skill_usage(script, skill_dir, user_workspace, timeout)
            if ok:
                return (
                    f"【缺少参数：{skill_name}】\n"
                    f"{dropped_note}"
                    f"用法（{script_file.name} --help）：\n{usage}\n"
                    f"请按上面的用法补参数后重试，例如：工具调用:{skill_name}|<子命令>|<参数>"
                )
            return (
                f"【缺少参数：{skill_name}】\n"
                f"{dropped_note}"
                f"该技能需要参数，但 --help 没有可用输出：\n{usage}\n"
                "请阅读技能文档（SKILL.md）后再调用。"
            )

        command = script + cleaned
        command_text = " ".join(command)
        proc = self._run_skill_capture(command, skill_dir, user_workspace, timeout)
        output = self._format_completed_process(proc)

        if proc.returncode == 0:
            return f"【执行完成：{skill_name}】\n命令：{command_text}\n结果：{output}"

        # 2) 参数不正确（argparse usage 错误）→ 直接附上正确用法
        if self._looks_like_usage_error(proc):
            usage, ok = self._skill_usage(script, skill_dir, user_workspace, timeout)
            hint = (
                f"用法（{script_file.name} --help）：\n{usage}"
                if ok
                else "（该技能没有 --help 输出，请阅读技能文档 SKILL.md）"
            )
            return (
                f"【参数不正确：{skill_name}】\n"
                f"命令：{command_text}\n{dropped_note}"
                f"结果：{output}\n{hint}"
            )

        # 3) 真失败：保留原始输出，另给一条自查指引
        return (
            f"【执行失败：{skill_name}】\n"
            f"命令：{command_text}\n{dropped_note}"
            f"结果：{output}\n"
            f"（若怀疑是参数问题，可用 工具调用:{skill_name}|--help 查看用法）"
        )

skill_runtime = SkillRuntime()
