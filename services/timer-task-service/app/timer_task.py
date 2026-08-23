"""
独立定时任务服务核心逻辑。

从 task-scheduler-service 分离出来，通过 gRPC 回调 scheduler 触发任务执行。
"""
import json
import os
import time
import threading
from datetime import datetime
from pathlib import Path

from app import config
from app.logger import timer_log
from app.scheduler_client import SchedulerClient
from app.time_parser import next_trigger, parse_repeat_spec, parse_time_spec, schedule_to_str

# ======================
# 定时任务配置
# ======================
TASK_DIR = Path(config.TIMER_TASK_DIR)
TASK_DIR.mkdir(parents=True, exist_ok=True)

scan_interval = float(config.TIMER_SCAN_INTERVAL_FAST)
NEED_FAST_SCAN = False
LAST_TASK_ADD_TIME = 0

# 新任务添加时立即唤醒扫描循环，避免慢扫描 sleep(60s) 期间新任务最长等 60 秒
_wake_event = threading.Event()

_scheduler_client: SchedulerClient | None = None


def _get_scheduler_client() -> SchedulerClient:
    global _scheduler_client
    if _scheduler_client is None:
        _scheduler_client = SchedulerClient()
    return _scheduler_client


# ======================
# 1. 添加定时任务
# ======================
def add_timer_task(
    user_id: str,
    channel_id: str,
    trigger_timestamp: float,
    content: str = "system:auto_commit",
    task_type: str = "submit_task",
    session_id: str | None = None,
    client_message_id: str = "",
    agent_id: str = "",
    time_str: str = "",
    repeat_str: str = "",
) -> str:
    """创建定时任务。

    协议格式：定时任务:任务类别|智能体id|任务内容|开始时间|重复计划(可选,0=不重复)
    - time_str   ：第 4 参「开始时间」，空=立即；「现在/立即/马上」=立即
    - repeat_str ：第 5 参「重复计划」，空/0/无/不重复=一次性；每30分钟/每5-10分钟=间隔重复
    - 兼容旧格式：第 4 参直接写重复计划（如 每30分钟）且第 5 参为空时，
      视为 立即开始 + 该重复计划。
    """
    global NEED_FAST_SCAN, LAST_TASK_ADD_TIME

    try:
        task_id = f"task_{int(time.time() * 1000)}_{user_id}"
        session_id = session_id or f"{channel_id}_{user_id}"

        # ---- 第 4 参：开始时间 ----
        schedule = None
        if time_str:
            try:
                trigger_timestamp, sched = parse_time_spec(time_str)
            except ValueError as e:
                timer_log(f"定时任务开始时间解析失败：{user_id} {task_type} {time_str} -> {e}")
                return f"定时任务创建失败：{e}"
            if sched:
                # 开始时间解析出 interval：说明第 4 参直接写了重复计划（旧格式兼容）
                if repeat_str:
                    return ("定时任务创建失败：开始时间不能是重复计划（每...），"
                            "请把重复计划填到第 5 参（开始时间|重复计划）")
                schedule = sched
        elif not trigger_timestamp or trigger_timestamp <= 0:
            # 无开始时间：仅当给了重复计划时允许「立即开始」，否则报错
            if not repeat_str:
                return ("定时任务创建失败：缺少开始时间"
                        "（可用如 10:00 / 2026-01-31 10:00 / 5分钟后 / 10:00-11:00，"
                        "或填 现在）")
            trigger_timestamp = time.time()  # 立即开始 + 重复计划

        # ---- 第 5 参：重复计划（可选，0=不重复） ----
        if repeat_str:
            r = repeat_str.strip().lower()
            if r in ("0", "无", "不重复", "none", "false", "-", "off", "no"):
                pass  # 显式不重复
            else:
                rpt = parse_repeat_spec(repeat_str)
                if not rpt:
                    return f"定时任务创建失败：无法识别的重复计划：{repeat_str}"
                if schedule:
                    return ("定时任务创建失败：开始时间和重复计划不能同时是间隔写法，"
                            "请把重复计划填到第 5 参（开始时间|重复计划）")
                schedule = rpt

        task_data = {
            "task_id": task_id,
            "user_id": user_id,
            "channel_id": channel_id,
            "session_id": session_id,
            "client_message_id": client_message_id,
            "agent_id": agent_id,
            "trigger_time": trigger_timestamp,
            "content": content,
            "task_type": task_type,
            "created_at": datetime.now().isoformat(),
        }
        if schedule:
            task_data["schedule"] = schedule

        path = TASK_DIR / f"{task_id}.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(task_data, f, ensure_ascii=False, indent=2)

        NEED_FAST_SCAN = True
        LAST_TASK_ADD_TIME = time.time()
        _wake_event.set()  # 立即唤醒扫描循环处理新任务

        next_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(trigger_timestamp))
        if schedule:
            desc = schedule_to_str(schedule)
            timer_log(f"创建定时任务：{user_id} {task_type} {trigger_timestamp} {content} [{desc}]")
            return f"定时任务{task_type}:{content}创建成功，首次将于 {next_str} 执行，此后{desc}重复"
        timer_log(f"创建定时任务：{user_id} {task_type} {trigger_timestamp} {content}")
        return f"定时任务{task_type}:{content}创建成功，将于 {next_str} 执行"

    except Exception as e:
        timer_log(f"定时任务创建失败：{user_id} {task_type} {trigger_timestamp} {content}")
        return f"定时任务创建失败：{str(e)}"


