"""Background worker: executes agent runs, project indexing, document processing and evaluations.

Run with: python -m app.worker   (requires OWNAI_EXECUTION_MODE=worker and Redis)
"""

from __future__ import annotations

import asyncio
import os
import signal

from app.core.config import get_settings
from app.core.dependencies import build_container
from app.core.exceptions import ConfigurationError
from app.core.logging import configure_logging, get_logger
from app.services.jobs import RedisStreamQueue

log = get_logger("worker")


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_json)
    if settings.execution_mode != "worker":
        raise ConfigurationError("The worker requires OWNAI_EXECUTION_MODE=worker (with Redis).")
    container = await build_container(settings, role="worker")
    assert isinstance(container.queue, RedisStreamQueue)
    container.health.start_background(settings.model_health_interval_s)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows: rely on KeyboardInterrupt
            pass
    try:
        await container.queue.consume(concurrency=int(os.environ.get("OWNAI_WORKER_CONCURRENCY", "2")), stop=stop)
    finally:
        log.info("worker.stopping")
        await container.aclose()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
