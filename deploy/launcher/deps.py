"""单体模式的依赖安装（方案 A：共享 venv）。

做三件事：
1. 建 `<DATA_DIR>/venv`（存在但 pip 损坏时自动重建）；
2. **按选中的功能**收集各服务的 requirements.txt，解析跨服务冲突，生成 `deps/requirements.lock.txt`，
   再应用 `requirements.mono.txt`（版本覆盖表）后安装；
3. 给选中的服务生成 gRPC stubs（每个服务有自己的 proto/ 与 app/generated/）。

为什么需要"解析 + 覆盖"：不同服务对同一个包的要求不同（grpcio 1.64/1.68/1.76、protobuf 5.27/5.29/6.33、
httpx 0.27/0.28、requests 2.32/>=2.33），同一个解释器装不下多套。规则：
- 都是精确 pin → 取最高版本；
- 都是下限/范围 → 取各下限的最大值；
- 混用 → 若精确版本满足所有下限就保留，否则退到下限；
- 覆盖表里的同名包最后生效（只放"必须统一"的那几个）。
装不上时 `--mode per-service` 退到每服务独立 venv。

用法：
  python -X utf8 deploy/launcher/deps.py --data-dir <dir> --features core,memory --check
  python -X utf8 deploy/launcher/deps.py --data-dir <dir> --features core,memory --install
  python -X utf8 deploy/launcher/deps.py ... --dry-run          # 只打印计划
  python -X utf8 deploy/launcher/deps.py ... --mode per-service # 每服务独立 venv
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

try:  # 既支持 python -m，也支持直接跑脚本
    from .plan import build, init_config, write_capability
    from .services import SERVICES
    from .stdio import use_utf8
except ImportError:  # pragma: no cover
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from launcher.plan import build, init_config, write_capability
    from launcher.services import SERVICES
    from launcher.stdio import use_utf8

# 面板/管道调用时 stdout 不是控制台，中文输出会 UnicodeEncodeError，先固定成 UTF-8
use_utf8()

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
FEATURES_JSON = Path(__file__).resolve().parent.parent / "features.json"


def load_feature_catalog() -> list[dict]:
    """功能清单的唯一来源：deploy/features.json（admin panel 也读这一份）。"""
    try:
        data = json.loads(FEATURES_JSON.read_text(encoding="utf-8-sig"))
    except Exception as exc:  # noqa: BLE001
        print(f"⚠ 读不到功能清单 {FEATURES_JSON}：{exc}")
        return []
    items = data.get("features") if isinstance(data, dict) else data
    return [item for item in (items or []) if isinstance(item, dict) and item.get("id")]


def describe_features() -> str:
    """把可选的部署项打印出来（脚本与面板口径一致）。"""
    rows = load_feature_catalog()
    if not rows:
        return "（没有读到功能清单）"
    lines = ["可选的部署项（deploy/features.json）："]
    for item in rows:
        fid = item.get("id")
        name = item.get("name") or ""
        requires = item.get("requires") or []
        services = item.get("services")
        modes = list((item.get("modes") or {}).keys())
        desc = f"  {fid:<16}{name}"
        if requires:
            desc += f"  [依赖 {','.join(requires)}]"
        if modes:
            desc += f"  [模式 {','.join(modes)}]"
        if isinstance(services, list) and services:
            desc += f"  [{len(services)} 个服务]"
        elif isinstance(services, dict):
            desc += f"  [模式对应服务: {','.join(services.keys())}]"
        lines.append(desc)
    lines.append("")
    lines.append("用法：--features core,memory,qq    --modes memory=local,qq=onebot")
    return "\n".join(lines)

OVERRIDES = Path(__file__).resolve().parent / "requirements.mono.txt"
# 兼容旧名（测试/外部引用）
MONO_LOCK = OVERRIDES
STAMP_NAME = ".deps-stamp.json"

# ── 本机自建 openviking-server（独立 venv，可选）───────────────
# 为什么单独一个 venv：它依赖 fastapi>=0.128 / protobuf>=6，与共享 venv 的 pin 冲突。
# 为什么锁这两个版本：
#   - openviking 0.4.10 = 与集群镜像同版本（ov.conf 结构一致）
#   - volcengine-python-sdk 5.0.14 = 29MB 单轮子（含 volcenginesdkarkruntime 等模块）；
#     5.0.56 是 0 字节元包，会拖几百个 volcenginesdk-* 子包，装到天荒地老
OPENVIKING_VENV_NAME = "openviking"
OPENVIKING_PIN = "openviking==0.4.10"
OPENVIKING_SDK_PIN = "openviking-sdk"
OPENVIKING_UMBRELLA = "volcengine-python-sdk==5.0.14"
OPENVIKING_BASE_SDK = "volcengine"
# 上游 0.4.10 是旧代码：mcp 2.x 把 FastMCP 改名/删掉了，会直接 import 失败
OPENVIKING_EXTRA_PINS = ["mcp<2"]


def venv_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "venv"


def venv_python(data_dir: Path) -> Path:
    if os.name == "nt":
        return venv_dir(data_dir) / "Scripts" / "python.exe"
    return venv_dir(data_dir) / "bin" / "python"


def _normalize_package(line: str) -> tuple[str, str]:
    """`grpcio==1.76.0` → ("grpcio", "grpcio==1.76.0")；忽略注释/选项行。"""
    text = line.strip()
    if not text or text.startswith("#"):
        return "", ""
    if text.startswith(("-r", "-c", "--")):
        return "", ""
    name = re.split(r"[<>=!~;\[\s]", text)[0].strip().lower()
    return name, text


def parse_requirements(path: Path) -> dict[str, str]:
    """返回 {包名: 原始行}。同文件内重复取第一条。"""
    result: dict[str, str] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except Exception:
        return result
    for line in lines:
        name, text = _normalize_package(line)
        if name and name not in result:
            result[name] = text
    return result



# 这些包单独用 `pip install --no-deps` 装：它们把重依赖写在 install_requires 里
# （openviking 要求 fastapi>=0.128），但运行时代码只用到其中一小部分。
# 这样既能装上，又不会把 gateway/dashboard 钉的 fastapi 0.115.6 顶掉。
NO_DEPS_PACKAGES = {
    "openviking": "声明 fastapi>=0.128，但客户端只用 openviking.AsyncHTTPClient（httpx）",
}

# 用 --no-deps 装的包，需要在之后单独补它**运行期真正用到的**依赖。
# 踩过的坑（虚拟机实测，缺一个就静默退化成 mock 记忆）：
#   loguru → jsonschema → json-repair(模块名 json_repair)
#   → openviking-sdk（缺它会报 "openviking-sdk is required for HTTP client usage"）
#   → pathspec
OPENVIKING_EXTRA = [
    "openviking-sdk",
    "loguru",
    "jsonschema",
    "json-repair",
    "pathspec",
    # openviking 里零散 import 的轻量依赖，一并装上免得再逐个试
    "apscheduler",
    "orjson",
    "tiktoken",
    "jinja2",
    "tenacity",
    "psutil",
    "rich",
    "typer",
    "aiofiles",
    "python-multipart",
    "toml",
    "ujson",
    "defusedxml",
    "feedparser",
    "olefile",
    "xlrd",
    "python-docx",
    "python-pptx",
    "openpyxl",
    "ebooklib",
]

# 包名 → 补充依赖清单
NO_DEPS_COMPANIONS = {"openviking": OPENVIKING_EXTRA}

# 兼容旧引用（曾经是"直接排除"）
EXCLUDED_IN_SHARED = NO_DEPS_PACKAGES

REQ_RE = re.compile(r"^([A-Za-z0-9_.\-]+)\s*(.*)$")


def _split_spec(text: str) -> tuple[str, str]:
    """'grpcio==1.76.0' → ('grpcio', '==1.76.0')；无版本 → ('grpcio', '')。"""
    match = REQ_RE.match(text.strip())
    if not match:
        return "", ""
    name = match.group(1).lower()
    spec = match.group(2).replace(" ", "")
    # extras 保留在包名一侧，如 uvicorn[standard]
    if "[" in text:
        bracket = text.index("]")
        name = text[: bracket + 1].lower()
        spec = text[bracket + 1 :].strip()
    return name, spec


def _version_key(version: str) -> tuple[int, ...]:
    parts = []
    for chunk in re.split(r"[.\-+]", version):
        parts.append(int(chunk) if chunk.isdigit() else 0)
    return tuple(parts)


def _pinned(spec: str) -> str | None:
    match = re.fullmatch(r"==\s*([0-9][0-9A-Za-z.\-+]*)", spec.strip())
    return match.group(1) if match else None


def _floor(spec: str) -> str | None:
    match = re.fullmatch(r">=\s*([0-9][0-9A-Za-z.\-+]*)", spec.strip())
    return match.group(1) if match else None


def resolve_spec(specs: list[str]) -> tuple[str, str]:
    """把同一个包的多条版本要求合并成一条；返回 (最终 spec, 说明)。"""
    pins = [p for p in (_pinned(spec) for spec in specs) if p]
    floors = [f for f in (_floor(spec) for spec in specs) if f]
    if pins:
        best = max(pins, key=_version_key)
        if floors:
            highest_floor = max(floors, key=_version_key)
            if _version_key(best) < _version_key(highest_floor):
                return f">={highest_floor}", f"pin {best} 低于下限 {highest_floor}，改用下限"
        if len(set(pins)) > 1:
            return f"=={best}", f"多个 pin，取最高 {best}"
        return f"=={best}", ""
    if floors:
        highest = max(floors, key=_version_key)
        return f">={highest}", "" if len(set(floors)) == 1 else f"多个下限，取最高 {highest}"
    return "", ""


def build_lock(plan, allow_excluded: bool = False) -> tuple[dict[str, str], list[dict], list[dict], dict]:
    """收集选中服务的依赖 → 解析冲突 → 应用覆盖表。

    返回 (最终 {包名: spec}, 冲突记录, 覆盖记录)。
    """
    per_service = service_requirements(plan)
    gathered: dict[str, list[str]] = {}
    for service, packages in per_service.items():
        for name, spec in packages.items():
            _, spec_only = _split_spec(spec)
            gathered.setdefault(name, []).append(spec_only)

    resolved: dict[str, str] = {}
    conflicts: list[dict] = []
    for name, specs in sorted(gathered.items()):
        unique = sorted(set(specs))
        spec, note = resolve_spec(specs)
        resolved[name] = spec
        if len(unique) > 1:
            conflicts.append({"package": name, "specs": unique, "chosen": spec or "(any)", "note": note})

    skipped: list[dict] = []
    no_deps: list[dict] = []
    for name in list(resolved):
        if name in NO_DEPS_PACKAGES and not allow_excluded:
            no_deps.append({
                "package": name,
                "spec": resolved.pop(name),
                "reason": NO_DEPS_PACKAGES[name],
            })

    overrides: list[dict] = []
    for name, spec in parse_requirements(OVERRIDES).items():
        _, spec_only = _split_spec(spec)
        if name in resolved and resolved[name] == spec_only:
            continue
        previous = resolved.get(name, "(未使用)")
        resolved[name] = spec_only
        overrides.append({"package": name, "from": previous, "to": spec_only})

    return resolved, conflicts, overrides, {"skipped": skipped, "no_deps": no_deps}


def write_lock(plan, resolved: dict[str, str], target: Path | None = None) -> Path:
    """把解析结果写成真正被安装的锁文件（纯 ASCII）。

    target=None 时写数据目录下的 deps/requirements.lock.txt；
    给 target 就是导出到别处（--export，供自己用 conda/pip 装时对照）。
    """
    lock_path = Path(target) if target is not None else Path(plan.data_dir) / "deps" / "requirements.lock.txt"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# GENERATED by deploy/launcher/deps.py - do not edit.",
        f"# features: {','.join(plan.features)}",
        f"# services: {','.join(s.id for s in plan.services)}",
        "",
    ]
    for name in sorted(resolved):
        lines.append(f"{name}{resolved[name]}")
    # 必须纯 ASCII：Windows 上 pip 用本地编码读 requirements，非 ASCII 会直接崩
    body = "\n".join(lines) + "\n"
    lock_path.write_text(body.encode("ascii", "ignore").decode("ascii") + "\n", encoding="ascii", newline="\n")
    return lock_path


def service_requirements(plan) -> dict[str, dict[str, str]]:
    """{服务 id: {包名: 规格}}"""
    result: dict[str, dict[str, str]] = {}
    for svc in plan.services:
        req = REPO_ROOT / "services" / svc.dir_name / "requirements.txt"
        if req.exists():
            result[svc.id] = parse_requirements(req)
    return result


def conflict_report(per_service: dict[str, dict[str, str]]) -> list[dict]:
    """同一包在不同服务里版本不一致的清单（共享 venv 会按锁文件覆盖）。"""
    seen: dict[str, dict[str, list[str]]] = {}
    for service, packages in per_service.items():
        for name, spec in packages.items():
            seen.setdefault(name, {}).setdefault(spec, []).append(service)
    conflicts = []
    for name, specs in sorted(seen.items()):
        if len(specs) > 1:
            conflicts.append({
                "package": name,
                "specs": [{"spec": spec, "services": sorted(services)} for spec, services in sorted(specs.items())],
            })
    return conflicts


def _file_hash(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()[:16]
    except Exception:
        return ""


def planned_stamp(data_dir: Path, lock: Path, mode: str) -> dict:
    return {
        "lock_hash": _file_hash(lock),
        "mode": mode,
        "python": sys.version.split()[0],
    }


def stamp_path(data_dir: Path, service: str = "") -> Path:
    if service:
        return Path(data_dir) / "venvs" / service / STAMP_NAME
    return venv_dir(data_dir) / STAMP_NAME


def read_stamp(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def needs_install(data_dir: Path, lock: Path, mode: str, service: str = "") -> bool:
    python = venv_python(data_dir) if not service else _service_python(data_dir, service)
    if not python.exists():
        return True
    ok, _detail = _venv_pip_ok(venv_dir(data_dir) if not service else Path(data_dir) / "venvs" / service)
    if not ok:
        return True
    stamp = read_stamp(stamp_path(data_dir, service))
    return stamp != planned_stamp(data_dir, lock, mode)


def _service_python(data_dir: Path, service: str) -> Path:
    base = Path(data_dir) / "venvs" / service
    return base / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def proto_targets(repo_root: Path, plan) -> list[dict]:
    """需要生成 stubs 的服务：有 scripts/gen_proto.sh 或 proto/*.proto 的。"""
    targets = []
    for svc in plan.services:
        svc_dir = repo_root / "services" / svc.dir_name
        proto_dir = svc_dir / "proto"
        protos = sorted(p.name for p in proto_dir.glob("*.proto")) if proto_dir.is_dir() else []
        if not protos:
            continue
        targets.append({
            "service": svc.id,
            "dir": str(svc_dir),
            "out": str(svc_dir / "app" / "generated"),
            "protos": protos,
        })
    return targets


def proto_command(python: Path, target: dict) -> list[str]:
    """用 grpc_tools.protoc 直接生成（不依赖 bash，Windows 上也能跑）。"""
    svc_dir = Path(target["dir"])
    cmd = [
        str(python), "-m", "grpc_tools.protoc",
        "-I", str(svc_dir / "proto"),
        f"--python_out={target['out']}",
        f"--grpc_python_out={target['out']}",
    ]
    cmd += [str(svc_dir / "proto" / name) for name in target["protos"]]
    return cmd


def _parse_modes(raw: str) -> dict[str, str]:
    """把 "memory=local,qq=satori" 解析成 dict。"""
    result: dict[str, str] = {}
    for pair in str(raw or "").split(","):
        if "=" not in pair:
            continue
        key, _, value = pair.partition("=")
        key, value = key.strip(), value.strip()
        if key and value:
            result[key] = value
    return result


def validate_features(raw: str) -> list[str]:
    """返回不认识的 feature id（空列表=全部合法）。"""
    catalog = {item["id"] for item in load_feature_catalog()}
    if not catalog:
        return []  # 清单读不到就不拦（不猜）
    wanted = [f.strip() for f in str(raw or "").split(",") if f.strip()]
    return [f for f in wanted if f not in catalog]


def build_plan(args) -> tuple[object, dict, list[dict]]:
    plan = build(
        repo_root=Path(args.repo_root),
        data_dir=Path(args.data_dir),
        features=[f.strip() for f in args.features.split(",") if f.strip()],
        modes=_parse_modes(getattr(args, "modes", "")),
    )
    per_service = service_requirements(plan)
    return plan, per_service, conflict_report(per_service)


def describe(args, plan, per_service, conflicts) -> str:
    lines = [
        f"数据目录：{args.data_dir}",
        f"模式：{args.mode}",
        f"将启动服务（{len(plan.services)}）：{', '.join(s.id for s in plan.services)}",
        f"版本覆盖表：{OVERRIDES.parent.name}/{OVERRIDES.name}（{len(parse_requirements(OVERRIDES))} 条）",
        f"将按选中功能解析依赖：{len(service_requirements(plan))} 个服务有 requirements.txt",
        f"各服务 requirements 冲突（共享 venv 按锁覆盖）：{len(conflicts)} 项",
    ]
    for item in conflicts:
        # 两种来源：conflict_report（带服务归属）与 build_lock（只带 spec 列表）
        specs_field = item["specs"]
        if specs_field and isinstance(specs_field[0], dict):
            specs = "；".join(f"{s['spec']} ← {', '.join(s['services'])}" for s in specs_field)
        else:
            specs = " / ".join(specs_field)
        lines.append(f"  - {item['package']}: {specs} → {item.get('chosen', '')}".rstrip())
    targets = proto_targets(Path(args.repo_root), plan)
    lines.append(f"需要生成 gRPC stubs 的服务（{len(targets)}）：{', '.join(t['service'] for t in targets) or '无'}")
    lines.append(f"venv：{venv_dir(Path(args.data_dir))}（{'已存在' if venv_dir(Path(args.data_dir)).exists() else '待创建'}）")
    return "\n".join(lines)


def _venv_pip_ok(target_dir: Path) -> tuple[bool, str]:
    """venv 里的 pip 能不能用（磁盘写满等情况会留下半残 pip）。"""
    base = target_dir / ("Scripts" if os.name == "nt" else "bin")
    python = target_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    if not base.exists() or not python.exists():
        return False, "venv 目录不完整"
    try:
        proc = subprocess.run(
            [str(python), "-X", "utf8", "-m", "pip", "-V"],
            capture_output=True, text=True, timeout=120,
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"pip 检测失败：{exc}"
    if proc.returncode != 0:
        first = (proc.stderr or proc.stdout or "").strip().splitlines()
        return False, first[-1] if first else "pip 不可用"
    return True, (proc.stdout or "").strip()


def ensure_venv(target_dir: Path, base_python: str) -> tuple[bool, str]:
    """确保 venv 可用：不存在就建；存在但 pip 坏了就删掉重建。"""
    python = base_python or sys.executable
    base = target_dir / ("Scripts" if os.name == "nt" else "bin")

    if base.exists():
        ok, detail = _venv_pip_ok(target_dir)
        if ok:
            return True, f"venv 已存在（{detail}）"
        # 半残 venv（例如上次磁盘写满）：重建，不猜、不将就
        try:
            shutil.rmtree(target_dir)
        except Exception as exc:  # noqa: BLE001
            return False, f"venv 损坏（{detail}），且删除失败：{exc}"
        print(f"检测到损坏的 venv（{detail}），已删除，准备重建")

    try:
        subprocess.run([python, "-m", "venv", str(target_dir)], check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as exc:
        return False, f"创建 venv 失败：{exc.stderr[-400:] if exc.stderr else exc}"
    except Exception as exc:  # noqa: BLE001
        return False, f"创建 venv 失败：{exc}"

    ok, detail = _venv_pip_ok(target_dir)
    if not ok:
        return False, f"venv 已创建但 pip 不可用：{detail}（磁盘空间不足时常见）"
    return True, f"已创建 venv：{target_dir}（{detail}）"


def openviking_venv_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "venvs" / OPENVIKING_VENV_NAME


def openviking_python(data_dir: Path) -> Path:
    base = openviking_venv_dir(data_dir)
    if os.name == "nt":
        return base / "Scripts" / "python.exe"
    return base / "bin" / "python"


def _pip(python: Path, args: list[str], timeout: int = 120) -> int:
    print(f"  pip {' '.join(args[:3])}...")
    proc = subprocess.run(
        [str(python), "-X", "utf8", "-m", "pip", "install", "--no-input",
         "--disable-pip-version-check", "--timeout", str(timeout), "--retries", "3", *args],
        text=True,
        env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
    )
    return proc.returncode


def install_openviking_server(args, data_dir: Path) -> int:
    """把 openviking-server 装进独立 venv（幂等，已装就跳过）。"""
    target = openviking_venv_dir(data_dir)
    ok, message = ensure_venv(target, args.python)
    print(message)
    if not ok:
        return 1
    python = openviking_python(data_dir)

    index_args: list[str] = []
    if getattr(args, "index_url", ""):
        index_args += ["--index-url", args.index_url]
    if getattr(args, "wheels", ""):
        index_args += ["--no-index", "--find-links", args.wheels]

    # 1) 本体（--no-deps：它的 Requires-Dist 里挂着 volcengine-python-sdk[ark] 巨型伞包）
    if _pip(python, ["--no-deps", OPENVIKING_PIN, OPENVIKING_SDK_PIN, *index_args],
            getattr(args, "timeout", 120)) != 0:
        print("openviking 本体安装失败")
        return 1

    # 2) 按元数据补运行期依赖，剔除 volcengine 伞包（单独按固定版本装）
    probe = subprocess.run(
        [str(python), "-c",
         "import importlib.metadata as m,sys;"
         "print(chr(10).join(r for r in (m.requires('openviking') or [])"
         " if 'volcengine' not in r.lower()))"],
        capture_output=True, text=True,
        env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
    )
    if probe.returncode != 0:
        print("读取 openviking 依赖清单失败：" + (probe.stderr or "")[-400:])
        return 1
    req_lines = [line.strip() for line in (probe.stdout or "").splitlines() if line.strip()]
    req_path = Path(data_dir) / "deps" / "openviking.requirements.txt"
    req_path.parent.mkdir(parents=True, exist_ok=True)
    req_path.write_text("\n".join(req_lines) + "\n", encoding="utf-8")
    print(f"  openviking 依赖 {len(req_lines)} 条（{req_path}）")
    if _pip(python, ["-r", str(req_path), *index_args], max(180, getattr(args, "timeout", 120))) != 0:
        print("openviking 依赖安装失败")
        return 1

    # 3) 旧代码需要的版本修正（新版 mcp 删了 FastMCP，openviking 0.4.10 直接 import 失败）
    if _pip(python, [*OPENVIKING_EXTRA_PINS, *index_args], getattr(args, "timeout", 120)) != 0:
        print("openviking 版本修正包安装失败")
        return 1

    # 4) volcengine：基础 SDK（SignerV4）+ 单轮子版 python-sdk（arkruntime 等模块）
    if _pip(python, [OPENVIKING_BASE_SDK, OPENVIKING_UMBRELLA, *index_args],
            getattr(args, "timeout", 120)) != 0:
        print("volcengine SDK 安装失败（server 会在 import 阶段报 volcenginesdkarkruntime 缺失）")
        return 1

    # 4) 冒烟：能 import bootstrap 才算装好
    check = subprocess.run(
        [str(python), "-c", "import openviking.server.bootstrap; print('ok')"],
        capture_output=True, text=True,
        env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
    )
    if check.returncode != 0 or "ok" not in (check.stdout or ""):
        print("openviking-server 冒烟失败：" + ((check.stderr or check.stdout or "")[-500:]))
        return 1
    print(f"完成：openviking-server 就绪（{target}）")
    return 0


def install(args) -> int:
    data_dir = Path(args.data_dir)
    bad = validate_features(args.features)
    if bad:
        print(f"✘ 未知的功能：{', '.join(bad)}")
        print(describe_features())
        return 2

    plan, per_service, conflicts = build_plan(args)
    print(describe(args, plan, per_service, conflicts))
    if conflicts and args.mode == "shared":
        print("\n注意：存在版本冲突，共享 venv 会按锁文件（requirements.mono.txt）覆盖；"
              "若服务跑不起来，用 --mode per-service 退到独立环境。")

    if args.mode == "per-service":
        return _install_per_service(args, plan, per_service)

    # 部署第一步：初始化数据目录（建目录 + 复制配置模板 + 写 capability/runtime）。
    # 让新用户只需要记一条命令：deps.py --install，不用先单独 init。
    for path in plan.dirs:
        path.mkdir(parents=True, exist_ok=True)
    added, kept = init_config(plan.repo_root, plan.data_dir)
    write_capability(plan, plan.data_dir / "capability.json")
    plan.data_dir.joinpath("runtime.json").write_text(
        json.dumps(plan.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"数据目录已初始化：{plan.data_dir}（模板新增 {added}，沿用 {kept}）")

    ok, message = ensure_venv(venv_dir(data_dir), args.python)
    print(message)
    if not ok:
        return 1
    python = venv_python(data_dir)

    index_args: list[str] = []
    if args.index_url:
        index_args += ["--index-url", args.index_url]
    if args.wheels:
        index_args += ["--no-index", "--find-links", args.wheels]

    resolved, conflicts, overrides, extras = build_lock(plan, allow_excluded=args.allow_excluded)
    lock_path = write_lock(plan, resolved)
    print(f"依赖解析：{len(resolved)} 个包（写于 {lock_path}）")
    for item in conflicts:
        print(f"  合并 {item['package']}: {' / '.join(item['specs'])} → {item['chosen']} {item['note']}".rstrip())
    for item in overrides:
        print(f"  覆盖 {item['package']}: {item['from']} → {item['to']}")
    no_deps = extras.get("no_deps") or []
    for item in no_deps:
        print(f"  单独安装（--no-deps）{item['package']}{item['spec']}：{item['reason']}")

    proc = subprocess.run(
        [str(python), "-X", "utf8", "-m", "pip", "install", "--no-input",
         "--disable-pip-version-check", "--timeout", str(args.timeout), "--retries", "5",
         "-r", str(lock_path), *index_args],
        text=True,
        env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
    )
    if proc.returncode != 0:
        print("依赖安装失败：网络/镜像源或版本冲突；可加 --index-url 换源，或用 --mode per-service")
        return proc.returncode

    # 单独安装"依赖声明过大"的包（如 openviking），避免顶掉 fastapi 这类公共 pin
    for item in no_deps:
        spec = f"{item['package']}{item['spec']}"
        print(f"安装 {spec}（--no-deps）")
        extra = subprocess.run(
            [str(python), "-X", "utf8", "-m", "pip", "install", "--no-input",
             "--disable-pip-version-check", "--no-deps", spec, *index_args],
            text=True,
            env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
        )
        if extra.returncode != 0:
            print(f"  ⚠ {spec} 安装失败（对应工具会在运行时提示缺少 openviking）")
            continue

        # --no-deps 装完必须补它运行期真正用到的依赖，否则服务能起来但会
        # 静默退化成 mock（记忆不写不读），排查成本极高。缺一个都不行。
        companions = NO_DEPS_COMPANIONS.get(item["package"]) or []
        if companions:
            print(f"  补装 {item['package']} 的运行期依赖（{len(companions)} 个）")
            fill = subprocess.run(
                [str(python), "-X", "utf8", "-m", "pip", "install", "--no-input",
                 "--disable-pip-version-check", *companions, *index_args],
                text=True,
                env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
            )
            if fill.returncode != 0:
                print("  ⚠ 补充依赖安装失败；openviking 记忆可能退化为 mock（会在服务日志里显式打印）")

    if _generate_protos(args, plan, python) != 0:
        return 1

    venv_dir(data_dir).mkdir(parents=True, exist_ok=True)
    stamp_path(data_dir).write_text(
        json.dumps(planned_stamp(data_dir, lock_path, args.mode), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print("完成：部署就绪（数据目录 + 共享 venv）")
    print(f"启动：python -X utf8 deploy/launcher/run.py --data-dir \"{plan.data_dir}\" "
          f"--features {','.join(plan.features)}")
    print(f"停止：同上，末尾加 --stop")
    print("部署后第一步：配置模型（浏览器打开 http://127.0.0.1:5700 →「配置管理」→ "
          "model-proxy/config/model_list.json 填真实的 api_url / api_key / model）")
    print("  —— 模板里的 key 是占位值，不换掉聊天会报 401/Unauthorized")

    if any(svc.id == "openviking" for svc in plan.services):
        print("\n本机自建 openviking：准备独立 venv")
        if install_openviking_server(args, data_dir) != 0:
            print("⚠ openviking-server 未就绪：记忆会退化为文件模式（面板可重试安装）")
            return 1
    return 0


def _install_per_service(args, plan, per_service) -> int:
    for svc in plan.services:
        packages = per_service.get(svc.id)
        if not packages:
            continue
        base = Path(args.data_dir) / "venvs" / svc.id
        ok, message = ensure_venv(base, args.python)
        print(f"[{svc.id}] {message}")
        if not ok:
            return 1
        req = REPO_ROOT / "services" / svc.dir_name / "requirements.txt"
        python = _service_python(Path(args.data_dir), svc.id)
        proc = subprocess.run(
            [str(python), "-X", "utf8", "-m", "pip", "install", "--no-input",
             "--disable-pip-version-check", "--timeout", str(args.timeout), "--retries", "5",
             "-r", str(req)],
            text=True,
            env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
        )
        if proc.returncode != 0:
            print(f"[{svc.id}] 安装失败")
            return proc.returncode
        stamp_path(Path(args.data_dir), svc.id).write_text(
            json.dumps({"requirements": _file_hash(req), "mode": "per-service"}, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
    print("完成：每服务独立 venv 就绪")
    return 0


def _generate_protos(args, plan, python: Path) -> int:
    targets = proto_targets(Path(args.repo_root), plan)
    if not targets:
        return 0
    print(f"生成 gRPC stubs：{len(targets)} 个服务")
    for target in targets:
        out = Path(target["out"])
        out.mkdir(parents=True, exist_ok=True)
        (out / "__init__.py").touch()
        cmd = proto_command(python, target)
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode != 0:
            print(f"  ✘ {target['service']}: {(proc.stderr or proc.stdout)[-300:]}")
            return 1
        print(f"  ✔ {target['service']}（{len(target['protos'])} 个 proto）")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="单体模式依赖安装（共享 venv）")
    parser.add_argument("--repo-root", default=str(REPO_ROOT))
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--features", default="core,memory")
    parser.add_argument("--modes", default="",
                        help='与 run.py 一致的选择，例如 "memory=local,qq=satori"；'
                             "决定要装哪些服务的依赖")
    parser.add_argument("--mode", choices=["shared", "per-service"], default="shared")
    parser.add_argument("--python", default="", help="用于创建 venv 的基础解释器，默认当前解释器")
    parser.add_argument("--index-url", default=os.getenv("PIP_INDEX_URL", ""))
    parser.add_argument("--wheels", default="", help="离线 wheels 目录（--no-index --find-links）")
    parser.add_argument("--timeout", type=int, default=120)
    parser.add_argument("--allow-excluded", action="store_true",
                        help="把默认排除的包也装上（例如 openviking，会与 fastapi pin 冲突）")
    parser.add_argument("--check", action="store_true", help="只体检")
    parser.add_argument("--install", action="store_true", help="执行安装")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划")
    parser.add_argument("--init", action="store_true",
                        help="只初始化数据目录（建目录 + 复制配置模板 + 写 capability/runtime）")
    parser.add_argument("--list-features", action="store_true",
                        help="列出可选的部署项（与 admin panel 同一份 features.json）")
    parser.add_argument("--openviking", action="store_true",
                        help="只安装/更新本机 openviking-server（独立 venv）")
    parser.add_argument("--export", nargs="?", const="@data", default="",
                        help="只导出合并/覆盖后的统一依赖清单（不建 venv、不装包）："
                             "--export 不给路径时写到 <数据目录>/deps/requirements.txt")
    parser.add_argument("--no-install", action="store_true",
                        help="同 --export：不自动装依赖，只产出依赖清单供自己 pip/conda 安装")
    args = parser.parse_args(argv[1:])

    if args.list_features:
        print(describe_features())
        return 0

    bad = validate_features(args.features)
    if bad:
        print(f"✘ 未知的功能：{', '.join(bad)}")
        print(describe_features())
        return 2

    plan, per_service, conflicts = build_plan(args)

    if args.init:
        for path in plan.dirs:
            path.mkdir(parents=True, exist_ok=True)
        added, kept = init_config(plan.repo_root, plan.data_dir)
        write_capability(plan, plan.data_dir / "capability.json")
        (plan.data_dir / "runtime.json").write_text(
            json.dumps(plan.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"数据目录已初始化：{plan.data_dir}（模板新增 {added}，沿用 {kept}）")
        return 0

    if args.openviking:
        return install_openviking_server(args, Path(args.data_dir))

    if args.export or args.no_install:
        # 只产出依赖清单：给"我自己有 conda / 想先看装什么 / 想手工控制"的人用
        # 不建 venv、不装任何包、不生成 stubs。
        resolved, conflicts, overrides, extras = build_lock(plan, allow_excluded=args.allow_excluded)
        raw = args.export if args.export else "@data"
        target = Path(args.data_dir) / "deps" / "requirements.txt" if raw == "@data" else Path(raw)
        lock_path = write_lock(plan, resolved, target)
        print(f"已导出统一依赖清单：{lock_path}（{len(resolved)} 个包，功能 {','.join(plan.features)}）")
        print("没有自动安装，也没有建 venv。自己装的话：")
        print(f"  pip install -r \"{lock_path}\"")
        print(f"  python -X utf8 deploy/gen_protos.py      # 还要生成 gRPC stubs，否则服务起不来")
        print("（想让它装进独立 venv：加 --install，基础解释器用 --python 指定，例如 conda 环境的 python.exe）")
        for item in conflicts:
            print(f"  合并 {item['package']}: {' / '.join(item['specs'])} → {item['chosen']} {item['note']}".rstrip())
        for item in overrides:
            print(f"  覆盖 {item['package']}: {item['from']} → {item['to']}")
        for item in extras.get("no_deps") or []:
            print(f"  不在主清单里（运行时才单独装）：{item['package']}{item['spec']}")
        print("--- 清单 ---")
        print(lock_path.read_text(encoding="ascii").rstrip())
        return 0

    if args.dry_run or args.check:
        print(describe(args, plan, per_service, conflicts))
        if not venv_python(Path(args.data_dir)).exists():
            print("状态：venv 尚未创建（需要 --install）")
        else:
            stamp = read_stamp(stamp_path(Path(args.data_dir)))
            expected = planned_stamp(Path(args.data_dir), MONO_LOCK, args.mode)
            print("状态：" + ("已是最新" if stamp == expected else f"需要重装（stamp {stamp} ≠ {expected}）"))
        if args.check:
            return 0 if not needs_install(Path(args.data_dir), MONO_LOCK, args.mode) else 1
        return 0

    if not args.install:
        parser.print_help()
        return 2
    return install(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
