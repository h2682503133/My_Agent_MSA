"""
OpenViking context adapter for My_Agent_MSA.

Matches original My_Agent/core/Agent/Agent.py semantics:
- full_session_id = f"{agent_id}_{session_id}", e.g. main_web_h268
- user_id is the real OpenViking user identity
- use official HTTP session APIs directly:
    get_session(session_id, auto_create=True)
    add_message(session_id, role, content=..., role_id=...)
    get_session_context(session_id, token_budget=...)
    commit_session(session_id)
"""

import asyncio
import inspect
import re
import threading
from typing import Any

from app import config
from app.logger import debug_log, log
from app.text_utils import clean_text


def _run_coro_sync(coro):
    """Run an async call from sync gRPC code without reusing event-loop clients."""
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


class OpenVikingServerBackend:
    def __init__(self, url: str, api_key: str = "", account: str = ""):
        self.url = (url or "").rstrip("/")
        self.api_key = api_key or ""
        self.account = account or "my-agent"

    async def _maybe_await(self, value):
        if inspect.isawaitable(value):
            return await value
        return value

    def _client_kwargs(self, client_cls, user_id: str, agent_id: str) -> dict[str, Any]:
        kwargs: dict[str, Any] = {}

        try:
            params = inspect.signature(client_cls).parameters
        except Exception:
            params = {}

        if "url" in params:
            kwargs["url"] = self.url
        elif "base_url" in params:
            kwargs["base_url"] = self.url
        elif "endpoint" in params:
            kwargs["endpoint"] = self.url

        if self.api_key:
            for key_name in ("api_key", "root_api_key", "token", "auth_token"):
                if key_name in params:
                    kwargs[key_name] = self.api_key
                    break

        if self.account:
            if "account" in params:
                kwargs["account"] = self.account
            elif "account_id" in params:
                kwargs["account_id"] = self.account

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

    async def _new_client(self, user_id: str, agent_id: str):
        if not self.url:
            raise RuntimeError("OPENVIKING_SERVER_URL is empty")

        import openviking as ov

        client_cls = getattr(ov, "AsyncHTTPClient", None)
        if client_cls is None:
            raise RuntimeError("openviking.AsyncHTTPClient is not available")

        try:
            client = client_cls(**self._client_kwargs(client_cls, user_id=user_id, agent_id=agent_id))
        except TypeError:
            client = client_cls(self.url)

        self._apply_headers(client, user_id=user_id, agent_id=agent_id)

        initialize = getattr(client, "initialize", None)
        if initialize is not None:
            await self._maybe_await(initialize())

        # initialize() creates _http; patch it too for root-key mode.
        self._apply_headers(client, user_id=user_id, agent_id=agent_id)
        return client

    def _apply_headers(self, client, user_id: str, agent_id: str) -> None:
        for attr, value in (
            ("api_key", self.api_key),
            ("root_api_key", self.api_key),
            ("account", self.account),
            ("account_id", self.account),
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
        if self.api_key:
            extra_headers["X-API-Key"] = self.api_key
            extra_headers["Authorization"] = f"Bearer {self.api_key}"
            extra_headers["X-OpenViking-API-Key"] = self.api_key
        if self.account:
            extra_headers["X-OpenViking-Account"] = self.account
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

    async def _close_client(self, client) -> None:
        for name in ("aclose", "close"):
            method = getattr(client, name, None)
            if method is None:
                continue
            try:
                await self._maybe_await(method())
                return
            except Exception:
                return

    async def ping(self, user_id: str = "system", agent_id: str = "system") -> bool:
        client = await self._new_client(user_id=user_id, agent_id=agent_id)
        try:
            if hasattr(client, "health"):
                await self._maybe_await(client.health())
                return True
            if hasattr(client, "list_sessions"):
                await self._maybe_await(client.list_sessions())
                return True
            return True
        finally:
            await self._close_client(client)

    async def ensure_session(self, client, full_session_id: str) -> dict[str, Any]:
        get_session = getattr(client, "get_session", None)
        if get_session is None:
            raise RuntimeError("OpenViking HTTP client has no get_session API")

        try:
            result = await self._maybe_await(get_session(session_id=full_session_id, auto_create=True))
        except TypeError:
            result = await self._maybe_await(get_session(full_session_id, auto_create=True))

        debug_log(f"OpenViking get_session auto_create ok: {full_session_id}")
        return result or {}

    def _clean_content(self, content: str) -> str:
        return clean_text(re.sub(r"</?think>", "", str(content or "")).strip())

    def _extract_parts_text(self, item: Any) -> str:
        if item is None:
            return ""
        if isinstance(item, str):
            return item
        if isinstance(item, dict):
            if "text" in item:
                return str(item.get("text") or "")
            if "content" in item:
                return str(item.get("content") or "")
            if "parts" in item and item.get("parts"):
                return self._extract_parts_text(item["parts"][0])
            return ""
        if hasattr(item, "text"):
            return str(getattr(item, "text") or "")
        if hasattr(item, "content"):
            return str(getattr(item, "content") or "")
        if hasattr(item, "parts") and getattr(item, "parts"):
            return self._extract_parts_text(getattr(item, "parts")[0])
        return ""

    def _message_to_role_content(self, msg: Any) -> tuple[str, str]:
        if isinstance(msg, dict):
            role = str(msg.get("role", ""))
            if msg.get("parts"):
                content = self._extract_parts_text(msg["parts"][0])
            else:
                content = str(msg.get("content") or msg.get("text") or "")
        else:
            role = str(getattr(msg, "role", ""))
            if getattr(msg, "parts", None):
                content = self._extract_parts_text(getattr(msg, "parts")[0])
            else:
                content = str(getattr(msg, "content", "") or getattr(msg, "text", ""))
        return role, self._clean_content(content)

    def _messages_from_context(
        self,
        ctx: dict[str, Any],
        max_messages: int,
    ) -> tuple[str, list[dict[str, Any]], list[dict[str, str]]]:
        session_summary = ""
        memories: list[dict[str, Any]] = []
        messages: list[dict[str, str]] = []

        latest_summary = str(
            ctx.get("latest_archive_overview", "") or ctx.get("session_summary", "") or ""
        ).strip()
        if latest_summary:
            session_summary = latest_summary

        for arc in ctx.get("pre_archive_abstracts", []) or ctx.get("memories", []) or []:
            if not isinstance(arc, dict):
                continue
            content = str(arc.get("abstract", "") or arc.get("content", "") or "").strip()
            if content:
                memories.append({
                    "memory_id": arc.get("id", "") or arc.get("memory_id", ""),
                    "content": content,
                    "score": float(arc.get("score", 0.0) or 0.0),
                    "token_count": int(arc.get("token_count", 0) or 0),
                })

        candidates = []
        for key in ("current_messages", "messages", "recent_messages"):
            vals = ctx.get(key)
            if vals:
                candidates = list(vals)
                break

        if max_messages and len(candidates) > max_messages * 2:
            candidates = candidates[-max_messages * 2:]

        for msg in candidates:
            try:
                role, content = self._message_to_role_content(msg)
                if content and "智能体返回：" not in content:
                    messages.append({"role": role, "content": content})
            except Exception:
                continue

        return session_summary, memories, messages

    async def search_context(
        self,
        user_id: str,
        agent_id: str,
        full_session_id: str,
        query: str,
        max_messages: int,
        max_tokens: int,
        commit_limit: int,
    ) -> dict[str, Any]:
        client = await self._new_client(user_id=user_id, agent_id=agent_id)
        try:
            await self.ensure_session(client, full_session_id)

            get_session_context = getattr(client, "get_session_context", None)
            if get_session_context is None:
                raise RuntimeError("OpenViking HTTP client has no get_session_context API")

            try:
                ctx = await self._maybe_await(get_session_context(
                    session_id=full_session_id,
                    token_budget=max_tokens or config.DEFAULT_TOKEN_BUDGET,
                ))
            except TypeError:
                ctx = await self._maybe_await(get_session_context(
                    full_session_id,
                    token_budget=max_tokens or config.DEFAULT_TOKEN_BUDGET,
                ))

            session_summary, memories, messages = self._messages_from_context(
                ctx or {},
                max_messages or config.DEFAULT_MAX_MESSAGES,
            )
            return {
                "session_summary": session_summary,
                "memories": memories,
                "recent_messages": messages,
                "error": "",
            }
        finally:
            await self._close_client(client)

    def _extract_message_count(self, session_info: Any) -> int:
        if not isinstance(session_info, dict):
            return 0
        for candidate in (
            session_info,
            session_info.get("meta") or {},
            session_info.get("metadata") or {},
            session_info.get("session") or {},
            session_info.get("result") or {},
        ):
            if not isinstance(candidate, dict):
                continue
            for key in ("message_count", "total_message_count", "messages_count"):
                try:
                    value = int(candidate.get(key, 0) or 0)
                except Exception:
                    value = 0
                if value:
                    return value
            messages = candidate.get("messages")
            if isinstance(messages, list):
                return len(messages)
        return 0

    async def _add_message_compat(
        self,
        add_message,
        session_id: str,
        role: str,
        content: str,
        role_id: str = "",
    ) -> None:
        """
        Compatibility wrapper for OpenViking HTTP client versions where
        add_message() may not accept role_id.
        """
        try:
            params = inspect.signature(add_message).parameters
        except Exception:
            params = {}

        kwargs = {
            "session_id": session_id,
            "role": role,
            "content": content,
        }

        if role_id and (not params or "role_id" in params):
            kwargs_with_role = dict(kwargs)
            kwargs_with_role["role_id"] = role_id
            try:
                await self._maybe_await(add_message(**kwargs_with_role))
                return
            except TypeError:
                pass

        await self._maybe_await(add_message(**kwargs))

    async def append_turn(
        self,
        user_id: str,
        agent_id: str,
        raw_session_id: str,
        full_session_id: str,
        user_message: str,
        assistant_message: str,
        tool_summaries: list[str],
        commit_limit: int,
    ) -> tuple[bool, str]:
        client = await self._new_client(user_id=user_id, agent_id=agent_id)
        try:
            await self.ensure_session(client, full_session_id)

            add_message = getattr(client, "add_message", None)
            if add_message is None:
                raise RuntimeError("OpenViking HTTP client has no add_message API")

            await self._add_message_compat(
                add_message,
                session_id=full_session_id,
                role="user",
                content=f"<{raw_session_id}>{user_message}",
                role_id=user_id,
            )
            await self._add_message_compat(
                add_message,
                session_id=full_session_id,
                role="assistant",
                content=assistant_message,
                role_id=agent_id or "main",
            )

            for summary in tool_summaries or []:
                if summary:
                    await self._add_message_compat(
                        add_message,
                        session_id=full_session_id,
                        role="assistant",
                        content=f"工具摘要：{summary}",
                        role_id=agent_id or "main",
                    )

            if commit_limit:
                try:
                    session_info = await self.ensure_session(client, full_session_id)
                    message_count = self._extract_message_count(session_info)
                    if message_count > commit_limit:
                        commit_session = getattr(client, "commit_session", None)
                        if commit_session is not None:
                            debug_log(f"[session-commit] {full_session_id} 提交 {message_count} 条记录")
                            await self._maybe_await(commit_session(session_id=full_session_id))
                except Exception as exc:
                    debug_log(f"commit check failed: {exc}")

            debug_log(f"server append_turn user={user_id} session={full_session_id}")
            return True, ""
        except Exception as exc:
            return False, str(exc)
        finally:
            await self._close_client(client)


class VikingStore:
    def __init__(self):
        self.mode = "mock" if config.MOCK_VIKING else config.OPENVIKING_BACKEND
        self.file_fallback = bool(config.OPENVIKING_FILE_FALLBACK)
        self.server = None

        config.VIKING_DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.file_context_path = config.VIKING_DATA_DIR / "file_context_store.json"

        if self.mode == "mock":
            log("OpenViking mock mode enabled")
            return

        if self.mode == "file":
            log(f"OpenViking file context mode enabled: {self.file_context_path}")
            return

        if self.mode == "server":
            self.server = OpenVikingServerBackend(
                config.OPENVIKING_SERVER_URL,
                config.OPENVIKING_API_KEY,
                config.OPENVIKING_ACCOUNT,
            )
            try:
                _run_coro_sync(self.server.ping(user_id="system", agent_id="system"))
                log(f"OpenViking server backend connected: {config.OPENVIKING_SERVER_URL}")
            except Exception as exc:
                log(f"OpenViking server init failed: {exc}")
                if self.file_fallback:
                    self.mode = "file"
                    log(f"fallback to file context mode: {self.file_context_path}")
                else:
                    self.mode = "mock"
                    log("fallback to mock mode")
            return

        log(f"Unknown OPENVIKING_BACKEND={self.mode}, fallback to file context")
        self.mode = "file"

    def full_session_id(self, agent_id: str, session_id: str) -> str:
        return f"{agent_id or 'main'}_{session_id}"

    def _mock_context(self) -> dict[str, Any]:
        return {
            "session_summary": "",
            "memories": [],
            "recent_messages": [
                {"role": "system", "content": "MOCK_VIKING=true：当前未连接真实 OpenViking，上下文为空。"}
            ],
            "error": "",
        }

    def search_context(
        self,
        user_id: str,
        session_id: str,
        agent_id: str,
        query: str,
        max_messages: int,
        max_tokens: int,
        commit_limit: int,
    ) -> dict[str, Any]:
        full_id = self.full_session_id(agent_id, session_id)

        if self.mode == "mock":
            return self._mock_context()

        if self.mode == "server":
            try:
                return _run_coro_sync(self.server.search_context(
                    user_id=user_id,
                    agent_id=agent_id or "main",
                    full_session_id=full_id,
                    query=query,
                    max_messages=max_messages,
                    max_tokens=max_tokens,
                    commit_limit=commit_limit,
                ))
            except Exception as exc:
                debug_log(f"server search_context failed: {exc}")
                if not self.file_fallback:
                    return {"session_summary": "", "memories": [], "recent_messages": [], "error": str(exc)}

        return {"session_summary": "", "memories": [], "recent_messages": [], "error": ""}

    def append_turn(
        self,
        user_id: str,
        session_id: str,
        agent_id: str,
        task_id: str,
        user_message: str,
        assistant_message: str,
        tool_summaries: list[str],
        commit_limit: int,
    ) -> tuple[bool, str]:
        full_id = self.full_session_id(agent_id, session_id)

        if self.mode == "mock":
            debug_log(f"mock append_turn user={user_id} session={full_id} task={task_id}")
            return True, ""

        if self.mode == "server":
            ok, error = _run_coro_sync(self.server.append_turn(
                user_id=user_id,
                agent_id=agent_id or "main",
                raw_session_id=session_id,
                full_session_id=full_id,
                user_message=user_message,
                assistant_message=assistant_message,
                tool_summaries=tool_summaries,
                commit_limit=commit_limit,
            ))
            if not ok:
                debug_log(f"server append_turn failed: {error}")
            return ok, error

        return False, "file backend disabled"

    def add_skill_document(self, skill_name: str, version: str, content: str, source_path: str) -> tuple[bool, str, str]:
        return False, "", "skill docs are not wired to OpenViking server adapter yet"

    def list_skill_docs(self, simple: bool = True) -> tuple[list[str], str]:
        return [], ""

    def read_skill_doc(self, skill_name: str, doc_type: str) -> tuple[bool, str, str]:
        return False, "", "skill docs are not wired to OpenViking server adapter yet"

    def search_skill_docs(self, query: str, skill_names: list[str], top_k: int, max_tokens: int) -> tuple[list[dict[str, Any]], str]:
        return [], ""


store = VikingStore()
