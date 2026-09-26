"""Project indexing pipeline.

scan → reconcile files → parse/mask → symbols → chunks → imports graph → analyzers →
embeddings → Qdrant. Incremental: only files whose hash changed are re-processed.
Secrets are masked *before* anything is stored or embedded; sensitive files
(``.env``, keys, credentials) are recorded but their contents are never indexed.
"""

from __future__ import annotations

import json
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.exceptions import AppError, ModelError, RAGError
from app.core.logging import get_logger
from app.database.base import utcnow
from app.database.models import (
    DocumentChunk,
    FileDependency,
    IndexJob,
    Project,
    ProjectFile,
    ProjectStatus,
    Symbol,
)
from app.projects.analyzers.code_facts import (
    detect_frameworks,
    detect_test_setup,
    find_db_usage,
    find_endpoints,
    find_env_usage,
)
from app.projects.analyzers.dependencies import analyze_dependencies
from app.projects.graph import resolve_imports
from app.projects.scanner import ScannedFile, render_tree, scan_project
from app.projects.symbols import extract_symbols
from app.rag.chunking import build_search_text, chunk_code, chunk_text
from app.rag.embeddings import EmbeddingService, collection_name
from app.rag.parsers import classify
from app.rag.vector_store import VectorPoint, VectorStore
from app.security.secrets import find_secrets, is_env_file, list_env_keys, mask_secrets

log = get_logger(__name__)


@dataclass
class IndexStats:
    files_total: int = 0
    files_changed: int = 0
    files_removed: int = 0
    chunks_created: int = 0
    symbols: int = 0
    embedded: int = 0
    secrets_masked: int = 0
    warnings: list[str] = field(default_factory=list)


def project_root(project: Project) -> Path:
    return Path(project.storage_path)


