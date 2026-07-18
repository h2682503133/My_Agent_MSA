import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class DeliveryTarget:
    channel: str
    user_id: str
    conversation_id: str
    reply_to: str = ""


@dataclass
class TaskEventDTO:
    event_id: str
    task_id: str
    user_id: str
    session_id: str
    channel: str
    type: str
    text: str = ""
    images: list[str] = field(default_factory=list)
    waiting: int = 0
    error: str = ""
    delivery_target: DeliveryTarget | None = None
    metadata: dict[str, str] = field(default_factory=dict)


def new_event_id() -> str:
    return f"event-{uuid.uuid4().hex[:12]}"
