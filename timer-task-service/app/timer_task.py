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

# ======================
# 定时任务配置
# ======================
TASK_DIR = Path(config.TIMER_TASK_DIR)
TASK_DIR.mkdir(parents=True, exist_ok=True)

scan_interval = float(config.TIMER_SCAN_INTERVAL_FAST)
NEED_FAST_SCAN = False
LAST_TASK_ADD_TIME = 0

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
) -> str:
    global NEED_FAST_SCAN, LAST_TASK_ADD_TIME

    try:
        task_id = f"task_{int(time.time() * 1000)}_{user_id}"
        session_id = session_id or f"{channel_id}_{user_id}"

        task_data = {
            "task_id": task_id,
            "user_id": user_id,
            "channel_id": channel_id,
            "session_id": session_id,
            "client_message_id": client_message_id,
            "trigger_time": trigger_timestamp,
            "content": content,
            "task_type": task_type,
            "created_at": datetime.now().isoformat(),
        }

        path = TASK_DIR / f"{task_id}.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(task_data, f, ensure_ascii=False, indent=2)

        NEED_FAST_SCAN = True
        LAST_TASK_ADD_TIME = time.time()

        timer_log(f"创建定时任务：{user_id} {task_type} {trigger_timestamp} {content}")
        return f"定时任务{task_type}:{content}创建成功，将在指定时间执行"

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
                tasks.append(task)

        tasks.sort(key=lambda x: x["trigger_time"])
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
                    os.remove(path)
            except Exception as e:
                timer_log(f"定时任务扫描失败：{path} {e}")
                continue

        time.sleep(scan_interval)


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
        )
        if result["ok"]:
            timer_log(f"定时任务提交成功：{user_id} {content} -> task_id={result['task_id']}")
        else:
            timer_log(f"定时任务提交失败：{user_id} {content} -> {result['error']}")
    else:
        timer_log(f"未知定时任务类型：{task_type}，跳过执行")


# ======================
# 启动服务
# ======================
def start_timer_service():
    t = threading.Thread(target=timer_scan_loop, daemon=True)
    t.start()
    print(f"timer-task-service 扫描已启动，task_dir={TASK_DIR}", flush=True)
