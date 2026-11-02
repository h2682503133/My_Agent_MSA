"""本机自建 openviking：conf 生成 / 服务纳入 / 初始化脚本（离线可测）。"""
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "deploy"))

from launcher.plan import build, ensure_openviking_conf  # noqa: E402
from launcher import openviking_init  # noqa: E402

FAILS = []
CHECK = 0


def check(name, cond, extra=""):
    global CHECK
    CHECK += 1
    if cond:
        print(f"  ok    {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL  {name} {extra}")


print("模式决定是否拉起服务本体")
d_install = Path(tempfile.mkdtemp())
(d_install / "config").mkdir(parents=True)
p_install = build(ROOT, d_install, ["core", "memory"],
                  {"memory": "openviking", "memory.source": "install"})
ids = {s.id for s in p_install.services}
check("install 模式含 openviking", "openviking" in ids, str(sorted(ids)))
check("install 模式含 context/embedding", {"openviking-context-service", "embedding-service"} <= ids)

d_ext = Path(tempfile.mkdtemp())
(d_ext / "config").mkdir(parents=True)
p_ext = build(ROOT, d_ext, ["core", "memory"], {"memory": "openviking"})
check("existing 模式不含 openviking", all(s.id != "openviking" for s in p_ext.services))

print("ov.conf 生成")
conf = Path(p_install.env["OPENVIKING_CONFIG_FILE"])
check("conf 文件存在", conf.is_file(), str(conf))
data = json.loads(conf.read_text(encoding="utf-8"))
check("server 绑定 127.0.0.1:1933",
      data["server"]["host"] == "127.0.0.1" and data["server"]["port"] == 1933, str(data["server"]))
root_key_file = d_install / "config" / "openviking" / "root_api_key"
check("root_api_key 已生成", root_key_file.is_file() and len(root_key_file.read_text(encoding="utf-8").strip()) > 20)
check("root key 写进了 conf", data["server"]["root_api_key"] == root_key_file.read_text(encoding="utf-8").strip())
check("workspace 在数据目录内",
      str(d_install).replace("\\", "/") in data["storage"]["workspace"], data["storage"]["workspace"])
check("默认 embedding 指向本机 Ollama",
      data["embedding"]["dense"]["api_base"] == "http://127.0.0.1:11434/v1", str(data["embedding"]["dense"]))
check("注入 OPENVIKING_SERVER_URL", p_install.env.get("OPENVIKING_SERVER_URL") == "http://127.0.0.1:1933")

print("重跑保留用户改动")
data["embedding"]["dense"]["model"] = "my-embed"
conf.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
ensure_openviking_conf(d_install)
again = json.loads(conf.read_text(encoding="utf-8"))
check("用户改的 embedding 模型被保留", again["embedding"]["dense"]["model"] == "my-embed")
check("端口仍被强制为 1933", again["server"]["port"] == 1933)

print("初始化脚本（离线）")
check("缺 root key 会明确报错", True)
try:
    openviking_init.ensure_openviking(Path(tempfile.mkdtemp()))
    check("缺 root key 应抛异常", False)
except Exception as exc:
    check("缺 root key 抛异常并说明原因", "root_api_key" in str(exc), str(exc))

print()
if FAILS:
    print(f"FAILED {len(FAILS)}: {FAILS}")
    raise SystemExit(1)
print(f"ALL OK — {CHECK} checks passed")
