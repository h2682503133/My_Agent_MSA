"""模式校验只做"模式是否定义"，不碰 files 存在性（交给注入层：单一事实源）。

历史问题：模式层用白名单跳过 FUNCTIONAL 文件名（process_* / world_info / tool_*），
但只要新增一个功能性文件就得同步改白名单，漏改就会把合法模式判成"文件缺失"
（真事故：/mode 'Hzh123654789' 报 PROCESS_clock.md 不存在）。

现在约定：
- `mode.validate_mode` 只看 模式名是否定义、定义是否为对象；
- 文件存在性由 `context_builder.build_system_prompt` 判定（大小写不敏感解析 + 缺失记 debug）。
"""
import sys
import tempfile
from pathlib import Path

SERVICE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE))

from app import mode  # noqa: E402

FAILS = []
CHECKS = 0


def check(name, cond, extra=""):
    global CHECKS
    CHECKS += 1
    print(f"  {'ok  ' if cond else 'FAIL'}  {name} {extra if not cond else ''}")
    if not cond:
        FAILS.append(name)


with tempfile.TemporaryDirectory() as tmp:
    d = Path(tmp)
    (d / "SOUL_ECO.md").write_text("x", encoding="utf-8")

    cfg = {
        "modes": {
            "eco": {"files": ["SOUL_ECO.md", "IDENTITY.md"]},
            "with_process": {"files": ["SOUL_ECO.md", "PROCESS_clock.md"]},
            "with_any_new_file": {"files": ["SOUL_ECO.md", "SOME_FUTURE_FEATURE.md"]},
            "no_files": {"model_profile": "x"},
            "broken_shape": "not-a-dict",
        }
    }

    print("定义合法即可通过（不再因 files 缺失/未知功能性文件而失败）")
    check("eco（声明了不存在的 IDENTITY.md）通过", mode.validate_mode(cfg, d, "eco") == "")
    check("声明 PROCESS_clock.md 通过", mode.validate_mode(cfg, d, "with_process") == "")
    check("声明未来才有的功能性文件也通过", mode.validate_mode(cfg, d, "with_any_new_file") == "")
    check("没有 files 也通过", mode.validate_mode(cfg, d, "no_files") == "")

    print("仍然要拦的：未定义 / 形态错误")
    err = mode.validate_mode(cfg, d, "nope")
    check("未知模式 → 报未知且不列可用", "未知模式" in err and "eco" not in err, err)
    err2 = mode.validate_mode(cfg, d, "broken_shape")
    check("定义不是对象 → 报错", "不是对象" in err2, err2)
    check("default 永远合法", mode.validate_mode(cfg, d, "default") == "")

print("单一事实源：mode 模块不再持有功能性文件名白名单")
src = (SERVICE / "app" / "mode.py").read_text(encoding="utf-8")
check("mode.py 不再有 _SPECIAL_FILE_NAMES", "_SPECIAL_FILE_NAMES" not in src)
check("mode.py 不再 import prompt_refs", "prompt_refs" not in src)

print()
if FAILS:
    print("FAILED:", FAILS)
    raise SystemExit(1)
print(f"ALL OK — {CHECKS} checks")
