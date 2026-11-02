"""用容器里现成的 grpcio-tools 生成各服务的 gRPC stubs（不联网、不改镜像）。

为什么用容器：宿主机 pip 装不上 grpcio-tools（网络受限），而各服务镜像里本来就有，
且版本与线上一致（tool-runtime 镜像：protobuf 6.33.5）。

用法：
  python -X utf8 deploy/gen_protos.py --image agent/tool-runtime-service:v129
  python -X utf8 deploy/gen_protos.py --image ... --only user-service,gateway-backend-service
  python -X utf8 deploy/gen_protos.py --image ... --check     # 只检查缺哪些

输出：
  app/generated/  （多数服务）或 proto_gen/（gateway / qq-*），与各服务 scripts/gen_proto.sh 一致。
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SERVICES_DIR = REPO / "services"

# 输出目录约定：有 app/ 包的写到 app/generated，否则写 proto_gen（与各服务的 sys.path 约定一致）
APP_GENERATED = {
    "agent-orchestrator-service",
    "embedding-service",
    "model-proxy-service",
    "openviking-context-service",
    "task-scheduler-service",
    "timer-task-service",
    "tool-runtime-service",
    "user-service",
}
PROTO_GEN = {
    "gateway-backend-service",
    "qq-llbot-service",
    "qq-official-service",
    "qq-onebot-service",
}


def services_with_protos(only: set[str] | None = None) -> list[dict]:
    rows = []
    for svc_dir in sorted(SERVICES_DIR.iterdir()):
        if not svc_dir.is_dir():
            continue
        name = svc_dir.name
        if only and name not in only:
            continue
        proto_dir = svc_dir / "proto"
        protos = sorted(p.name for p in proto_dir.glob("*.proto")) if proto_dir.is_dir() else []
        if not protos:
            continue
        if name in APP_GENERATED:
            out_dir = svc_dir / "app" / "generated"
        elif name in PROTO_GEN:
            out_dir = svc_dir / "proto_gen"
        else:
            continue
        rows.append({"service": name, "proto_dir": proto_dir, "protos": protos, "out_dir": out_dir})
    return rows


def expected_files(row: dict) -> list[Path]:
    out = row["out_dir"]
    files = []
    for proto in row["protos"]:
        stem = Path(proto).stem
        files.append(out / f"{stem}_pb2.py")
        if stem != "embedding":  # embedding.proto 没有 service，不生成 grpc 桩
            files.append(out / f"{stem}_pb2_grpc.py")
    return files


def run_container(image: str, workdir: Path, limit: int = 300) -> tuple[bool, str]:
    cmd = [
        "docker", "run", "--rm",
        "-v", f"{workdir}:/work",
        "-w", "/work",
        image,
        "python", "-m", "grpc_tools.protoc",
        "-I", "proto",
        "--python_out=out",
        "--grpc_python_out=out",
        *[f"proto/{name}" for name in sorted(p.name for p in (workdir / "proto").glob("*.proto"))],
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=limit)
    except subprocess.TimeoutExpired:
        return False, "容器超时"
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout or "")[-400:]
    return True, ""


def generate(row: dict, image: str) -> tuple[bool, str]:
    with tempfile.TemporaryDirectory(prefix="proto-") as tmp:
        work = Path(tmp)
        shutil.copytree(row["proto_dir"], work / "proto")
        (work / "out").mkdir()
        ok, err = run_container(image, work)
        if not ok:
            return False, err
        shutil.copytree(work / "out", row["out_dir"], dirs_exist_ok=True)
    # 生成物所在目录补 __init__.py（app/generated 需要；proto_gen 不需要但无害）
    init_file = row["out_dir"] / "__init__.py"
    if not init_file.exists():
        init_file.write_text("", encoding="utf-8")
    return True, ""


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="用容器里的 grpcio-tools 生成 gRPC stubs")
    parser.add_argument("--image", default="agent/tool-runtime-service:v129")
    parser.add_argument("--only", default="", help="逗号分隔的服务名")
    parser.add_argument("--check", action="store_true", help="只检查缺失，不生成")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv[1:])

    only = {s.strip() for s in args.only.split(",") if s.strip()} or None
    rows = services_with_protos(only)
    if not rows:
        print("没有找到带 proto 的服务")
        return 1

    report = []
    missing_total = 0
    for row in rows:
        need = expected_files(row)
        missing = [str(p.relative_to(REPO)) for p in need if not p.exists()]
        missing_total += len(missing)
        item = {"service": row["service"], "protos": row["protos"], "out": str(row["out_dir"].relative_to(REPO)),
                "missing": missing}
        if missing and not args.check:
            ok, err = generate(row, args.image)
            item["generated"] = ok
            item["error"] = err
            if ok:
                item["missing_after"] = [str(p.relative_to(REPO)) for p in need if not p.exists()]
        report.append(item)

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for item in report:
            status = "缺 %d" % len(item["missing"]) if item["missing"] else "已齐"
            if item.get("generated") is True:
                status += "；已生成"
            elif item.get("generated") is False:
                status += f"；生成失败：{item.get('error', '')[:120]}"
            print(f"  {item['service']:32} {len(item['protos'])} proto  {status}  → {item['out']}")
        print(f"\n合计缺失 {missing_total} 个文件" if missing_total else "\n全部齐全")

    if args.check:
        return 0 if missing_total == 0 else 1
    failed = [i["service"] for i in report if i.get("generated") is False]
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
