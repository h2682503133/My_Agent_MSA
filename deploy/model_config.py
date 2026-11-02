"""模型配置生成器：把「模型来源」（远端 API / 本地 Ollama）写成 model_list.json。

模型解析（model-proxy）只看 `models` + `aliases`，不关心来源：

    models.main.api_url   = 远端 base_url  或  本地 Ollama 的 /api/chat
    aliases."default-main" = "main"        或  "local-ollama"

所以"选来源"= 生成不同的 entries/aliases，业务代码一行不用改。

用法（面板与 launcher 都调它，保持单一实现）：
  python -X utf8 deploy/model_config.py --data-dir <dir> --values <values.json>
  python -X utf8 deploy/model_config.py --data-dir <dir> --values <values.json> --print

values.json 需要的键（缺 key 时按"保留模板"处理）：
  model.source      remote | local
  model.base_url    远端 API 地址（remote）
  model.api_key     远端 API Key（remote）
  model.model       远端模型名（remote）
  ollama.url        本地地址，如 http://127.0.0.1:11434（local）
  ollama.chat_path  接口路径，默认 /api/chat（local）
  ollama.model      本地模型名（local）
  ollama.profile    entries 名，默认 local-ollama（local）
  embedding.source  remote | local（默认 remote：沿用模板里的 embedding entry）
  embedding.base_url / api_key / model   远端 embedding 覆盖（可选）
  ollama.embedding_model   本地 embedding 模型，默认 nomic-embed-text
  ollama.embed_path        本地 embedding 路径，默认 /api/embed（Ollama 新版返回 {embeddings:[[]]}）
  profiles          可选：要绑定的 profile 列表，默认 main,tool,reader,talker,writer
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

DEFAULT_PROFILES = ["main", "tool", "reader", "talker", "writer"]


def _load(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))  # 容忍 BOM（Windows 记事本/PowerShell 写出的文件）
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _build_remote(values: dict, base: dict, profiles: list[str]) -> dict:
    api_url = str(values.get("model.base_url") or "").strip()
    api_key = str(values.get("model.api_key") or "").strip()
    model_name = str(values.get("model.model") or "").strip()
    missing = [k for k, v in (("model.base_url", api_url), ("model.api_key", api_key), ("model.model", model_name)) if not v]
    if missing:
        raise ValueError(f"远端模型缺少必填：{', '.join(missing)}")

    models = dict(base.get("models") or {})
    aliases = dict(base.get("aliases") or {})
    for profile in profiles:
        entry = dict(models.get(profile) or {})
        entry.update({
            "provider": "openai_compatible",
            "api_url": api_url,
            "method": "POST",
            "model": model_name,
            "api_key": api_key,
        })
        entry.setdefault("temperature", 0.7)
        entry.setdefault("model_params", {})
        models[profile] = entry
        aliases[f"default-{profile}"] = profile
    return {"default": base.get("default", "main"), "models": models, "aliases": aliases}


def _build_local(values: dict, base: dict, profiles: list[str]) -> dict:
    url = str(values.get("ollama.url") or "").strip().rstrip("/")
    model_name = str(values.get("ollama.model") or "").strip()
    if not url or not model_name:
        raise ValueError("本地模型缺少必填：ollama.url / ollama.model")
    chat_path = str(values.get("ollama.chat_path") or "/api/chat").strip()
    if not chat_path.startswith("/"):
        chat_path = "/" + chat_path
    profile_name = str(values.get("ollama.profile") or "local-ollama").strip() or "local-ollama"

    models = dict(base.get("models") or {})
    aliases = dict(base.get("aliases") or {})
    models[profile_name] = {
        "provider": "ollama",
        "api_url": f"{url}{chat_path}",
        "method": "POST",
        "model": model_name,
        "api_key": "ollama",
        "temperature": 0.7,
        "model_params": {},
    }
    for profile in profiles:
        aliases[f"default-{profile}"] = profile_name
    return {"default": base.get("default", "main"), "models": models, "aliases": aliases}


def _apply_embedding(values: dict, payload: dict) -> dict:
    """embedding 单独选来源：remote 沿用/覆盖模板 entry，local 指向 Ollama 的 /api/embed。"""
    source = str(values.get("embedding.source") or "remote").strip().lower()
    models = dict(payload.get("models") or {})
    aliases = dict(payload.get("aliases") or {})
    if source == "local":
        url = str(values.get("ollama.url") or "").strip().rstrip("/")
        if not url:
            raise ValueError("本地 embedding 缺少 ollama.url")
        path = str(values.get("ollama.embed_path") or "/api/embed").strip()
        if not path.startswith("/"):
            path = "/" + path
        model_name = str(values.get("ollama.embedding_model") or "nomic-embed-text").strip()
        name = str(values.get("embedding.profile") or "local-embedding").strip() or "local-embedding"
        models[name] = {
            "provider": "ollama",
            "api_url": f"{url}{path}",
            "method": "POST",
            "model": model_name,
            "api_key": "ollama",
            "temperature": 0,
            "model_params": {},
        }
        aliases["default-embedding"] = name
    else:
        base_url = str(values.get("embedding.base_url") or "").strip()
        model_name = str(values.get("embedding.model") or "").strip()
        api_key = str(values.get("embedding.api_key") or "").strip()
        if base_url or model_name:
            entry = dict(models.get("embedding") or {})
            if base_url:
                entry["api_url"] = base_url
            if model_name:
                entry["model"] = model_name
            if api_key:
                entry["api_key"] = api_key
            entry.setdefault("provider", "openai_compatible")
            entry.setdefault("method", "POST")
            entry.setdefault("temperature", 0)
            models["embedding"] = entry
        aliases.setdefault("default-embedding", "embedding")
    payload["models"] = models
    payload["aliases"] = aliases
    return payload


def generate(values: dict, current: dict) -> tuple[dict, str]:
    """返回 (新的 model_list 内容, 说明)。不写盘，便于测试。"""
    source = str(values.get("model.source") or "remote").strip().lower()
    profiles = values.get("profiles") or DEFAULT_PROFILES
    if isinstance(profiles, str):
        profiles = [p.strip() for p in profiles.split(",") if p.strip()]

    if source == "remote":
        payload = _build_remote(values, current, profiles)
        note = f"远端 API：{values.get('model.model')}（绑定 {len(profiles)} 个 profile）"
    elif source == "local":
        payload = _build_local(values, current, profiles)
        note = f"本地 Ollama：{values.get('ollama.model')} @ {values.get('ollama.url')}"
    else:
        raise ValueError(f"未知 model.source={source!r}（只支持 remote / local）")

    payload = _apply_embedding(values, payload)
    emb = str(values.get("embedding.source") or "remote").strip().lower()
    note += "；embedding=" + ("本地 Ollama" if emb == "local" else "沿用仓库模板/远端")
    return payload, note


def write(data_dir: Path, values: dict, print_only: bool = False) -> str:
    target = Path(data_dir) / "config" / "model-proxy" / "config" / "model_list.json"
    current = _load(target) or _load(Path(__file__).resolve().parent.parent / "config" / "model-proxy" / "config" / "model_list.json")
    payload, note = generate(values, current)
    text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if print_only:
        return f"{note}\n{text}"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return f"{note} → {target}"


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="按模型来源生成 model_list.json")
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--values", required=True, help="values.json 路径")
    parser.add_argument("--print", action="store_true", help="只打印不写盘")
    args = parser.parse_args(argv[1:])

    values_path = Path(args.values)
    if not values_path.exists():
        print(f"错误：找不到 {values_path}")
        return 2
    try:
        values = json.loads(values_path.read_text(encoding="utf-8-sig"))
    except Exception as exc:  # noqa: BLE001
        print(f"错误：解析 {values_path} 失败：{exc}")
        return 2

    try:
        print(write(Path(args.data_dir), values, print_only=args.print))
    except ValueError as exc:
        print(f"错误：{exc}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
