from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class LoginRequest(BaseModel):
    user_id: str = Field(..., min_length=1)
    session_id: Optional[str] = None


class LoginResponse(BaseModel):
    ok: bool
    user_id: str
    session_id: str
    error: str = ""


class FrontendMessage(BaseModel):
    user_id: str = Field(..., min_length=1)
    session_id: Optional[str] = None
    content: str = Field(..., min_length=1)
    client_message_id: Optional[str] = None
    metadata: Dict[str, str] = Field(default_factory=dict)
    agent_id: Optional[str] = None


class CreateTaskResult(BaseModel):
    ok: bool
    task_id: str = ""
    status: str = ""
    waiting: int = 0
    error: str = ""


class DeliveryTarget(BaseModel):
    channel: str = "web"
    user_id: str
    conversation_id: str
    reply_to: str = ""


class TaskEvent(BaseModel):
    event_id: str = ""
    task_id: str = ""
    user_id: str = ""
    session_id: str = ""
    channel: str = "web"
    type: str
    text: str = ""
    images: List[str] = Field(default_factory=list)
    waiting: int = 0
    error: str = ""
    delivery_target: Optional[DeliveryTarget] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
