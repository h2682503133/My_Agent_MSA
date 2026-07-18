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


def timer_log(msg):
    log_to_file(msg, "timer")
    print(msg, flush=True)
