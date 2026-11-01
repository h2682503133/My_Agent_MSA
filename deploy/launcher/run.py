"""单体模式 launcher：按功能起/停一组本地服务子进程（无 Docker、无 k8s）。

用法（admin-panel 也是调这个）：
  python -m launcher.run --data-dir "%LOCALAPPDATA%/MyAgent/data" --features core,memory
  python -m launcher.run ... --dry-run           # 只打印计划，不启动
  python -m launcher.run ... --stop              # 按 pids.json 停止上次启动的进程
  python -m launcher.run ... --check-deps        # 只检查各服务的依赖能否 import

约定：
- 每个服务一个子进程（各服务顶层包都叫 `app`，不能共享解释器）；
- 端点全注入 127.0.0.1，日志落 <DATA_DIR>/logs/<service>.log；
- 运行态写 <DATA_DIR>/runtime.json（面板读它显示状态）。
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

try:  # 既支持 python -m launcher.run，也支持 python deploy/launcher/run.py
    from .deps import venv_python
    from .plan import Plan, build, init_config, write_capability
    from .services import SERVICES
except ImportError:  # pragma: no cover - 直接跑脚本时
    import sys as _sys

    _sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from launcher.deps import venv_python
    from launcher.plan import Plan, build, init_config, write_capability
    from launcher.services import SERVICES

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _log(msg: str) -> None:
    print(f"[launcher] {msg}", flush=True)


def _port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def prepare(plan: Plan) -> None:
    for path in plan.dirs:
        path.mkdir(parents=True, exist_ok=True)

    # 配置模板：从仓库 config/ 复制到数据目录（已存在的不覆盖，方便用户改过之后不被冲掉）
    added, kept = init_config(plan.repo_root, plan.data_dir)
    if added or kept:
        _log(f"配置模板：新增 {added} 个文件，沿用已有 {kept} 个")

    write_capability(plan, plan.data_dir / "capability.json")
    plan.data_dir.joinpath("runtime.json").write_text(
        json.dumps(plan.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def check_deps(plan: Plan) -> int:
    """只检查各服务目录能否 import 各自入口（不启动）。"""
    missing = 0
    for svc in plan.services:
        svc_dir = plan.repo_root / "services" / svc.dir_name
        if not svc_dir.is_dir():
            print(f"  ✘ {svc.id}: 目录不存在 {svc_dir}")
            missing += 1
            continue
        env = {**os.environ, **plan.env, "PYTHONPATH": str(svc_dir)}
        proc = subprocess.run(
            [sys.executable, "-c", "import importlib,sys; importlib.import_module(sys.argv[1])", 
             "app.main" if svc.cmd[:1] == ("-m",) else "serve"],
            cwd=str(svc_dir),
            env=env,
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0:
            print(f"  ✔ {svc.id}")
        else:
            first = (proc.stderr or proc.stdout or "").strip().splitlines()
            print(f"  ✘ {svc.id}: {first[-1] if first else 'import 失败'}")
            missing += 1
    return missing


def _child_python(plan: Plan) -> str:
    """子进程用哪个解释器：装了共享 venv 就用它（依赖都在里面），否则用当前解释器。"""
    candidate = venv_python(plan.data_dir)
    return str(candidate) if candidate.exists() else sys.executable


def _spawn(plan: Plan, svc) -> subprocess.Popen:
    svc_dir = plan.repo_root / "services" / svc.dir_name
    log_path = plan.data_dir / "logs" / f"{svc.id}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, **plan.env, "PYTHONPATH": str(svc_dir)}
    cmd = [_child_python(plan), *svc.cmd]
    creation = 0
    if os.name == "nt":
        creation = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
    handle = open(log_path, "a", encoding="utf-8", buffering=1)
    handle.write(f"\n=== start {svc.id} {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
    return subprocess.Popen(
        cmd,
        cwd=str(svc_dir),
        env=env,
        stdout=handle,
        stderr=subprocess.STDOUT,
        creationflags=creation,
    )



def _spawn_web(plan: Plan) -> subprocess.Popen | None:
    """单体前端：静态文件 + /api 反代（替代 nginx；用基础解释器，不依赖 venv 里的包）。"""
    if not plan.web_port:
        return None
    script = Path(__file__).resolve().parent / "web.py"
    log_path = plan.data_dir / "logs" / "web-frontend.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(log_path, "a", encoding="utf-8", buffering=1)
    handle.write(f"\n=== start web-frontend {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
    creation = 0
    if os.name == "nt":
        creation = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
    cmd = [
        sys.executable, str(script),
        "--host", os.getenv("WEB_HOST", "127.0.0.1"),
        "--port", str(plan.web_port),
        "--upstream", f"http://127.0.0.1:{plan.gateway_port}",
        "--static", str(plan.repo_root / "services" / "frontend-service"),
    ]
    return subprocess.Popen(
        cmd,
        cwd=str(plan.repo_root),
        env={**os.environ, **plan.env},
        stdout=handle,
        stderr=subprocess.STDOUT,
        creationflags=creation,
    )


def _spawn_assets(plan: Plan) -> subprocess.Popen | None:
    """单体图床：/assets/<file> 静态服务（替代 k8s 的 image-assets-service）。"""
    if not plan.assets_port:
        return None
    script = Path(__file__).resolve().parent / "assets.py"
    log_path = plan.data_dir / "logs" / "assets.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(log_path, "a", encoding="utf-8", buffering=1)
    handle.write(f"\n=== start assets {time.strftime('%Y-%m-%d %H:%M:%S')} ===\n")
    creation = 0
    if os.name == "nt":
        creation = getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0
        )
    cmd = [
        sys.executable, str(script),
        "--port", str(plan.assets_port),
        "--dir", str(plan.data_dir / "assets" / "images"),
    ]
    return subprocess.Popen(
        cmd,
        cwd=str(plan.repo_root),
        env={**os.environ, **plan.env},
        stdout=handle,
        stderr=subprocess.STDOUT,
        creationflags=creation,
    )



def _load_known_pids(data_dir: Path) -> dict[str, int]:
    """读上次 pids.json 里的服务 → pid（用于"沿用已在运行的服务"）。"""
    path = Path(data_dir) / "pids.json"
    if not path.is_file():
        return {}
    try:
        state = json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:  # noqa: BLE001
        return {}
    result: dict[str, int] = {}
    for item in state.get("processes") or []:
        svc_id = str(item.get("id") or "")
        pid = item.get("pid")
        if svc_id and isinstance(pid, int):
            result[svc_id] = pid
    return result


def _pid_alive(pid: int) -> bool:
    if not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False
    except Exception:  # noqa: BLE001
        return False

def start(plan: Plan) -> int:
    prepare(plan)
    if venv_python(plan.data_dir).exists():
        _log(f"使用共享 venv 解释器：{venv_python(plan.data_dir)}")
    else:
        _log(
            "提示：还没建共享 venv（依赖不会自动安装）。先执行：\n"
            f"  python deploy/launcher/deps.py --data-dir \"{plan.data_dir}\" "
            f"--features {','.join(plan.features)} --install"
        )
    for warning in plan.warnings:
        _log(f"提示：{warning}")

    started: list[tuple[str, subprocess.Popen, dict[str, int]]] = []

    assets_proc = _spawn_assets(plan)
    if assets_proc is not None:
        started.append(("assets", assets_proc, {"http": plan.assets_port}))
        _log(f"启动 assets（pid {assets_proc.pid}）→ http://127.0.0.1:{plan.assets_port}/assets/")

    web_proc = _spawn_web(plan)
    if web_proc is not None:
        started.append(("web-frontend", web_proc, {"http": plan.web_port}))
        _log(f"启动 web-frontend（pid {web_proc.pid}）→ http://127.0.0.1:{plan.web_port}/login.html")

    # 上次运行记录：被跳过的服务如果本来就是自己起的，要把 pid 继承下来，
    # 否则 pids.json 会丢掉它们 → 之后 --stop 管不住，变成孤儿进程。
    known = _load_known_pids(plan.data_dir)
    inherited: dict[str, int] = {}

    for svc in plan.services:
        if any(_port_open(port) for port in svc.ports.values()):
            pid = known.get(svc.id, 0)
            if pid and _pid_alive(pid):
                inherited[svc.id] = pid
                _log(f"沿用已在运行的 {svc.id}（pid {pid}，端口 {svc.ports}）")
            else:
                _log(
                    f"⚠ 跳过 {svc.id}：端口已被占用 {svc.ports}，但不是本启动器启动的进程。"
                    "该服务可能跑的是旧代码/旧配置；确认后请先释放端口再启动"
                )
            continue
        proc = _spawn(plan, svc)
        started.append((svc.id, proc, svc.ports))
        _log(f"启动 {svc.id}（pid {proc.pid}）")

    # 等健康
    deadline = time.time() + 40
    pending = {svc_id: ports for svc_id, _, ports in started}
    while pending and time.time() < deadline:
        for svc_id in list(pending):
            ports = pending[svc_id]
            if all(_port_open(port) for port in ports.values()):
                _log(f"就绪 {svc_id}")
                pending.pop(svc_id)
        time.sleep(0.5)

    if pending:
        _log(f"未就绪：{list(pending)}（可看 logs/ 下对应日志）")

    state = {
        "started_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "data_dir": str(plan.data_dir),
        "capability": str(plan.data_dir / "capability.json"),
        "processes": [
            {"id": svc_id, "pid": proc.pid, "ports": ports}
            for svc_id, proc, ports in started
        ] + [
            # 沿用（端口占用但确是自己上次起的）：也写进来，--stop 才停得掉
            {"id": svc_id, "pid": pid, "ports": {}, "inherited": True}
            for svc_id, pid in inherited.items()
        ],
        "not_ready": list(pending),
    }
    (plan.data_dir / "pids.json").write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    _log(
        f"已启动 {len(started)} 个服务"
        + (f"，沿用 {len(inherited)} 个已在运行的" if inherited else "")
        + f"；状态写入 {plan.data_dir / 'pids.json'}"
    )

    if not started:
        return 1
    try:
        while True:
            time.sleep(1)
            if all(proc.poll() is not None for _, proc, _ in started):
                _log("全部子进程已退出")
                return 0
    except KeyboardInterrupt:
        _log("收到中断，停止子进程…")
        stop(plan)
        return 0


def stop(plan: Plan) -> int:
    state_path = plan.data_dir / "pids.json"
    if not state_path.exists():
        _log("没有找到 pids.json，无需停止")
        return 0
    state = json.loads(state_path.read_text(encoding="utf-8"))
    for item in state.get("processes") or []:
        pid = item.get("pid")
        if not pid:
            continue
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
            else:
                os.kill(pid, signal.SIGTERM)
            _log(f"已停止 {item.get('id')}（pid {pid}）")
        except Exception as exc:  # noqa: BLE001
            _log(f"停止 {item.get('id')} 失败：{exc}")
    state_path.unlink(missing_ok=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="My_Agent 单体模式 launcher（无 Docker）")
    parser.add_argument("--repo-root", default=str(REPO_ROOT))
    parser.add_argument("--data-dir", default=str(Path.home() / "MyAgent" / "data"))
    parser.add_argument("--features", default="core,memory")
    parser.add_argument("--modes", default="", help='例如 "memory=local,qq=satori"')
    parser.add_argument("--extra-services", default="", help="额外指定要起的服务 id（逗号分隔）")
    parser.add_argument(
        "--openviking-url",
        default="",
        help="openviking 服务器地址（如 http://10.0.0.5:1933）；优先级高于 config/launcher.json",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--check-deps", action="store_true")
    parser.add_argument("--stop", action="store_true")
    parser.add_argument("--web-host", default=os.getenv("WEB_HOST", "127.0.0.1"),
                        help="单体网页监听地址；要让局域网访问用 0.0.0.0（默认只本机）")
    args = parser.parse_args(argv)

    features = [item.strip() for item in args.features.split(",") if item.strip()]
    modes: dict[str, str] = {}
    for pair in args.modes.split(","):
        if "=" in pair:
            key, _, value = pair.partition("=")
            modes[key.strip()] = value.strip()
    extra = [item.strip() for item in args.extra_services.split(",") if item.strip()]

    if args.openviking_url:
        os.environ["OPENVIKING_SERVER_URL"] = args.openviking_url

    plan = build(
        repo_root=Path(args.repo_root),
        data_dir=Path(args.data_dir),
        features=features,
        modes=modes,
        extra_services=extra,
    )

    if args.dry_run:
        print(json.dumps(plan.to_dict(), ensure_ascii=False, indent=2))
        return 0
    if args.check_deps:
        return 0 if check_deps(plan) == 0 else 1
    if args.stop:
        return stop(plan)
    return start(plan)


if __name__ == "__main__":
    raise SystemExit(main())
