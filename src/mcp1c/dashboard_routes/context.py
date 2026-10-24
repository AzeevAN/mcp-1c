"""Общие ресурсы, доступ и согласование обработчиков дашборда."""

from __future__ import annotations

import asyncio
import os
import threading
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from functools import wraps
from pathlib import Path
from typing import TYPE_CHECKING

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from ..dashboard_backend import admin_authorized, can_read, csrf_denied
from ..intake_v2_api import IntakeApiService, IntakeWork

if TYPE_CHECKING:
    from ..capabilities import CapabilityRuntime
    from ..heavy_operations import HeavyOperations
    from ..process_restart import RestartController
    from ..reference_provider import ReferenceService
    from ..registry import Registry


Endpoint = Callable[[Request], Awaitable[Response]]


def _json_error(message: str, status_code: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status_code)


def _admin_denied(request: Request, *, action: str) -> JSONResponse | None:
    if not os.environ.get("ADMIN_TOKEN", ""):
        return _json_error(f"{action} выключено: не задан ADMIN_TOKEN.", 404)
    if not admin_authorized(request):
        return _json_error("Нужен вход администратора.", 403)
    return None


def _mutation_denied(request: Request, *, action: str) -> JSONResponse | None:
    denied = _admin_denied(request, action=action)
    if denied is not None:
        return denied
    csrf = csrf_denied(request)
    if csrf is not None:
        return _json_error("Запрос отклонён проверкой источника.", csrf.status_code)
    return None


# Проверка доступа — первым шагом обработчика, до чтения тела и параметров.
# Раньше она копировалась в каждый обработчик, и новый маршрут без копии
# оказывался открытым молча; декоратор делает проверку видимой в объявлении.
# Порядок важен: токен проверяется до любой работы, поэтому общий лимит
# неудачных попыток (`check_token`) считает и эти запросы.
def read_endpoint(endpoint):
    """Обработчик JSON API, доступный с токеном чтения (или без него, если
    `API_TOKEN` не задан)."""

    @wraps(endpoint)
    async def guarded(request: Request, *args, **kwargs):
        if not can_read(request):
            return _json_error("Нужен токен чтения.", 401)
        return await endpoint(request, *args, **kwargs)

    return guarded


def admin_endpoint(action: str, *, mutation: bool = False):
    """Обработчик, требующий входа администратора.

    `action` попадает в ответ, когда `ADMIN_TOKEN` не задан. `mutation=True`
    добавляет проверку источника запроса (CSRF) — для всего, что меняет
    состояние.
    """

    check = _mutation_denied if mutation else _admin_denied

    def decorate(endpoint):
        @wraps(endpoint)
        async def guarded(request: Request, *args, **kwargs):
            denied = check(request, action=action)
            if denied is not None:
                return denied
            return await endpoint(request, *args, **kwargs)

        return guarded

    return decorate


async def _json_body(request: Request) -> dict:
    try:
        payload = await request.json()
    except (ValueError, RecursionError):
        return {}
    return payload if isinstance(payload, dict) else {}


@dataclass
class DashboardContext:
    """Ресурсы и единственный набор intake-состояния одного набора маршрутов."""

    registry: Registry
    static_dir: Path
    reference: ReferenceService
    restart: RestartController
    capabilities: CapabilityRuntime
    intake: IntakeApiService | None
    role_tools_refresh: Callable[[], Awaitable[bool]] | None
    heavy: HeavyOperations
    intake_tasks: set[asyncio.Task[None]] = field(default_factory=set, init=False)
    intake_start_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False)
    intake_create_lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    # Ставится под `admin_endpoint(..., mutation=True)`: права и CSRF
    # проверяются снаружи, до захвата допуска, поэтому чужой запрос не
    # занимает слот тяжёлой операции даже на мгновение.
    def heavy_endpoint(self, endpoint):
        @wraps(endpoint)
        async def admitted(request):
            if self.restart.requested:
                return _json_error("Перезапуск уже запрошен.", 409)
            lease = self.heavy.acquire()
            if lease is None:
                return _json_error(
                    "Одновременно допускается только одна тяжёлая операция: "
                    "уже принимается или выполняется загрузка/intake. "
                    "Дождитесь её завершения и повторите запрос.", 409,
                )
            token = self.heavy.current.set(lease)
            try:
                return await endpoint(request)
            finally:
                self.heavy.current.reset(token)
                lease.release()
        return admitted

    def create_intake_service(self) -> IntakeApiService:
        with self.intake_create_lock:
            if self.intake is None:
                self.intake = IntakeApiService.for_registry(self.registry)
            return self.intake

    async def intake_service(self) -> IntakeApiService:
        # Первое создание сервиса закрывает оборванные job прежнего процесса и
        # дочищает их work синхронным rmtree; на гигабайтной выгрузке это
        # секунды, и в event loop они остановили бы все запросы сервера.
        if self.intake is not None:
            return self.intake
        return await run_in_threadpool(self.create_intake_service)

    def intake_task_done(self, task: asyncio.Task[None]) -> None:
        self.intake_tasks.discard(task)
        if not task.cancelled():
            task.exception()

    async def run_intake_work(
        self, service: IntakeApiService, work: IntakeWork
    ) -> None:
        try:
            await self.heavy.run(service.prepare, work)
        except Exception:
            # Application service уже сохранил bounded error в durable job.
            return
