import copy
import json
import os
from pathlib import Path
from typing import Any

from app import config


class ModelProfileStore:
    """
    模型配置读取器。

    支持三种格式：

    1. 新推荐格式 model_list.json：
    {
      "default": "main",
      "models": {
        "main": {
          "provider": "openai_compatible",
          "api_url": "...",
          "method": "POST",
          "model": "...",
          "api_key_env": "MOONSHOT_API_KEY",
          "temperature": 0.7,
          "model_params": {}
        }
      },
      "aliases": {
        "default-main": "main",
        "dujiawei-main": "main"
      }
    }

    2. 旧版 model_profiles.json：
    {
      "default": "default-main",
      "profiles": {
        "default-main": {...}
      }
    }

    3. 原 orchestrator / agent_list 风格的平铺模型配置：
    {
      "main": {
        "api_url": "...",
        "method": "POST",
        "model": "...",
        "api_key": "...",
        "temperature": 0.9,
        "model_params": {}
      },
      "tool": {...}
    }

    注意：
    - 每次 get_profile() 都会重新读取配置文件，方便通过 PV 实时调整模型配置。
    - commit_limit / files / system_prompt 等字段属于 orchestrator，不会传给模型接口。
    - api_key_env 优先从环境变量读取；没有 api_key_env 时才使用 api_key 字段。
    """

    def __init__(self, model_list_path: Path, fallback_profiles_path: Path):
        self.model_list_path = model_list_path
        self.fallback_profiles_path = fallback_profiles_path

    def _active_path(self) -> Path:
        if self.model_list_path.exists():
            return self.model_list_path
        return self.fallback_profiles_path

    def load(self) -> dict[str, Any]:
        """
        每次调用都从磁盘/PV 重新读取配置。

        这样修改 /app/config/model_list.json 后，不需要重启 model-proxy-service Pod；
        下一次 ChatCompletion 请求会读取最新配置。
        """
        path = self._active_path()
        if not path.exists():
            raise FileNotFoundError(
                f"model list not found: {self.model_list_path} "
                f"or fallback {self.fallback_profiles_path}"
            )

        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def get_profile(self, name: str | None) -> dict[str, Any]:
        data = self.load()

        # 新格式：models + aliases
        if "models" in data:
            return self._get_from_models(data, name)

        # 旧格式：profiles
        if "profiles" in data:
            return self._get_from_profiles(data, name)

        # 原 agent_list 风格：平铺 main/tool/reader
        return self._get_from_flat_agent_list(data, name)

    def _get_from_models(self, data: dict[str, Any], name: str | None) -> dict[str, Any]:
        models = data.get("models", {})
        aliases = data.get("aliases", {})
        default_name = data.get("default", "")

        profile_name = name or default_name
        profile_name = aliases.get(profile_name, profile_name)

        if profile_name not in models:
            fallback = aliases.get(default_name, default_name)
            if fallback and fallback in models:
                profile_name = fallback
            else:
                raise KeyError(f"model profile not found: {name}")

        profile = copy.deepcopy(models[profile_name])
        profile["profile_name"] = profile_name
        return self._normalize_profile(profile)

    def _get_from_profiles(self, data: dict[str, Any], name: str | None) -> dict[str, Any]:
        profiles = data.get("profiles", {})
        default_name = data.get("default", "")

        profile_name = name or default_name

        if profile_name not in profiles:
            if default_name and default_name in profiles:
                profile_name = default_name
            else:
                raise KeyError(f"model profile not found: {name}")

        profile = copy.deepcopy(profiles[profile_name])
        profile["profile_name"] = profile_name
        return self._normalize_profile(profile)

    def _get_from_flat_agent_list(self, data: dict[str, Any], name: str | None) -> dict[str, Any]:
        default_name = data.get("default", "main")
        if isinstance(default_name, dict):
            default_name = "main"

        profile_name = name or default_name

        # 兼容 orchestrator 传 default-main / default-tool / default-reader
        aliases = {
            "default-main": "main",
            "default-tool": "tool",
            "default-reader": "reader",
        }
        profile_name = aliases.get(profile_name, profile_name)

        if profile_name not in data:
            if "main" in data:
                profile_name = "main"
            else:
                raise KeyError(f"model profile not found: {name}")

        profile = copy.deepcopy(data[profile_name])
        profile["profile_name"] = profile_name
        return self._normalize_profile(profile)

    def _normalize_profile(self, profile: dict[str, Any]) -> dict[str, Any]:
        # 原 agent_list 里没有 provider，这里根据 api_url 做一个保守推断。
        if "provider" not in profile:
            api_url = str(profile.get("api_url", "")).lower()
            if "/api/chat" in api_url or "ollama" in api_url:
                profile["provider"] = "ollama"
            else:
                profile["provider"] = "openai_compatible"

        # 环境变量优先，避免把 key 固化在 ConfigMap。
        api_key_env = profile.get("api_key_env", "")
        if api_key_env:
            profile["api_key"] = os.getenv(api_key_env, profile.get("api_key", ""))

        # 去掉 orchestrator 专属字段，避免误传。
        profile.pop("files", None)
        profile.pop("commit_limit", None)
        profile.pop("system_prompt", None)

        return profile


profile_store = ModelProfileStore(
    model_list_path=config.MODEL_LIST_PATH,
    fallback_profiles_path=config.MODEL_PROFILES_PATH,
)
