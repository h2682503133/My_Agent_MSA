"""
OpenViking context adapter for My_Agent_MSA.

Matches original My_Agent/core/Agent/Agent.py semantics:
- full_session_id = f"{agent_id}_{session_id}", e.g. main_web_h268 (session_id = web_h268)
- user_id is the real OpenViking user identity
- use official HTTP session APIs directly:
    get_session(session_id, auto_create=True)
    add_message(session_id, role, content=..., role_id=...)
    get_session_context(session_id, token_budget=...)
    commit_session(session_id)
"""

import asyncio
import inspect
import json as _json
import re
import threading
import urllib.request
import urllib.error
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
    def __init__(self, url: str, api_key: str = "", account: str = "", account_mode: str = "fixed", root_api_key: str = ""):
        self.url = (url or "").rstrip("/")
        self.api_key = api_key or ""
        self.root_api_key = root_api_key or ""
        self.account = account or "my-agent"
        self.account_mode = account_mode or "fixed"
        self._user_api_keys: dict[str, str] = {}  # user_id -> per-user API key cache
    def _resolve_account(self, user_id: str) -> str:
        """所有用户共用同一个 OpenViking Account，靠 per-user API key 隔离。"""
        return self.account

    def _http_request(self, method: str, path: str, api_key: str, body: dict | None = None) -> dict:
        """同步 HTTP 请求 OpenViking REST API。"""
        url = f"{self.url}{path}"
        data = _json.dumps(body).encode("utf-8") if body else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("X-API-Key", api_key)
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return _json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body_text = e.read().decode("utf-8", errors="replace") if e.fp else ""
            raise RuntimeError(f"OpenViking HTTP {e.code}: {body_text}")

    def _ensure_openviking_user(self, user_id: str) -> str:
        """确保 OpenViking 中存在该用户，返回其 per-user API key。
        
        用 root_api_key 在 account 下创建用户，返回 user_key。
        如果用户已存在（409），尝试从缓存返回。
        """
        if not self.root_api_key:
            raise RuntimeError(
                "OPENVIKING_ROOT_API_KEY is required to create users. "
                "Set it via env or secret file."
            )

        account = self._resolve_account(user_id)
        debug_log(f"[user-create] creating OpenViking user {user_id} in account {account}")
        try:
            result = self._http_request(
                "POST",
                f"/api/v1/admin/accounts/{account}/users",
                self.root_api_key,
                {"user_id": user_id, "role": "user"},
            )
        except RuntimeError as e:
            error_msg = str(e)
            if "409" in error_msg or "already exists" in error_msg.lower():
                stored = self._fetch_key_from_user_service(user_id)
                if stored:
                    self._user_api_keys[user_id] = stored
                    log(f"[user-create] user {user_id} exists, using stored key")
                    return stored
                log(f"[user-create] user {user_id} exists, deleting and recreating")
                self._http_request("DELETE", f"/api/v1/admin/accounts/{account}/users/{user_id}", self.root_api_key)
                result = self._http_request(
                    "POST", f"/api/v1/admin/accounts/{account}/users", self.root_api_key,
                    {"user_id": user_id, "role": "user"},
                )
            else:
                raise

        user_key = (result.get("result", {}) or {}).get("user_key", "") or result.get("user_key", "") or result.get("api_key", "") or ""
        if user_key:
            self._user_api_keys[user_id] = user_key
            self._store_key_to_user_service(user_id, user_key)
            log(f"[user-create] OpenViking user {user_id} created, key cached")
        else:
            log(f"[user-create] OpenViking user {user_id} created but no key in response: {result}")

        return user_key

    def _fetch_key_from_user_service(self, user_id: str) -> str | None:
        """从 user-service HTTP 端点获取已存储的 API key。"""
        user_svc_url = getattr(config, "USER_SERVICE_URL", "")
        if not user_svc_url:
            return None
        try:
            import urllib.request as _ur
            url = f"{user_svc_url}/openviking_key/{user_id}"
            req = _ur.Request(url, method="GET")
            with _ur.urlopen(req, timeout=5) as resp:
                data = _json.loads(resp.read().decode("utf-8"))
                if data.get("ok") and data.get("api_key"):
                    return data["api_key"]
        except Exception:
            pass
        return None

    def _store_key_to_user_service(self, user_id: str, api_key: str) -> None:
        """将 API key 存储到 user-service。"""
        user_svc_url = getattr(config, "USER_SERVICE_URL", "")
        if not user_svc_url:
            return
        try:
            import urllib.request as _ur
            url = f"{user_svc_url}/openviking_key/{user_id}"
            body = _json.dumps({"api_key": api_key}).encode("utf-8")
            req = _ur.Request(url, data=body, method="POST")
            req.add_header("Content-Type", "application/json")
            _ur.urlopen(req, timeout=5)
        except Exception:
            pass

    def _resolve_user_token(self, user_id: str) -> str:
        """获取 per-user token（用于 X-OpenViking-User-Key header）。"""
        if user_id == "system":
            return ""
        cached = self._user_api_keys.get(user_id)
        if cached:
            return cached
        if self.root_api_key:
            try:
                return self._ensure_openviking_user(user_id)
            except Exception as exc:
                log(f"[user-create] failed to create user {user_id}: {exc}")
        return ""

    def _resolve_api_key(self, user_id: str) -> str:
        """API Key：system → agent-service key（技能文档等系统操作），其他 → per-user key。"""
        if user_id == "system":
            return self.api_key or self.root_api_key

        cached = self._user_api_keys.get(user_id)
        if cached:
            return cached
        if self.root_api_key:
            try:
                key = self._ensure_openviking_user(user_id)
                if key:
                    return key
            except Exception as exc:
                log(f"[user-create] failed to create user {user_id}: {exc}")
        return self.api_key


    async def _maybe_await(self, value):
        if inspect.isawaitable(value):
            return await value
        return value

    def _client_kwargs(self, client_cls, user_id: str, agent_id: str, api_key: str = "") -> dict[str, Any]:
        kwargs: dict[str, Any] = {}
        resolved_key = api_key or self.api_key

        try:
            params = inspect.signature(client_cls).parameters
        except Exception:
            params = {}

        has_var_keyword = any(
            param.kind == inspect.Parameter.VAR_KEYWORD for param in params.values()
        )

        if "url" in params:
            kwargs["url"] = self.url
        elif "base_url" in params:
            kwargs["base_url"] = self.url
        elif "endpoint" in params:
            kwargs["endpoint"] = self.url
        else:
            # openviking >=0.4 uses *args/**kwargs; always pass url
            kwargs["url"] = self.url
            kwargs["api_key"] = resolved_key

        if resolved_key:
            for key_name in ("api_key", "root_api_key", "token", "auth_token"):
                if key_name in params:
                    kwargs[key_name] = resolved_key
                    break

        account = self._resolve_account(user_id)
        if account:
            kwargs["account"] = account
            kwargs["account_id"] = account

        if user_id:
            kwargs["user_id"] = user_id
            kwargs["user"] = user_id

        if agent_id:
            kwargs["agent_id"] = agent_id
            kwargs["agent"] = agent_id

        if not has_var_keyword:
            # 只传构造函数接受的参数，避免 TypeError 导致 key/身份丢失
            kwargs = {key: value for key, value in kwargs.items() if key in params}

        return kwargs

    async def _new_client(self, user_id: str, agent_id: str):
        if not self.url:
            raise RuntimeError("OPENVIKING_SERVER_URL is empty")

        import openviking as ov

        client_cls = getattr(ov, "AsyncHTTPClient", None)
        if client_cls is None:
            raise RuntimeError("openviking.AsyncHTTPClient is not available")

        resolved_key = self._resolve_api_key(user_id)
        user_token = self._resolve_user_token(user_id)
        debug_log(f"[client] user_id={user_id} key={resolved_key[:30]}... token={user_token[:30] if user_token else 'NONE'}...")
        try:
            client = client_cls(**self._client_kwargs(client_cls, user_id=user_id, agent_id=agent_id, api_key=resolved_key))
        except TypeError:
            client = client_cls(self.url)

        self._apply_headers(client, user_id=user_id, agent_id=agent_id, api_key=resolved_key, user_token=user_token)

        initialize = getattr(client, "initialize", None)
        if initialize is not None:
            await self._maybe_await(initialize())

        # initialize() creates _http; patch it too for root-key mode.
        self._apply_headers(client, user_id=user_id, agent_id=agent_id, api_key=resolved_key, user_token=user_token)
        return client

    def _apply_headers(self, client, user_id: str, agent_id: str, api_key: str = "", user_token: str = "") -> None:
        account = self._resolve_account(user_id)
        resolved_key = api_key or self.api_key
        for attr, value in (
            ("api_key", resolved_key),
            ("_api_key", resolved_key),
            ("account", account),
            ("account_id", account),
            ("_account", account),
            ("user_id", user_id),
            ("user", user_id),
            ("_user_id", user_id),
            ("agent_id", agent_id),
            ("agent", agent_id),
            ("_actor_peer_id", agent_id),
        ):
            if value and hasattr(client, attr):
                try:
                    setattr(client, attr, value)
                except Exception:
                    pass

        extra_headers = {}
        if resolved_key:
            extra_headers["X-API-Key"] = resolved_key
            extra_headers["Authorization"] = f"Bearer {resolved_key}"
            extra_headers["X-OpenViking-API-Key"] = resolved_key
        if account:
            extra_headers["X-OpenViking-Account"] = account
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
                    headers[key] = value
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

    def _hit_to_content(self, hit: dict[str, Any]) -> str:
        for key in ("abstract", "overview", "content", "text"):
            value = str(hit.get(key) or "").strip()
            if value:
                return value
        return ""

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
        top_k: int = 8,
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

            # Semantic search for query-relevant memories
            if query and top_k > 0:
                try:
                    hits: list[dict[str, Any]] = []
                    find_method = getattr(client, "find", None)
                    if find_method is not None:
                        find_result = await self._maybe_await(find_method(query=query, limit=top_k))
                        if isinstance(find_result, dict):
                            hits = list(find_result.get("memories", []) or [])
                    if not hits:
                        search_result = await self._maybe_await(client.search(
                            query=query,
                            session_id=full_session_id,
                            limit=top_k,
                        ))
                        if isinstance(search_result, dict):
                            hits = list(search_result.get("memories", []) or [])

                    seen_contents = {m.get("content", "") for m in memories}
                    for hit in hits:
                        if not isinstance(hit, dict):
                            continue
                        content = self._hit_to_content(hit)
                        if content and content not in seen_contents:
                            seen_contents.add(content)
                            memories.append({
                                "memory_id": hit.get("id", "") or hit.get("memory_id", "") or hit.get("uri", ""),
                                "content": content,
                                "score": float(hit.get("score", 0.0) or 0.0),
                                "token_count": int(hit.get("token_count", 0) or 0),
                            })
                except Exception as exc:
                    debug_log(f"semantic search failed (non-fatal): {exc}")

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


    # ── Skill methods ──────────────────────────────────────────────

    async def add_skill_document(
        self, skill_name: str, version: str, content: str, source_path: str
    ) -> tuple[bool, str, str]:
        client = await self._new_client(user_id="system", agent_id="skills")
        try:
            add_skill = getattr(client, "add_skill", None)
            if add_skill is None:
                raise RuntimeError("OpenViking HTTP client has no add_skill API")
            result = await self._maybe_await(add_skill(source_path or content, wait=True))
            uri = result.get("uri", "") if isinstance(result, dict) else ""
            return True, uri, ""
        except Exception as exc:
            return False, "", str(exc)
        finally:
            await self._close_client(client)

    async def list_skill_docs(self) -> tuple[list[str], str]:
        client = await self._new_client(user_id="system", agent_id="skills")
        try:
            list_skills = getattr(client, "list_skills", None)
            if list_skills is None:
                raise RuntimeError("OpenViking HTTP client has no list_skills API")
            result = await self._maybe_await(list_skills())
            skills = result.get("skills", []) if isinstance(result, dict) else []
            names = []
            for s in skills:
                if isinstance(s, dict):
                    names.append(s.get("name", s.get("skill_name", "")))
                elif isinstance(s, str):
                    names.append(s)
            return names, ""
        except Exception as exc:
            return [], str(exc)
        finally:
            await self._close_client(client)

    async def read_skill_doc(self, skill_name: str, doc_type: str) -> tuple[bool, str, str]:
        client = await self._new_client(user_id="system", agent_id="skills")
        try:
            get_skill = getattr(client, "get_skill", None)
            if get_skill is None:
                raise RuntimeError("OpenViking HTTP client has no get_skill API")
            skill = await self._maybe_await(get_skill(skill_name, include_content=True, include_files=True))
            if not isinstance(skill, dict):
                return False, "", f"unexpected skill response type: {type(skill)}"
            if doc_type == "abstract":
                content = skill.get("abstract", "") or ""
            elif doc_type == "overview":
                content = skill.get("overview", "") or ""
            elif doc_type == "manual":
                files = skill.get("files", [])
                if isinstance(files, list):
                    for f in files:
                        if isinstance(f, dict) and f.get("name", "").upper() == "SKILL.MD":
                            content = f.get("content", "") or ""
                            break
                    else:
                        content = skill.get("content", "") or ""
                else:
                    content = skill.get("content", "") or ""
            else:
                content = skill.get("content", "") or ""
            return True, content, ""
        except Exception as exc:
            return False, "", str(exc)
        finally:
            await self._close_client(client)

    async def search_skill_docs(
        self, query: str, skill_names: list[str], top_k: int, max_tokens: int
    ) -> tuple[list[dict], str]:
        client = await self._new_client(user_id="system", agent_id="skills")
        try:
            find_skills = getattr(client, "find_skills", None)
            if find_skills is None:
                raise RuntimeError("OpenViking HTTP client has no find_skills API")
            result = await self._maybe_await(find_skills(query, limit=top_k))
            hits_raw = result.get("hits", []) if isinstance(result, dict) else []
            hits = []
            for h in hits_raw:
                if isinstance(h, dict):
                    hits.append({
                        "skill_name": h.get("skill_name", h.get("name", "")),
                        "doc_id": h.get("doc_id", h.get("uri", "")),
                        "chunk_id": h.get("chunk_id", ""),
                        "version": h.get("version", ""),
                        "title": h.get("title", ""),
                        "content": h.get("content", h.get("text", "")),
                        "token_count": int(h.get("token_count", 0) or 0),
                        "score": float(h.get("score", 0.0) or 0.0),
                    })
            return hits, ""
        except Exception as exc:
            return [], str(exc)
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
                config.OPENVIKING_ACCOUNT_MODE,
                config.OPENVIKING_ROOT_API_KEY,
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
        return f"{agent_id}_{session_id}"

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
        top_k: int = 8,
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
                    top_k=top_k,
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
        if self.mode == "mock":
            return True, "", ""
        if self.mode == "server":
            return _run_coro_sync(self.server.add_skill_document(
                skill_name=skill_name, version=version, content=content, source_path=source_path
            ))
        return False, "", "file backend disabled"

    def list_skill_docs(self, simple: bool = True) -> tuple[list[str], str]:
        if self.mode == "mock":
            return [], ""
        if self.mode == "server":
            return _run_coro_sync(self.server.list_skill_docs())
        return [], ""

    def read_skill_doc(self, skill_name: str, doc_type: str) -> tuple[bool, str, str]:
        if self.mode == "mock":
            return True, "", ""
        if self.mode == "server":
            return _run_coro_sync(self.server.read_skill_doc(
                skill_name=skill_name, doc_type=doc_type
            ))
        return False, "", "file backend disabled"

    def search_skill_docs(self, query: str, skill_names: list[str], top_k: int, max_tokens: int) -> tuple[list[dict[str, Any]], str]:
        if self.mode == "mock":
            return [], ""
        if self.mode == "server":
            return _run_coro_sync(self.server.search_skill_docs(
                query=query, skill_names=skill_names, top_k=top_k, max_tokens=max_tokens
            ))
        return [], ""


store = VikingStore()
