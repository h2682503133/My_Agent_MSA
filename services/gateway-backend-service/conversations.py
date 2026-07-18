"""内存态对话管理，不持久化，服务重启即丢失。"""

import time
from dataclasses import dataclass, field
from typing import Dict, List


@dataclass
class Conversation:
    agent_id: str
    user_id: str
    messages: List[dict] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)

    def touch(self) -> None:
        self.last_active = time.time()

    def to_dict(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "user_id": self.user_id,
            "message_count": len(self.messages),
            "created_at": self.created_at,
            "last_active": self.last_active,
        }


class ConversationManager:
    """纯内存对话管理，{user_id: {agent_id: Conversation}}"""

    def __init__(self) -> None:
        self._store: Dict[str, Dict[str, Conversation]] = {}

    def list_conversations(self, user_id: str) -> List[dict]:
        user_convs = self._store.get(user_id, {})
        return [
            conv.to_dict()
            for conv in sorted(
                user_convs.values(),
                key=lambda c: c.last_active,
                reverse=True,
            )
        ]

    def get_conversation(self, user_id: str, agent_id: str) -> Conversation | None:
        return self._store.get(user_id, {}).get(agent_id)

    def get_or_create(self, user_id: str, agent_id: str) -> Conversation:
        if user_id not in self._store:
            self._store[user_id] = {}

        conv = self._store[user_id].get(agent_id)
        if conv is None:
            conv = Conversation(agent_id=agent_id, user_id=user_id)
            self._store[user_id][agent_id] = conv
        return conv

    def create_conversation(self, user_id: str, agent_id: str) -> Conversation:
        if user_id not in self._store:
            self._store[user_id] = {}

        conv = Conversation(agent_id=agent_id, user_id=user_id)
        self._store[user_id][agent_id] = conv
        return conv

    def delete_conversation(self, user_id: str, agent_id: str) -> bool:
        user_convs = self._store.get(user_id)
        if not user_convs or agent_id not in user_convs:
            return False
        del user_convs[agent_id]
        if not user_convs:
            del self._store[user_id]
        return True

    def add_message(self, user_id: str, agent_id: str, msg: dict) -> None:
        conv = self.get_or_create(user_id, agent_id)
        conv.messages.append(msg)
        conv.touch()


conversation_manager = ConversationManager()
