import os
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
BASE_DIR = APP_DIR.parent

MODEL_PROXY_GRPC_PORT = int(os.getenv("MODEL_PROXY_GRPC_PORT", "5302"))

# 新配置名：更贴近原 orchestrator / agent_list 的模型列表语义。
MODEL_LIST_PATH = Path(
    os.getenv("MODEL_LIST_PATH", str(BASE_DIR / "config" / "model_list.json"))
)

# 兼容旧名 model_profiles.json。
MODEL_PROFILES_PATH = Path(
    os.getenv("MODEL_PROFILES_PATH", str(BASE_DIR / "config" / "model_profiles.json"))
)

REQUEST_TIMEOUT_SECONDS = int(os.getenv("REQUEST_TIMEOUT_SECONDS", "300"))

# no-mock build: model-proxy always calls the configured provider.
MOCK_MODEL = False