class IndexingService:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession], embeddings: EmbeddingService,
                 vector_store: VectorStore, *, max_files: int, max_file_kb: int) -> None:
        self.sessions = session_factory
        self.embeddings = embeddings
        self.vector_store = vector_store
        self.max_files = max_files
        self.max_file_kb = max_file_kb

    async def index_project(self, project_id: uuid.UUID, *, job_id: uuid.UUID | None = None, full: bool = False,
                            progress: Any = None) -> IndexJob:
        async with self.sessions() as session:
            project = await session.get(Project, project_id)
            if project is None:
                raise AppError("Project not found.")
            job = await session.get(IndexJob, job_id) if job_id else None
            if job is None:
                job = IndexJob(organization_id=project.organization_id, project_id=project.id)
                session.add(job)
            job.status = "running"
            job.started_at = utcnow()
            project.status = ProjectStatus.INDEXING.value
            project.status_message = "Scanning files…"
            await session.commit()
            job_id = job.id
        try:
            stats = await self._run(project_id, full=full, progress=progress)
        except Exception as exc:
            log.exception("indexing.failed", project_id=str(project_id))
            message = exc.message if isinstance(exc, AppError) else "Indexing failed due to an internal error."
            async with self.sessions() as session:
                await session.execute(update(IndexJob).where(IndexJob.id == job_id).values(
                    status="failed", error=message, finished_at=utcnow()))
                await session.execute(update(Project).where(Project.id == project_id).values(
                    status=ProjectStatus.ERROR.value, status_message=message))
                await session.commit()
            raise
        async with self.sessions() as session:
            job = await session.get(IndexJob, job_id)
            assert job is not None
            job.status = "completed_with_warnings" if stats.warnings else "completed"
            job.stats = {k: v for k, v in stats.__dict__.items() if k != "warnings"}
            job.warnings = stats.warnings
            job.finished_at = utcnow()
            project = await session.get(Project, project_id)
            assert project is not None
            project.status = ProjectStatus.READY.value
            project.status_message = "; ".join(stats.warnings[:3]) if stats.warnings else "Indexed"
            project.indexed_at = utcnow()
            await session.commit()
            await session.refresh(job)
            return job

    # ------------------------------------------------------------------------------------------
    async def _run(self, project_id: uuid.UUID, *, full: bool, progress: Any) -> IndexStats:
        stats = IndexStats()
        async with self.sessions() as session:
            project = await session.get(Project, project_id)
            assert project is not None
            root = project_root(project)
            org_id = project.organization_id
            if not root.is_dir():
                raise AppError(f"Project directory not found: {root.name}")
            scan = scan_project(root, max_files=self.max_files, max_file_kb=self.max_file_kb,
                                extra_ignores=(project.settings or {}).get("ignore", []))
            if scan.truncated:
                stats.warnings.append(f"Only the first {self.max_files} files were indexed.")
            stats.files_total = len(scan.files)
            existing = {f.path: f for f in (await session.execute(
                select(ProjectFile).where(ProjectFile.project_id == project_id))).scalars().all()}
            scanned_paths = {f.path for f in scan.files}

            # removed files
            removed = [f for p, f in existing.items() if p not in scanned_paths]
            if removed:
                await self._delete_vectors(org_id, [f.id for f in removed], session)
                await session.execute(delete(ProjectFile).where(ProjectFile.id.in_([f.id for f in removed])))
                stats.files_removed = len(removed)

            changed: list[tuple[ScannedFile, ProjectFile]] = []
            for sf in scan.files:
                pf = existing.get(sf.path)
                if pf is None:
                    pf = ProjectFile(organization_id=org_id, project_id=project_id, path=sf.path, sha256=sf.sha256)
                    session.add(pf)
                elif pf.sha256 == sf.sha256 and pf.indexed and not full:
                    continue
                pf.language, pf.size_bytes, pf.sha256 = sf.language, sf.size, sf.sha256
                pf.line_count, pf.is_sensitive, pf.is_test = sf.line_count, sf.is_sensitive, sf.is_test
                pf.indexed = False
                changed.append((sf, pf))
            await session.flush()
            stats.files_changed = len(changed)
            if progress:
                await progress(f"Processing {len(changed)} changed file(s) of {len(scan.files)}…")

            all_paths = {f.path for f in scan.files}
            path_to_id = {p: f.id for p, f in existing.items() if p in scanned_paths}
            for _, pf in changed:
                path_to_id[pf.path] = pf.id

            changed_ids = [pf.id for _, pf in changed]
            if changed_ids:
                await self._delete_vectors(org_id, changed_ids, session)
                await session.execute(delete(DocumentChunk).where(DocumentChunk.file_id.in_(changed_ids)))
                await session.execute(delete(Symbol).where(Symbol.file_id.in_(changed_ids)))
                await session.execute(delete(FileDependency).where(FileDependency.from_file_id.in_(changed_ids)))

            for sf, pf in changed:
                if sf.is_sensitive:
                    pf.indexed = True  # recorded, intentionally not indexed
                    continue
                try:
                    raw = (root / sf.path).read_text(encoding="utf-8", errors="replace")
                except OSError as exc:
                    stats.warnings.append(f"Could not read {sf.path}: {exc.strerror}")
                    continue
                secrets = find_secrets(raw)
                pf.has_secrets = bool(secrets)
                stats.secrets_masked += len(secrets)
                text = mask_secrets(raw) if secrets else raw
                doc_type, language = classify(sf.path)
                extraction = extract_symbols(language, text)
                for sym in extraction.symbols:
                    session.add(Symbol(organization_id=org_id, project_id=project_id, file_id=pf.id, name=sym.name[:300],
                                       qualified_name=sym.qualified_name[:600], kind=sym.kind, start_line=sym.start_line,
                                       end_line=sym.end_line, signature=sym.signature, parent=sym.parent))
                stats.symbols += len(extraction.symbols)
                for imp in resolve_imports(sf.path, language, extraction.imports, all_paths):
                    session.add(FileDependency(project_id=project_id, from_file_id=pf.id,
                                               to_file_id=path_to_id.get(imp.target) if imp.target else None,
                                               import_ref=imp.ref[:500], is_external=imp.external))
                chunks = (chunk_code(text, extraction.symbols) if doc_type in ("code", "schema")
                          else chunk_text(text, extraction.symbols))
                for idx, chunk in enumerate(chunks):
                    session.add(DocumentChunk(
                        organization_id=org_id, project_id=project_id, file_id=pf.id, file_path=sf.path,
                        language=language, document_type=doc_type, symbol=chunk.symbol, chunk_index=idx,
                        start_line=chunk.start_line, end_line=chunk.end_line, content=chunk.content,
                        search_text=build_search_text(sf.path, chunk.symbol, chunk.content),
                        token_count=chunk.token_count, content_hash=chunk.content_hash,
                        meta={"kind": chunk.kind, "is_test": sf.is_test},
                    ))
                stats.chunks_created += len(chunks)
                pf.indexed = True
            await session.flush()

            project.overview = await self._overview(session, project, root, scan.files, scan.directories)
            await session.commit()

        embedded, warning = await self.embed_pending(org_id, project_id=project_id, progress=progress)
        stats.embedded = embedded
        if warning:
            stats.warnings.append(warning)
        return stats

    async def _delete_vectors(self, org_id: uuid.UUID, file_ids: list[uuid.UUID], session: AsyncSession) -> None:
        rows = (await session.execute(select(DocumentChunk.id, DocumentChunk.embedding_model).where(
            DocumentChunk.file_id.in_(file_ids), DocumentChunk.embedded.is_(True)))).all()
        await self.delete_chunk_vectors([(r[0], r[1]) for r in rows])

    async def delete_chunk_vectors(self, rows: list[tuple[uuid.UUID, str | None]]) -> None:
        """Delete vectors for chunks; ``embedding_model`` stores the collection each chunk was written to."""
        by_collection: dict[str, list[uuid.UUID]] = {}
        for chunk_id, collection in rows:
            if collection:
                by_collection.setdefault(collection, []).append(chunk_id)
        for collection, ids in by_collection.items():
            try:
                await self.vector_store.delete_ids(collection, ids)
            except RAGError as exc:
                # stale vectors are harmless: retrieval re-validates every hit against PostgreSQL
                log.warning("indexing.vector_delete_failed", error=exc.message)

    async def embed_pending(self, org_id: uuid.UUID, *, project_id: uuid.UUID | None = None,
                            document_id: uuid.UUID | None = None, progress: Any = None) -> tuple[int, str | None]:
        """Embed chunks not yet embedded. Returns (count, warning)."""
        if self.embeddings.model() is None:
            return 0, "No embedding model configured — semantic search disabled (keyword search still works)."
        if not await self.embeddings.available():
            return 0, "Embedding model is offline — semantic search disabled until re-indexed (keyword search works)."
        total = 0
        batch_size = 64
        while True:
            async with self.sessions() as session:
                stmt = select(DocumentChunk).where(DocumentChunk.organization_id == org_id,
                                                   DocumentChunk.embedded.is_(False))
                if project_id:
                    stmt = stmt.where(DocumentChunk.project_id == project_id)
                if document_id:
                    stmt = stmt.where(DocumentChunk.document_id == document_id)
                chunks = list((await session.execute(stmt.limit(batch_size))).scalars().all())
                if not chunks:
                    break
                texts = [f"{c.file_path}{' ' + c.symbol if c.symbol else ''}\n{c.content}" for c in chunks]
                try:
                    vectors, model = await self.embeddings.embed_documents(texts)
                    name = collection_name(model, len(vectors[0]))
                    await self.vector_store.ensure_collection(name, len(vectors[0]))
                    await self.vector_store.upsert(name, [
                        VectorPoint(id=c.id, vector=v, payload={
                            "org_id": str(c.organization_id), "project_id": str(c.project_id) if c.project_id else None,
                            "document_id": str(c.document_id) if c.document_id else None, "file_path": c.file_path,
                            "language": c.language, "document_type": c.document_type, "symbol": c.symbol,
                            "start_line": c.start_line, "end_line": c.end_line, "chunk_id": str(c.id),
                        })
                        for c, v in zip(chunks, vectors, strict=True)
                    ])
                except (ModelError, RAGError) as exc:
                    log.warning("indexing.embedding_failed", error=exc.message, code=str(exc.code))
                    return total, f"Embedding stopped after {total} chunks: {exc.message} (keyword search still works)."
                for c in chunks:
                    c.embedded = True
                    c.embedding_model = name
                await session.commit()
                total += len(chunks)
                if progress:
                    await progress(f"Embedded {total} chunks…")
        return total, None

    async def _overview(self, session: AsyncSession, project: Project, root: Path, files: list[ScannedFile],
                        directories: list[str]) -> dict[str, Any]:
        paths = [f.path for f in files]
        deps, dep_errors = analyze_dependencies(root, [f.path for f in files if not f.is_sensitive])
        dep_names = {d.name for d in deps}
        package_scripts: dict[str, str] = {}
        if (root / "package.json").is_file():
            try:
                package_scripts = json.loads((root / "package.json").read_text(encoding="utf-8")).get("scripts") or {}
            except (ValueError, OSError):
                package_scripts = {}
        endpoints: list[dict[str, Any]] = []
        env_vars: set[str] = set()
        db_usage: list[dict[str, Any]] = []
        env_files: dict[str, list[str]] = {}
        for f in files:
            if f.is_sensitive:
                if is_env_file(f.path):
                    try:
                        env_files[f.path] = list_env_keys((root / f.path).read_text(encoding="utf-8", errors="ignore"))
                    except OSError:
                        pass
                continue
            if f.language is None or f.size > 400_000:
                continue
            try:
                text = (root / f.path).read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if not f.is_test:
                endpoints.extend(e.to_dict() for e in find_endpoints(f.path, f.language, text))
            env_vars |= find_env_usage(text)
            db_usage.extend(find_db_usage(f.path, text))
        readme = next((p for p in paths if p.lower() in {"readme.md", "readme.rst", "readme.txt", "readme"}), None)
        readme_excerpt = ""
        if readme:
            readme_excerpt = mask_secrets((root / readme).read_text(encoding="utf-8", errors="ignore")[:3000])
        languages = Counter(f.language for f in files if f.language and not f.is_sensitive)
        git_info: dict[str, Any] = {"is_repo": (root / ".git").is_dir()}
        head = root / ".git" / "HEAD"
        if head.is_file():
            ref = head.read_text(encoding="utf-8", errors="ignore").strip()
            git_info["branch"] = ref.split("/")[-1] if ref.startswith("ref:") else ref[:12]
        return {
            "generated_at": datetime.now(UTC).isoformat(),
            "file_count": len(files),
            "languages": dict(languages.most_common()),
            "frameworks": detect_frameworks(dep_names, paths),
            "dependencies": [d.to_dict() for d in deps[:500]],
            "dependency_errors": dep_errors,
            "endpoints": endpoints[:300],
            "env_vars": sorted(env_vars)[:300],
            "env_files": env_files,  # names only, never values
            "database_usage": db_usage[:100],
            "tests": detect_test_setup(paths, dep_names, package_scripts),
            "package_scripts": package_scripts,
            "sensitive_files": [f.path for f in files if f.is_sensitive][:100],
            "files_with_secrets": list((await session.execute(select(ProjectFile.path).where(
                ProjectFile.project_id == project.id, ProjectFile.has_secrets.is_(True)))).scalars().all())[:100],
            "readme": readme,
            "readme_excerpt": readme_excerpt,
            "tree": render_tree(paths, max_depth=4, max_entries=300),
            "top_directories": sorted({d.split("/")[0] for d in directories})[:50],
            "git": git_info,
        }
