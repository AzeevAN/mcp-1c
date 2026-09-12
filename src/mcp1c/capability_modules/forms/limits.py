"""Изолированный bounded executor для тяжёлых чистых операций Forms."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
import threading
from typing import TypeVar


MAX_CONCURRENT_OPERATIONS = 2
MAX_PENDING_OPERATIONS = 10
OPERATION_TIMEOUT_SECONDS = 10.0
MAX_SPECIFICATION_BYTES = 256 * 1024
MAX_MODULE_BYTES = 2 * 1024 * 1024
MAX_RESULT_BYTES = 4 * 1024 * 1024

_Result = TypeVar("_Result")


class FormsToolInputError(ValueError):
    """Вход MCP-обёртки превышает опубликованный предел."""


class FormsToolResultError(RuntimeError):
    """Ответ операции превышает опубликованный предел."""


class FormsToolBusyError(RuntimeError):
    """Все места bounded очереди Forms заняты."""


class FormsToolTimeoutError(TimeoutError):
    """Клиент перестал ждать bounded операцию Forms."""


class FormsExecutionGate:
    """Отделить CPU-работу Forms от общего worker pool MCP-сервера."""

    def __init__(
        self,
        *,
        workers: int = MAX_CONCURRENT_OPERATIONS,
        pending_limit: int = MAX_PENDING_OPERATIONS,
        timeout_seconds: float = OPERATION_TIMEOUT_SECONDS,
    ) -> None:
        self.pending_limit = pending_limit
        self.timeout_seconds = timeout_seconds
        self._pending = 0
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(
            max_workers=workers,
            thread_name_prefix="mcp1c-forms",
        )

    @property
    def pending(self) -> int:
        with self._lock:
            return self._pending

    def _enter(self) -> None:
        with self._lock:
            if self._pending >= self.pending_limit:
                raise FormsToolBusyError(
                    "Очередь Forms заполнена; повторите вызов после завершения "
                    "одной из текущих операций."
                )
            self._pending += 1

    def _leave(self) -> None:
        with self._lock:
            self._pending -= 1

    async def run(self, operation: Callable[[], _Result]) -> _Result:
        self._enter()
        loop = asyncio.get_running_loop()

        def guarded() -> _Result:
            try:
                return operation()
            finally:
                self._leave()

        future = loop.run_in_executor(self._executor, guarded)
        try:
            async with asyncio.timeout(self.timeout_seconds):
                return await asyncio.shield(future)
        except TimeoutError as error:
            raise FormsToolTimeoutError(
                "Операция Forms превысила лимит ожидания; её результат отброшен."
            ) from error
