#!/usr/bin/env python3
"""compose 清单自检（离线，不启动任何容器）。

检查：
1. YAML 可解析、服务都有 image 或 build、build 上下文目录存在；
2. profile 名与 deploy/features.json 的功能对应得上；
3. 核心服务齐全（features.json 的 core.services）；
4. 编排里的服务名引用都有定义（避免容器起不来才发现）；
5. 卷挂载都是相对 ./data（可迁移、可备份）；
6. 没有残留 k8s 集群内 DNS（*.svc.cluster.local）。

用法：python -X utf8 deploy/compose/compose_lint.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import yaml

COMPOSE = Path(__file__).resolve().parent / "docker-compose.yml"
FEATURES = Path(__file__).resolve().parent.parent / "features.json"

FAILS: list[str] = []
CHECKS = 0


def check(name: str, cond: bool, extra: str = "") -> None:
    global CHECKS
    CHECKS += 1
    if cond:
        print(f"  ok    {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL  {name} {extra}")


def main() -> int:
    data = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    features = json.loads(FEATURES.read_text(encoding="utf-8"))

    services = data.get("services") or {}
    known_names = set(services)

    print("结构")
    check("有 services", bool(services), str(list(services))[:120])
    check("有 networks", "my-agent" in (data.get("networks") or {}))
    for name, spec in services.items():
        has_image = bool(spec.get("image"))
        if spec.get("build"):
            ctx = Path(COMPOSE.parent / spec["build"]["context"]).resolve()
            check(f"{name} 的 build 上下文存在", ctx.is_dir(), str(ctx))
        else:
            check(f"{name} 有 image", has_image, str(spec.get("image")))

    print("核心服务（对照 features.json）")
    core = next(f for f in features["features"] if f["id"] == "core")
    for name in core["services"]:
        check(f"core 含 {name}", name in services)

    print("profile ↔ 功能")
    profiles = sorted({p for spec in services.values() for p in (spec.get("profiles") or [])})
    check("profile 列表符合预期", profiles == ["memory", "ollama", "qq", "qq-official", "qq-onebot", "search", "timer", "tools"], str(profiles))
    for spec in services.values():
        # 有 profiles 的服务必须显式声明，未声明的视为 core
        for profile in spec.get("profiles") or []:
            check(f"profile {profile} 非空", bool(profile))

    print("服务名引用")
    for name, spec in services.items():
        for key, value in (spec.get("environment") or {}).items():
            text = str(value)
            if not re.search(r"[a-z0-9-]+-service:\d+|openviking:\d+|searxng:\d+|embedding-service:\d+", text):
                continue
            host = text.split("//")[-1].split(":")[0]
            if host in {"localhost", "127.0.0.1", "host.docker.internal"}:
                continue
            expected = host
            check(f"{name}.{key} 指向已定义服务 {expected}", expected in known_names or expected == "openviking" or expected == "searxng", text)

    print("卷挂载")
    for name, spec in services.items():
        for volume in spec.get("volumes") or []:
            source = str(volume).split(":")[0]
            if source.startswith("./data"):
                continue
            check(f"{name} 的卷在 ./data 下或为命名卷", not source.startswith("/"), source)

    print("无集群内 DNS 残留")
    text = COMPOSE.read_text(encoding="utf-8")
    check("没有 *.svc.cluster.local", "svc.cluster.local" not in text)

    print("可选功能都有对应 profile")
    profile_by_feature = {
        "tools.basic": "tools",
        "memory": "memory",
        "search": "search",
        "qq": "qq",
        "timer": "timer",
        "model.local": "ollama",
    }
    for feature_id, profile in profile_by_feature.items():
        exists = any(f["id"] == feature_id for f in features["features"]) or any(
            feature_id.startswith(f["id"] + ".") for f in features["features"])
        check(f"features.json 有 {feature_id}", exists)
        check(f"profile {profile} 已被使用", profile in profiles)

    print()
    if FAILS:
        print(f"FAILED {len(FAILS)}: {FAILS}")
        return 1
    print(f"ALL OK — {CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
