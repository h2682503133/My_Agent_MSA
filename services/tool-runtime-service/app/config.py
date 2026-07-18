import os


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return int(value)


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value.lower() in {"1", "true", "yes", "on"}


TOOL_RUNTIME_HOST = os.getenv("TOOL_RUNTIME_HOST", "0.0.0.0")
TOOL_RUNTIME_PORT = env_int("TOOL_RUNTIME_PORT", 5303)

WORKSPACE_DIR = os.getenv("WORKSPACE_DIR", "/app/workspace")
MAX_LIST_FILES = env_int("MAX_LIST_FILES", 500)
MAX_READ_BYTES = env_int("MAX_READ_BYTES", 1024 * 1024)
DEFAULT_TIMEOUT_SECONDS = env_int("DEFAULT_TIMEOUT_SECONDS", 30)

SKILL_ROOT_DIR = os.getenv("SKILL_ROOT_DIR", os.path.join(WORKSPACE_DIR, "skill"))

OPENVIKING_SERVER_URL = os.getenv("OPENVIKING_SERVER_URL", "http://openviking.agent.svc.cluster.local:1933")
OPENVIKING_API_KEY = os.getenv("OPENVIKING_API_KEY", "")
OPENVIKING_ACCOUNT = os.getenv("OPENVIKING_ACCOUNT", "my-agent")
# OpenViking root-key access to tenant-scoped APIs requires tenant + user context.
# Keep these defaults aligned with openviking-context-service's root-key ping style.
OPENVIKING_USER = os.getenv("OPENVIKING_USER", "system")
OPENVIKING_AGENT = os.getenv("OPENVIKING_AGENT", "skills")

CLAW_DOWNLOAD_MODE = os.getenv("CLAW_DOWNLOAD_MODE", "external-vm").lower()
CLAW_EXTERNAL_VM_HOST = os.getenv("CLAW_EXTERNAL_VM_HOST", "")
CLAW_EXTERNAL_VM_USER = os.getenv("CLAW_EXTERNAL_VM_USER", "")
CLAW_EXTERNAL_VM_PORT = env_int("CLAW_EXTERNAL_VM_PORT", 22)
CLAW_EXTERNAL_VM_SSH_KEY = os.getenv("CLAW_EXTERNAL_VM_SSH_KEY", "")
CLAW_EXTERNAL_VM_SKILL_ROOT_DIR = os.getenv("CLAW_EXTERNAL_VM_SKILL_ROOT_DIR", "/srv/nfs/my-agent/workspace/skill")
CLAW_EXTERNAL_VM_CLAWHUB_BIN = os.getenv("CLAW_EXTERNAL_VM_CLAWHUB_BIN", "clawhub")
CLAW_EXTERNAL_VM_STRICT_HOST_KEY_CHECKING = env_bool("CLAW_EXTERNAL_VM_STRICT_HOST_KEY_CHECKING", False)

ENABLE_SHELL_TOOLS = env_bool("ENABLE_SHELL_TOOLS", False)
