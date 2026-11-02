#!/usr/bin/env python3
"""版本单一来源：deploy/release.json（镜像 tag）+ services/*/requirements.txt（pip 依赖）。

为什么需要：镜像 tag 原来散落在
  - deploy/services/*.yaml（直接 kubectl apply 的静态文件）
  - deploy/compose/docker-compose.yml
  - deploy/tool-runtime-apply.sh（默认值）
  - deploy/gen_protos.py（--image 默认值，之前还留着 v129）
  - services/*/README.md（给人抄的 docker build/save 示例）
每加一次版本要手改 6~8 处，漏改就漂移（本轮就漏了 apply 脚本和 compose）。
现在只保留 release.json 一个事实源，其余由本工具 sync/check。

命令
----
    python -X utf8 deploy/release.py check            # 只校验，漂移 → 非零退出（部署前跑）
    python -X utf8 deploy/release.py sync             # 把事实源写回所有派生文件
    python -X utf8 deploy/release.py bump --changed   # 只 bump 自上次改 release.json 以来动过的服务
    python -X utf8 deploy/release.py bump tool-runtime-service
    python -X utf8 deploy/release.py apply [--dry-run]  # 按事实源 kubectl set image + 等 rollout
    python -X utf8 deploy/release.py status           # 集群现状 vs 事实源
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MANIFEST = ROOT / "deploy" / "release.json"
SERVICES_DIR = ROOT / "deploy" / "services"

# 参与校验/同步的文件（顺序稳定，便于 diff）
TARGET_FILES = [
    *sorted(SERVICES_DIR.glob("*.yaml")),
    ROOT / "deploy" / "compose" / "docker-compose.yml",
    ROOT / "deploy" / "tool-runtime-apply.sh",
    ROOT / "deploy" / "gen_protos.py",
    *sorted((ROOT / "services").glob("*/README.md")),
]

# 这些 yaml 的 image 不是我们的构建产物（第三方/环境变量注入），不纳入管理
IGNORED_FILES = {
    "deploy/services/image-assets-service.yaml",  # nginx:alpine（第三方）
    "deploy/services/llbot.yaml",                 # ${LLBOT_IMAGE}
    "deploy/services/llbot-pmhq.yaml",            # ${PMHQ_IMAGE}
    "deploy/services/lan-expose.yaml",            # nginx:alpine
}

COMPOSE_TAG = "compose"      # compose 自己构建的本地镜像，不跟语义版本
# 只认两类引用：agent/<svc>:tag 与已知的 ghcr.io/...:tag。
# （不要用宽泛的 "...:tag" 正则：${TOOL_RUNTIME_IMAGE:-agent/x:v1} 这种会被切错）
REF_RE = re.compile(
    r"(?P<repo>\bagent/[a-z0-9][a-z0-9\-]*|ghcr\.io/[A-Za-z0-9_.\-/]+)"
    r":(?P<tag>[A-Za-z0-9_.\-]+)"
)


# ── 事实源读写 ────────────────────────────────────────────────
def load_manifest() -> dict:
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def save_manifest(data: dict) -> None:
    MANIFEST.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def manifest_refs(data: dict) -> dict[str, tuple[str, str]]:
    """repo -> (期望 tag, 所属类别)。类别：service / external。"""
    refs: dict[str, tuple[str, str]] = {}
    for name, tag in (data.get("services") or {}).items():
        refs[f"agent/{name}"] = (str(tag), "service")
    for repo, spec in (data.get("external") or {}).items():
        tag = spec.get("tag") if isinstance(spec, dict) else spec
        refs[str(repo)] = (str(tag), "external")
    return refs


# ── 扫描 / 校验 / 同步 ────────────────────────────────────────
def _rel(path: Path) -> str:
    return str(path.relative_to(ROOT)).replace("\\", "/")


def scan(data: dict) -> list[dict]:
    """返回所有 image 引用：{file, repo, tag, expected, kind, managed}"""
    refs = manifest_refs(data)
    known = set(refs)
    rows: list[dict] = []
    for path in TARGET_FILES:
        if not path.is_file():
            continue
        rel = _rel(path)
        managed = rel not in IGNORED_FILES
        text = path.read_text(encoding="utf-8")
        for match in REF_RE.finditer(text):
            repo, tag = match.group("repo"), match.group("tag")
            if not (repo.startswith("agent/") or repo in known):
                continue
            expected = refs.get(repo, (None, "unknown"))[0]
            kind = refs.get(repo, (None, "unknown"))[1]
            rows.append({
                "file": rel,
                "repo": repo,
                "tag": tag,
                "expected": expected,
                "kind": kind,
                "managed": managed,
            })
    return rows


def check(data: dict, quiet: bool = False) -> int:
    refs = manifest_refs(data)
    problems: list[str] = []
    for row in scan(data):
        if not row["managed"]:
            continue
        if row["expected"] is None:
            problems.append(f"{row['file']}: {row['repo']} 不在 release.json 里（先加进 manifest）")
            continue
        if row["kind"] == "service" and row["tag"] == COMPOSE_TAG:
            continue
        if row["tag"] != row["expected"]:
            problems.append(
                f"{row['file']}: {row['repo']}:{row['tag']} ≠ manifest {row['expected']}"
            )
    problems.extend(check_pip(data))
    if problems:
        if not quiet:
            print("版本漂移（跑 `python -X utf8 deploy/release.py sync` 修）：")
            for line in problems:
                print("  -", line)
        return 1
    if not quiet:
        print(f"OK：{len(refs)} 个镜像与所有派生文件一致（pip 与镜像版本也一致）")
    return 0


def check_pip(data: dict) -> list[str]:
    """pip 依赖的唯一来源是 services/*/requirements.txt，这里只校验它和镜像 tag 不冲突。"""
    problems: list[str] = []
    external = data.get("external") or {}
    ov = external.get("ghcr.io/volcengine/openviking") or {}
    ov_tag = (ov.get("tag") if isinstance(ov, dict) else ov) or ""
    req = ROOT / "services" / "openviking-context-service" / "requirements.txt"
    if req.is_file() and ov_tag:
        wanted = "openviking==" + str(ov_tag).lstrip("v")
        lines = [
            line.split("#")[0].strip()
            for line in req.read_text(encoding="utf-8").splitlines()
        ]
        if wanted not in lines:
            problems.append(
                f"{_rel(req)}: 缺少 {wanted}（应与 openviking 镜像 tag {ov_tag} 一致）"
            )
    # mono 侧不许再抄一份版本号
    deps = ROOT / "deploy" / "launcher" / "deps.py"
    if deps.is_file():
        text = deps.read_text(encoding="utf-8")
        for hit in re.finditer(r"OPENVIKING_PIN\s*=\s*[\"']([^\"']+)[\"']", text):
            problems.append(
                f"{_rel(deps)}: 仍写死 OPENVIKING_PIN={hit.group(1)!r}"
                "（应从 services/openviking-context-service/requirements.txt 读）"
            )
    return problems


def sync(data: dict) -> int:
    refs = manifest_refs(data)
    changed: list[str] = []
    for path in TARGET_FILES:
        if not path.is_file():
            continue
        rel = _rel(path)
        if rel in IGNORED_FILES:
            continue
        text = original = path.read_text(encoding="utf-8")

        def repl(match: re.Match) -> str:
            repo, tag = match.group("repo"), match.group("tag")
            spec = refs.get(repo)
            if spec is None:
                return match.group(0)
            expected, kind = spec
            if kind == "service" and tag == COMPOSE_TAG:
                return match.group(0)          # compose 的本地构建 tag 不动
            return f"{repo}:{expected}"

        text = REF_RE.sub(repl, text)
        if text != original:
            path.write_text(text, encoding="utf-8")
            changed.append(rel)
    print("已同步：" + (", ".join(changed) if changed else "无变化（已经是事实源状态）"))
    return 0 if check(data, quiet=True) == 0 else 1


# ── bump ─────────────────────────────────────────────────────
def next_tag(tag: str) -> str:
    match = re.fullmatch(r"v(\d+)\.(\d+)", str(tag))
    if not match:
        raise SystemExit(f"tag 不是 vMAJOR.MINOR 形式：{tag}")
    return f"v{match.group(1)}.{int(match.group(2)) + 1}"


def changed_services(since: str | None, deployed: str = "") -> list[str]:
    """git 判定"哪些服务该 bump"。

    基准优先级：`--since` > manifest 里记录的 deployed_commit（上一次 apply 的提交）
    > release.json 最后一次改动 > HEAD~1（此时会提示不可靠）。
    """
    base = since or deployed
    if not base:
        base = subprocess.run(
            ["git", "log", "-1", "--format=%H", "--", "deploy/release.json"],
            cwd=ROOT, capture_output=True, text=True,
        ).stdout.strip()
    if not base:
        print("提示：不知道上次部署的提交（manifest 里没有 deployed_commit），退回 HEAD~1 判定")
        base = "HEAD~1"
    out = subprocess.run(
        ["git", "diff", "--name-only", f"{base}..HEAD"],
        cwd=ROOT, capture_output=True, text=True,
    ).stdout
    names = set()
    for line in out.splitlines():
        parts = line.replace("\\", "/").split("/")
        if len(parts) >= 2 and parts[0] == "services":
            names.add(parts[1])
    return sorted(names)


def bump(data: dict, names: list[str], since: str | None) -> int:
    targets = []
    if "--changed" in names or "changed" in names:
        targets = changed_services(since, str(data.get("deployed_commit") or ""))
        if not targets:
            print("自上次 release.json 变更以来没有服务改动，无需 bump")
            return 0
        print("git 判定需要 bump：" + ", ".join(targets))
    else:
        targets = names
    services = data.setdefault("services", {})
    unknown = [n for n in targets if n not in services]
    if unknown:
        raise SystemExit("manifest 里没有这些服务：" + ", ".join(unknown))
    for name in targets:
        old = services[name]
        services[name] = next_tag(old)
        print(f"  {name}: {old} → {services[name]}")
    save_manifest(data)
    return 0


# ── apply / status ──────────────────────────────────────────
def kubectl(args: list[str], dry: bool = False) -> tuple[int, str]:
    if shutil.which("kubectl") is None:
        return 1, "kubectl 不在 PATH（只有集群侧需要 apply）"
    cmd = ["kubectl", "-n", "agent", *args]
    if dry:
        return 0, " ".join(cmd)
    result = subprocess.run(cmd, capture_output=True, text=True)
    return result.returncode, (result.stdout or result.stderr or "").strip()


def apply(data: dict, dry: bool) -> int:
    if check(data, quiet=True) != 0:
        print("先 sync：派生文件与 manifest 不一致")
        return 1
    services = data.get("services") or {}
    for name, tag in services.items():
        code, out = kubectl(["set", "image", f"deploy/{name}", f"{name}=agent/{name}:{tag}"], dry)
        print(("[dry] " if dry else "") + f"{name} -> {tag}: {out.splitlines()[-1] if out else code}")
    external = data.get("external") or {}
    for repo, spec in external.items():
        if not isinstance(spec, dict) or not spec.get("deploy"):
            continue
        ref = f"{repo}:{spec['tag']}"
        code, out = kubectl(
            ["set", "image", f"deploy/{spec['deploy']}",
             f"{spec.get('container') or spec['deploy']}={ref}"], dry
        )
        print(("[dry] " if dry else "") + f"{spec['deploy']} -> {ref}: {out.splitlines()[-1] if out else code}")
    if dry:
        return 0
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    if head:
        data["deployed_commit"] = head
        save_manifest(data)
        print(f"deployed_commit = {head[:10]}")
    for name in list(services) + [s.get("deploy") for s in external.values() if isinstance(s, dict)]:
        if not name:
            continue
        code, out = kubectl(["rollout", "status", f"deploy/{name}", "--timeout=240s"])
        print(f"rollout {name}: {'OK' if code == 0 else out}")
    return 0


def status(data: dict) -> int:
    if shutil.which("kubectl") is None:
        print("kubectl 不在 PATH")
        return 1
    drift = 0
    for name, tag in (data.get("services") or {}).items():
        code, out = kubectl(["get", "deploy", name, "-o", "jsonpath={.spec.template.spec.containers[0].image}"])
        live = out.strip()
        want = f"agent/{name}:{tag}"
        if live != want:
            drift += 1
            print(f"  {name}: 集群 {live or '?'} ≠ manifest {want}")
    for repo, spec in (data.get("external") or {}).items():
        if not isinstance(spec, dict) or not spec.get("deploy"):
            continue
        code, out = kubectl(
            ["get", "deploy", spec["deploy"], "-o",
             f"jsonpath={{.spec.template.spec.containers[?(@.name==\"{spec.get('container') or spec['deploy']}\")].image}}"]
        )
        live = out.strip()
        want = f"{repo}:{spec['tag']}"
        if live != want:
            drift += 1
            print(f"  {spec['deploy']}: 集群 {live or '?'} ≠ manifest {want}")
    print("与集群一致" if drift == 0 else f"{drift} 处漂移（跑 apply 修）")
    return 0 if drift == 0 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="镜像/依赖版本唯一来源工具")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check")
    sub.add_parser("sync")
    p_bump = sub.add_parser("bump")
    p_bump.add_argument("names", nargs="*")
    p_bump.add_argument("--changed", action="store_true")
    p_bump.add_argument("--since", default=None)
    p_bump.add_argument("--no-sync", action="store_true")
    p_apply = sub.add_parser("apply")
    p_apply.add_argument("--dry-run", action="store_true")
    sub.add_parser("status")
    args = parser.parse_args(argv)

    data = load_manifest()
    if args.cmd == "check":
        return check(data)
    if args.cmd == "sync":
        return sync(data)
    if args.cmd == "status":
        return status(data)
    if args.cmd == "apply":
        return apply(data, args.dry_run)
    names = list(args.names)
    if args.changed:
        names.append("--changed")
    code = bump(data, names, args.since)
    if code == 0 and not args.no_sync:
        data = load_manifest()
        sync(data)
    return code


if __name__ == "__main__":
    sys.exit(main())
