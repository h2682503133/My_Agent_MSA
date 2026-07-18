import time

def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)

def debug_log(msg: str) -> None:
    log(f"[debug] {msg}")