# ======================
# 2. 查询当前用户所有定时任务
# ======================
def list_user_tasks(user_id: str) -> list[dict]:
    tasks = []
    try:
        for filename in os.listdir(TASK_DIR):
            if not filename.endswith(".json"):
                continue

            path = TASK_DIR / filename
            with open(path, "r", encoding="utf-8") as f:
                task = json.load(f)

            if task.get("user_id") == user_id:
                trigger_time = task.get("trigger_time", 0)
                task["trigger_time_str"] = time.strftime(
                    "%Y-%m-%d %H:%M:%S", time.localtime(trigger_time)
                )
                task["schedule_str"] = schedule_to_str(task.get("schedule") or {})
                tasks.append(task)

        tasks.sort(key=lambda x: x["trigger_time"])
        timer_log(f"用户 {user_id} 查询定时任务，共 {len(tasks)} 条")
        return tasks

    except Exception:
        return []


# ======================
# 3. 删除指定任务
# ======================
def delete_user_task(user_id: str, task_id: str) -> str:
    try:
        target_file = None
        for filename in os.listdir(TASK_DIR):
            if not filename.endswith(".json"):
                continue
            path = TASK_DIR / filename
            with open(path, "r", encoding="utf-8") as f:
                task = json.load(f)

            if task.get("user_id") == user_id and task.get("task_id") == task_id:
                target_file = path
                break

        if not target_file or not os.path.exists(target_file):
            return "未找到该定时任务"

        os.remove(target_file)
        timer_log(f"用户 {user_id} 删除定时任务 {task_id} 成功")
        return "定时任务已删除"

    except Exception as e:
        timer_log(f"删除定时任务失败：{user_id} {task_id} {str(e)}")
        return "删除定时任务失败"


# ======================
# 智能扫描：检查是否有临近任务
# ======================
def has_nearby_task(seconds=180):
    now = time.time()
    try:
        for filename in os.listdir(TASK_DIR):
            if not filename.endswith(".json"):
                continue
            path = TASK_DIR / filename
            with open(path, "r", encoding="utf-8") as f:
                task = json.load(f)
            t = task.get("trigger_time", 0)
            if 0 < t <= now + seconds:
                return True
    except Exception:
        pass
    return False


