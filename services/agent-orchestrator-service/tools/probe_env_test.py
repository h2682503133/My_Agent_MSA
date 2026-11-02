"""探测脚本自检（离线：替换掉外部命令执行，完全不接触本机部署）。"""

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import tools.probe_env as probe_env  # noqa: E402

FAILS = []
CHECKS = 0


def check(name, cond, extra=""):
    global CHECKS
    CHECKS += 1
    if cond:
        print(f"  ok    {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL  {name} {extra}")


def fake_runner(mapping):
    """mapping: {"docker": (ok, out), ...} → 替换 _run。"""

    def _run(cmd, timeout=4.0):
        return mapping.get(cmd[0], (False, f"未桩定 {cmd[0]}"))

    return _run


def main():
    real_run = probe_env._run
    real_which = probe_env.shutil.which
    try:
        print("裸 Windows：只有 Python")
        probe_env._run = fake_runner({})
        probe_env.shutil.which = lambda name: ""  # 全部命令都"找不到"
        result = probe_env.probe(skip_commands=False)
        check("档位 T0", result["runtime_tier"] == "T0", result["runtime_tier"])
        check("docker 不可用", result["probes"]["docker"] is False)
        check("search 回落 builtin", result["features"]["search"] == "builtin", str(result["features"]))
        check("memory 回落 local", result["features"]["memory_mode"] == "local")
        check("降级项包含 search", any(d["feature"] == "search" for d in result["degraded"]), str(result["degraded"]))
        check("降级项包含 container", any(d["feature"] == "container" for d in result["degraded"]))
        check("写出来的 JSON 可解析", json.loads(json.dumps(result, ensure_ascii=False))["schema"] == 1)

        print("有 Docker + compose（无 k8s）")
        probe_env.shutil.which = lambda name: f"/usr/bin/{name}"
        probe_env._run = fake_runner({
            "docker": (True, "27.0.0"),
            "kubectl": (False, "找不到集群"),
        })
        windows = probe_env.probe()["runtime_tier"]
        check("windows → T1", windows == "T1", windows)

        print("k8s 可用")
        os.environ["_PROBE_OS_FORCE"] = "linux"
        probe_env._run = fake_runner({
            "docker": (True, "27.0.0"),
            "kubectl": (True, "namespace/default"),
        })
        original_system = probe_env.platform.system
        probe_env.platform.system = lambda: "Linux"
        try:
            result = probe_env.probe()
            check("linux + k8s → T5", result["runtime_tier"] == "T5", result["runtime_tier"])
            check("storage 建议 nfs", result["features"]["storage"] == "nfs", str(result["features"]))
        finally:
            probe_env.platform.system = original_system

        print("端点探测走 env（不真连）")
        probe_env._run = fake_runner({})
        probe_env._tcp_probe = lambda url, timeout=1.5: True
        os.environ["BROWSER_MCP_URL"] = "http://127.0.0.1:9335/mcp"
        os.environ["OPENVIKING_SERVER_URL"] = "http://127.0.0.1:1933"
        try:
            result = probe_env.probe()
            check("browser 端点 up → 能力开", result["features"]["browser"] is True)
            check("openviking up → 走长期记忆", result["features"]["memory_mode"] == "openviking", str(result["features"]))
        finally:
            os.environ.pop("BROWSER_MCP_URL", None)
            os.environ.pop("OPENVIKING_SERVER_URL", None)

        print("--skip-commands 不调用任何外部命令")
        called = []

        def _spy(cmd, timeout=4.0):
            called.append(cmd[0])
            return False, "spy"

        probe_env._run = _spy
        probe_env.shutil.which = real_which
        probe_env.probe(skip_commands=True)
        check("没有调用外部命令", called == [], str(called))

        print("写文件")
        out = Path(tempfile.mkdtemp()) / "sub" / "capability.json"
        code = probe_env.main(["probe", "--skip-commands", "--out", str(out)])
        check("退出码 0", code == 0)
        check("文件已生成", out.exists())
        data = json.loads(out.read_text(encoding="utf-8"))
        check("内容含 features", isinstance(data.get("features"), dict), str(data)[:80])
    finally:
        probe_env._run = real_run
        probe_env.shutil.which = real_which
        os.environ.pop("_PROBE_OS_FORCE", None)

    print()
    if FAILS:
        print(f"FAILED {len(FAILS)}: {FAILS}")
        return 1
    print(f"ALL OK — {CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
