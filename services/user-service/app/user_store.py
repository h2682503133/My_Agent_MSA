"""用户存储。

身份模型
--------
- **规范记录**（canonical）：文件名即 store 自己的 `user_id`，`channels` 里持有该用户的
  全部渠道绑定。渠道绑定是**唯一真相**。
- **渠道身份（别名指针）**：某些渠道用**异构 id** 寻址人 —— 例如 QQ 用 QQ 号，
  而 qq-llbot 把 QQ 号当 `user_id` 传（`qq_bridge.py` 的 `user_id = event.user.id`）。
  于是同一人会有两个 id。指针记录形如::

      // 2195239690.json   —— 不含实际内容，只是跳转
      {"user_id": "2195239690", "alias_of": "h268", "channels": {}}

  指针是**派生状态**，真相永远在规范记录的 `channels` 里。

为什么用指针而不是索引表
------------------------
索引表（`_index/channel_qq_2195239690.json`）要双写、会漂移，还得一次性回填。
指针落在"查不到时本来就会去读的那个文件"里：一跳解析、O(1)、不新增目录、
不产生第二份真相。

为什么在存储层对账而不是在各 RPC 里增删
----------------------------------------
`channels` 有多个写入口（登录自动绑 web / 绑定接口 / 解绑接口 / `upsert_user`
整体替换），逐个维护指针每多一个入口就多一处会漂移的代码 —— 而 `upsert_user`
的 `existing.update(data)` 恰恰会**整体替换** `channels`，绕过 `bind_channel`。
所以对账做的是 `desired - current` **差集**，收敛到派生状态，与入口无关。
"""

import json
import os
from datetime import datetime
from pathlib import Path

from app import config
from app.logger import user_log

# 指针字段：存在即表示"本文件是别名，内容要去 alias_of 指向的规范记录读"
ALIAS_FIELD = "alias_of"


def _alias_target(data: dict | None) -> str:
    """该记录指向的规范 user_id；不是指针则返回 ""。"""
    if not isinstance(data, dict):
        return ""
    return str(data.get(ALIAS_FIELD) or "").strip()


def _desired_aliases(user_id: str, channels: dict) -> set[str]:
    """规范记录希望存在的别名集合。

    自映射（`channel_user_id == user_id`，如 web 渠道）**不需要指针** —— 查自己就是查它。
    """
    if not isinstance(channels, dict):
        return set()
    out = set()
    for data in channels.values():
        if not isinstance(data, dict):
            continue
        chan_uid = str(data.get("channel_user_id") or "").strip()
        if chan_uid and chan_uid != user_id:
            out.add(chan_uid)
    return out


