"""本机自建 OpenViking 的初始化：建 account + 建服务用户 + 写 api_key。

为什么单独一个 CLI：集群里这步是 shell 脚本 + WSL；单体没有 bash，
面板点「初始化」时直接调这个脚本（幂等，可重复点）。

用法：
    <venv>\Scripts\python.exe -X utf8 deploy/launcher/openviking_init.py --data-dir <数据目录>
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from launcher.stdio import use_utf8  # noqa: E402  （要先插 sys.path）

# 面板/管道调用时 stdout 不是控制台，中文输出会 UnicodeEncodeError，先固定成 UTF-8
use_utf8()

DEFAULT_URL = "http://127.0.0.1:1933"
DEFAULT_ACCOUNT = "my-agent"
DEFAULT_USER = "agent-service"


def _request(url: str, method: str, api_key: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("X-API-Key", api_key)
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        text = exc.read().decode("utf-8", errors="replace") if exc.fp else ""
        try:
            payload = json.loads(text or "{}")
        except Exception:  # noqa: BLE001
            payload = {"raw": text}
        return exc.code, payload


def key_is_valid(base: str, api_key: str) -> bool:
    """用 /health 带 X-API-Key 验证 key 是否还有效。

    为什么需要：库被清空（或换了服务器）后 api_key 文件还在，
    只看"文件存在"就跳过初始化 → 服务端没有这个账号，全部 401/404。
    """
    try:
        code, payload = _request(f"{base}/health", "GET", api_key)
    except Exception:  # noqa: BLE001
        return False
    if code != 200:
        return False
    return bool(payload.get("healthy") or payload.get("status") == "ok")


def list_accounts(base: str, root_key: str) -> set[str]:
    """用 root key 列出已有 account id；失败返回空集（调用方按"不存在"处理）。"""
    try:
        code, payload = _request(f"{base}/api/v1/admin/accounts", "GET", root_key)
    except Exception:  # noqa: BLE001
        return set()
    if code != 200:
        return set()
    result = payload.get("result")
    items = result if isinstance(result, list) else []
    out: set[str] = set()
    for item in items:
        if isinstance(item, dict):
            aid = item.get("account_id") or item.get("account") or item.get("id")
            if aid:
                out.add(str(aid))
    return out


def ensure_account(base: str, root_key: str, account: str, admin_user: str) -> tuple[str, str]:
    """创建 account；服务端要求同时给 admin_user_id（只给 account_id 会 400）。

    返回 (说明, 可能的 user_key)。user_key 不一定随响应回来（看服务端是否暴露），
    所以调用方拿不到 key 时还会走 ensure_user。
    """
    code, payload = _request(f"{base}/api/v1/admin/accounts", "POST", root_key,
                             {"account_id": account, "admin_user_id": admin_user})
    if code in (200, 201):
        result = payload.get("result") if isinstance(payload.get("result"), dict) else payload
        key = (result or {}).get("user_key") or (result or {}).get("api_key") or ""
        return f"account 已创建：{account}（admin_user={admin_user}）", key
    if code in (400, 409):
        return f"account 已存在：{account}", ""
    raise RuntimeError(f"创建 account 失败 HTTP {code}: {payload}")


def ensure_user(base: str, root_key: str, account: str, user: str) -> tuple[str, str]:
    path = f"{base}/api/v1/admin/accounts/{account}/users"
    code, payload = _request(path, "POST", root_key, {"user_id": user, "role": "user"})
    if code in (400, 409):
        # 已存在：删掉重建，确保能拿到新 key（与 viking_store 的行为一致）
        _request(f"{path}/{user}", "DELETE", root_key)
        code, payload = _request(path, "POST", root_key, {"user_id": user, "role": "user"})
    if code not in (200, 201):
        raise RuntimeError(f"创建用户失败 HTTP {code}: {payload}")
    result = payload.get("result") if isinstance(payload.get("result"), dict) else payload
    key = (result or {}).get("user_key") or (result or {}).get("api_key") or ""
    if not key:
        raise RuntimeError(f"创建用户成功但响应里没有 key：{payload}")
    return key, f"用户已就绪：{user}"


def ensure_openviking(data_dir: Path, base: str = DEFAULT_URL,
                      account: str = DEFAULT_ACCOUNT, user: str = DEFAULT_USER,
                      force: bool = False) -> dict:
    conf_dir = Path(data_dir) / "config" / "openviking"
    conf_dir.mkdir(parents=True, exist_ok=True)
    root_file = conf_dir / "root_api_key"
    key_file = conf_dir / "api_key"

    if not root_file.is_file() or not root_file.read_text(encoding="utf-8").strip():
        raise RuntimeError(f"缺少 root_api_key：{root_file}（先启动一次以生成）")
    root_key = root_file.read_text(encoding="utf-8").strip()

    has_key_file = key_file.is_file() and bool(key_file.read_text(encoding="utf-8").strip())
    accounts = list_accounts(base, root_key)
    if has_key_file and not force and account in accounts:
        return {"ok": True, "skipped": True,
                "detail": f"account 与 api_key 都在（{account}），跳过；要重建加 --force"}
    if not accounts:
        prefix = "服务端没有账号（库被清空或新装），初始化；"
    elif account not in accounts:
        prefix = f"服务端缺少 account {account}，初始化；"
    else:
        prefix = "api_key 文件缺失或已失效，重新签发；"
    if account not in accounts:
        accounts_created = True
    else:
        accounts_created = False

    steps: list[str] = []
    if prefix:
        steps.append(prefix.rstrip("；"))
    key = ""
    if accounts_created:
        note, key = ensure_account(base, root_key, account, user)
        steps.append(note)
    if not key:
        # 建用户拿 key（account 新建但没回 key，或 account 已存在但 key 文件缺失/失效）
        key, detail = ensure_user(base, root_key, account, user)
        steps.append(detail)
    else:
        steps.append(f"admin 用户随 account 创建：{user}")
    key_file.write_text(key + "\n", encoding="utf-8")
    steps.append(f"api_key 已写入：{key_file}")
    return {"ok": True, "skipped": False, "detail": "；".join(steps)}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="初始化本机 OpenViking（account/user/api_key）")
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--account", default=DEFAULT_ACCOUNT)
    parser.add_argument("--user", default=DEFAULT_USER)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args(argv[1:])

    try:
        result = ensure_openviking(Path(args.data_dir), args.url.rstrip("/"),
                                   args.account, args.user, args.force)
    except Exception as exc:  # noqa: BLE001
        print(f"初始化失败：{exc}")
        return 1
    print(result["detail"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
