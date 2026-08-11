import os


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return int(value)


USER_GRPC_HOST = os.getenv("USER_GRPC_HOST", "0.0.0.0")
USER_GRPC_PORT = env_int("USER_GRPC_PORT", 5104)

USER_DATA_DIR = os.getenv("USER_DATA_DIR", "/data/users")
USER_HTTP_PORT = os.getenv("USER_HTTP_PORT", "5204")
