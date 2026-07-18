from datetime import datetime


def log(message: str) -> None:
    now = datetime.now().strftime("%H:%M:%S")
    print(f"[{now}] {message}", flush=True)


def debug(message: str) -> None:
    log(f"[debug] {message}")
