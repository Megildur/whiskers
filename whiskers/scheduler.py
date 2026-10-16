"""One model call at a time.

A CPU can only run one generation at full speed, so everything takes turns. Replies to people always
go first: if a background chore (writing memories, updating the journal) is running when someone
talks to Whiskers, the chore is cancelled and retried later.
"""
from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator, Awaitable, Callable


class Scheduler:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._waiting = 0
        self._chore: asyncio.Future | None = None
        self.last_reply_at = 0.0

    @property
    def pending_replies(self) -> int:
        return self._waiting

    @property
    def busy(self) -> bool:
        return self._waiting > 0 or self._lock.locked()

    def idle_for(self) -> float:
        return 0.0 if self.busy else time.monotonic() - self.last_reply_at

    @asynccontextmanager
    async def reply_turn(self) -> AsyncIterator[None]:
        self._waiting += 1
        if self._chore is not None and not self._chore.done():
            self._chore.cancel()
        try:
            async with self._lock:
                yield
        finally:
            self._waiting -= 1
            self.last_reply_at = time.monotonic()

    async def run_chore(self, make_chore: Callable[[], Awaitable[object]]) -> bool:
        """Run a background chore if nothing else is going on. False if skipped or interrupted."""
        if self.busy:
            return False
        async with self._lock:
            if self._waiting:
                return False
            chore = self._chore = asyncio.ensure_future(make_chore())
            try:
                await asyncio.wait({chore})
            except asyncio.CancelledError:
                chore.cancel()
                raise
            finally:
                self._chore = None
            if chore.cancelled():
                return False
            chore.result()  # re-raise the chore's own error, if any
            return True
