import time


def _timestamp() -> str:
    return time.strftime("%H:%M:%S")


def log(message: str) -> None:
    print(f"[{_timestamp()}] {message}", flush=True)


def debug_log(message: str) -> None:
    log(f"[debug] {message}")
