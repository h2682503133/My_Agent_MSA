# scheduler.py 完整原版功能 + 只接收 task + 无重试（只执行一次）
import json
import queue
import time
import threading
from collections import OrderedDict
import socket
#from core.logger import gateway_log
from core.Task.agent_entry import process_user_task
from core.Task.Task import Task
# 【固定模板路径，彻底解决 login.html 找不到】

from core.logger import gateway_log

# ======================
# 全局配置
# ======================
queue_clients = set()
processed = 0
MESSAGE_QUEUE = queue.Queue(maxsize=50)
MAX_RETRY = 0  # 🔥 改成 0 = 不重试
MAX_TASK_TIME = 300

USER_QUEUES: OrderedDict[str, queue.Queue] = OrderedDict()
BATCH_SIZE = 2
BATCH_SLOTS = [None] * BATCH_SIZE
BUSY_USERS: set[str] = set()

USER_LOCK = threading.Lock()
BUSY_LOCK = threading.Lock()
SLOTS_LOCK = threading.Lock()

# ======================
# SSE 队列刷新
# ======================
def notify_queue_update():
    for client in list(queue_clients):
        try:
            client.put(True)
        except:
            queue_clients.remove(client)

# ======================
# 槽位调度器（核心逻辑 · 完全按你的规则）
# ======================
def slot_scheduler():
    global processed
    while True:
        if not MESSAGE_QUEUE.qsize():
            time.sleep(0.1)
            continue

        user_list = list(USER_QUEUES.keys())
        for user_id in user_list:
            if processed >= BATCH_SIZE:
                break

            try:
                user_q = USER_QUEUES[user_id]
                if user_q.empty():
                    continue

                with BUSY_LOCK:
                    if user_id in BUSY_USERS:
                        continue

                with SLOTS_LOCK:
                    if None not in BATCH_SLOTS:
                        break
                    slot_idx = BATCH_SLOTS.index(None)

                # ------------------------------
                # 取出队首任务（不弹出）
                # ------------------------------
                task, callback = user_q.queue[0]

                # ------------------------------
                # 🔥 禁用重试：永远只执行一次
                # ------------------------------
                task.retry_count = 0  # 固定为0，不累计
                task.status = "running"
                task.slot_index = slot_idx

                # ------------------------------
                # 占用槽位
                # ------------------------------
                with SLOTS_LOCK:
                    BATCH_SLOTS[slot_idx] = user_id
                with BUSY_LOCK:
                    BUSY_USERS.add(user_id)

                processed += 1

                threading.Thread(
                    target=run_task,
                    args=(task, callback),
                    daemon=True
                ).start()

            except Exception:
                continue

        if processed == 0:
            time.sleep(0.1)

# ======================
# 槽执行器 · 只执行一次（无重试）
# ======================
def run_task(task: Task, callback):
    user_id = task.user.id
    success = False

    gateway_log(f"{task.slot_index}号槽正处理{user_id}的请求，仅执行一次")
    try:
        def task_func():
            task.status = "running"
            process_user_task(task)

        t = threading.Thread(target=task_func, daemon=True)
        t.start()
        t.join(timeout=MAX_TASK_TIME)

        success = not t.is_alive()

    except Exception:
        success = False

    finally:
        with SLOTS_LOCK:
            BATCH_SLOTS[task.slot_index] = None
        with BUSY_LOCK:
            BUSY_USERS.discard(user_id)
        global processed
        processed -= 1

    # 🔥 无论成功失败，都直接移除任务，绝不重试
    if user_id in USER_QUEUES and not USER_QUEUES[user_id].empty():
        USER_QUEUES[user_id].get()

# ======================
# 通用提交接口（QQ/外部调用）
# ======================
def submit_task(task: Task):
    user_id = task.user.id

    with USER_LOCK:
        if user_id not in USER_QUEUES:
            USER_QUEUES[user_id] = queue.Queue(maxsize=10)
        q = USER_QUEUES[user_id]

    MESSAGE_QUEUE.put(time.time())
    notify_queue_update()
    q.put((task, lambda s, d: None))

def start_scheduler():
    threading.Thread(target=slot_scheduler, daemon=True).start()

def get_local_ip():
    with open("config/gateway_setting.json", "r", encoding="utf-8") as f:
        CONFIG = json.load(f)
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        test_ip = CONFIG["local_ip"]["test_ip"]
        test_port = CONFIG["local_ip"]["test_port"]
        s.connect((test_ip, test_port))
        ip = s.getsockname()[0]
    except:
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip
