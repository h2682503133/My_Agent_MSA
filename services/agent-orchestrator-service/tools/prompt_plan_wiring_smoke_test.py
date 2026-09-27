"""结构化调用的接线（first_call 解析 + 三副本摘除 + 显式失败）：纯本地冒烟测试。

分两部分：
  1. 行为：`TaskRuntime.replace_input_content` 真的把**三份副本**都换掉，
     以及普通聊天（没解析出计划）时三份都不动
  2. 结构：`first_call` 确实调用解析/替换、`process_task` 里 `PromptPlanError`
     分支排在通用 `except` **之前**（否则会被 raw fallback 吞掉）

用法：
    python tools/prompt_plan_wiring_smoke_test.py
"""

import json
import os
import sys
import tempfile
from pathlib import Path

SERVICE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SERVICE_DIR))

TMP = Path(tempfile.mkdtemp(prefix="prompt_plan_wiring_"))
os.environ["SYSTEM_PROMPT_DIR"] = str(TMP / "system_prompt")
os.environ["AGENT_CONFIG_PATH"] = str(TMP / "agent_list.json")
os.environ["PROCESS_DIR"] = str(TMP / "process")

from app.prompt_plan import parse_prompt_plan  # noqa: E402
from app.task_runtime import RuntimeUser, TaskRuntime  # noqa: E402

FAILURES: list[str] = []
OK = 0


def ok(label: str) -> None:
    global OK
    OK += 1
    print(f"  OK   {label}")


def fail(label: str, detail: str = "") -> None:
    FAILURES.append(label)
    print(f"  FAIL {label}" + (f"\n       {detail}" if detail else ""))


def check(label: str, cond: bool, detail: str = "") -> None:
    ok(label) if cond else fail(label, detail)


def make_task(content: str) -> TaskRuntime:
    return TaskRuntime(
        task_id="task-1",
        user=RuntimeUser(id="player", session_id="rpg_world1"),
        content=content,
        channel="web",
    )


RAW = json.dumps({
    "exclude_block": ["tool"],
    "insert": [{"at": "before_input", "content": ["设定X"]}],
    "present": ["alice", "bob"],
    "text": "玩家推开了门",
}, ensure_ascii=False)


print("── 行为：命中计划后三份副本一起换 ──")
task = make_task(RAW)
plan = parse_prompt_plan(task.content)
check("先确认这段确实是结构化调用", plan is not None)
task.prompt_plan = plan
task.replace_input_content(plan.text)

check("① task.content 变成 text", task.content == "玩家推开了门", repr(task.content))
check("① task.content 里没有指令区残留", "exclude_block" not in task.content, repr(task.content))
check("② temp_dialog_input 只剩 text（且带请求时间前缀）",
      str(task.temp_dialog_input).endswith(",请求内容:玩家推开了门"), repr(task.temp_dialog_input))
check("② temp_dialog_input 里没有指令区残留",
      "exclude_block" not in str(task.temp_dialog_input), repr(task.temp_dialog_input))
check("③ 栈底用户帧的 input 也换掉",
      task.agent_context[0]["input"] == "玩家推开了门", repr(task.agent_context[0]))
check("③ 栈仍只有一帧", len(task.agent_context) == 1, repr(task.agent_context))
check("计划挂上了 task", task.prompt_plan is plan)
check("present 原样保留（扇出时再规范化）", task.prompt_plan.present == ("alice", "bob"))

print("── 行为：普通聊天（解析不出）三份副本都不动 ──")
plain = make_task("我们往北走吧")
no_plan = parse_prompt_plan(plain.content)
check("普通聊天解析结果为 None", no_plan is None)
check("① content 未动", plain.content == "我们往北走吧")
check("② temp_dialog_input 未动",
      str(plain.temp_dialog_input).endswith(",请求内容:我们往北走吧"), repr(plain.temp_dialog_input))
check("③ 栈底帧未动", plain.agent_context[0]["input"] == "我们往北走吧")

print("── 行为：text 为空字符串也能替换干净 ──")
empty = make_task(json.dumps({"text": ""}, ensure_ascii=False))
empty_plan = parse_prompt_plan(empty.content)
check("空 text 也是合法结构化调用", empty_plan is not None)
empty.replace_input_content(empty_plan.text)
check("① content 变成空串", empty.content == "")
check("② 前缀保留、内容为空", str(empty.temp_dialog_input).endswith(",请求内容:"), repr(empty.temp_dialog_input))
check("③ 栈底帧为空串", empty.agent_context[0]["input"] == "")

print("── 结构不变量：first_call 接线 ──")
src = (SERVICE_DIR / "app" / "agent_runtime.py").read_text(encoding="utf-8")
check("first_call 里解析用户输入", "parse_prompt_plan(task.content)" in src)
check("命中后把计划挂到 task", "task.prompt_plan = plan" in src)
check("命中后一次性替换三副本", "task.replace_input_content(plan.text)" in src)

_entry = src.find("cls.first_call(task)")
_plan_exc = src.find("except PromptPlanError as exc:", _entry)
_generic_exc = src.find("except Exception as exc:", _entry)
check("在 first_call 调用点之后找到了 PromptPlanError 分支", _plan_exc > _entry, f"{_plan_exc}")
check("PromptPlanError 分支排在通用 except 之前（不被 raw fallback 吞掉）",
      0 <= _plan_exc < _generic_exc, f"plan={_plan_exc} generic={_generic_exc}")
if _plan_exc > 0 and _generic_exc > _plan_exc:
    check("失败分支内部不调用 raw fallback",
          "_emit_raw_model_fallback" not in src[_plan_exc:_generic_exc])
check("失败时置 task.status=failed", 'task.status = "failed"' in src[_plan_exc:_generic_exc])
check("失败时发 task_failed 事件", '"task_failed"' in src[_plan_exc:_generic_exc])
check("失败时把原始 JSON 一并回给调用方（便于定位）", "原文（前 300 字）" in src[_plan_exc:_generic_exc])

print("── 结构不变量：replace_input_content 三处都换 ──")
tr = (SERVICE_DIR / "app" / "task_runtime.py").read_text(encoding="utf-8")
check("① self.content = new_content", "self.content = new_content" in tr)
check("② set_temp_dialog_input(...)",
      "self.set_temp_dialog_input(self._dialog_input_text(new_content))" in tr)
check("③ agent_context[0][\"input\"]",
      'self.agent_context[0]["input"] = new_content' in tr)
check("__post_init__ 复用同一个格式函数（不会两处漂移）",
      "self.set_temp_dialog_input(self._dialog_input_text(self.content))" in tr)

print()
if FAILURES:
    print(f"FAILED {len(FAILURES)} / {OK + len(FAILURES)}")
    for name in FAILURES:
        print(f"  - {name}")
    sys.exit(1)
print(f"ALL OK — {OK} checks")
