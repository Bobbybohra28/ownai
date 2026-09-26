from __future__ import annotations

import asyncio
import os
import shutil
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from tests.conftest import FIXTURES, requires_integration

pytestmark = requires_integration


@pytest.fixture(scope="session")
def integration_settings(tmp_path_factory, fake_model_server):
    from cryptography.fernet import Fernet

    from app.core.config import Settings

    workdir = tmp_path_factory.mktemp("ownai")
    models_file = workdir / "models.yaml"
    models_file.write_text(yaml.safe_dump({"models": {
        "chat": {"provider": "openai_compatible", "endpoint": f"{fake_model_server.url}/v1", "model": "fake-chat",
                 "roles": ["fast", "coding", "reasoning"], "context_length": 32768, "timeout_s": 30},
        "embed": {"provider": "openai_compatible", "endpoint": f"{fake_model_server.url}/v1", "model": "fake-embed",
                  "roles": ["embedding"], "embedding_dimensions": 64, "timeout_s": 30},
    }}))
    fixtures_copy = workdir / "fixtures"
    shutil.copytree(FIXTURES, fixtures_copy)
    return Settings(
        _env_file=None,
        app_env="test",
        database_url=os.environ["OWNAI_TEST_DATABASE_URL"],
        qdrant_url=os.environ.get("OWNAI_TEST_QDRANT_URL", "http://localhost:6333"),
        execution_mode="inline",
        secret_key="test-secret-key-" + "x" * 32,
        encryption_key=Fernet.generate_key().decode(),
        models_config_path=models_file,
        projects_root=workdir / "projects",
        local_import_roots=[fixtures_copy],
        sandbox_url=os.environ.get("OWNAI_TEST_SANDBOX_URL"),
        sandbox_token=os.environ.get("OWNAI_TEST_SANDBOX_TOKEN", ""),
        log_json=False,
        log_level="WARNING",
        model_health_ttl_s=5,
        max_run_seconds=600,
    )


@pytest.fixture(scope="session")
def fixtures_root(integration_settings) -> Path:
    return integration_settings.local_import_roots[0]


def _migrate(database_url: str) -> None:
    from alembic import command
    from alembic.config import Config
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import create_async_engine

    async def reset() -> None:
        engine = create_async_engine(database_url)
        async with engine.begin() as conn:
            await conn.execute(text("DROP SCHEMA public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))
        await engine.dispose()

    asyncio.run(reset())
    backend = Path(__file__).resolve().parents[2]
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "migrations"))
    os.environ["OWNAI_DATABASE_URL"] = database_url
    from app.core.config import get_settings

    get_settings.cache_clear()
    command.upgrade(cfg, "head")


@pytest.fixture(scope="session")
async def app(integration_settings):
    await asyncio.to_thread(_migrate, integration_settings.database_url)
    from app.main import create_app

    application = create_app(integration_settings)
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture(scope="session")
async def container(app):
    return app.state.container


class ApiClient:
    def __init__(self, app: Any) -> None:
        self.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", timeout=600)
        self.token: str | None = None

    async def request(self, method: str, path: str, expect: int | None = None, **kwargs: Any) -> httpx.Response:
        headers = kwargs.pop("headers", {})
        headers["X-Requested-With"] = "ownai"
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        response = await self.http.request(method, "/api/v1" + path, headers=headers, **kwargs)
        if expect is not None:
            assert response.status_code == expect, f"{method} {path} -> {response.status_code}: {response.text[:500]}"
        return response

    async def register(self, email: str | None = None, password: str = "Str0ng-password!") -> dict[str, Any]:
        email = email or f"user-{uuid.uuid4().hex[:8]}@example.com"
        r = await self.request("POST", "/auth/register", expect=201,
                               json={"email": email, "password": password, "display_name": "Tester"})
        self.token = r.json()["access_token"]
        return r.json() | {"password": password}


@pytest.fixture
async def client(app):
    c = ApiClient(app)
    await c.register()
    yield c
    await c.http.aclose()


@pytest.fixture
async def fake(fake_model_server):
    async with httpx.AsyncClient(base_url=fake_model_server.url) as http:
        await http.post("/_control", json={"reset": True})
        yield http
        await http.post("/_control", json={"reset": True})


async def wait_for(predicate, timeout: float = 120, interval: float = 0.25):
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        value = await predicate()
        if value:
            return value
        await asyncio.sleep(interval)
    raise AssertionError("condition not met in time")


def parse_sse(text: str) -> list[tuple[str, dict]]:
    import json

    events = []
    for block in text.split("\n\n"):
        event_type, data = None, None
        for line in block.splitlines():
            if line.startswith("event:"):
                event_type = line[6:].strip()
            elif line.startswith("data:"):
                data = json.loads(line[5:])
        if event_type and data is not None:
            events.append((event_type, data))
    return events
