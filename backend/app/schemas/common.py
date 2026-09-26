"""Request/response DTOs shared by the API routers."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# --- auth ------------------------------------------------------------------------------------
class RegisterRequest(BaseModel):
    email: str = Field(max_length=320)
    password: str = Field(min_length=1, max_length=256)
    display_name: str = Field(default="", max_length=200)


class LoginRequest(BaseModel):
    email: str = Field(max_length=320)
    password: str = Field(max_length=256)


class UserOut(ORMModel):
    id: uuid.UUID
    email: str
    display_name: str
    is_superadmin: bool
    preferences: dict[str, Any] = Field(default_factory=dict)


class OrgOut(ORMModel):
    id: uuid.UUID
    name: str
    slug: str
    plan_id: str
    is_personal: bool


class TokenResponse(BaseModel):
    access_token: str
    token_type: Literal["bearer"] = "bearer"  # noqa: S105
    expires_at: datetime
    user: UserOut
    organization: OrgOut
    role: str


class UpdateUserRequest(BaseModel):
    display_name: str | None = Field(default=None, max_length=200)
    preferences: dict[str, Any] | None = None


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


class AddMemberRequest(BaseModel):
    email: str = Field(max_length=320)
    role: Literal["admin", "member", "viewer"] = "member"


class UpdateMemberRequest(BaseModel):
    role: Literal["admin", "member", "viewer"]


# --- projects ------------------------------------------------------------------------------------
class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)


class GitImport(ProjectCreate):
    url: str = Field(min_length=4, max_length=2000)
    branch: str | None = Field(default=None, max_length=200)


class LocalImport(ProjectCreate):
    path: str = Field(min_length=1, max_length=2000)
    link: bool = Field(default=False, description="Work on the directory in place instead of copying it")


class ProjectOut(ORMModel):
    id: uuid.UUID
    name: str
    slug: str
    description: str
    source_type: str
    source_ref: str
    linked: bool
    status: str
    status_message: str
    created_at: datetime
    indexed_at: datetime | None


# --- conversations ------------------------------------------------------------------------------
class ConversationCreate(BaseModel):
    title: str = Field(default="New conversation", max_length=300)
    project_id: uuid.UUID | None = None
    mode: str = "auto"


class ConversationUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=300)
    mode: str | None = None
    project_id: uuid.UUID | None = None
    archived: bool | None = None


class ConversationOut(ORMModel):
    id: uuid.UUID
    title: str
    project_id: uuid.UUID | None
    mode: str
    archived: bool
    created_at: datetime
    updated_at: datetime


class MessageCreate(BaseModel):
    content: str = Field(min_length=1, max_length=20_000)
    mode: str | None = None


class MessageOut(ORMModel):
    id: uuid.UUID
    role: str
    content: str
    run_id: uuid.UUID | None
    created_at: datetime
    meta: dict[str, Any] = Field(default_factory=dict)


# --- approvals / tools / rag / memory ---------------------------------------------------------------
class ApprovalDecision(BaseModel):
    note: str = Field(default="", max_length=2000)


class ToolInvokeRequest(BaseModel):
    project_id: uuid.UUID | None = None
    args: dict[str, Any] = Field(default_factory=dict)


class RAGSearchRequest(BaseModel):
    query: str = Field(min_length=2, max_length=2000)
    project_id: uuid.UUID | None = None
    top_k: int = Field(default=8, ge=1, le=30)
    language: str | None = None
    document_type: str | None = None
    path_prefix: str | None = None
    answer: bool = Field(default=False, description="Also generate a cited answer with the RAG agent")


class MemoryCreate(BaseModel):
    scope: Literal["user", "project"] = "user"
    kind: str = "preference"
    content: str = Field(min_length=1, max_length=4000)
    project_id: uuid.UUID | None = None


class MemoryUpdate(BaseModel):
    content: str | None = Field(default=None, max_length=4000)
    confirmed: bool | None = None
    importance: int | None = Field(default=None, ge=1, le=5)


# --- models / agents --------------------------------------------------------------------------------
class ModelUpdate(BaseModel):
    enabled: bool | None = None
    priority: int | None = Field(default=None, ge=0, le=100)


class AgentUpdate(BaseModel):
    enabled: bool


# --- sql -----------------------------------------------------------------------------------------------
class DbConnectionCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    dialect: Literal["postgresql", "sqlite"]
    host: str = Field(default="", max_length=255)
    port: int | None = Field(default=None, ge=1, le=65535)
    database: str = Field(min_length=1, max_length=1000, description="DB name, or project-relative file for SQLite")
    username: str = Field(default="", max_length=200)
    password: str | None = Field(default=None, max_length=1000)
    read_only: bool = True


class SQLQueryRequest(BaseModel):
    query: str = Field(min_length=1, max_length=20_000)
    max_rows: int = Field(default=200, ge=1, le=5000)


class SQLValidateRequest(BaseModel):
    query: str = Field(min_length=1, max_length=20_000)
    dialect: Literal["postgresql", "sqlite"] = "postgresql"


# --- git -----------------------------------------------------------------------------------------------
class GitCommitRequest(BaseModel):
    message: str = Field(min_length=3, max_length=2000)
    paths: list[str] = Field(default_factory=list)


class GitBranchRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)


# --- evaluation / billing ---------------------------------------------------------------------------------
class EvaluationCreate(BaseModel):
    dataset: str
    model_id: str
    project_id: uuid.UUID | None = None


class SetPlanRequest(BaseModel):
    plan_id: str


class CheckoutRequest(BaseModel):
    plan_id: str
    success_url: str
    cancel_url: str
