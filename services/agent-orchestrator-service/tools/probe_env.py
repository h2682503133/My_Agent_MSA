"""环境探测：生成 capability.json（部署程序与面板都调用它）。

原则：
- **只读探测**，任何一项失败都只是"不可用"，绝不抛错、绝不改变环境；
- 不假设存在 Docker / WSL / k8s / Node；探不到就如实写 false；
- 产物给三层用：installer 校验"能不能装你选的功能"、orchestrator 过滤工具、
  dashboard「杂项 → 运行环境」展示。

用法：
  python -X utf8 tools/probe_env.py                       # 探测并写 capability.json
  python -X utf8 tools/probe_env.py --print               # 只打印不写
  python -X utf8 tools/probe_env.py --skip-commands       # 不跑外部命令/网络（离线自检用）
  python -X utf8 tools/probe_env.py --out /tmp/cap.json   # 指定输出
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_OUT = Path(os.getenv("CAPABILITY_PATH", "/app/config/capability.json"))


def _run(cmd: list[str], timeout: float = 4.0) -> tuple[bool, str]:
    """跑一条只读命令；超时/不存在都返回 (False, 原因)。"""
    exe = shutil.which(cmd[0])
    if not exe:
        return False, f"找不到 {cmd[0]}"
    try:
        proc = subprocess.run(
            [exe, *cmd[1:]],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return False, f"{cmd[0]} 超时"
    except Exception as exc:  # noqa: BLE001
        return False, f"{cmd[0]} 执行失败：{exc}"
    output = (proc.stdout or proc.stderr or "").strip()
    return proc.returncode == 0, output.splitlines()[0] if output else ""


def _tcp_probe(url: str, timeout: float = 1.5) -> bool:
    """TCP 探活（不建连接池、不发协议数据）。"""
    try:
        parsed = urllib.parse.urlparse(url if "://" in url else f"tcp://{url}")
        host = parsed.hostname or ""
        port = parsed.port
        if not port:
            port = 443 if parsed.scheme in {"https", "wss"} else 80
        if not host:
            return False
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:  # noqa: BLE001
        return False


def probe(skip_commands: bool = False) -> dict:
    facts: dict = {}
    notes: dict = {}

    facts["os"] = platform.system().lower() or "unknown"
    facts["python"] = platform.python_version()

    # ── 外部命令（都只读）──
    if skip_commands:
        for key in ("docker", "compose", "kubectl", "node", "clawhub", "codex"):
            facts[key] = False
        notes["skipped"] = "已跳过外部命令探测"
    else:
        ok, out = _run(["docker", "version", "--format", "{{.Server.Version}}"], timeout=5)
        facts["docker"] = bool(ok)
        notes["docker"] = "" if ok else out

        ok, out = _run(["docker", "compose", "version"], timeout=5)
        facts["compose"] = bool(ok)
        notes["compose"] = "" if ok else out

        ok, out = _run(["kubectl", "version", "--client", "--output=json"], timeout=5)
        client_ok = bool(ok)
        cluster_ok = False
        if client_ok and not skip_commands:
            cluster_ok, cluster_out = _run(
                ["kubectl", "get", "ns", "--request-timeout=3s", "-o", "name"], timeout=6
            )
            notes["kubectl"] = "" if cluster_ok else cluster_out
        facts["kubectl"] = "cluster" if cluster_ok else ("client" if client_ok else "none")

        ok, out = _run(["node", "--version"], timeout=4)
        facts["node"] = bool(ok)
        notes["node"] = "" if ok else out

        ok, out = _run(["clawhub", "--version"], timeout=5)
        facts["clawhub"] = bool(ok)
        notes["clawhub"] = "" if ok else out

        ok, out = _run(["codex", "--version"], timeout=5)
        facts["codex"] = bool(ok)
        notes["codex"] = "" if ok else out

    # ── 端点（env 里配了才探）──
    endpoints = {
        "openviking": os.getenv("OPENVIKING_SERVER_URL", ""),
        "searxng": os.getenv("SEARXNG_URL", ""),
        "browser": os.getenv("BROWSER_MCP_URL", "") or os.getenv("BROWSER_CDP_URL", ""),
        "dsh": os.getenv("DSH_ONEBOT_WS", ""),
        "qq": os.getenv("SATORI_HOST", ""),
    }
    facts["endpoints"] = {}
    for name, url in endpoints.items():
        if not url:
            facts["endpoints"][name] = "unset"
        elif skip_commands:
            facts["endpoints"][name] = "unknown"
        else:
            facts["endpoints"][name] = "up" if _tcp_probe(url) else "down"

    # ── 磁盘 ──
    try:
        target = Path(os.getenv("DATA_DIR", ".")).parent
        usage = shutil.disk_usage(str(target if target.exists() else Path.cwd()))
        facts["disk_free_gb"] = round(usage.free / (1024 ** 3), 1)
    except Exception:  # noqa: BLE001
        facts["disk_free_gb"] = None

    # ── 推导运行档 ──
    if facts["kubectl"] == "cluster":
        tier = "T2" if facts["os"] == "windows" else "T5"
    elif facts["compose"] or facts["docker"]:
        tier = "T1" if facts["os"] == "windows" else "T4"
    else:
        tier = "T0" if facts["os"] == "windows" else "T3"

    # ── 能力候选（"有没有可能用"，最终取值由 installer 按用户选择写入）──
    endpoints_facts = facts["endpoints"]
    features = {
        "tool_runtime": True,
        "memory_mode": "openviking" if endpoints_facts.get("openviking") == "up" else "local",
        "openviking": endpoints_facts.get("openviking") == "up",
        "clawhub": bool(facts.get("clawhub")) or bool(facts.get("node")),
        "skill_exec": True,
        "browser": endpoints_facts.get("browser") == "up",
        "search": "searxng" if endpoints_facts.get("searxng") == "up" else "builtin",
        "external_exec": bool(facts.get("codex")) or endpoints_facts.get("dsh") == "up",
        "host_files": facts["os"] == "windows" or tier in {"T0", "T3"},
        "image_gen": True,
        "fetch_extract": "full",
        "qq_adapter": endpoints_facts.get("qq") or "none",
        "lan_expose": tier in {"T1", "T2", "T4", "T5"},
        "storage": "nfs" if tier in {"T2", "T5"} else "local-dir",
    }

    degraded = []
    if not facts.get("docker"):
        # 没 Docker 是单体模式的正常形态，不是"坏了"：标 info，不计入降级数
        degraded.append({"feature": "container", "reason": "未检测到可用的 Docker，只能用单体（T0/T3）模式", "level": "info"})
    if tier in {"T2", "T5"} and features["storage"] == "nfs" and not os.getenv("NFS_SERVER"):
        degraded.append({"feature": "storage", "reason": "k8s 需要共享存储，但未配置 NFS_SERVER（可用单节点 hostPath 替代）"})
    if features["memory_mode"] == "local":
        degraded.append({"feature": "memory", "reason": "未检测到 OpenViking，回落本地窗口滚动"})
    if features["search"] == "builtin":
        degraded.append({"feature": "search", "reason": "未检测到 SearXNG，使用内置 Bing CN 回落（无引擎/语言过滤）"})
    if not features["browser"]:
        degraded.append({"feature": "browser", "reason": "未检测到 MCP/CDP 端点"})
    if not features["external_exec"]:
        degraded.append({"feature": "external-exec", "reason": "未检测到 dsh / codex"})
    if not features["clawhub"]:
        degraded.append({"feature": "skills", "reason": "未检测到 Node/clawhub，技能只能读不能搜装"})
    if features["qq_adapter"] == "none":
        degraded.append({"feature": "qq", "reason": "未配置 QQ 适配器端点"})

    return {
        "schema": 1,
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "os": facts["os"],
        "runtime_tier": tier,
        "probes": facts,
        "notes": notes,
        "features": features,
        "degraded": degraded,
        "partial": "probe-only: 功能最终启用与否由 installer 按用户选择覆盖",
    }


def _use_utf8() -> None:
    """输出被重定向到管道/文件时 Windows 默认 locale 编码，中文 print 会崩，这里统一成 UTF-8。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass


def main(argv: list[str]) -> int:
    _use_utf8()
    parser = argparse.ArgumentParser(description="探测运行环境并写 capability.json")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--print", action="store_true", help="只打印，不写文件")
    parser.add_argument("--skip-commands", action="store_true", help="不跑外部命令/网络探测")
    args = parser.parse_args(argv[1:])

    started = time.time()
    result = probe(skip_commands=args.skip_commands)
    result["elapsed_ms"] = int((time.time() - started) * 1000)

    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.print:
        print(text)
        return 0

    out = Path(args.out)
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n", encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        print(f"错误：写 {out} 失败：{exc}")
        return 1
    info_count = sum(1 for item in result["degraded"] if item.get("level") == "info")
    real_count = len(result["degraded"]) - info_count
    extra = f"，另有 {info_count} 条说明" if info_count else ""
    print(f"已写入 {out}（档位 {result['runtime_tier']}，降级 {real_count} 项{extra}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
