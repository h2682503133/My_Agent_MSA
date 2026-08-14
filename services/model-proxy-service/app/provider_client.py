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

    def _base_headers(self, profile: dict[str, Any]) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}

        for key, value in profile.get("headers", {}).items():
            headers[str(key)] = str(value)

        api_key = profile.get("api_key", "")
        if api_key:
            auth_scheme = profile.get("auth_scheme", "Bearer")
            headers["Authorization"] = f"{auth_scheme} {api_key}"

        return headers

    def _merge_params(self, profile: dict[str, Any], params: dict[str, str]) -> dict[str, Any]:
        body = {}
        body.update(profile.get("model_params", {}) or {})

        # profile 默认值
        if "temperature" in profile:
            body["temperature"] = profile["temperature"]
        if "max_tokens" in profile:
            body["max_tokens"] = profile["max_tokens"]

        # request 覆盖值
        for key, value in params.items():
            if key in {"temperature", "top_p"}:
                try:
                    body[key] = float(value)
                except Exception:
                    body[key] = value
            elif key in {"max_tokens", "num_ctx", "num_predict"}:
                body[key] = _parse_int(value)
            elif key == "stream":
                body[key] = _to_bool(value)
            else:
                body[key] = value

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

        if tool_text and not text:
            text = tool_text
        elif not text and finish_reason == "content_filter":
            text = "【模型输出被安全策略拦截，请调整措辞后重试】"

        usage = data.get("usage", {})

        return {
            "ok": True,
            "text": text,
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
            text = msg.get("content", "")
            if not text:
                text = msg.get("reasoning_content", "") or msg.get("thinking", "")
        elif "response" in data:
            text = data.get("response", "")
        elif "text" in data:
            text = data.get("text", "")
        else:
            text = str(data)

        return {
            "ok": True,
            "text": text,
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
