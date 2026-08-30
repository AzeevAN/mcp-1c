"""Узкий шов между HTTP-дашбордом и внешним супервизором процесса."""

from __future__ import annotations

import os
import secrets
import signal
import threading
from collections.abc import Callable


SELF_RESTART_ENV = "MCP1C_ALLOW_SELF_RESTART"
DEFAULT_RESTART_DELAY = 0.5


def _terminate_current_process() -> None:
    os.kill(os.getpid(), signal.SIGTERM)


class RestartController:
    """Один раз завершить процесс после отправки HTTP-ответа.

    Контроллер не запускает Docker и не знает о конкретном оркестраторе.
    Возврат процесса обеспечивает внешний supervisor; без явного разрешения
    маршрут обязан остаться недоступным, иначе bare-запуск просто погаснет.
    """

    def __init__(
        self,
        *,
        enabled: bool,
        terminate: Callable[[], None] | None = None,
        delay: float = DEFAULT_RESTART_DELAY,
        runtime_id: str | None = None,
    ) -> None:
        self.enabled = enabled
        self.runtime_id = runtime_id or secrets.token_urlsafe(12)
        self.delay = max(0.0, delay)
        self._terminate = terminate or _terminate_current_process
        self._requested = False
        self._lock = threading.Lock()

    @classmethod
    def from_environment(cls) -> "RestartController":
        return cls(enabled=os.environ.get(SELF_RESTART_ENV, "") == "1")

    @property
    def requested(self) -> bool:
        with self._lock:
            return self._requested

    def reserve(self) -> bool:
        """Зарезервировать единственный рестарт до отправки ответа."""
        if not self.enabled:
            return False
        with self._lock:
            if self._requested:
                return False
            self._requested = True
            return True

    def terminate_after_response(self) -> None:
        """Запланировать завершение из ASGI background task."""
        timer = threading.Timer(self.delay, self._terminate)
        timer.daemon = True
        timer.start()
