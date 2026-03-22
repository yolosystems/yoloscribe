"""Pydantic request/response models for the YoloScribe API."""

from pydantic import BaseModel, Field, field_validator

from config import MAX_CHAT_CONTENT_BYTES, MAX_CHAT_HISTORY_TURNS, MAX_CHAT_MESSAGE_BYTES


class HistoryMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    # YOL-45: reject messages that exceed the character limit (returns HTTP 422).
    message: str = Field(max_length=MAX_CHAT_MESSAGE_BYTES)
    current_content: str
    history: list[HistoryMessage] = []
    site: str = "default"
    file_path: str = "content.md"

    # YOL-42: silently truncate oversized page content so the agent can still
    # operate on partial content rather than failing the whole request.
    @field_validator("current_content")
    @classmethod
    def truncate_current_content(cls, v: str) -> str:
        if len(v) > MAX_CHAT_CONTENT_BYTES:
            return v[:MAX_CHAT_CONTENT_BYTES] + "\n...[truncated]"
        return v

    # YOL-41: drop the oldest turns when history exceeds the cap, preserving
    # the most recent context which is most relevant to the current turn.
    @field_validator("history")
    @classmethod
    def cap_history(cls, v: list[HistoryMessage]) -> list[HistoryMessage]:
        if len(v) > MAX_CHAT_HISTORY_TURNS:
            return v[-MAX_CHAT_HISTORY_TURNS:]
        return v


class ChatResponse(BaseModel):
    reply: str
    updated_content: str | None = None
    navigate_to: str | None = None


class UserCreatedEvent(BaseModel):
    user_id: str


class SecretValue(BaseModel):
    value: str


class ProvisionRequest(BaseModel):
    site_name: str
    theme: str


class ProvisionResponse(BaseModel):
    site_url: str


class CreatePageRequest(BaseModel):
    site: str
    page_path: str


class CreateAgentRequest(BaseModel):
    site: str
    page_path: str = ""
    agent_name: str


class SharedUser(BaseModel):
    email: str
    access: str  # "view" | "write"


class PageSettings(BaseModel):
    visibility: str  # "public" | "private" | "shared"
    shared_with: list[SharedUser] = []


class AccessRequest(BaseModel):
    site: str
    path: str
