"""Sandbox isolation properties, verified against the real sandbox runner + Docker."""

from __future__ import annotations

import os

import httpx
import pytest

from app.core.exceptions import AppError
from app.sandbox.client import SandboxClient

pytestmark = pytest.mark.skipif(not os.environ.get("OWNAI_TEST_SANDBOX_URL"),
                                reason="set OWNAI_TEST_SANDBOX_URL to run sandbox tests")


@pytest.fixture
async def sandbox():
    async with httpx.AsyncClient() as http:
        yield SandboxClient(os.environ["OWNAI_TEST_SANDBOX_URL"], os.environ.get("OWNAI_TEST_SANDBOX_TOKEN", ""), http)


async def run(sandbox: SandboxClient, code: str, **kwargs):
    return await sandbox.execute(profile="python", argv=["python", "x.py"], overlay={"x.py": code}, **kwargs)


async def test_runs_code_and_project_tests(sandbox, sample_project_dir):
    result = await run(sandbox, "print(6 * 7)")
    assert result.ok and result.stdout.strip() == "42"
    tests = await sandbox.execute(profile="python", argv=["python", "-m", "pytest", "-q", "-p", "no:cacheprovider"],
                                  workspace=sample_project_dir)
    assert tests.exit_code == 1 and "3 failed, 2 passed" in tests.stdout  # the fixture's real bug


async def test_credential_files_are_not_shipped(sandbox, sample_project_dir):
    result = await sandbox.execute(profile="python", argv=["python", "-c", "import os; print(os.path.exists('.env'))"],
                                   workspace=sample_project_dir)
    assert result.stdout.strip() == "False"


async def test_no_network(sandbox):
    result = await run(sandbox, "import urllib.request\nurllib.request.urlopen('http://1.1.1.1', timeout=3)")
    assert result.exit_code != 0 and "Network is unreachable" in result.stderr


async def test_timeout_is_enforced(sandbox):
    result = await run(sandbox, "import time\ntime.sleep(60)", timeout_s=3)
    assert result.timed_out and result.duration_ms < 20_000


async def test_read_only_root_and_non_root_user(sandbox):
    result = await run(sandbox, "import os\nprint(os.getuid())\nopen('/etc/evil', 'w')")
    assert result.stdout.strip() == "1000" and "Read-only file system" in result.stderr


async def test_memory_limit(sandbox):
    result = await run(sandbox, "x = bytearray(1024 * 1024 * 1024)", memory_mb=128)
    assert result.oom_killed or result.exit_code != 0


async def test_disallowed_commands(sandbox):
    for argv, profile in ((["bash", "-c", "id"], "python"), (["pip", "install", "requests"], "python")):
        with pytest.raises(AppError):
            await sandbox.execute(profile=profile, argv=argv)
