"""Redis Streams job queue against a real Redis: long jobs are not re-run, crashed workers' jobs are."""

import asyncio
import os
import uuid

import pytest

from app.services import jobs

REDIS_URL = os.environ.get("OWNAI_TEST_REDIS_URL")
pytestmark = pytest.mark.skipif(not REDIS_URL, reason="set OWNAI_TEST_REDIS_URL to run job queue tests")


@pytest.fixture
async def redis(monkeypatch):
    from redis.asyncio import Redis

    client = Redis.from_url(REDIS_URL)
    suffix = uuid.uuid4().hex[:8]
    monkeypatch.setattr(jobs, "STREAM", f"test:jobs:{suffix}")
    monkeypatch.setattr(jobs, "DEAD_LETTER", f"test:jobs:dead:{suffix}")
    monkeypatch.setattr(jobs, "CLAIM_IDLE_MS", 600)
    monkeypatch.setattr(jobs, "HEARTBEAT_S", 0.2)
    yield client
    await client.delete(jobs.STREAM, jobs.DEAD_LETTER)
    await client.aclose()


def queue(redis, name: str) -> jobs.RedisStreamQueue:
    q = jobs.RedisStreamQueue(redis)
    q.consumer = name
    return q


async def test_running_job_is_not_reclaimed(redis) -> None:
    calls: list[str] = []

    async def slow(payload: dict) -> None:
        calls.append(payload["n"])
        await asyncio.sleep(2.0)  # much longer than CLAIM_IDLE_MS

    a, b = queue(redis, "worker-a"), queue(redis, "worker-b")
    for q in (a, b):
        q.register("slow", slow)
        await q.ensure_group()
    await a.enqueue("slow", {"n": "1"})
    entries = await redis.xreadgroup(jobs.GROUP, a.consumer, {jobs.STREAM: ">"}, count=1)
    eid, fields = entries[0][1][0]
    running = asyncio.create_task(a._handle(eid, fields))
    reclaimed: list[object] = []
    for _ in range(6):  # a second worker scans for abandoned jobs while the first is still running
        await asyncio.sleep(0.3)
        await b._reclaim(lambda e, f: reclaimed.append(e))
    await running
    assert calls == ["1"] and reclaimed == []
    assert (await redis.xpending(jobs.STREAM, jobs.GROUP))["pending"] == 0


async def test_crashed_workers_job_is_reclaimed(redis) -> None:
    b = queue(redis, "worker-b")
    await b.ensure_group()
    await b.enqueue("any", {})
    await redis.xreadgroup(jobs.GROUP, "crashed-worker", {jobs.STREAM: ">"}, count=1)  # delivered, never acked
    await asyncio.sleep(0.8)
    reclaimed: list[object] = []
    await b._reclaim(lambda e, f: reclaimed.append(e))
    assert len(reclaimed) == 1
