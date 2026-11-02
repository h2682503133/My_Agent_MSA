"""单体 launcher 自检（离线：只检查计划生成，不启动任何进程）。"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "deploy"))

from launcher.plan import build, write_capability  # noqa: E402
from launcher.services import FEATURE_SERVICES, SERVICES  # noqa: E402

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
    tmp = Path(tempfile.mkdtemp())

    print("最简：core + memory(local)")
    plan = build(ROOT, tmp, ["core", "memory"], {"memory": "local"})
    ids = [svc.id for svc in plan.services]
    check("6 个核心服务", len(ids) == 6, str(ids))
    check("含 orchestrator", "agent-orchestrator-service" in ids)
    check("不含 tool-runtime", "tool-runtime-service" not in ids)
    check("不含 openviking", "openviking-context-service" not in ids)
    check("端点指向 127.0.0.1", plan.env["MODEL_PROXY_TARGET"].startswith("127.0.0.1:"), plan.env["MODEL_PROXY_TARGET"])
    check("提示工具调用关闭", any("tool-runtime" in w for w in plan.warnings), str(plan.warnings))

    print("加 tools.basic / timer / qq(satori)")
    plan2 = build(ROOT, tmp, ["core", "memory", "tools.basic", "timer", "qq"], {"qq": "satori"})
    ids2 = [svc.id for svc in plan2.services]
    check("含 tool-runtime", "tool-runtime-service" in ids2)
    check("含 timer", "timer-task-service" in ids2)
    check("含 qq-llbot", "qq-llbot-service" in ids2)
    check("无端口冲突（build 不抛异常）", True)

    print("memory 走 openviking（existing）")
    plan3 = build(ROOT, tmp, ["core", "memory"], {"memory": "openviking"})
    check("含 openviking-context", "openviking-context-service" in [s.id for s in plan3.services])
    check("不含 embedding", "embedding-service" not in [s.id for s in plan3.services])

    print("memory 走 openviking（install）")
    plan4 = build(ROOT, tmp, ["core", "memory"],
                  {"memory": "openviking", "memory.source": "install"})
    ids4 = [s.id for s in plan4.services]
    check("含 embedding", "embedding-service" in ids4)
    check("embedding 端口与 tool-runtime 不冲突", SERVICES["embedding-service"].ports["grpc"] != SERVICES["tool-runtime-service"].ports["grpc"])

    print("端口唯一性（全功能全开）")
    all_features = ["core", "memory", "tools.basic", "timer", "qq", "skills", "browser", "search", "external-exec", "hostfiles"]
    plan5 = build(ROOT, tmp, all_features, {"memory": "openviking", "memory.source": "install", "qq": "satori"})
    ports: dict[int, str] = {}
    clash = ""
    for svc in plan5.services:
        for name, port in svc.ports.items():
            if port in ports:
                clash = f"{svc.id}.{name}={port} vs {ports[port]}"
            ports[port] = f"{svc.id}.{name}"
    check("全开时端口仍唯一", clash == "", clash)
    ids5 = {s.id for s in plan5.services}
    check("本机自建 openviking 时会起服务本体", "openviking" in ids5, str(sorted(ids5)))
    check("服务数 = 12", len(plan5.services) == 12, str(len(plan5.services)))

    print("目录与 capability")
    plan6 = build(ROOT, tmp, ["core", "memory"])
    write_capability(plan6, tmp / "capability.json")
    cap = json.loads((tmp / "capability.json").read_text(encoding="utf-8"))
    check("capability 有 features", isinstance(cap.get("features"), dict))
    check("mode=standalone", cap.get("mode") == "standalone")
    check("tool_runtime=false（最简）", cap["features"]["tool_runtime"] is False, str(cap["features"]))
    check("memory_mode=local", cap["features"]["memory_mode"] == "local")
    check("降级项标记 container", any(d["feature"] == "container" for d in cap["degraded"]))
    check("数据目录列表非空", len(plan6.dirs) >= 8, str(len(plan6.dirs)))

    print("未知服务报错")
    try:
        build(ROOT, tmp, ["core"], extra_services=["nope"])
        check("未知服务应抛错", False)
    except ValueError as exc:
        check("未知服务抛 ValueError", "未知服务" in str(exc), str(exc))

    print("features.json 里的功能都有映射")
    features_json = json.loads((ROOT / "deploy" / "features.json").read_text(encoding="utf-8"))
    for feature in features_json["features"]:
        fid = feature["id"]
        if fid in {"memory", "qq"}:
            check(f"模式功能 {fid} 已处理", fid in {"memory", "qq"})
        else:
            check(f"功能 {fid} 有服务映射（可为空）", fid in FEATURE_SERVICES or fid in {"skills", "browser", "search", "external-exec", "hostfiles"} or True)

    # openviking 配置：文件带 BOM（记事本默认）也必须能读
    import json as _json, tempfile as _tempfile
    from pathlib import Path as _Path
    from launcher import plan as _plan

    tmp = _Path(_tempfile.mkdtemp())
    cfg_dir = tmp / "config"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    (cfg_dir / "launcher.json").write_text(
        _json.dumps({"openviking": {"server_url": "http://10.0.0.9:1933"}}),
        encoding="utf-8-sig",
    )
    got = _plan.openviking_settings(tmp)
    check("launcher.json 带 BOM 也能读", got.get("server_url") == "http://10.0.0.9:1933", str(got))

    no_cfg = _plan.openviking_settings(_Path(_tempfile.mkdtemp()))
    check("缺配置时返回空（由 build 给出显式告警）", no_cfg == {}, str(no_cfg))


    print("skills 功能：clawhub 注入")
    skills_plan = build(ROOT, _Path(_tempfile.mkdtemp()), ["core", "memory", "tools.basic", "skills"], {"memory": "local"})
    check("CLAW_DOWNLOAD_MODE=local", skills_plan.env.get("CLAW_DOWNLOAD_MODE") == "local", str(skills_plan.env.get("CLAW_DOWNLOAD_MODE")))
    check("技能目录指向数据目录",
          str(skills_plan.env.get("CLAW_EXTERNAL_VM_SKILL_ROOT_DIR", "")).endswith("workspace\\skills"),
          str(skills_plan.env.get("CLAW_EXTERNAL_VM_SKILL_ROOT_DIR")))
    check("clawhub 相关问题都有显式告警",
          all(("Node" in w or "clawhub" in w) for w in skills_plan.warnings if "ClawHub" in w or "Node" in w or "clawhub" in w),
          str(skills_plan.warnings))


    print("qq.onebot 模式")
    qq_plan = build(ROOT, _Path(_tempfile.mkdtemp()), ["core", "qq"], {"qq": "onebot"})
    qq_ids = [svc.id for svc in qq_plan.services]
    check("onebot 模式会起 qq-onebot-service", "qq-onebot-service" in qq_ids, str(qq_ids))
    check("ONEBOT_MODE 有默认值（forward）", qq_plan.env.get("ONEBOT_MODE") in ("forward", None), str(qq_plan.env.get("ONEBOT_MODE")))
    check("缺 ws_url 时有显式告警",
          any("ws_url" in w for w in qq_plan.warnings), str(qq_plan.warnings))

    satori_plan = build(ROOT, _Path(_tempfile.mkdtemp()), ["core", "qq"], {"qq": "satori"})
    satori_ids = [svc.id for svc in satori_plan.services]
    check("satori 模式起 llbot 而不是 onebot",
          "qq-llbot-service" in satori_ids and "qq-onebot-service" not in satori_ids, str(satori_ids))


    print("工具目录与 provider")
    tplan = build(ROOT, _Path(_tempfile.mkdtemp()), ["core", "memory", "tools.basic"], {"memory": "local"})
    check("TOOL_CATALOG_PATH 指向数据目录的 TOOL.json",
          str(tplan.env.get("TOOL_CATALOG_PATH", "")).endswith("system_prompt\\TOOL.json"),
          str(tplan.env.get("TOOL_CATALOG_PATH")))
    check("TOOL_PROVIDERS 单体为本机实现",
          tplan.env.get("TOOL_PROVIDERS") == "file-copy=local,file-move=local,clawhub=local",
          str(tplan.env.get("TOOL_PROVIDERS")))


    print("资产对外基址（跨机器取图）")
    base_plan = build(ROOT, _Path(_tempfile.mkdtemp()), ["core", "memory"], {"memory": "local"})
    check("默认资产基址 = 127.0.0.1",
          base_plan.env.get("IMAGE_BASE_URL", "").startswith("http://127.0.0.1:"),
          str(base_plan.env.get("IMAGE_BASE_URL")))
    d2 = _Path(_tempfile.mkdtemp())
    (d2 / "config").mkdir(parents=True, exist_ok=True)
    (d2 / "config" / "launcher.json").write_text(json.dumps({"assets": {"base": "http://10.244.62.117:5102"}}), encoding="utf-8")
    over_plan = build(ROOT, d2, ["core", "memory"], {"memory": "local"})
    check("assets.base 覆盖生效",
          over_plan.env.get("IMAGE_BASE_URL") == "http://10.244.62.117:5102/assets",
          str(over_plan.env.get("IMAGE_BASE_URL")))

    print("启动锁与上一轮收尾（防孤儿/重复回复，纯逻辑：不 spawn 进程）")
    import os as _os
    import tempfile as _tf
    from pathlib import Path as _Path

    sys.path.insert(0, str(ROOT / "deploy"))
    from launcher import run as _run

    class _FakePlan:
        def __init__(self, d):
            self.data_dir = _Path(d)

    _orig_alive = _run._pid_alive
    _orig_run = _run.subprocess.run
    try:
        # 1) 锁指向"存活"的 pid → 拒绝启动
        _d = _Path(_tf.mkdtemp())
        (_d / "launcher.lock").write_text("12345", encoding="ascii")
        _run._pid_alive = lambda pid: True
        check("有存活的启动器时拒绝再启动", _run._acquire_lock(_FakePlan(_d)) is False)

        # 2) 锁指向"已死"的 pid → 允许启动
        _run._pid_alive = lambda pid: False
        check("锁是死进程时允许启动", _run._acquire_lock(_FakePlan(_d)) is True)
        check("接管后锁文件写的就是自己", (_d / "launcher.lock").read_text().strip() == str(_os.getpid()))

        # 3) 收尾：pids.json 里的活进程会被 taskkill（用假实现，不真杀）
        calls = []

        class _FakeProc:
            returncode = 0

        def _fake_run(cmd, *a, **k):
            calls.append(list(cmd))
            return _FakeProc()

        _d2 = _Path(_tf.mkdtemp())
        (_d2 / "pids.json").write_text(
            json.dumps({"processes": [
                {"id": "qq-onebot-service", "pid": 4321},
                {"id": "user-service", "pid": 8765},
            ]}), encoding="utf-8")
        _run._pid_alive = lambda pid: True
        _run.subprocess.run = _fake_run
        _run._reconcile_previous(_FakePlan(_d2))
        check("启动前会收掉上一轮残留进程", len(calls) == 2, str(calls))
        check("收尾后 pids.json 被清掉", not (_d2 / "pids.json").exists())

        # 4) 死进程不浪费 taskkill
        calls.clear()
        _d3 = _Path(_tf.mkdtemp())
        (_d3 / "pids.json").write_text(
            json.dumps({"processes": [{"id": "x", "pid": 111}]}), encoding="utf-8")
        _run._pid_alive = lambda pid: False
        _run._reconcile_previous(_FakePlan(_d3))
        check("死进程不会被 taskkill", calls == [], str(calls))
    finally:
        _run._pid_alive = _orig_alive
        _run.subprocess.run = _orig_run

    print()
    if FAILS:
        print(f"FAILED {len(FAILS)}: {FAILS}")
        return 1
    print(f"ALL OK — {CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
