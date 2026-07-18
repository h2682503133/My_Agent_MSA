from __future__ import annotations

import asyncio
import inspect
import os
import shlex
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Iterable

from app import config


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
        user_id = (getattr(config, "OPENVIKING_USER", "") or "system").strip()
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
            ("root_api_key", config.OPENVIKING_API_KEY),
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
    def _first_arg(args: list[str], kwargs: dict[str, str], *names: str) -> str:
        for name in names:
            value = kwargs.get(name)
            if value:
                return str(value)
        return str(args[0]) if args else ""

    def _skill_md_path(self, skill_slug: str) -> Path:
        skill_dir = self.skill_root / skill_slug
        for filename in ("SKILL.md", "skill.md"):
            candidate = skill_dir / filename
            if candidate.exists():
                return candidate
        return skill_dir / "SKILL.md"

    def dispatch(
        self,
        tool_name: str,
        args: list[str],
        kwargs: dict[str, str],
        user_workspace: Path,
        timeout: int,
    ) -> str:
        name = (tool_name or "").strip()

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

            add_result = self._openviking_add_skill(str(skill_md_path))
            uri = add_result.get("uri", "") if isinstance(add_result, dict) else ""
            return f"✅ 技能导入成功：{skill_slug} | URI: {uri}"
        except Exception as exc:
            return f"❌ 导入技能失败：{exc}"

    def clawhub_list(self, timeout: int) -> str:
        return self._run_clawhub(["list"], timeout=timeout)

    def skill_delete(self, skill_slug: str, timeout: int) -> str:
        if not skill_slug:
            return "错误：技能名称不能为空"

        try:
            self._openviking_rm(f"viking://agent/skills/{skill_slug}")
        except Exception:
            pass

        uninstall_result = self._run_clawhub(["uninstall", "--yes", skill_slug], timeout=timeout)

        skill_dir = self.skill_root / skill_slug
        if skill_dir.exists():
            shutil.rmtree(skill_dir)

        return f"✅ 技能已完全卸载（Viking 已移除 + 文件夹已删除）：{skill_slug}\n{uninstall_result}"

    def skill_list(self) -> str:
        try:
            skills = self._openviking_ls("viking://agent/skills/")
            if not skills:
                return "📭 Viking 知识库中暂无任何技能"

            output = "📚 已安装技能列表：\n"
            for index, skill in enumerate(skills, 1):
                if isinstance(skill, dict):
                    name = skill.get("name", "未命名")
                    desc = skill.get("abstract", "无描述")
                else:
                    name = str(skill)
                    desc = ""
                output += f"{index}. {name} - {desc}\n"
            return output.strip()
        except Exception as exc:
            return f"❌ 获取技能列表失败：{exc}"

    def skill_list_simple(self) -> str:
        try:
            names = self._openviking_ls("viking://agent/skills/", simple=True)
            if not names:
                return "📭 暂无技能"
            return "已安装技能：\n" + "\n".join(f"- {name}" for name in names)
        except Exception as exc:
            return f"❌ 获取失败：{exc}"

    def skill_abstract(self, skill_name: str) -> str:
        if not skill_name:
            return "读取 abstract 失败，请提供技能名"
        try:
            return self._openviking_read(f"viking://agent/skills/{skill_name}/.abstract.md") or "无简介"
        except Exception:
            return f"读取 abstract 失败,请检查知识库中是否有名为{skill_name}的技能"

    def skill_overview(self, skill_name: str) -> str:
        if not skill_name:
            return "读取 overview 失败，请提供技能名"
        try:
            return self._openviking_read(f"viking://agent/skills/{skill_name}/.overview.md") or "无使用说明"
        except Exception:
            return f"读取 overview 失败,请检查知识库中是否有名为{skill_name}的技能"

    def skill_manual(self, skill_name: str) -> str:
        if not skill_name:
            return "读取 SKILL.md 失败，请提供技能名"
        try:
            return self._openviking_read(f"viking://agent/skills/{skill_name}/SKILL.md") or "无执行文档"
        except Exception:
            return f"读取 SKILL.md 失败,请检查知识库中是否有名为{skill_name}的技能"

    def run_skill(self, skill_name: str, args: Iterable[str], user_workspace: Path, timeout: int) -> str:
        if not skill_name:
            return "错误：技能名称不能为空"

        skill_dir = self.skill_root / skill_name
        script_file = skill_dir / "scripts" / f"{skill_name}.py"

        if not script_file.exists():
            return "本技能无法通过该方式调用，请阅读下面文档学习该任务的执行流程\n"+self.skill_overview(skill_name)

        command = [sys.executable, str(script_file), *[str(arg) for arg in args]]
        proc = subprocess.run(
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
        output = self._format_completed_process(proc)
        command_text = " ".join(command)
        return f"【执行完成：{skill_name}】\n命令：{command_text}\n结果：{output}"


skill_runtime = SkillRuntime()
