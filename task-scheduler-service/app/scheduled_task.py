"""Lightweight task object used only by task-scheduler-service.

Important boundary:
- This object is a scheduling DTO: queueing, slot assignment, idempotency, routing.
- It intentionally does NOT contain agent runtime state such as agent_context,
  push_context/pop_context, tool_log, main_log, pending task map, etc.
- agent-orchestrator-service receives ExecuteTaskRequest and constructs its own
  TaskRuntime / original heavy Task internally.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class DeliveryTarget:
    channel: str = ""
    user_id: str = ""
    conversation_id: str = ""
    reply_to: str = ""

    def to_dict(self) -> dict[str, str]:
        return {
            "channel": self.channel,
            "user_id": self.user_id,
            "conversation_id": self.conversation_id,
            "reply_to": self.reply_to,
        }


@dataclass
class ScheduledTask:
    """Scheduler-local lightweight task DTO.

    The scheduler only needs enough data to queue the task, enforce one running
    task per user, call agent-orchestrator-service, and route TaskEvent back to
    the right channel gateway.
    """

    task_id: str
    user_id: str
    session_id: str
    channel: str
    content: str
    client_message_id: str = ""
    delivery_target: DeliveryTarget = field(default_factory=DeliveryTarget)
    metadata: dict[str, str] = field(default_factory=dict)
    agent_id: str = ""

    # Scheduling-only state. These fields replace the tiny subset of the old
    # Task object that scheduler.py actually needs.
    status: str = "queued"
    waiting: int = 0
    retry_count: int = 0
    slot_index: int = -1
    create_time: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if not self.delivery_target.channel:
            self.delivery_target.channel = self.channel
        if not self.delivery_target.user_id:
            self.delivery_target.user_id = self.user_id
        if not self.delivery_target.conversation_id:
            self.delivery_target.conversation_id = self.session_id
        if not self.delivery_target.reply_to:
            self.delivery_target.reply_to = self.client_message_id
        if self.client_message_id:
            self.metadata.setdefault("client_message_id", self.client_message_id)

    @property
    def created_at_iso(self) -> str:
        return datetime.fromtimestamp(self.create_time, tz=timezone.utc).astimezone().isoformat()

    @property
    def idempotency_key(self) -> tuple[str, str, str] | None:
        """Return the key used to dedupe CreateTask retries.

        Empty client_message_id means the caller did not request idempotency.
        """
        if not self.client_message_id:
            return None
        return (self.user_id, self.session_id, self.client_message_id)

    def to_execute_metadata(self) -> dict[str, str]:
        """Metadata passed to agent-orchestrator-service.

        delivery_target is kept as scheduler/channel routing state and is not part
        of ExecuteTaskRequest in the current protocol. The scheduler will fill it
        back into TaskEvent if orchestrator omits it.
        """
        metadata = dict(self.metadata)
        if self.client_message_id:
            metadata.setdefault("client_message_id", self.client_message_id)
        return metadata

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "user_id": self.user_id,
            "session_id": self.session_id,
            "channel": self.channel,
            "content": self.content,
            "client_message_id": self.client_message_id,
            "delivery_target": self.delivery_target.to_dict(),
            "metadata": dict(self.metadata),
            "status": self.status,
            "waiting": self.waiting,
            "retry_count": self.retry_count,
            "slot_index": self.slot_index,
            "created_at": self.created_at_iso,
        }
