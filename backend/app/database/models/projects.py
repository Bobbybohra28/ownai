from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Computed,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base, TimestampMixin, UUIDPkMixin


class ProjectStatus(StrEnum):
    CREATED = "created"
    IMPORTING = "importing"
    INDEXING = "indexing"
    READY = "ready"
    ERROR = "error"


class Project(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "projects"
    __table_args__ = (UniqueConstraint("organization_id", "slug"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    slug: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text, default="")
    source_type: Mapped[str] = mapped_column(String(20), default="empty")  # empty|upload|git|local
    source_ref: Mapped[str] = mapped_column(Text, default="")  # git URL (credentials stripped) or local path
    linked: Mapped[bool] = mapped_column(Boolean, default=False)  # local path used in place (not copied)
    storage_path: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default=ProjectStatus.CREATED.value)
    status_message: Mapped[str] = mapped_column(Text, default="")
    overview: Mapped[dict[str, Any]] = mapped_column(default=dict)  # languages, frameworks, deps, endpoints...
    settings: Mapped[dict[str, Any]] = mapped_column(default=dict)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    indexed_at: Mapped[datetime | None]


class ProjectFile(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "project_files"
    __table_args__ = (UniqueConstraint("project_id", "path"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(index=True)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    path: Mapped[str] = mapped_column(Text)
    language: Mapped[str | None] = mapped_column(String(40))
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    sha256: Mapped[str] = mapped_column(String(64))
    line_count: Mapped[int] = mapped_column(Integer, default=0)
    is_sensitive: Mapped[bool] = mapped_column(Boolean, default=False)
    has_secrets: Mapped[bool] = mapped_column(Boolean, default=False)
    is_test: Mapped[bool] = mapped_column(Boolean, default=False)
    indexed: Mapped[bool] = mapped_column(Boolean, default=False)


class Symbol(UUIDPkMixin, Base):
    __tablename__ = "symbols"
    __table_args__ = (Index("ix_symbols_project_name", "project_id", "name"),)

    organization_id: Mapped[uuid.UUID] = mapped_column(index=True)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    file_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("project_files.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(300))
    qualified_name: Mapped[str] = mapped_column(String(600))
    kind: Mapped[str] = mapped_column(String(40))  # class|function|method|interface|struct|table|endpoint...
    start_line: Mapped[int] = mapped_column(Integer)
    end_line: Mapped[int] = mapped_column(Integer)
    signature: Mapped[str] = mapped_column(Text, default="")
    parent: Mapped[str | None] = mapped_column(String(300))


class FileDependency(UUIDPkMixin, Base):
    __tablename__ = "file_dependencies"

    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    from_file_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("project_files.id", ondelete="CASCADE"), index=True)
    to_file_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("project_files.id", ondelete="CASCADE"))
    import_ref: Mapped[str] = mapped_column(String(500))
    is_external: Mapped[bool] = mapped_column(Boolean, default=False)


class IndexJob(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "index_jobs"

    organization_id: Mapped[uuid.UUID] = mapped_column(index=True)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(30), default="queued")  # queued|running|completed|completed_with_warnings|failed
    stats: Mapped[dict[str, Any]] = mapped_column(default=dict)
    warnings: Mapped[list[Any]] = mapped_column(default=list)
    error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]


class DbConnection(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "db_connections"

    organization_id: Mapped[uuid.UUID] = mapped_column(index=True)
    project_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(120))
    dialect: Mapped[str] = mapped_column(String(20))  # postgresql|sqlite
    # connection details; the password/DSN secret is stored encrypted only
    host: Mapped[str] = mapped_column(String(255), default="")
    port: Mapped[int | None] = mapped_column(Integer)
    database: Mapped[str] = mapped_column(Text, default="")
    username: Mapped[str] = mapped_column(String(200), default="")
    secret_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary)
    read_only: Mapped[bool] = mapped_column(Boolean, default=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))


class Document(UUIDPkMixin, TimestampMixin, Base):
    __tablename__ = "documents"

    organization_id: Mapped[uuid.UUID] = mapped_column(index=True)
    project_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(400))
    doc_type: Mapped[str] = mapped_column(String(30))  # markdown|text|pdf|docx|notes|schema
    mime: Mapped[str] = mapped_column(String(120), default="")
    sha256: Mapped[str] = mapped_column(String(64))
    storage_path: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(30), default="pending")
    error: Mapped[str | None] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, default=1)
    meta: Mapped[dict[str, Any]] = mapped_column("metadata", default=dict)
    created_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))


class DocumentChunk(UUIDPkMixin, Base):
    """A retrievable chunk from a project file or an uploaded document."""

    __tablename__ = "document_chunks"
    __table_args__ = (
        Index("ix_document_chunks_search", "search_vector", postgresql_using="gin"),
        Index("ix_document_chunks_project", "project_id", "file_path"),
    )

    organization_id: Mapped[uuid.UUID] = mapped_column(index=True)
    project_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("projects.id", ondelete="CASCADE"))
    file_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("project_files.id", ondelete="CASCADE"), index=True)
    document_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("documents.id", ondelete="CASCADE"), index=True)
    file_path: Mapped[str] = mapped_column(Text)
    language: Mapped[str | None] = mapped_column(String(40))
    document_type: Mapped[str] = mapped_column(String(30))  # code|markdown|text|pdf|docx|config|schema
    symbol: Mapped[str | None] = mapped_column(String(600))
    chunk_index: Mapped[int] = mapped_column(Integer)
    start_line: Mapped[int] = mapped_column(Integer)
    end_line: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    search_text: Mapped[str] = mapped_column(Text)
    search_vector: Mapped[Any] = mapped_column(
        TSVECTOR, Computed("to_tsvector('simple', search_text)", persisted=True)
    )
    token_count: Mapped[int] = mapped_column(Integer, default=0)
    content_hash: Mapped[str] = mapped_column(String(64))
    embedding_model: Mapped[str | None] = mapped_column(String(200))
    embedded: Mapped[bool] = mapped_column(Boolean, default=False)
    meta: Mapped[dict[str, Any]] = mapped_column("metadata", default=dict)
