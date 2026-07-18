import json
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

        text = ""
        if "choices" in data:
            text = data["choices"][0]["message"]["content"]
        elif "text" in data:
            text = data["text"]
        else:
            text = str(data)

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
            "messages": messages,
        })

        headers = self._base_headers(profile)
        response = self._request(method, api_url, headers, body)
        data = response.json()

        if "message" in data:
            text = data["message"].get("content", "")
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

    def _request(self, method: str, url: str, headers: dict[str, str], body: dict[str, Any]) -> requests.Response:
        last_exc = None

        # 对齐原 Agent.py：500 错误重试 1 次
        for attempt in range(2):
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

                debug_log(f"model provider returned {response.status_code}, retry {attempt + 1}")
                last_exc = RuntimeError(f"{response.status_code} {response.text}")

            except Exception as exc:
                last_exc = exc
                debug_log(f"model request exception, retry {attempt + 1}: {exc}")

        raise RuntimeError(str(last_exc) if last_exc else "model request failed")


provider_client = ProviderClient()
