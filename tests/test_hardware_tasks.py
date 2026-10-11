"""Worker lifetime checks using ordinary threads, never GPIO or cameras."""

import asyncio
import tempfile
import threading
import unittest
from pathlib import Path

from hardware_tasks import HardwareTasks


class HardwareTasksTests(unittest.IsolatedAsyncioTestCase):
    async def test_result_and_worker_failure_reach_caller(self):
        manager = HardwareTasks()
        self.assertEqual(await manager.run(lambda value: value + 1, 41), 42)

        def fail():
            raise ValueError("camera failed")

        with self.assertRaisesRegex(ValueError, "camera failed"):
            await manager.run(fail)
        await manager.close()

    async def test_repeated_cancellation_keeps_lock_and_tempdir_until_worker_finishes(self):
        manager = HardwareTasks()
        lock = asyncio.Lock()
        started = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()
        observed = {}

        def worker(path):
            observed["path"] = path
            loop.call_soon_threadsafe(started.set)
            if not release.wait(5):
                raise TimeoutError("Test did not release the worker")
            path.write_bytes(b"completed recording")

        async def operation():
            async with lock:
                with tempfile.TemporaryDirectory(prefix="planter-worker-test-") as directory:
                    await manager.run(worker, Path(directory) / "video.mp4")

        operation_task = asyncio.create_task(operation())
        try:
            await asyncio.wait_for(started.wait(), timeout=2)
            for _ in range(2):
                operation_task.cancel()
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                self.assertFalse(operation_task.done())
                self.assertTrue(lock.locked())
                self.assertTrue(observed["path"].parent.exists())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await operation_task
            self.assertFalse(lock.locked())
            self.assertFalse(observed["path"].parent.exists())
        finally:
            release.set()
            await asyncio.gather(operation_task, return_exceptions=True)
            await manager.close()

    async def test_close_waits_for_worker_and_rejects_new_operations(self):
        manager = HardwareTasks()
        started = asyncio.Event()
        release = threading.Event()
        loop = asyncio.get_running_loop()

        def worker():
            loop.call_soon_threadsafe(started.set)
            if not release.wait(5):
                raise TimeoutError("Test did not release the worker")

        operation = asyncio.create_task(manager.run(worker))
        shutdown = None
        try:
            await asyncio.wait_for(started.wait(), timeout=2)
            shutdown = asyncio.create_task(manager.close())
            await asyncio.sleep(0)
            self.assertFalse(shutdown.done())
            with self.assertRaisesRegex(RuntimeError, "shutting down"):
                await manager.run(lambda: None)
            release.set()
            await operation
            await shutdown
        finally:
            release.set()
            await asyncio.gather(operation, return_exceptions=True)
            if shutdown is not None:
                await asyncio.gather(shutdown, return_exceptions=True)

    async def test_cancelled_close_still_waits_for_hardware_cleanup(self):
        manager = HardwareTasks()
        started = asyncio.Event()
        release = threading.Event()
        finished = threading.Event()
        loop = asyncio.get_running_loop()

        def worker():
            loop.call_soon_threadsafe(started.set)
            if not release.wait(5):
                raise TimeoutError("Test did not release the worker")
            finished.set()

        operation = asyncio.create_task(manager.run(worker))
        shutdown = None
        try:
            await asyncio.wait_for(started.wait(), timeout=2)
            shutdown = asyncio.create_task(manager.close())
            await asyncio.sleep(0)
            for _ in range(2):
                shutdown.cancel()
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                self.assertFalse(shutdown.done())
                self.assertFalse(finished.is_set())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await shutdown
            self.assertTrue(finished.is_set())
            await operation
        finally:
            release.set()
            await asyncio.gather(operation, return_exceptions=True)
            if shutdown is not None:
                await asyncio.gather(shutdown, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
