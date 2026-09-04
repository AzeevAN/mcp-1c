"""Допуск без очереди и отдельный лимитер потоков для тяжёлого HTTP intake."""

from __future__ import annotations

import asyncio
from contextvars import ContextVar
from functools import partial
from threading import Lock

import anyio


async def _settled(task: asyncio.Task):
    """Отмена клиента не бросает работающий поток и используемые им файлы."""
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        while not task.done():
            try:
                with anyio.CancelScope(shield=True):
                    await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not task.cancelled():
            task.exception()
        raise


class _Lease:
    def __init__(self, gate: HeavyOperations):
        self.gate = gate
        self.users = 1

    def release(self) -> None:
        self.users -= 1
        if self.users == 0:
            self.gate._admission.release()


class HeavyOperations:
    """Одна принимаемая/выполняемая операция; никакой очереди ожидающих."""

    def __init__(self):
        self._admission = Lock()
        self._limiter = anyio.CapacityLimiter(1)
        self.current: ContextVar[_Lease] = ContextVar("heavy_operation")
        self._pending: set[asyncio.Task] = set()

    def acquire(self) -> _Lease | None:
        return _Lease(self) if self._admission.acquire(blocking=False) else None

    async def run(self, function, *args, **kwargs):
        # AnyIO уже используется транспортом. Свой limiter не расходует
        # default tokens, которыми обслуживаются sources/queries и прочее чтение.
        task = asyncio.create_task(anyio.to_thread.run_sync(
            partial(function, *args, **kwargs), limiter=self._limiter,
        ))
        return await _settled(task)

    def spawn(self, coroutine) -> asyncio.Task:
        lease = self.current.get()
        lease.users += 1
        inner = asyncio.create_task(coroutine)
        self._pending.add(inner)

        def finished(task):
            self._pending.discard(task)
            lease.release()
            if not task.cancelled():
                task.exception()

        inner.add_done_callback(finished)
        # Даже отмена внешней задачи до её первого шага не отменяет inner.
        # Допуск освобождает завершение работы, а не callback HTTP-запроса.
        outer = asyncio.create_task(_settled(inner))
        outer.add_done_callback(
            lambda task: task.exception() if not task.cancelled() else None
        )
        return outer
