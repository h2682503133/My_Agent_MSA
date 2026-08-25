import json
import time
from typing import Any

import requests

from app import config
from app.logger import debug_log


def _to_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).lower() in {"1", "true", "yes", "on"}


def _parse_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default


class ProviderClient:
    def chat_completion(
        self,
        profile: dict[str, Any],
        messages: list[dict[str, str]],
        params: dict[str, str],
    ) -> dict[str, Any]:
        provider = profile.get("provider", "openai_compatible")
        if provider in {"openai", "openai_compatible"}:
            return self._call_openai_compatible(profile, messages, params)
        if provider in {"ollama", "ollama_chat"}:
            return self._call_ollama(profile, messages, params)

        # 默认按 OpenAI-compatible 尝试
        return self._call_openai_compatible(profile, messages, params)

    def embedding(
        self,
        profile: dict[str, Any],
        texts: list[str],
        params: dict[str, str],
    ) -> dict[str, Any]:
        """文本向量化：支持 OpenAI-compatible / Ollama 嵌入接口。

        profile 指向 model_list.json 中配置的 embedding 模型：
        {
          "embedding": {
            "provider": "openai_compatible",
            "api_url": "https://.../v1/embeddings",
            "model": "text-embedding-v4",
            ...
          }
        }
        """
        texts = [t for t in (texts or []) if t is not None]
        if not texts:
            raise RuntimeError("embedding input texts is empty")

        provider = profile.get("provider", "openai_compatible")
        if provider in {"ollama", "ollama_chat"}:
            return self._embed_ollama(profile, texts, params)
        return self._embed_openai_compatible(profile, texts, params)

    def _embed_openai_compatible(
        self,
        profile: dict[str, Any],
        texts: list[str],
        params: dict[str, str],
    ) -> dict[str, Any]:
        api_url = profile["api_url"]
        method = profile.get("method", "POST").upper()
        model = profile["model"]

        body = dict(profile.get("model_params", {}) or {})
        for key, value in params.items():
            body[key] = value
        body.update({"model": model, "input": texts})

        headers = self._base_headers(profile)
        response = self._request(method, api_url, headers, body)
        data = response.json()

        items: list[dict[str, Any]] = []
        if isinstance(data, dict) and isinstance(data.get("data"), list):
            # OpenAI 风格：data: [{embedding: [...], index: N}]
            for entry in data["data"]:
                if not isinstance(entry, dict):
                    continue
                vec = entry.get("embedding")
                if not isinstance(vec, list):
                    continue
                items.append({
                    "index": _parse_int(entry.get("index", len(items))),
                    "vector": [float(x) for x in vec],
                })
        elif isinstance(data, dict) and isinstance(data.get("embeddings"), list):
            # 部分兼容实现直接返回 embeddings 数组
            for i, vec in enumerate(data["embeddings"]):
                if not isinstance(vec, list):
                    continue
                items.append({"index": i, "vector": [float(x) for x in vec]})

        if not items:
            raise RuntimeError(f"embedding response has no vectors: {str(data)[:300]}")

        return {
            "ok": True,
            "embeddings": items,
            "provider": profile.get("provider", "openai_compatible"),
            "model": model,
            "error": "",
        }

    def _embed_ollama(
        self,
        profile: dict[str, Any],
        texts: list[str],
        params: dict[str, str],
    ) -> dict[str, Any]:
        api_url = profile["api_url"]
        model = profile["model"]

        body = dict(profile.get("model_params", {}) or {})
        for key, value in params.items():
            body[key] = value
        body.update({"model": model, "input": texts})

        headers = self._base_headers(profile)
        response = self._request("POST", api_url, headers, body)
        data = response.json()

        embeddings = data.get("embeddings") or []
        items: list[dict[str, Any]] = []
        for i, vec in enumerate(embeddings):
            if not isinstance(vec, list):
                continue
            items.append({"index": i, "vector": [float(x) for x in vec]})

        if not items:
            raise RuntimeError(f"embedding response has no vectors: {str(data)[:300]}")

        return {
            "ok": True,
            "embeddings": items,
            "provider": profile.get("provider", "ollama"),
            "model": model,
            "error": "",
        }

    def _base_headers(self, profile: dict[str, Any]) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}

        for key, value in profile.get("headers", {}).items():
            headers[str(key)] = str(value)

        api_key = profile.get("api_key", "")
        if api_key:
            auth_scheme = profile.get("auth_scheme", "Bearer")
            headers["Authorization"] = f"{auth_scheme} {api_key}"

        return headers

    # ── 统一采样参数（canonical）：用户只需在 model_params 顶层写这些键，
    #    按 provider 自动适配重排（ollama → options 嵌套；openai → 顶层）──
    _CANONICAL_FLOAT_KEYS = {
        "temperature", "top_p", "min_p", "tfs_z",
        "repetition_penalty", "presence_penalty", "frequency_penalty",
        "mirostat_tau", "mirostat_eta",
    }
    _CANONICAL_INT_KEYS = {
        "max_tokens", "num_ctx", "num_predict",
        "top_k", "repeat_last_n", "mirostat", "seed",
    }
    _CANONICAL_ALIASES = {"repeat_penalty": "repetition_penalty"}  # 兼容 ollama 旧写法
    _OLLAMA_SAMPLE_KEYS = {
        "temperature", "top_p", "top_k", "min_p", "tfs_z",
        "repetition_penalty", "repeat_last_n",
        "mirostat", "mirostat_tau", "mirostat_eta",
        "num_ctx", "num_predict", "seed",
    }
    _OPENAI_SAMPLE_KEYS = {
        "temperature", "top_p",
        "presence_penalty", "frequency_penalty",
        "max_tokens", "seed",
    }

    def _coerce_sampling_value(self, key: str, value: Any) -> Any:
        """canonical 采样键统一转数值类型（gRPC params 是字符串，model_params 是 JSON 原生类型）。"""
        if key in self._CANONICAL_FLOAT_KEYS:
            try:
                return float(value)
            except Exception:
                return value
        if key in self._CANONICAL_INT_KEYS:
            try:
                return int(float(value))
            except Exception:
                return value
        return value

    def _collect_sampling(self, body: dict) -> dict:
        """从 body 顶层 + 旧 options 包装收集 canonical 采样键（options 值优先，兼容 ollama 原始写法）。"""
        sampling: dict[str, Any] = {}
        nested = body.pop("options", None)
        sources = [body] + ([nested] if isinstance(nested, dict) else [])
        for src in sources:
            for key in list(src.keys()):
                value = src[key]
                k = self._CANONICAL_ALIASES.get(key, key)
                if k in self._CANONICAL_FLOAT_KEYS or k in self._CANONICAL_INT_KEYS:
                    sampling.setdefault(k, value)
                    if src is body:
                        body.pop(key, None)
        return sampling

    def _rearrange_sampling(self, profile: dict, body: dict, sampling: dict) -> dict:
        """按 provider 重排采样键：ollama → body.options；openai → 顶层；双向转换 max_tokens/num_predict。"""
        is_ollama = profile.get("provider") in {"ollama", "ollama_chat"}
        if is_ollama:
            # OpenAI 习惯写 max_tokens → ollama 用 num_predict
            if "max_tokens" in sampling and "num_predict" not in sampling:
                sampling["num_predict"] = sampling["max_tokens"]
            options = {k: v for k, v in sampling.items() if k in self._OLLAMA_SAMPLE_KEYS}
            body["options"] = options
        else:
            # ollama 习惯写 num_predict → OpenAI 用 max_tokens
            if "num_predict" in sampling and "max_tokens" not in sampling:
                sampling["max_tokens"] = sampling["num_predict"]
            for k, v in sampling.items():
                if k in self._OPENAI_SAMPLE_KEYS:
                    body[k] = v
                else:
                    debug_log(f"model_proxy: 参数 {k} 不被 openai_compatible 支持，已忽略")
        return body

    def _merge_params(self, profile: dict[str, Any], params: dict[str, str]) -> dict[str, Any]:
        body = {}
        body.update(profile.get("model_params", {}) or {})

        # profile 顶层默认（向后兼容）
        if "temperature" in profile:
            body.setdefault("temperature", profile["temperature"])
        if "max_tokens" in profile:
            body.setdefault("max_tokens", profile["max_tokens"])

        # request 覆盖值（类型转换）
        for key, value in params.items():
            if key == "stream":
                body[key] = _to_bool(value)
            else:
                body[key] = self._coerce_sampling_value(key, value)

        # 统一采样适配 + 重排（非 canonical 键原样保留）
        sampling = self._collect_sampling(body)
        body = self._rearrange_sampling(profile, body, sampling)

        body["stream"] = False
        return body

    def _call_openai_compatible(
        self,
        profile: dict[str, Any],
        messages: list[dict[str, str]],
        params: dict[str, str],
    ) -> dict[str, Any]:
        api_url = profile["api_url"]
        method = profile.get("method", "POST").upper()
        model = profile["model"]

        body = self._merge_params(profile, params)
        body.update({
            "model": model,
            "messages": messages,
        })

        headers = self._base_headers(profile)

        response = self._request(method, api_url, headers, body)
        data = response.json()

        text, finish_reason, tool_text = self._parse_openai_compatible(data)

        # content 为空且没有工具调用时，原样重发一次。
        # content_filter 是安全拦截，重发大概率仍被拦截，直接返回提示。
        if not text and not tool_text and finish_reason != "content_filter":
            response = self._request(method, api_url, headers, body)
            data = response.json()
            text, finish_reason, tool_text = self._parse_openai_compatible(data)

        # 透传模型原生思考（deepseek reasoning_content 等），供 orchestrator 两阶段世界书使用；
        # 不影响 text/重发逻辑。
        reasoning = ""
        try:
            choices = data.get("choices") or []
            if choices and isinstance(choices[0], dict):
                _msg = choices[0].get("message") or {}
                if isinstance(_msg, dict):
                    reasoning = str(_msg.get("reasoning_content") or _msg.get("thinking") or "").strip()
        except Exception:
            reasoning = ""

        if tool_text and not text:
            text = tool_text
        elif not text and finish_reason == "content_filter":
            text = "【模型输出被安全策略拦截，请调整措辞后重试】"

        usage = data.get("usage", {})

        return {
            "ok": True,
            "text": text,
            "reasoning": reasoning,
            "prompt_tokens": _parse_int(usage.get("prompt_tokens", 0)),
            "completion_tokens": _parse_int(usage.get("completion_tokens", 0)),
            "provider": profile.get("provider", "openai_compatible"),
            "model": model,
            "error": "",
        }

    def _parse_openai_compatible(self, data: dict[str, Any]) -> tuple[str, str, str]:
        """解析 OpenAI-compatible 响应，返回 (text, finish_reason, tool_text)。

        - text 仅取 message.content；
        - content 为空时把 tool_calls 转成协议文本（工具调用:...）放入 tool_text；
        - reasoning_content 不在此处回填，空输出交给调用方决定是否重发。
        """
        text = ""
        finish_reason = ""
        tool_text = ""
        if "choices" in data:
            choices = data.get("choices") or []
            choice = choices[0] if choices else {}
            message = choice.get("message", {}) if isinstance(choice, dict) else {}
            finish_reason = str(choice.get("finish_reason") or "")
            text = message.get("content") or ""
            if not text:
                tool_text = self._format_tool_calls(message.get("tool_calls"))
        elif "text" in data:
            text = data["text"]
        else:
            text = str(data)
        return text, finish_reason, tool_text

    @staticmethod
    def _format_tool_calls(tool_calls: Any) -> str:
        """OpenAI tool_calls -> 协议文本「工具调用:name|arg...」，兼容上游语法解析。"""
        if not tool_calls:
            return ""

        shell_names = {"shell", "run-shell", "command"}
        lines = []
        for tc in tool_calls:
            if not isinstance(tc, dict):
                continue
            fn = tc.get("function") or {}
            name = str(fn.get("name") or "").strip()
            if not name:
                continue
            args = fn.get("arguments")
            if name.lower() in shell_names:
                command = ProviderClient._extract_shell_command(args)
                lines.append(f"工具调用:shell|{command}")
            else:
                args_text = ProviderClient._flatten_tool_arguments(args)
                lines.append(f"工具调用:{name}|{args_text}" if args_text else f"工具调用:{name}")
        return "\n".join(lines)

    @staticmethod
    def _flatten_tool_arguments(arguments: Any) -> str:
        """JSON 对象/数组/字符串 参数 -> 以 | 分隔的协议参数串。"""
        if isinstance(arguments, dict):
            return "|".join(str(v) for v in arguments.values() if str(v).strip())
        if isinstance(arguments, str):
            text = arguments.strip()
            if text.startswith("{") or text.startswith("["):
                try:
                    return ProviderClient._flatten_tool_arguments(json.loads(text))
                except Exception:
                    return text
            return text
        if isinstance(arguments, list):
            return "|".join(str(a) for a in arguments if str(a).strip())
        return str(arguments or "")

    @staticmethod
    def _extract_shell_command(arguments: Any) -> str:
        """shell 工具参数：优先取 command 字段，其余场景取第一个非空值。"""
        if isinstance(arguments, dict):
            command = str(arguments.get("command") or "").strip()
            if command:
                return command
            for value in arguments.values():
                if str(value).strip():
                    return str(value).strip()
            return ""
        if isinstance(arguments, str):
            text = arguments.strip()
            if text.startswith("{") or text.startswith("["):
                try:
                    return ProviderClient._extract_shell_command(json.loads(text))
                except Exception:
                    return text
            return text
        if isinstance(arguments, list):
            return "|".join(str(a) for a in arguments if str(a).strip())
        return str(arguments or "")

    def _call_ollama(
        self,
        profile: dict[str, Any],
        messages: list[dict[str, str]],
        params: dict[str, str],
    ) -> dict[str, Any]:
        api_url = profile["api_url"]
        method = profile.get("method", "POST").upper()
        model = profile["model"]

        body = self._merge_params(profile, params)
        body.update({
            "model": model,
            "messages": [self._to_ollama_message(m) for m in messages],
        })

        headers = self._base_headers(profile)
        response = self._request(method, api_url, headers, body)
        data = response.json()

        if "message" in data:
            msg = data["message"]
            reasoning = str(msg.get("reasoning_content", "") or msg.get("thinking", "") or "").strip()
            text = msg.get("content", "")
            if not text:
                text = reasoning
        elif "response" in data:
            text = data.get("response", "")
            reasoning = ""
        elif "text" in data:
            text = data.get("text", "")
            reasoning = ""
        else:
            text = str(data)
            reasoning = ""

        return {
            "ok": True,
            "text": text,
            "reasoning": reasoning,
            "prompt_tokens": _parse_int(data.get("prompt_eval_count", 0)),
            "completion_tokens": _parse_int(data.get("eval_count", 0)),
            "provider": profile.get("provider", "ollama"),
            "model": model,
            "error": "",
        }

    @staticmethod
    def _to_ollama_message(msg: dict[str, Any]) -> dict[str, Any]:
        """OpenAI 多模态 content 数组 -> Ollama 的 {content, images} 消息格式。"""
        content = msg.get("content")
        if not isinstance(content, list):
            return msg

        text_parts = []
        images: list[str] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get("type") == "text":
                text_parts.append(str(part.get("text") or ""))
            elif part.get("type") == "image_url":
                url = (part.get("image_url") or {}).get("url", "")
                if url:
                    if url.startswith("data:") and ";base64," in url:
                        images.append(url.split(";base64,", 1)[1])
                    else:
                        images.append(url)

        out = {"role": msg.get("role", "user"), "content": "".join(text_parts)}
        if images:
            out["images"] = images
        return out

    def _request(self, method: str, url: str, headers: dict[str, str], body: dict[str, Any]) -> requests.Response:
        last_exc = None

        # 失败后重试 2 次（共 3 次尝试），间隔 3 秒
        max_attempts = 3
        retry_delay = 3
        for attempt in range(max_attempts):
            try:
                if method == "GET":
                    response = requests.get(
                        url,
                        headers=headers,
                        json=body,
                        timeout=config.REQUEST_TIMEOUT_SECONDS,
                    )
                else:
                    response = requests.post(
                        url,
                        headers=headers,
                        json=body,
                        timeout=config.REQUEST_TIMEOUT_SECONDS,
                    )

                if response.status_code < 500:
                    response.raise_for_status()
                    return response

                debug_log(f"model provider returned {response.status_code}, attempt {attempt + 1}/{max_attempts}")
                last_exc = RuntimeError(f"{response.status_code} {response.text}")

            except Exception as exc:
                last_exc = exc
                debug_log(f"model request exception, attempt {attempt + 1}/{max_attempts}: {exc}")

            if attempt < max_attempts - 1:
                time.sleep(retry_delay)

        raise RuntimeError(str(last_exc) if last_exc else "model request failed")


provider_client = ProviderClient()
