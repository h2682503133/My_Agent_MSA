import json
import os
from datetime import datetime
from pathlib import Path

from app import config
from app.logger import user_log


class UserStore:
    def __init__(self):
        self.data_dir = Path(config.USER_DATA_DIR)
        self.data_dir.mkdir(parents=True, exist_ok=True)

    def _user_path(self, user_id: str) -> Path:
        return self.data_dir / f"{user_id}.json"

    def _load(self, user_id: str) -> dict | None:
        path = self._user_path(user_id)
        if not path.exists():
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def _save(self, user_id: str, data: dict):
        path = self._user_path(user_id)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def get_user(self, user_id: str) -> dict | None:
        data = self._load(user_id)
        if data is None:
            data = {"user_id": user_id, "channels": {}, "created_at": datetime.now().isoformat()}
            self._save(user_id, data)
            user_log(f"自动创建用户: {user_id}")
        return data

    def upsert_user(self, user_id: str, user_json: str) -> str:
        try:
            data = json.loads(user_json) if user_json else {}
        except json.JSONDecodeError:
            return "JSON 格式错误"

        existing = self._load(user_id)
        if existing:
            existing.update(data)
            data = existing
        else:
            data.setdefault("user_id", user_id)
            data.setdefault("channels", {})
            data.setdefault("created_at", datetime.now().isoformat())

        self._save(user_id, data)
        user_log(f"用户已保存: {user_id}")
        return user_id

    def delete_user(self, user_id: str) -> bool:
        path = self._user_path(user_id)
        if not path.exists():
            return False
        os.remove(path)
        user_log(f"用户已删除: {user_id}")
        return True

    def list_users(self) -> list[dict]:
        users = []
        for path in sorted(self.data_dir.glob("*.json")):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    user = json.load(f)
                    users.append({
                        "user_id": user.get("user_id", path.stem),
                        "created_at": user.get("created_at", ""),
                    })
            except Exception:
                pass
        return users

    def bind_channel(self, user_id: str, channel: str, channel_user_id: str, priority: int) -> str:
        data = self._load(user_id)
        if data is None:
            data = {"user_id": user_id, "channels": {}, "created_at": datetime.now().isoformat()}
            user_log(f"自动创建用户: {user_id}")

        channels = data.setdefault("channels", {})
        channels[channel] = {
            "channel": channel,
            "channel_user_id": channel_user_id,
            "priority": priority,
        }
        self._save(user_id, data)
        user_log(f"用户 {user_id} 绑定渠道 {channel}: {channel_user_id} (优先级={priority})")
        return "ok"

    def unbind_channel(self, user_id: str, channel: str) -> str:
        data = self._load(user_id)
        if data is None:
            return "用户不存在"

        channels = data.get("channels", {})
        if channel not in channels:
            return "渠道未绑定"

        del channels[channel]
        self._save(user_id, data)
        user_log(f"用户 {user_id} 解绑渠道 {channel}")
        return "ok"

    def set_openviking_key(self, user_id: str, api_key: str) -> bool:
        """存储用户的 OpenViking per-user API key。"""
        data = self._load(user_id)
        if data is None:
            data = {"user_id": user_id, "channels": {}, "created_at": datetime.now().isoformat()}
        data["openviking_api_key"] = api_key
        self._save(user_id, data)
        user_log(f"用户 {user_id} OpenViking key 已存储")
        return True

    def get_openviking_key(self, user_id: str) -> str | None:
        """获取用户的 OpenViking per-user API key。"""
        data = self._load(user_id)
        if data is None:
            return None
        return data.get("openviking_api_key")



user_store = UserStore()
