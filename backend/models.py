"""Pydantic request/response models for the AgentScribe API."""

from pydantic import BaseModel


class HistoryMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    message: str
    current_content: str
    history: list[HistoryMessage] = []
    site: str = "default"
    file_path: str = "content.md"


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


class SharedUser(BaseModel):
    email: str
    access: str  # "view" | "write"


class PageSettings(BaseModel):
    visibility: str  # "public" | "private" | "shared"
    shared_with: list[SharedUser] = []


class AccessRequest(BaseModel):
    site: str
    path: str
