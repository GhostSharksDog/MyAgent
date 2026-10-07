"""同一 API 进程内，会话的读取、执行、清理和保存保持先后顺序。"""

import asyncio
from weakref import WeakValueDictionary

from fastapi import HTTPException


class SessionGate:
    def __init__(self):
        self.locks = WeakValueDictionary()

    async def acquire(self, session_id: str | None, wait_seconds: float = 0):
        if not session_id:
            return Lease(None)
        lock = self.locks.get(session_id)
        if lock is None:
            lock = asyncio.Lock()
            self.locks[session_id] = lock
        try:
            async with asyncio.timeout(wait_seconds or None):
                await lock.acquire()
        except TimeoutError as exc:
            raise HTTPException(409, "上一轮仍在清理，等待已超时；请稍后重试。") from exc
        return Lease(lock)


class Lease:
    def __init__(self, lock):
        self.lock = lock

    def release(self):
        if self.lock is not None:
            self.lock.release()
            self.lock = None
