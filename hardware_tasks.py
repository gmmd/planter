"""Keep camera/pump worker threads alive until hardware cleanup has finished."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable


logger = logging.getLogger(__name__)


class HardwareTasks:
    def __init__(self) -> None:
        self._workers: set[asyncio.Task] = set()
        self._closing = False

    async def run(self, callback: Callable[..., Any], *args: Any) -> Any:
        if self._closing:
            raise RuntimeError("Hardware is shutting down")
        worker = asyncio.create_task(asyncio.to_thread(callback, *args))
        self._workers.add(worker)
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            # A cancelled await does not stop a thread. Keep its caller's locks
            # and temporary files until the worker has stopped the pump/camera.
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            if not worker.cancelled() and worker.exception() is not None:
                logger.error("Hardware worker failed during cancellation", exc_info=worker.exception())
            raise
        finally:
            self._workers.discard(worker)

    async def close(self) -> None:
        self._closing = True
        if self._workers:
            drain = asyncio.gather(
                *(asyncio.shield(worker) for worker in tuple(self._workers)),
                return_exceptions=True,
            )
            cancelled = False
            while not drain.done():
                try:
                    await asyncio.shield(drain)
                except asyncio.CancelledError:
                    cancelled = True
            drain.result()
            if cancelled:
                raise asyncio.CancelledError


hardware_tasks = HardwareTasks()
