"""Document upload and indexing (Markdown, TXT, PDF, DOCX, code, schema, notes)."""

from __future__ import annotations

import hashlib
import re
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from sqlalchemy import delete, select

from app.core.exceptions import ValidationFailed
from app.core.logging import get_logger
from app.database.models import Document, DocumentChunk
from app.projects.symbols import extract_symbols
from app.rag.chunking import build_search_text, chunk_code, chunk_text
from app.rag.parsers import parse_bytes
from app.security.secrets import mask_secrets

if TYPE_CHECKING:
    from app.core.dependencies import Container

log = get_logger(__name__)
ALLOWED_SUFFIXES = {".md", ".mdx", ".txt", ".rst", ".pdf", ".docx", ".sql", ".json", ".yaml", ".yml", ".py", ".ts",
                    ".js", ".java", ".go", ".rs", ".c", ".cpp", ".h", ".csv", ".log", ".toml", ".ini", ".html"}


def safe_filename(name: str) -> str:
    base = Path(name.replace("\\", "/")).name
    cleaned = re.sub(r"[^A-Za-z0-9._\- ]+", "_", base).strip(". ")
    return cleaned[:150] or "document"


async def store_document(container: Container, *, org_id: uuid.UUID, project_id: uuid.UUID | None,
                         user_id: uuid.UUID, filename: str, data: bytes, title: str | None = None) -> Document:
    name = safe_filename(filename)
    suffix = Path(name).suffix.lower()
    if suffix not in ALLOWED_SUFFIXES:
        raise ValidationFailed(f"Unsupported document type '{suffix}'. Supported: {', '.join(sorted(ALLOWED_SUFFIXES))}")
    if len(data) > container.settings.max_upload_mb * 1024 * 1024:
        raise ValidationFailed("The document is too large.")
    doc_id = uuid.uuid4()
    folder = container.settings.projects_root / str(org_id) / "_documents" / str(doc_id)
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(data)
    async with container.sessions() as session:
        document = Document(id=doc_id, organization_id=org_id, project_id=project_id, title=(title or name)[:400],
                            doc_type=suffix.lstrip(".") if suffix in (".pdf", ".docx") else "text", mime="",
                            sha256=hashlib.sha256(data).hexdigest(), storage_path=str(path), status="queued",
                            created_by=user_id)
        session.add(document)
        await session.commit()
        await session.refresh(document)
    await container.queue.enqueue("document.index", {"document_id": str(doc_id)})
    return document


async def process_document(container: Container, document_id: uuid.UUID) -> None:
    async with container.sessions() as session:
        document = await session.get(Document, document_id)
        if document is None:
            return
        document.status = "processing"
        await session.commit()
        org_id, project_id, path = document.organization_id, document.project_id, Path(document.storage_path)
    try:
        parsed = parse_bytes(path.name, path.read_bytes())
        text = mask_secrets(parsed.text)
        extraction = extract_symbols(parsed.language, text)
        chunks = chunk_code(text, extraction.symbols) if parsed.doc_type in ("code", "schema") else chunk_text(
            text, extraction.symbols)
        virtual_path = f"documents/{path.name}"
        async with container.sessions() as session:
            await session.execute(delete(DocumentChunk).where(DocumentChunk.document_id == document_id))
            for idx, chunk in enumerate(chunks):
                page = parsed.page_for_line(chunk.start_line)
                session.add(DocumentChunk(
                    organization_id=org_id, project_id=project_id, document_id=document_id, file_path=virtual_path,
                    language=parsed.language, document_type=parsed.doc_type, symbol=chunk.symbol, chunk_index=idx,
                    start_line=chunk.start_line, end_line=chunk.end_line, content=chunk.content,
                    search_text=build_search_text(virtual_path, chunk.symbol, chunk.content), token_count=chunk.token_count,
                    content_hash=chunk.content_hash, meta={"page": page, "document_title": path.name},
                ))
            document = await session.get(Document, document_id)
            assert document is not None
            document.doc_type = parsed.doc_type
            document.meta = {"chunks": len(chunks), "pages": len(parsed.page_starts) or None}
            await session.commit()
        embedded, warning = await container.indexing.embed_pending(org_id, document_id=document_id)
        async with container.sessions() as session:
            document = await session.get(Document, document_id)
            assert document is not None
            document.status = "ready"
            document.error = warning
            document.meta = {**document.meta, "embedded": embedded}
            await session.commit()
    except Exception as exc:
        log.exception("document.processing_failed", document_id=str(document_id))
        async with container.sessions() as session:
            document = await session.get(Document, document_id)
            if document is not None:
                document.status = "failed"
                document.error = getattr(exc, "message", None) or f"{type(exc).__name__}"
                await session.commit()


async def delete_document(container: Container, document: Document) -> None:
    async with container.sessions() as session:
        rows = (await session.execute(select(DocumentChunk.id, DocumentChunk.embedding_model).where(
            DocumentChunk.document_id == document.id, DocumentChunk.embedded.is_(True)))).all()
    await container.indexing.delete_chunk_vectors([(r[0], r[1]) for r in rows])
    path = Path(document.storage_path)
    async with container.sessions() as session:
        obj = await session.get(Document, document.id)
        if obj is not None:
            await session.delete(obj)
            await session.commit()
    if path.exists() and container.settings.projects_root.resolve() in path.resolve().parents:
        path.unlink(missing_ok=True)
        try:
            path.parent.rmdir()
        except OSError:
            pass
