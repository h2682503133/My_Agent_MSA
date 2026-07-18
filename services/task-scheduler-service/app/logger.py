# 从原 core/logger.py 拆出的最小日志函数。
# 原文件会启动 socket log_server；微服务化后不建议导入即开端口，所以这里只保留 gateway_log 的行为。
import datetime
import os
import time


def log_to_file(msg: str, log_type: str):
    date_str = datetime.datetime.now().strftime("%Y-%m-%d")
    log_dir = f"logs/{log_type}"
    log_path = f"{log_dir}/{date_str}.log"

    os.makedirs(log_dir, exist_ok=True)

    with open(log_path, "a", encoding="utf-8") as f:
        f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")


def gateway_log(msg):
    log_to_file(msg, "gateway")
    print(msg, flush=True)
