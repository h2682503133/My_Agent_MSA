from flask import Flask, request, jsonify
import requests
import json
import os

# 加载配置
with open("config/gateway_setting.json", "r", encoding="utf-8") as f:
    CONFIG = json.load(f)

from core.Task.Task import Task
from core.Task.User import User


# ======================
# 初始化服务
# ======================
app = Flask("main")
app.config['JSON_SORT_KEYS'] = False

MAIN_HOST = CONFIG["main"]["host"]
MAIN_PORT = CONFIG["main"]["port"]
LOG_PORT = CONFIG["log"]["port"]

# ======================
# 统一发送出口（send 函数极简）
# ======================
def send_to_channel(payload, callback_port):
    url = f"http://127.0.0.1:{callback_port}/send"

    try:
        requests.post(url, json=payload, timeout=5)
    except Exception as e:
        print(f"[发送] 失败: {e}")

# ======================
# 接收渠道消息
# ======================
@app.route("/submit_task", methods=["POST"])
def submit_task_api():
    data = request.get_json()

    user_id = data.get("user_id")
    content = data.get("content")
    channel_id = data.get("channel_id")
    callback_port = data.get("callback_port")

    # 远程 Output 代理
    class RemoteOutput:
        def __init__(self, user_id, callback_port):
            self.user_id = user_id
            self.callback_port = callback_port

        def send(self, payload):
            # 直接转发你传的字典
            send_to_channel(payload, self.callback_port)

    output = RemoteOutput(user_id, callback_port)
    user = User(user_id, f"{channel_id}_{user_id}", output)
    try:
        print(Task.task_map)
        task = Task.task_map[user_id]
        task.set_temp_dialog_output(content)
        Task.remove_pending_task(user_id)
        task.status = "waiting"
        
    except KeyError:
        task_name = f"task_{int(os.times()[4] * 1000)}_{user_id}"
        task = Task(task_name, user, content)
    from core.Task.scheduler import submit_task
    submit_task(task)
    return jsonify(ok=1, task_id=task.task_id)