def _channels_of(data: dict | None) -> dict:
    if not isinstance(data, dict):
        return {}
    channels = data.get("channels")
    return channels if isinstance(channels, dict) else {}


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
        """裸写：**不**触发对账。对账由各变更方法显式调用，避免递归。"""
        path = self._user_path(user_id)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    # ─── 别名解析 ────────────────────────────────────────────

    def resolve_alias(self, user_id: str) -> str:
        """把渠道身份解析成规范 user_id（**只一跳**，不追链）。

        目标缺失 / 目标自身也是指针 → 返回原 id（不追链，避免环与误配）。
        """
        data = self._load(user_id)
        target = _alias_target(data)
        if not target or target == user_id:
            return user_id
        canonical = self._load(target)
        if canonical is None or _alias_target(canonical):
            user_log(f"别名目标无效 {user_id} -> {target}（缺失或链式），按自身处理")
            return user_id
        return target

    def _alias_holders(self, user_id: str) -> set[str]:
        """当前哪些文件的 alias_of 指向该用户（扫描，故能自愈历史漂移）。"""
        holders = set()
        for path in self.data_dir.glob("*.json"):
            if _alias_target(self._load(path.stem)) == user_id and path.stem != user_id:
                holders.add(path.stem)
        return holders

    def _channel_holder(self, channel: str, channel_user_id: str) -> str:
        """该渠道身份已被哪个规范用户绑定；无则返回 ""。指针文件不算持有者。"""
        for path in self.data_dir.glob("*.json"):
            data = self._load(path.stem)
            if data is None or _alias_target(data):
                continue
            entry = _channels_of(data).get(channel)
            if isinstance(entry, dict) and str(entry.get("channel_user_id") or "").strip() == channel_user_id:
                return path.stem
        return ""

    # ─── 指针写入 / 移除 ─────────────────────────────────────

    def _write_alias(self, alias: str, owner: str) -> str:
        """写指针。返回 "written" / "skipped" / "conflict"。"""
        if not alias or alias == owner:
            return "skipped"
        existing = self._load(alias)
        if existing is not None and not _alias_target(existing):
            if _channels_of(existing):
                # 该 id 已是一个有渠道的独立用户：绝不把它降级成指针
                user_log(f"别名冲突，拒绝写入: {alias} 已是独立用户（含渠道），目标 {owner}")
                return "conflict"
            # 空记录（get_user 自动创建过）：就地转成指针
            existing[ALIAS_FIELD] = owner
            existing.setdefault("user_id", alias)
            self._save(alias, existing)
            user_log(f"空记录转为别名: {alias} -> {owner}")
            return "written"

        data = existing if existing is not None else {}
        previous = _alias_target(data)
        data["user_id"] = alias
        data[ALIAS_FIELD] = owner
        data.setdefault("channels", {})
        data.setdefault("created_at", datetime.now().isoformat())
        self._save(alias, data)
        if previous != owner:
            user_log(f"已写别名指针: {alias} -> {owner}")
        return "written"

    def _remove_alias(self, alias: str, owner: str) -> bool:
        """去掉指针字段（**保留文件**，非破坏性）。"""
        data = self._load(alias)
        if _alias_target(data) != owner:
            return False
        data.pop(ALIAS_FIELD, None)
        self._save(alias, data)
        user_log(f"已移除别名指针: {alias} -/-> {owner}")
        return True

    def _apply_alias_diff(self, user_id: str, desired: set[str], current: set[str]) -> tuple[int, int, int]:
        written = removed = conflicts = 0
        for alias in sorted(current - desired):
            if self._remove_alias(alias, user_id):
                removed += 1
        for alias in sorted(desired - current):
            result = self._write_alias(alias, user_id)
            if result == "written":
                written += 1
            elif result == "conflict":
                conflicts += 1
        return written, removed, conflicts

    def _reconcile_aliases(self, user_id: str, previous_desired: set[str]) -> None:
        """把指针收敛到 `channels` 的派生状态。

        参数是变更**前**算好的 `desired` 集合，而不是变更前的记录对象 —— 调用方持有的
        记录往往与待写入的是**同一个 dict**，变更后再取 `desired` 会得到新值，短路失效、
        指针永远写不出去。

        短路：`desired` 与变更前相同则直接跳过（登录时的 web 自映射幂等、desired 恒为空集，
        所以 41 个 web-only 用户零扫描开销）。首次全量自愈由 `reconcile_all()` 负责。
        """
        data = self._load(user_id)
        if data is None or _alias_target(data):
            return
        desired = _desired_aliases(user_id, _channels_of(data))
        if desired == previous_desired:
            return
        self._apply_alias_diff(user_id, desired, self._alias_holders(user_id))

    def _current_desired(self, user_id: str) -> set[str]:
        """变更前捕获 desired（必须在改动记录之前调用）。"""
        return _desired_aliases(user_id, _channels_of(self._load(user_id)))

    def reconcile_all(self) -> dict:
        """全量对账（启动时调用）：回填存量指针 + 自愈历史漂移。幂等。"""
        stats = {"users": 0, "written": 0, "removed": 0, "conflicts": 0}
        for path in sorted(self.data_dir.glob("*.json")):
            data = self._load(path.stem)
            if data is None or _alias_target(data):
                continue
            stats["users"] += 1
            desired = _desired_aliases(path.stem, _channels_of(data))
            written, removed, conflicts = self._apply_alias_diff(
                path.stem, desired, self._alias_holders(path.stem)
            )
            stats["written"] += written
            stats["removed"] += removed
            stats["conflicts"] += conflicts
        user_log(
            f"别名对账完成: 检查 {stats['users']} 个用户，"
            f"写 {stats['written']} 移除 {stats['removed']} 冲突 {stats['conflicts']}"
        )
        return stats

    # ─── 读 ─────────────────────────────────────────────────

    def get_user(self, user_id: str) -> dict | None:
        """读用户。**透明跟随别名**：传渠道身份（如 QQ 号）返回规范记录。

        跟随放在这里而不是各调用方，是为了让所有读 `user_json.channels` 的地方
        （调度器的 `get_channels`、网关的 `_other_channels`）一次性受益。
        """
        data = self._load(user_id)
        if data is None:
            data = {"user_id": user_id, "channels": {}, "created_at": datetime.now().isoformat()}
            self._save(user_id, data)
            user_log(f"自动创建用户: {user_id}")
            return data

        target = _alias_target(data)
        if target and target != user_id:
            canonical = self._load(target)
            if canonical is not None and not _alias_target(canonical):
                return canonical
            user_log(f"别名目标无效 {user_id} -> {target}（缺失或链式），按自身处理")
        return data

    def upsert_user(self, user_id: str, user_json: str) -> str:
        try:
            incoming = json.loads(user_json) if user_json else {}
        except json.JSONDecodeError:
            return "JSON 格式错误"
        if not isinstance(incoming, dict):
            return "JSON 格式错误"

        canonical = self.resolve_alias(user_id)
        if canonical != user_id:
            user_log(f"写入重定向到规范用户: {user_id} -> {canonical}")
        user_id = canonical

        previous = self._load(user_id)
        previous_desired = _desired_aliases(user_id, _channels_of(previous))

        data = dict(previous) if previous else {}
        data.update(incoming)
        data.setdefault("user_id", user_id)
        data.setdefault("channels", {})
        data.setdefault("created_at", datetime.now().isoformat())

        # 冲突：incoming 的渠道身份若已属于别人则拒绝（同一渠道身份不能有两个主人）
        for channel, entry in _channels_of(data).items():
            if not isinstance(entry, dict):
                continue
            chan_uid = str(entry.get("channel_user_id") or "").strip()
            if not chan_uid:
                continue
            holder = self._channel_holder(channel, chan_uid)
            if holder and holder != user_id:
                user_log(f"写入被拒：{channel}:{chan_uid} 已属于用户 {holder}")
                return f"渠道身份 {channel}:{chan_uid} 已被用户 {holder} 绑定"

        self._save(user_id, data)
        self._reconcile_aliases(user_id, previous_desired)
        user_log(f"用户已保存: {user_id}")
        return user_id

    def delete_user(self, user_id: str) -> bool:
        path = self._user_path(user_id)
        if not path.exists():
            return False

        data = self._load(user_id)
        if _alias_target(data):
            # 指针是派生状态：只删这一个文件，不牵动规范用户
            os.remove(path)
            user_log(f"已删除别名指针: {user_id} -> {_alias_target(data)}")
            return True

        os.remove(path)
        # 清理指向它的指针（去掉字段，保留文件）
        for alias in self._alias_holders(user_id):
            self._remove_alias(alias, user_id)
        user_log(f"已删除用户: {user_id}")
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

    # ─── 写：渠道绑定 ────────────────────────────────────────

    def bind_channel(self, user_id: str, channel: str, channel_user_id: str, priority: int) -> str:
        canonical = self.resolve_alias(user_id)
        if canonical != user_id:
            user_log(f"绑定重定向到规范用户: {user_id} -> {canonical}")
        user_id = canonical

        channel = str(channel or "").strip()
        channel_user_id = str(channel_user_id or "").strip()
        if not channel:
            return "渠道名不能为空"

        # 冲突：同一渠道身份已被别的用户绑定 → 拒绝（保证解析结果唯一、可复现）
        if channel_user_id:
            holder = self._channel_holder(channel, channel_user_id)
            if holder and holder != user_id:
                user_log(f"绑定被拒：{channel}:{channel_user_id} 已属于用户 {holder}")
                return f"渠道身份 {channel}:{channel_user_id} 已被用户 {holder} 绑定"

        previous = self._load(user_id)
        previous_desired = _desired_aliases(user_id, _channels_of(previous))
        data = dict(previous) if previous else None
        if data is None:
            data = {"user_id": user_id, "channels": {}, "created_at": datetime.now().isoformat()}
            user_log(f"自动创建用户: {user_id}")

        channels = dict(_channels_of(data))
        channels[channel] = {
            "channel": channel,
            "channel_user_id": channel_user_id,
            "priority": priority,
        }
        data["channels"] = channels
        self._save(user_id, data)
        self._reconcile_aliases(user_id, previous_desired)
        user_log(f"用户 {user_id} 绑定渠道 {channel}: {channel_user_id} (优先级={priority})")
        return "ok"

    def unbind_channel(self, user_id: str, channel: str) -> str:
        canonical = self.resolve_alias(user_id)
        if canonical != user_id:
            user_log(f"解绑重定向到规范用户: {user_id} -> {canonical}")
        user_id = canonical

        previous = self._load(user_id)
        if previous is None:
            return "用户不存在"

        channels = previous.get("channels", {})
        if channel not in channels:
            return "渠道未绑定"

        previous_desired = _desired_aliases(user_id, _channels_of(previous))
        data = dict(previous)
        channels = dict(channels)
        channels.pop(channel, None)
        data["channels"] = channels
        self._save(user_id, data)
        self._reconcile_aliases(user_id, previous_desired)
        user_log(f"用户 {user_id} 解绑渠道 {channel}")
        return "ok"

    def set_openviking_key(self, user_id: str, api_key: str) -> bool:
        """存储用户的 OpenViking per-user API key。

        **不走别名重定向**：openviking-context-service 对私聊用**原始 user_id** 取 key
        （`viking_store._storage_user_id` → `GET /openviking_key/{user_id}`），key 决定的是
        **记忆命名空间**。若在此重定向，渠道身份（QQ 号）会拿到规范用户的 key，等于把两个
        身份的记忆静默合并 —— 那是数据层面的决定，不属于"渠道解析"，不能顺手做。
        """
        data = self._load(user_id)
        if data is None:
            data = {"user_id": user_id, "channels": {}, "created_at": datetime.now().isoformat()}
        data["openviking_api_key"] = api_key
        self._save(user_id, data)
        user_log(f"用户 {user_id} OpenViking key 已存储")
        return True

    def get_openviking_key(self, user_id: str) -> str | None:
        """获取用户的 OpenViking per-user API key（按原始 user_id，不跟随别名）。"""
        data = self._load(user_id)
        if data is None:
            return None
        return data.get("openviking_api_key")


user_store = UserStore()
