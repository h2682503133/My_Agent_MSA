"""单体依赖安装器自检（离线：不创建 venv、不联网、不写仓库外的持久文件）。"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "deploy"))

from launcher import deps  # noqa: E402

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


def _args(data_dir: Path, features: str = "core,memory"):
    return SimpleNamespace(
        repo_root=str(ROOT),
        data_dir=str(data_dir),
        features=features,
        mode="shared",
        python="",
        index_url="",
        wheels="",
        timeout=120,
    )


def main() -> int:
    tmp = Path(tempfile.mkdtemp())

    print("requirements 解析")
    parsed = deps.parse_requirements(ROOT / "services" / "user-service" / "requirements.txt")
    check("解析出包", len(parsed) >= 3, str(parsed))
    check("包名小写", all(name == name.lower() for name in parsed))
    check("带版本号", "grpcio" in parsed and parsed["grpcio"].startswith("grpcio=="), str(parsed.get("grpcio")))
    check("跳过注释与空行", "" not in parsed)

    print("按功能解析服务 + 冲突报告")
    plan, per_service, conflicts = deps.build_plan(_args(tmp, "core,memory,tools.basic"))
    check("服务数 7", len(plan.services) == 7, str([s.id for s in plan.services]))
    with_req = [svc.id for svc in plan.services
                if (ROOT / "services" / svc.dir_name / "requirements.txt").exists()]
    check("有 requirements 的服务都被收集",
          len(per_service) == len(with_req), f"{sorted(per_service)} vs {sorted(with_req)}")
    names = {item["package"] for item in conflicts}
    check("识别 grpcio 冲突", "grpcio" in names, str(names))
    check("识别 protobuf 冲突", "protobuf" in names)
    check("识别 httpx 冲突", "httpx" in names)
    check("冲突项带服务归属", all(s["services"] for item in conflicts for s in item["specs"]))

    print("版本覆盖表")
    overrides = deps.parse_requirements(deps.OVERRIDES)
    check("覆盖表存在", len(overrides) >= 4, str(len(overrides)))
    check("覆盖 grpcio 1.76", overrides.get("grpcio") == "grpcio==1.76.0", str(overrides.get("grpcio")))
    check("覆盖 protobuf 6.x", overrides.get("protobuf", "").startswith("protobuf==6"), str(overrides.get("protobuf")))
    check("websockets 是下限不是 pin", overrides.get("websockets", "").startswith("websockets>="), str(overrides.get("websockets")))

    print("按功能解析依赖")
    resolved, conflicts, applied, extras = deps.build_lock(plan)
    check("解析出包", len(resolved) >= 8, str(len(resolved)))
    check("独立安装清单存在", "no_deps" in extras and "skipped" in extras, str(list(extras)))
    check("openviking 走 --no-deps 而不是进主锁", "openviking" not in resolved)
    check("tools.basic 场景 openviking 在 no_deps", any(x["package"] == "openviking" for x in extras["no_deps"]),
          str(extras["no_deps"]))
    plan_core = deps.build(repo_root=Path(deps.REPO_ROOT), data_dir=tmp / "data-core",
                           features=["core", "memory"], modes={})
    _r, _c, _a, extras_core = deps.build_lock(plan_core)
    check("core+memory 完全不涉及 openviking", not extras_core["no_deps"] and "openviking" not in _r,
          str(extras_core["no_deps"]))
    check("冲突有记录", len(conflicts) >= 3, str(len(conflicts)))
    check("覆盖表生效或已符合", bool(applied) or resolved.get("grpcio") == "==1.76.0",
          str([item["package"] for item in applied]))
    check("grpcio 最终是 1.76", resolved.get("grpcio") == "==1.76.0", str(resolved.get("grpcio")))

    print("选中 tools.basic 时 openviking 才出现，且不进主锁")
    plan_tools = deps.build(
        repo_root=Path(deps.REPO_ROOT), data_dir=tmp / "data-tools",
        features=["core", "memory", "tools.basic"], modes={},
    )
    resolved_tools, _c2, _a2, extras_tools = deps.build_lock(plan_tools)
    check("openviking 不在主锁", "openviking" not in resolved_tools)
    check("openviking 在 no_deps 清单", any(x["package"] == "openviking" for x in extras_tools["no_deps"]),
          str(extras_tools["no_deps"]))

    print("解析规则")
    check("多个 pin 取最高", deps.resolve_spec(["==1.64.1", "==1.68.1"])[0] == "==1.68.1")
    check("pin 低于下限时改用下限", deps.resolve_spec(["==2.32.3", ">=2.33.0"])[0] == ">=2.33.0")
    check("多个下限取最高", deps.resolve_spec([">=13.1", ">=15.0.1"])[0] == ">=15.0.1")
    check("相同 pin 不变", deps.resolve_spec(["==0.28.1", "==0.28.1"])[0] == "==0.28.1")

    print("proto 生成计划")
    targets = deps.proto_targets(ROOT, plan)
    check("有需要生成 stubs 的服务", len(targets) >= 5, str([t["service"] for t in targets]))
    sample = targets[0]
    cmd = deps.proto_command(deps.venv_python(tmp), sample)
    check("命令用 grpc_tools.protoc", "grpc_tools.protoc" in cmd)
    check("含 -I proto", any(str(ROOT / "services" / sample["service"] / "proto") == part for part in cmd))
    check("输出到 app/generated", any("--python_out" in part for part in cmd))
    check("带上 proto 文件", any(part.endswith(".proto") for part in cmd))

    print("venv 路径与 stamp")
    check("venv 指到数据目录", deps.venv_dir(tmp) == tmp / "venv")
    check("venv 未创建时提示需要安装", deps.needs_install(tmp, deps.OVERRIDES, "shared") is True)
    check("模块名与文件路径一致", deps.stamp_path(tmp).name == deps.STAMP_NAME)

    print("--check / --dry-run 不改动环境")
    before = set(p.name for p in tmp.iterdir())
    code = deps.main(["deps", "--data-dir", str(tmp), "--features", "core", "--check"])
    after = set(p.name for p in tmp.iterdir())
    check("check 返回非 0（需要安装）", code == 1, str(code))
    check("没有创建 venv", "venv" not in after and before == after, str(after - before))

    code2 = deps.main(["deps", "--data-dir", str(tmp), "--features", "core", "--dry-run"])
    check("dry-run 返回 0", code2 == 0, str(code2))

    print("描述输出包含关键信息")
    text = deps.describe(_args(tmp), plan, per_service, conflicts)
    for needle in ("覆盖表", "冲突", "gRPC stubs", "venv"):
        check(f"描述含「{needle}」", needle in text, text[:120])

    print()
    if FAILS:
        print(f"FAILED {len(FAILS)}: {FAILS}")
        return 1
    print(f"ALL OK — {CHECKS} checks passed")
    return 0



def test_openviking_companions():
    """openviking 用 --no-deps 装，必须补这些运行期依赖（虚拟机实测逐个缺过）。"""
    from launcher import deps as deps_mod

    companions = deps_mod.NO_DEPS_COMPANIONS.get("openviking") or []
    check("openviking 有补充依赖清单", bool(companions))
    for required in ("openviking-sdk", "loguru", "jsonschema", "json-repair", "pathspec"):
        check(f"包含 {required}", required in companions, str(companions))

    # --no-deps 安装后必须真的去装 companions（防止只声明不执行）
    src = (deps_mod.__file__ and open(deps_mod.__file__, encoding="utf-8").read()) or ""
    check("安装流程引用了 NO_DEPS_COMPANIONS", "NO_DEPS_COMPANIONS.get" in src)
    check("补装失败会给出显式告警", "openviking 记忆可能退化为 mock" in src)


if __name__ == "__main__":
    test_openviking_companions()
    raise SystemExit(main())
