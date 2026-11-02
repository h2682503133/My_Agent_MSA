"""配置模板初始化自检（不碰真实数据目录）。"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "deploy"))

from launcher.plan import init_config  # noqa: E402

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
    data = tmp / "data"

    print("首次初始化")
    added, kept = init_config(ROOT, data)
    check("复制了模板文件", added > 0, f"added={added}")
    check("没有沿用项", kept == 0, f"kept={kept}")
    check("orchestrator 配置到位", (data / "config" / "orchestrator" / "config" / "agent_list.json").exists())
    check("提示词到位", (data / "config" / "orchestrator" / "system_prompt" / "TOOL.json").exists())
    check("模型清单到位", (data / "config" / "model-proxy" / "config" / "model_list.json").exists())
    check("不复制 .reply_images", not (data / "config" / "orchestrator" / "config" / ".reply_images").exists())
    check("不复制 __pycache__", not any(p.name == "__pycache__" for p in data.rglob("*")))

    print("重复执行：不覆盖用户改过的文件")
    target = data / "config" / "model-proxy" / "config" / "model_list.json"
    original = target.read_text(encoding="utf-8")
    target.write_text('{"custom": true}', encoding="utf-8")
    added2, kept2 = init_config(ROOT, data)
    check("第二次没有新增", added2 == 0, f"added={added2}")
    check("已有文件被沿用", kept2 > 0, f"kept={kept2}")
    check("用户改动没被覆盖", target.read_text(encoding="utf-8") == '{"custom": true}')

    print("部分缺失时只补缺的")
    (data / "config" / "qq-llbot").mkdir(parents=True, exist_ok=True)
    for item in (data / "config" / "qq-llbot").glob("*"):
        item.unlink()
    added3, kept3 = init_config(ROOT, data)
    check("补回了被删的配置", added3 > 0, f"added={added3}")
    check("其它文件仍是沿用", kept3 > 0, f"kept={kept3}")

    print("模板目录不存在时安全返回")
    added4, kept4 = init_config(tmp / "no-such-repo", tmp / "data2")
    check("返回 (0, 0)", (added4, kept4) == (0, 0), f"{added4},{kept4}")

    print()
    if FAILS:
        print(f"FAILED {len(FAILS)}: {FAILS}")
        return 1
    print(f"ALL OK — {CHECKS} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
