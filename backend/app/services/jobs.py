"""Background job queue.

* ``RedisStreamQueue`` — production: jobs go to a Redis Stream consumed by ``worker``
  processes (consumer group, ack, redelivery of jobs from crashed workers, dead-lettering).
* ``InlineQueue`` — single-process development/tests: jobs run as asyncio tasks in the
  API process.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import uuid
from abc import ABC, abstractmethod
from collections.abc import Awaitable, Callable
from typing import Any

from app.core.logging import bind_contextvars, clear_contextvars, get_logger

log = get_logger(__name__)

Handler = Callable[[dict[str, Any]], Awaitable[None]]
STREAM = "ownai:jobs"
GROUP = "ownai-workers"
DEAD_LETTER = "ownai:jobs:dead"
MAX_DELIVERIES = 3
# A job is considered abandoned (worker crashed) when it has not been heartbeated for this long.
CLAIM_IDLE_MS = 5 * 60 * 1000
HEARTBEAT_S = 30.0


def _text(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


class JobQueue(ABC):
    def __init__(self) -> None:
        self.handlers: dict[str, Handler] = {}

    def register(self, job_type: str, handler: Handler) -> None:
        self.handlers[job_type] = handler

    @abstractmethod
    async def enqueue(self, job_type: str, payload: dict[str, Any]) -> str: ...

    async def _dispatch(self, job_type: str, payload: dict[str, Any], job_id: str) -> None:
        handler = self.handlers.get(job_type)
        if handler is None:
            log.error("jobs.no_handler", job_type=job_type, job_id=job_id)
            return
        bind_contextvars(job_id=job_id, job_type=job_type)
        try:
            await handler(payload)
        finally:
            clear_contextvars()


class InlineQueue(JobQueue):
    def __init__(self) -> None:
        super().__init__()
        self._tasks: set[asyncio.Task[None]] = set()

    async def enqueue(self, job_type: str, payload: dict[str, Any]) -> str:
        job_id = uuid.uuid4().hex

        async def runner() -> None:
            try:
                await self._dispatch(job_type, payload, job_id)
            except Exception:
                log.exception("jobs.inline_failed", job_type=job_type)

        task = asyncio.create_task(runner(), name=f"job-{job_type}-{job_id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return job_id

    async def drain(self) -> None:
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)


class RedisStreamQueue(JobQueue):
    def __init__(self, redis: Any) -> None:
        super().__init__()
        self.redis = redis
        self.consumer = f"{socket.gethostname()}-{os.getpid()}"

    async def enqueue(self, job_type: str, payload: dict[str, Any]) -> str:
        entry = await self.redis.xadd(STREAM, {"type": job_type, "payload": json.dumps(payload, default=str)},
                                      maxlen=100_000, approximate=True)
        return entry.decode() if isinstance(entry, bytes) else str(entry)

    async def ensure_group(self) -> None:
        try:
            await self.redis.xgroup_create(STREAM, GROUP, id="0", mkstream=True)
        except Exception as exc:  # BUSYGROUP: already exists
            if "BUSYGROUP" not in str(exc):
                raise

    async def _handle(self, entry_id: Any, fields: dict[Any, Any]) -> None:
        eid = _text(entry_id)
        job_type = _text(fields.get(b"type") or fields.get("type") or "")
        payload = json.loads(_text(fields.get(b"payload") or fields.get("payload") or "{}"))
        heartbeat = asyncio.create_task(self._heartbeat(eid))
        try:
            await self._dispatch(job_type, payload, eid)
        except Exception:
            log.exception("jobs.failed", job_type=job_type, job_id=eid)
        finally:
            heartbeat.cancel()
            await self.redis.xack(STREAM, GROUP, eid)

    async def _heartbeat(self, eid: str) -> None:
        """Keep a running job's idle time low so other workers never reclaim it while it is still running.

        XCLAIM ... JUSTID by the owning consumer resets the idle time without counting another delivery.
        """
        while True:
            await asyncio.sleep(HEARTBEAT_S)
            try:
                await self.redis.xclaim(STREAM, GROUP, self.consumer, min_idle_time=0, message_ids=[eid], justid=True)
            except Exception as exc:  # a missed beat only risks an early reclaim; keep trying
                log.warning("jobs.heartbeat_failed", job_id=eid, error=str(exc))

    async def _reclaim(self, spawn: Callable[[Any, dict[Any, Any]], None]) -> None:
        """Re-deliver jobs from crashed workers; dead-letter jobs that keep failing."""
        try:
            pending = await self.redis.xpending_range(STREAM, GROUP, min="-", max="+", count=50)
        except Exception as exc:
            log.warning("jobs.pending_check_failed", error=str(exc))
            return
        for item in pending:
            eid = item["message_id"]
            if item["time_since_delivered"] < CLAIM_IDLE_MS:
                continue
            if item["times_delivered"] >= MAX_DELIVERIES:
                entries = await self.redis.xrange(STREAM, min=eid, max=eid)
                if entries:
                    await self.redis.xadd(DEAD_LETTER, entries[0][1])
                await self.redis.xack(STREAM, GROUP, eid)
                log.error("jobs.dead_lettered", job_id=str(eid))
                continue
            claimed = await self.redis.xclaim(STREAM, GROUP, self.consumer, min_idle_time=CLAIM_IDLE_MS, message_ids=[eid])
            for entry_id, fields in claimed:
                log.warning("jobs.reclaimed", job_id=_text(entry_id))
                spawn(entry_id, fields)

    async def consume(self, *, concurrency: int = 2, stop: asyncio.Event | None = None) -> None:
        await self.ensure_group()
        semaphore = asyncio.Semaphore(concurrency)
        running: set[asyncio.Task[None]] = set()
        stop = stop or asyncio.Event()
        last_reclaim = 0.0

        def spawn(eid: Any, fields: dict[Any, Any], *, acquired: bool = False) -> None:
            async def run() -> None:
                if not acquired:
                    await semaphore.acquire()
                try:
                    await self._handle(eid, fields)
                finally:
                    semaphore.release()

            task = asyncio.create_task(run())
            running.add(task)
            task.add_done_callback(running.discard)

        log.info("worker.started", consumer=self.consumer, concurrency=concurrency)
        while not stop.is_set():
            loop_time = asyncio.get_running_loop().time()
            if loop_time - last_reclaim > 60:
                last_reclaim = loop_time
                await self._reclaim(spawn)
            await semaphore.acquire()
            try:
                result = await self.redis.xreadgroup(GROUP, self.consumer, {STREAM: ">"}, count=1, block=5000)
            except Exception as exc:
                semaphore.release()
                log.error("worker.read_failed", error=str(exc))
                await asyncio.sleep(2)
                continue
            if not result:
                semaphore.release()
                continue
            for _stream, entries in result:
                for entry_id, fields in entries:  # count=1: exactly one entry per acquired slot
                    spawn(entry_id, fields, acquired=True)
        if running:
            await asyncio.gather(*running, return_exceptions=True)
