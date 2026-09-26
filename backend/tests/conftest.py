"""Shared fixtures.

Unit tests need nothing external. Integration tests need PostgreSQL + Qdrant and are
enabled by setting ``OWNAI_TEST_DATABASE_URL`` (and optionally ``OWNAI_TEST_QDRANT_URL``,
``OWNAI_TEST_SANDBOX_URL``/``OWNAI_TEST_SANDBOX_TOKEN``). They use the fake model server.
"""

from __future__ import annotations

import os
import socket
import sys
from pathlib import Path

import pytest

BACKEND = Path(__file__).resolve().parents[1]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(BACKEND))


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="session")
def fake_model_server():
    from tests.fakes.fake_model_server import FakeModelServer

    server = FakeModelServer(free_port())
    server.start()
    yield server
    server.stop()


@pytest.fixture(scope="session")
def sample_project_dir() -> Path:
    return FIXTURES / "sample_project"


def integration_enabled() -> bool:
    return bool(os.environ.get("OWNAI_TEST_DATABASE_URL"))


requires_integration = pytest.mark.skipif(not integration_enabled(),
                                          reason="set OWNAI_TEST_DATABASE_URL to run integration tests")
