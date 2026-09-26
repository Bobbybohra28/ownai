"""Project lifecycle: create, import (zip / git / local), re-index, browse files, delete."""

from __future__ import annotations

import re
import shutil
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.exceptions import ConflictError, ErrorCode, NotFoundError, ValidationFailed
from app.core.logging import get_logger
from app.database.models import DocumentChunk, IndexJob, Organization, Project, ProjectFile, ProjectStatus
from app.database.repositories.base import get_scoped
from app.projects import importer
from app.projects.scanner import render_tree
from app.security.paths import PathJail
from app.security.secrets import is_env_file, list_env_keys, mask_secrets

if TYPE_CHECKING:
    from app.core.dependencies import Container

log = get_logger(__name__)


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:80] or "project"


class ProjectService:
    def __init__(self, container: Container) -> None:
        self.c = container

    def _root_for(self, org_id: uuid.UUID, project_id: uuid.UUID) -> Path:
        return (self.c.settings.projects_root / str(org_id) / str(project_id)).resolve()

    async def _new(self, session: AsyncSession, org: Organization, user_id: uuid.UUID, name: str, description: str,
                   source_type: str, source_ref: str = "", linked_path: Path | None = None) -> Project:
        name = name.strip()
        if not name:
            raise ValidationFailed("Project name is required.")
        await self.c.billing.check(session, org, "projects")
        slug = slugify(name)
        exists = (await session.execute(select(func.count(Project.id)).where(
            Project.organization_id == org.id, Project.slug == slug))).scalar_one()
        if exists:
            slug = f"{slug}-{uuid.uuid4().hex[:6]}"
        project_id = uuid.uuid4()
        storage = linked_path or self._root_for(org.id, project_id)
        project = Project(id=project_id, organization_id=org.id, name=name[:200], slug=slug, description=description,
                          source_type=source_type, source_ref=importer.strip_url_credentials(source_ref),
                          linked=linked_path is not None, storage_path=str(storage), created_by=user_id,
                          status=ProjectStatus.IMPORTING.value)
        session.add(project)
        await session.flush()
        return project

    async def enqueue_index(self, session: AsyncSession, project: Project, *, full: bool = False) -> IndexJob:
        job = IndexJob(organization_id=project.organization_id, project_id=project.id, status="queued")
        session.add(job)
        project.status = ProjectStatus.INDEXING.value
        project.status_message = "Queued for indexing"
        await session.commit()
        await self.c.queue.enqueue("project.index", {"project_id": str(project.id), "job_id": str(job.id), "full": full})
        return job

    async def create_empty(self, session: AsyncSession, org: Organization, user_id: uuid.UUID, name: str,
                           description: str = "") -> Project:
        project = await self._new(session, org, user_id, name, description, "empty")
        Path(project.storage_path).mkdir(parents=True, exist_ok=True)
        await self.enqueue_index(session, project)
        return project

    async def import_zip(self, session: AsyncSession, org: Organization, user_id: uuid.UUID, name: str,
                         archive: Path, description: str = "") -> Project:
        project = await self._new(session, org, user_id, name, description, "upload")
        root = Path(project.storage_path)
        try:
            count = importer.extract_zip(archive, root, max_bytes=self.c.settings.max_upload_mb * 1024 * 1024 * 5)
            importer.sanitize_git_dir(root)
        except Exception:
            shutil.rmtree(root, ignore_errors=True)
            await session.rollback()
            raise
        log.info("project.imported_zip", project_id=str(project.id), files=count)
        await self.enqueue_index(session, project)
        return project

    async def import_git(self, session: AsyncSession, org: Organization, user_id: uuid.UUID, name: str, url: str,
                         branch: str | None, description: str = "") -> Project:
        importer.validate_git_url(url)
        project = await self._new(session, org, user_id, name, description, "git", source_ref=url)
        root = Path(project.storage_path)
        root.parent.mkdir(parents=True, exist_ok=True)
        try:
            await importer.git_clone(url, root, branch=branch)
        except Exception:
            shutil.rmtree(root, ignore_errors=True)
            await session.rollback()
            raise
        await self.enqueue_index(session, project)
        return project

    async def import_local(self, session: AsyncSession, org: Organization, user_id: uuid.UUID, name: str, path: str,
                           link: bool, description: str = "") -> Project:
        roots = [Path(p) for p in self.c.settings.local_import_roots]
        if link:
            source = importer.link_local(path, roots)
            project = await self._new(session, org, user_id, name, description, "local", source_ref=str(source),
                                      linked_path=source)
        else:
            project = await self._new(session, org, user_id, name, description, "local", source_ref=path)
            try:
                importer.copy_local(path, Path(project.storage_path), roots)
            except Exception:
                shutil.rmtree(project.storage_path, ignore_errors=True)
                await session.rollback()
                raise
        await self.enqueue_index(session, project)
        return project

    async def get(self, session: AsyncSession, project_id: uuid.UUID, org_id: uuid.UUID) -> Project:
        return await get_scoped(session, Project, project_id, org_id, label="Project")

    async def delete(self, session: AsyncSession, project: Project) -> None:
        rows = (await session.execute(select(DocumentChunk.id, DocumentChunk.embedding_model).where(
            DocumentChunk.project_id == project.id, DocumentChunk.embedded.is_(True)))).all()
        await self.c.indexing.delete_chunk_vectors([(r[0], r[1]) for r in rows])
        storage = Path(project.storage_path)
        await session.delete(project)
        await session.commit()
        if not project.linked and storage.is_dir() and self.c.settings.projects_root.resolve() in storage.resolve().parents:
            shutil.rmtree(storage, ignore_errors=True)

    async def tree(self, session: AsyncSession, project: Project) -> dict[str, Any]:
        rows = (await session.execute(select(ProjectFile.path, ProjectFile.language, ProjectFile.size_bytes,
                                             ProjectFile.is_sensitive, ProjectFile.has_secrets)
                                      .where(ProjectFile.project_id == project.id).order_by(ProjectFile.path))).all()
        files = [{"path": r[0], "language": r[1], "size": r[2], "sensitive": r[3], "has_secrets": r[4]} for r in rows]
        return {"files": files, "tree": render_tree([f["path"] for f in files], max_depth=6, max_entries=2000)}

    def read_file(self, project: Project, path: str) -> dict[str, Any]:
        jail = PathJail(Path(project.storage_path))
        rel = jail.normalize(path)
        if is_env_file(rel):
            target = jail.resolve(rel, allow_sensitive=True, must_exist=True)
            return {"path": rel, "sensitive": True, "content": None,
                    "variables": list_env_keys(target.read_text(encoding="utf-8", errors="ignore"))}
        target = jail.resolve(rel, must_exist=True)
        if not target.is_file():
            raise ValidationFailed(f"'{rel}' is not a file.")
        data = target.read_bytes()
        if len(data) > 2_000_000:
            raise ValidationFailed("File is too large to display.", code=ErrorCode.VALIDATION_ERROR)
        if b"\x00" in data[:4096]:
            return {"path": rel, "binary": True, "content": None, "size": len(data)}
        return {"path": rel, "content": mask_secrets(data.decode("utf-8", errors="replace")), "size": len(data)}

    async def ensure_ready(self, project: Project) -> None:
        if project.status not in (ProjectStatus.READY.value,):
            raise ConflictError(f"Project '{project.name}' is {project.status}; wait until indexing finishes.")

    async def latest_job(self, session: AsyncSession, project_id: uuid.UUID) -> IndexJob | None:
        return (await session.execute(select(IndexJob).where(IndexJob.project_id == project_id)
                                      .order_by(IndexJob.created_at.desc()).limit(1))).scalar_one_or_none()


def ensure_exists(project: Project | None) -> Project:
    if project is None:
        raise NotFoundError("Project not found.")
    return project