# ======================
# 后台扫描线程（智能调度）
# ======================
def timer_scan_loop():
    global scan_interval, NEED_FAST_SCAN
    while True:
        try:
            now = time.time()

            if NEED_FAST_SCAN:
                scan_interval = float(config.TIMER_SCAN_INTERVAL_FAST)
                if now - LAST_TASK_ADD_TIME > 30:
                    NEED_FAST_SCAN = False
            else:
                if has_nearby_task(180):
                    scan_interval = float(config.TIMER_SCAN_INTERVAL_FAST)
                else:
                    scan_interval = float(config.TIMER_SCAN_INTERVAL_SLOW)

            for filename in os.listdir(TASK_DIR):
                if not filename.endswith(".json"):
                    continue

                path = TASK_DIR / filename
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        task = json.load(f)

                    trigger_time = task.get("trigger_time", 0)
                    if now >= trigger_time:
                        execute_timer_task(task)
                        schedule = task.get("schedule")
                        if schedule:
                            nxt = next_trigger(schedule, now)
                            if nxt and nxt > now:
                                # 重复任务：重排下一次触发时间，继续保留
                                task["trigger_time"] = nxt
                                with open(path, "w", encoding="utf-8") as f:
                                    json.dump(task, f, ensure_ascii=False, indent=2)
                                timer_log(
                                    f"定时任务已重排：{task.get('task_id')} "
                                    f"下次 {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(nxt))}"
                                )
                                continue
                        os.remove(path)
                except Exception as e:
                    timer_log(f"定时任务扫描失败：{path} {e}")
                    continue
        except Exception as e:
            # 单次扫描整体异常（如 NFS 抖动导致 os.listdir 失败）不能杀死扫描线程
            timer_log(f"定时任务扫描循环异常：{e}")

        # 等待下一轮：新任务添加会立即唤醒（否则按当前 scan_interval 睡眠）
        _wake_event.wait(timeout=scan_interval)
        _wake_event.clear()


# ======================
# 执行任务：通过 gRPC 回调 scheduler
# ======================
def execute_timer_task(task_data: dict):
    task_type = task_data.get("task_type", "submit_task")
    user_id = task_data["user_id"]
    channel_id = task_data.get("channel_id", "web")
    session_id = task_data.get("session_id") or f"{channel_id}_{user_id}"
    content = task_data["content"]
    client_message_id = task_data.get("client_message_id", "")

    timer_log(
        f"{user_id}于{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime())}"
        f"的定时任务[{content}]开始执行"
    )

    if task_type == "submit_task":
        client = _get_scheduler_client()
        result = client.create_task(
            user_id=user_id,
            session_id=session_id,
            channel=channel_id,
            content=content,
            client_message_id=client_message_id,
            agent_id=task_data.get("agent_id", ""),
        )
        if result["ok"]:
            timer_log(f"定时任务提交成功：{user_id} {content} -> task_id={result['task_id']}")
        else:
            timer_log(f"定时任务提交失败：{user_id} {content} -> {result['error']}")
    elif task_type == "send_message":
        client = _get_scheduler_client()
        result = client.create_task(
            user_id=user_id,
            session_id=session_id,
            channel=channel_id,
            content=content,
            client_message_id=client_message_id,
            agent_id=task_data.get("agent_id", ""),
            metadata={"source": "timer_task", "timer_type": "send_message"},
        )
        if result["ok"]:
            timer_log(f"定时消息发送成功：{user_id} {content} -> task_id={result['task_id']}")
        else:
            timer_log(f"定时消息发送失败：{user_id} {content} -> {result['error']}")
    else:
        timer_log(f"未知定时任务类型：{task_type}，跳过执行")


# ======================
# 启动服务
# ======================
def start_timer_service():
    t = threading.Thread(target=timer_scan_loop, daemon=True)
    t.start()
    print(f"timer-task-service 扫描已启动，task_dir={TASK_DIR}", flush=True)
