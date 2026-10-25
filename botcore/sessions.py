"""主对话与后台任务共用的会话串行和失效版本。"""

import asyncio
from contextvars import ContextVar

_current = ContextVar("active_session", default=None)
_locks = {}


class SessionInvalidated(Exception):
    """请求期间发生了 reset 或人格切换，应丢弃旧请求的后续输出。"""


class SessionLock:
    def __init__(self):
        self.lock = asyncio.Lock()
        self.version = 0
        self.token = None
        self.loop = None

    async def __aenter__(self):
        version = self.version
        await self.lock.acquire()
        if version != self.version:
            self.lock.release()
            raise SessionInvalidated()
        self.token = _current.set((self, version))
        return self

    async def __aexit__(self, *args):
        _current.reset(self.token)
        self.lock.release()


def session_lock(chat_type: str, target_id: str, persona: str | None = None):
    key = (chat_type, str(target_id), persona if chat_type == "group" else None)
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    lock = _locks.get(key)
    if lock is None or (loop is not None and lock.loop not in (None, loop) and not lock.lock.locked()):
        previous_version = lock.version if lock else 0
        lock = _locks[key] = SessionLock()
        lock.version = previous_version
    if loop is not None:
        lock.loop = loop
    return lock


def invalidate(chat_type: str, target_id: str, persona: str | None = None):
    session_lock(chat_type, target_id, persona).version += 1


def ensure_current():
    value = _current.get()
    if value and value[0].version != value[1]:
        raise SessionInvalidated()
