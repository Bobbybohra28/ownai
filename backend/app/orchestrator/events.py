"""Run events: typed progress events streamed to clients (SSE) and persisted.

Only safe, user-facing progress is emitted ("Searching project…", "Running tests…").
Model reasoning is never emitted.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from abc import ABC, abstractmethod
from collections import defaultdict
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from app.core.logging import get_logger

log = get_logger(__name__)

EVENT_TYPES = {
    "run_started", "status", "intent", "plan", "step_started", "step_finished", "model_selected", "notice",
    "tool_started", "tool_finished", "approval_required", "approval_resolved", "changeset", "verification",
    "token", "answer", "sources", "error", "run_finished", "run_paused", "heartbeat",
}
TERMINAL_EVENTS = {"run_finished", "run_paused"}  # the SSE stream closes after these
STREAM_TTL_S = 24 * 3600


class RunEventRecord(dict):
    """{"seq": int, "type": str, "data": dict, "ts": str}"""


class EventBus(ABC):
    @abstractmethod
    async def publish(self, run_id: uuid.UUID, event_type: str, data: dict[str, Any]) -> int: ...

    @abstractmethod
    async def read(self, run_id: uuid.UUID, after_seq: int, *, block_ms: int = 15000) -> list[RunEventRecord]: ...


class InMemoryEventBus(EventBus):
    def __init__(self) -> None:
        self._events: dict[uuid.UUID, list[RunEventRecord]] = defaultdict(list)
        self._cond = asyncio.Condition()

    async def publish(self, run_id: uuid.UUID, event_type: str, data: dict[str, Any]) -> int:
        async with self._cond:
            seq = len(self._events[run_id]) + 1
            self._events[run_id].append(RunEventRecord(seq=seq, type=event_type, data=data,
                                                        ts=datetime.now(UTC).isoformat()))
            self._cond.notify_all()
            return seq

    async def read(self, run_id: uuid.UUID, after_seq: int, *, block_ms: int = 15000) -> list[RunEventRecord]:
        async with self._cond:
            events = [e for e in self._events.get(run_id, []) if e["seq"] > after_seq]
            if events:
                return events
            try:
                await asyncio.wait_for(self._cond.wait(), timeout=block_ms / 1000)
            except TimeoutError:
                return []
            return [e for e in self._events.get(run_id, []) if e["seq"] > after_seq]


class RedisEventBus(EventBus):
    """Redis Streams with explicit ids ``<seq>-1`` so SSE ``Last-Event-ID`` maps directly to a position."""

    def __init__(self, redis: Any) -> None:
        self.redis = redis

    @staticmethod
    def _key(run_id: uuid.UUID) -> str:
        return f"ownai:run:{run_id}:events"

    async def publish(self, run_id: uuid.UUID, event_type: str, data: dict[str, Any]) -> int:
        seq = int(await self.redis.incr(f"ownai:run:{run_id}:seq"))
        key = self._key(run_id)
        payload = json.dumps({"type": event_type, "data": data, "ts": datetime.now(UTC).isoformat()}, default=str)
        await self.redis.xadd(key, {"e": payload}, id=f"{seq}-1", maxlen=20000, approximate=True)
        await self.redis.expire(key, STREAM_TTL_S)
        await self.redis.expire(f"ownai:run:{run_id}:seq", STREAM_TTL_S)
        return seq

    async def read(self, run_id: uuid.UUID, after_seq: int, *, block_ms: int = 15000) -> list[RunEventRecord]:
        result = await self.redis.xread({self._key(run_id): f"{after_seq}-1"}, block=block_ms, count=500)
        events: list[RunEventRecord] = []
        for _stream, entries in result or []:
            for entry_id, fields in entries:
                eid = entry_id.decode() if isinstance(entry_id, bytes) else entry_id
                raw = fields.get(b"e") or fields.get("e")
                body = json.loads(raw)
                events.append(RunEventRecord(seq=int(eid.split("-")[0]), type=body["type"], data=body["data"],
                                             ts=body["ts"]))
        return events


PersistFn = Callable[[uuid.UUID, int, str, dict[str, Any]], Awaitable[None]]


class RunEventEmitter:
    """Per-run emitter used by the orchestrator and agents."""

    NOT_PERSISTED = {"token", "heartbeat"}

    def __init__(self, bus: EventBus, run_id: uuid.UUID, persist: PersistFn | None = None) -> None:
        self.bus = bus
        self.run_id = run_id
        self.persist = persist

    async def emit(self, event_type: str, **data: Any) -> None:
        if event_type not in EVENT_TYPES:
            raise ValueError(f"Unknown event type {event_type}")
        try:
            seq = await self.bus.publish(self.run_id, event_type, data)
        except Exception as exc:  # streaming failure must not kill the run; it is still persisted
            log.error("events.publish_failed", run_id=str(self.run_id), error=str(exc))
            seq = -(time.time_ns() % 2_000_000_000)  # unique placeholder; the event is still persisted
        if self.persist and event_type not in self.NOT_PERSISTED:
            try:
                await self.persist(self.run_id, seq, event_type, data)
            except Exception as exc:
                log.error("events.persist_failed", run_id=str(self.run_id), error=str(exc))

    async def status(self, message: str, **extra: Any) -> None:
        await self.emit("status", message=message, **extra)
