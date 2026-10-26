"""Источники, загрузки и операции полного приёма конфигураций."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from typing import cast
from urllib.parse import quote

from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException
from starlette.requests import Request
from starlette.responses import JSONResponse

from .. import __version__, dashboard_backend, source_ids, tools
from ..dashboard_backend import admin_authorized, session_level
from ..intake_v2 import CandidateStoreError, IntakeV2ContractError
from ..intake_v2_api import (
    IntakeApiConflict,
    IntakeApiError,
    IntakeApiInvalid,
    IntakeApiNotFound,
)
from ..intake_v2_lifecycle import LifecycleError
from ..intake_v2_operations import OperationConflict, OperationError
from ..intake_v2_transport import (
    TransportError,
    TransportLimitError,
    TransportStorageFullError,
)
from ..registry import (
    KIND_SYNTAX,
    RegistryChangedExternally,
    RegistryError,
    SourceInUseError,
    SourcesChanged,
)
from ..runtime_config import DASHBOARD_ON
from .context import (
    DashboardContext,
    Endpoint,
    _json_body,
    _json_error,
    admin_endpoint,
    read_endpoint,
)


def _source_payload(source: tools.SourceStateRow | None) -> dict | None:
    if source is None:
        return None
    return {
        "id": source.id,
        "kind": source.kind,
        "platform": source.platform,
        "items_total": source.items_total,
        "status": source.status,
        "loaded_at": source.loaded_at,
        "code_version": source.code_version,
        "incomplete": source.incomplete,
        "warnings": list(source.warnings),
    }


def _coverage_payload(coverage: tools.CodeCoverage | None) -> dict | None:
    if coverage is None:
        return None
    projection = cast(tools._CodeCoveragePayload, coverage.to_dict())
    structures = dict(projection["form_structures"])
    # HTTP-клиент уже читает unreadable, журнал сохраняет unavailable.
    structures["unreadable"] = structures.pop("unavailable")
    limitations = projection["limitations"]
    return {
        "has_limitations": projection["has_limitations"],
        "modules": projection["modules"],
        "procedures": projection["procedures"],
        "form_structures": structures,
        "form_modules": projection["form_modules"],
        "problems_total": limitations["occurrences_total"],
        "problem_categories": [
            {"category": category, "count": count}
            for category, count in limitations["categories"].items()
        ],
    }

def _sources_payload(
    snapshot: tools.SourcesSnapshot,
    *,
    admin: bool,
) -> dict:
    sources_by_id = {source.id: source for source in snapshot.sources}
    configurations = []
    for configuration in snapshot.configurations:
        corpora = []
        for corpus in configuration.code:
            source = sources_by_id.get(corpus.source_id)
            corpora.append(
                {
                    "id": corpus.source_id
                    or source_ids.modules_id(configuration.name),
                    "label": corpus.corpus,
                    "kind": corpus.kind,
                    "native_generation": corpus.native_generation,
                    "last_successful_load_at": corpus.last_successful_load_at,
                    "phase": corpus.phase,
                    "state": corpus.state,
                    "source": _source_payload(source),
                    "coverage": _coverage_payload(corpus.coverage),
                    "journal": corpus.journal,
                    "journal_url": (
                        "/api/v1/sources/coverage?source_id="
                        + quote(corpus.source_id, safe="")
                        if corpus.journal and corpus.source_id
                        else ""
                    ),
                    # Ключ сохранён ради контракта API: с 4.5.0 индекс кода
                    # строится синхронно и ошибки фоновой сборки нет — всегда пусто.
                    "error": "",
                }
            )
        configurations.append(
            {
                "id": configuration.name,
                "version": configuration.version,
                "platform": configuration.platform,
                "compatibility_mode": configuration.compatibility_mode,
                "source_origin": configuration.source_origin,
                "activation_mode": configuration.activation_mode,
                "activation_status": configuration.activation_status,
                "platform_declaration": configuration.platform_declaration,
                "predefined_available": configuration.predefined_available,
                "syntax_relation": configuration.syntax_relation,
                "objects": configuration.objects,
                "edges": configuration.edges,
                "loaded_at": configuration.loaded_at,
                "last_successful_load_at": configuration.last_successful_load_at,
                "notes": list(configuration.notes),
                "native_generation": configuration.native_generation,
                "source": _source_payload(sources_by_id.get(configuration.name)),
                "corpora": corpora,
            }
        )
    references = [
        _source_payload(source)
        for source in snapshot.sources
        if source.kind == KIND_SYNTAX
    ]
    return {
        "api_version": "v1",
        "permissions": {"read": True, "admin": admin},
        "configurations": configurations,
        "references": references,
    }


def _job_payload(job) -> dict:
    def value(name: str):
        return job[name] if isinstance(job, dict) else getattr(job, name)

    return {
        "name": str(value("name")),
        "size": int(value("size")),
        "state": str(value("state")),
        "error": str(value("error")),
    }


def _admin_sources_payload(prepared) -> dict:
    """Административная часть снимка без второго обхода живого Registry."""
    configurations = list(prepared.sources.configuration_names)
    incoming = []
    for row in prepared.incoming:
        incoming.append(
            {
                "name": row.name,
                "size": row.size,
                "state": row.state,
                "detail": row.detail,
                "settling": row.settling,
            }
        )
    return {
        "api_version": "v1",
        "limits": {"upload_bytes": dashboard_backend.MAX_UPLOAD},
        "configuration_names": configurations,
        "jobs": [_job_payload(job) for job in reversed(prepared.jobs)],
        "incoming": incoming,
        "incoming_exists": prepared.incoming_exists,
        # В интерфейсе важен переносимый путь тома, а не абсолютный путь хоста.
        "incoming_dir": "data/incoming/",
        "orphans": [
            {"path": row.relative, "size": row.size}
            for row in prepared.orphans
        ],
        "snapshot_error": prepared.sources_error,
    }


def _copy_upload(source, target: Path, job: dict) -> int:
    """Перенести принятый multipart-файл в каталог задания, соблюдая предел."""
    source.seek(0)
    size = 0
    with target.open("wb") as output:
        while True:
            chunk = source.read(dashboard_backend.CHUNK)
            if not chunk:
                break
            size += len(chunk)
            job["size"] = size
            if size > dashboard_backend.MAX_UPLOAD:
                raise dashboard_backend.UploadTooLarge
            output.write(chunk)
    return size


# Ответы intake-обработчиков на ошибки сервиса: (статус, типы) по порядку,
# первое совпадение выигрывает — как цепочка `except`. Таблицы собраны в
# одном месте, чтобы расхождения между обработчиками были видны рядом, а не
# прятались в четырёх копиях. Расхождения намеренно сохранены: это
# наблюдаемый HTTP-контракт, а исключение, которого нет в таблице,
# по-прежнему уходит наружу как 500.
_IntakeErrorTable = tuple[tuple[int, tuple[type[BaseException], ...]], ...]

_INTAKE_READ_ERRORS: _IntakeErrorTable = (
    (
        409,
        (
            IntakeApiError,
            LifecycleError,
            OperationError,
            RegistryError,
            CandidateStoreError,
        ),
    ),
)
_INTAKE_START_ERRORS: _IntakeErrorTable = (
    (404, (IntakeApiNotFound,)),
    (409, (IntakeApiConflict, LifecycleError, CandidateStoreError)),
    (422, (IntakeApiError, IntakeV2ContractError)),
)
_INTAKE_JOB_ERRORS: _IntakeErrorTable = (
    (404, (IntakeApiNotFound,)),
    (422, (IntakeApiInvalid, IntakeV2ContractError)),
    (
        409,
        (IntakeApiError, LifecycleError, OperationError, CandidateStoreError),
    ),
)
_INTAKE_CONFIRM_ERRORS: _IntakeErrorTable = (
    (404, (IntakeApiNotFound,)),
    (
        409,
        (IntakeApiConflict, OperationConflict, CandidateStoreError, RegistryChangedExternally),
    ),
    (
        422,
        (IntakeApiError, LifecycleError, OperationError, IntakeV2ContractError),
    ),
)
_INTAKE_DISCARD_ERRORS: _IntakeErrorTable = (
    (404, (IntakeApiNotFound,)),
    (409, (IntakeApiConflict, CandidateStoreError)),
    (
        422,
        (IntakeApiError, LifecycleError, OperationError, IntakeV2ContractError),
    ),
)
_INTAKE_DELETE_CANDIDATE_ERRORS: _IntakeErrorTable = (
    (404, (IntakeApiNotFound,)),
    (409, (IntakeApiConflict, CandidateStoreError)),
    (422, (IntakeApiError, LifecycleError, OperationError)),
)


def _intake_error_response(
    error: Exception, table: _IntakeErrorTable
) -> JSONResponse:
    """JSON-ответ по таблице; неизвестную ошибку пробрасывает дальше.

    Вызывается внутри `except`; повторный `raise` того же объекта сохраняет
    его traceback, так что необработанная ошибка выглядит как раньше.
    """
    for status, types in table:
        if isinstance(error, types):
            return _json_error(str(error), status)
    raise error


def handlers(context: DashboardContext) -> dict[str, Endpoint]:
    registry = context.registry
    reference = context.reference
    restart = context.restart
    heavy = context.heavy
    role_tools_refresh = context.role_tools_refresh
    intake_service = context.intake_service
    intake_start_lock = context.intake_start_lock
    intake_tasks = context.intake_tasks
    intake_task_done = context.intake_task_done
    run_intake_work = context.run_intake_work
    heavy_endpoint = context.heavy_endpoint

    @read_endpoint
    async def bootstrap(request: Request) -> JSONResponse:
        def counts() -> tuple[int, int, int, int]:
            # `snapshot()` ждёт `_lock` Registry, который публикация держит и
            # на fsync, — поэтому в потоке, а не в event loop.
            snapshot = registry.snapshot()
            return (
                len(snapshot.configurations),
                sum(len(loaded.config) for loaded in snapshot.configurations.values()),
                # Native-корпус кода не имеет строки учёта KIND_MODULES, а
                # легаси-строка recovery может ещё не иметь загруженного
                # пакета. `snapshot.modules` — объединение обоих по одному
                # ключу (`<конфигурация>:modules`, `<конфигурация>:ext:<имя>`),
                # поэтому каждый корпус считается ровно один раз.
                len(snapshot.modules),
                len(snapshot.syntax_versions),
            )

        (
            configurations,
            metadata_objects,
            code_corpora,
            reference_sources,
        ) = await run_in_threadpool(counts)
        return JSONResponse(
            {
                "api_version": "v1",
                "dashboard_mode": DASHBOARD_ON,
                "server": {"status": "ok", "version": __version__},
                "permissions": {
                    "read": True,
                    "admin": admin_authorized(request),
                },
                "authentication": {
                    "read_required": bool(os.environ.get("API_TOKEN", "")),
                    "admin_available": bool(os.environ.get("ADMIN_TOKEN", "")),
                    "session_level": session_level(request),
                },
                "summary": {
                    "configurations": configurations,
                    "metadata_objects": metadata_objects,
                    "code_corpora": code_corpora,
                    "reference_sources": reference_sources,
                },
            }
        )

    @read_endpoint
    async def sources_api(request: Request) -> JSONResponse:
        try:
            snapshot = await run_in_threadpool(tools.sources_snapshot, registry)
        except SourcesChanged as error:
            # Публикация нового поколения может дважды обогнать CAS снимка.
            # Это ожидаемый конфликт чтения, а не авария сервера.
            return _json_error(str(error), 409)
        return JSONResponse(
            _sources_payload(snapshot, admin=admin_authorized(request))
        )

    @admin_endpoint("Изменение версии платформы", mutation=True)
    async def platform_set_api(request: Request) -> JSONResponse:
        body = await _json_body(request)
        configuration = body.get("configuration")
        version = body.get("platform_version")
        if not isinstance(configuration, str) or not configuration:
            return _json_error("configuration должен быть непустым.", 422)
        if not isinstance(version, str):
            return _json_error("platform_version должен быть строкой.", 422)
        try:
            declaration = await run_in_threadpool(
                registry.set_platform_version, configuration, version
            )
        except (RegistryError, ValueError) as error:
            return _json_error(str(error), 422)
        return JSONResponse({"configuration": configuration, **declaration.to_dict()})

    @admin_endpoint("Изменение версии платформы", mutation=True)
    async def platform_clear_api(request: Request) -> JSONResponse:
        body = await _json_body(request)
        configuration = body.get("configuration")
        if not isinstance(configuration, str) or not configuration:
            return _json_error("configuration должен быть непустым.", 422)
        try:
            await run_in_threadpool(registry.clear_platform_version, configuration)
        except RegistryError as error:
            return _json_error(str(error), 422)
        return JSONResponse({"configuration": configuration, "status": "unknown"})

    @admin_endpoint("Управление источниками")
    async def admin_sources_api(request: Request) -> JSONResponse:
        prepared = await run_in_threadpool(
            dashboard_backend.prepare_sources_page,
            registry,
            authorized=True,
        )
        payload = _admin_sources_payload(prepared)
        payload["reference"] = reference.payload(detailed=True)
        payload["runtime"] = {"self_restart": restart.enabled}
        return JSONResponse(payload)

    @admin_endpoint("Просмотр подключённых каталогов")
    async def directory_sources_api(request: Request) -> JSONResponse:
        try:
            return JSONResponse(await run_in_threadpool((await intake_service()).directory_sources))
        except IntakeApiError as error:
            return _json_error(str(error), 409)

    @admin_endpoint("Управление подключёнными каталогами", mutation=True)
    @heavy_endpoint
    async def directory_change_api(request: Request) -> JSONResponse:
        operation = request.path_params["operation"]
        if operation not in {"bind", "unbind", "refresh"}:
            return _json_error("Неизвестная операция с каталогом.", 404)
        payload = await _json_body(request)
        allowed = {"configuration", "source_id"} if operation == "bind" else {"configuration"}
        if set(payload) != allowed or not all(isinstance(v, str) and v for v in payload.values()):
            return _json_error("Нужны строковые поля configuration и, для привязки, source_id.", 422)
        service = (await intake_service())
        try:
            async with intake_start_lock:
                if operation == "bind":
                    result = await heavy.run(service.bind_directory, payload["configuration"], payload["source_id"])
                elif operation == "unbind":
                    result = await heavy.run(service.unbind_directory, payload["configuration"])
                else:
                    result = {"candidate": await heavy.run(service.refresh_directory, payload["configuration"])}
        except (IntakeApiError, LifecycleError) as error:
            return _json_error(str(error), 409)
        return JSONResponse(result)

    @admin_endpoint("Просмотр кандидатов")
    async def intake_snapshot_api(request: Request) -> JSONResponse:
        try:
            payload = await run_in_threadpool((await intake_service()).snapshot)
        except Exception as error:
            return _intake_error_response(error, _INTAKE_READ_ERRORS)
        return JSONResponse(payload)

    @admin_endpoint("Загрузка кандидата", mutation=True)
    @heavy_endpoint
    async def intake_upload_api(request: Request) -> JSONResponse:
        service = (await intake_service())
        try:
            form = await dashboard_backend.limited_upload_form(
                request,
                service.lifecycle.browser.max_upload_bytes,
                allowed_fields=frozenset(),
                file_factory=service.lifecycle.browser._new_upload_writer,
            )
        except dashboard_backend.UploadTooLarge:
            limit = service.lifecycle.browser.max_upload_bytes // 1024 // 1024
            return _json_error(f"Файл больше {limit} МБ.", 413)
        except dashboard_backend.UploadStorageFull:
            return _json_error("Недостаточно места для временного файла загрузки.", 507)
        except MultiPartException:
            return _json_error(
                "Некорректная multipart-форма: разрешён один ZIP в поле `file`.",
                400,
            )
        uploaded = form.get("file")
        if not isinstance(uploaded, UploadFile) or not uploaded.filename:
            await form.close()
            return _json_error("Файл не выбран.", 400)
        name = Path(uploaded.filename).name
        if Path(name).suffix.lower() != ".zip":
            await form.close()
            return _json_error("Принимается только ZIP файловой выгрузки.", 400)
        try:
            await uploaded.seek(0)
            candidate = await heavy.run(
                service.accept_upload,
                name,
                uploaded.file,
                expected_size=uploaded.size,
            )
        except TransportStorageFullError as error:
            return _json_error(str(error), 507)
        except TransportLimitError as error:
            return _json_error(str(error), 413)
        except (LifecycleError, TransportError, ValueError) as error:
            return _json_error(str(error), 422)
        finally:
            await form.close()
        return JSONResponse({"candidate": candidate}, status_code=201)

    @admin_endpoint("Запуск разбора", mutation=True)
    @heavy_endpoint
    async def intake_start_api(request: Request) -> JSONResponse:
        payload = await _json_body(request)
        allowed = {"candidate_id", "action", "job_id", "parent_configuration"}
        if set(payload) - allowed:
            return _json_error("Получены неизвестные поля запроса.", 422)
        candidate_id = payload.get("candidate_id")
        action = payload.get("action")
        job_id = payload.get("job_id", "")
        parent = payload.get("parent_configuration", "")
        if not all(
            isinstance(value, str)
            for value in (candidate_id, action, job_id, parent)
        ):
            return _json_error("Поля запроса должны быть строками.", 422)
        service = (await intake_service())
        async with intake_start_lock:
            if any(not task.done() for task in intake_tasks):
                return _json_error(
                    "Одновременно выполняется только одна тяжёлая операция intake.",
                    409,
                )
            try:
                work = await heavy.run(
                    service.start,
                    candidate_id,
                    action,
                    job_id=job_id,
                    parent_configuration=parent,
                )
            except Exception as error:
                return _intake_error_response(error, _INTAKE_START_ERRORS)
            task = heavy.spawn(run_intake_work(service, work))
            intake_tasks.add(task)
            task.add_done_callback(intake_task_done)
        return JSONResponse(
            {"job": service.job_payload(work.job_id)},
            status_code=202,
        )

    @admin_endpoint("Просмотр сохранённых операций")
    async def intake_jobs_api(request: Request) -> JSONResponse:
        try:
            return JSONResponse(await run_in_threadpool((await intake_service()).jobs_snapshot))
        except Exception as error:
            return _intake_error_response(error, _INTAKE_READ_ERRORS)

    @admin_endpoint("Просмотр операции")
    async def intake_job_api(request: Request) -> JSONResponse:
        try:
            payload = await run_in_threadpool(
                (await intake_service()).job_payload,
                request.path_params.get("job_id", ""),
            )
        except Exception as error:
            return _intake_error_response(error, _INTAKE_JOB_ERRORS)
        return JSONResponse({"job": payload})

    @admin_endpoint("Публикация preview", mutation=True)
    @heavy_endpoint
    async def intake_confirm_api(request: Request) -> JSONResponse:
        payload = await _json_body(request)
        if set(payload) != {"job_id"} or not isinstance(
            payload.get("job_id"), str
        ):
            return _json_error("Нужен единственный строковый job_id.", 422)
        service = (await intake_service())
        async with intake_start_lock:
            if any(not task.done() for task in intake_tasks):
                return _json_error(
                    "Нельзя публиковать preview во время другой тяжёлой операции.",
                    409,
                )
            try:
                try:
                    job = await heavy.run(service.confirm, payload["job_id"])
                    response: JSONResponse = JSONResponse({"job": job})
                except Exception as error:
                    response = _intake_error_response(
                        error, _INTAKE_CONFIRM_ERRORS
                    )
            finally:
                # Публикация и последующая очистка (browser ZIP, payload
                # ответа) — разные шаги, и упасть может любой, в том числе
                # необработанным OSError или RegistryError. Какой бы ни был
                # исход, поколение могло смениться: каталог инструментов
                # ролей сверяется всегда. Сверка дешёвая и идемпотентная.
                if role_tools_refresh is not None:
                    await role_tools_refresh()
        return response

    @admin_endpoint("Отмена preview", mutation=True)
    @heavy_endpoint
    async def intake_discard_api(request: Request) -> JSONResponse:
        payload = await _json_body(request)
        if set(payload) != {"job_id"} or not isinstance(
            payload.get("job_id"), str
        ):
            return _json_error("Нужен единственный строковый job_id.", 422)
        async with intake_start_lock:
            if any(not task.done() for task in intake_tasks):
                return _json_error(
                    "Нельзя отменять preview во время другой тяжёлой операции.",
                    409,
                )
            try:
                result = await heavy.run(
                    (await intake_service()).discard,
                    payload["job_id"],
                )
            except Exception as error:
                return _intake_error_response(error, _INTAKE_DISCARD_ERRORS)
        return JSONResponse(result)

    @admin_endpoint("Удаление браузерного ZIP", mutation=True)
    @heavy_endpoint
    async def intake_delete_candidate_api(request: Request) -> JSONResponse:
        payload = await _json_body(request)
        if set(payload) != {"candidate_id"} or not isinstance(
            payload.get("candidate_id"), str
        ):
            return _json_error("Нужен единственный строковый candidate_id.", 422)
        async with intake_start_lock:
            if any(not task.done() for task in intake_tasks):
                return _json_error(
                    "Нельзя удалять ZIP во время другой тяжёлой операции.", 409
                )
            try:
                result = await heavy.run(
                    (await intake_service()).remove_browser_candidate,
                    payload["candidate_id"],
                )
            except Exception as error:
                return _intake_error_response(
                    error, _INTAKE_DELETE_CANDIDATE_ERRORS
                )
        return JSONResponse(result)

    @admin_endpoint("Загрузка", mutation=True)
    @heavy_endpoint
    async def upload_source_api(request: Request) -> JSONResponse:
        try:
            form = await dashboard_backend.limited_upload_form(request)
        except dashboard_backend.UploadTooLarge:
            return _json_error(
                f"Файл больше {dashboard_backend.MAX_UPLOAD // 1024 // 1024} МБ.",
                413,
            )
        except dashboard_backend.UploadStorageFull:
            return _json_error("Недостаточно места для временного файла загрузки.", 507)
        except MultiPartException:
            return _json_error(
                "Некорректная multipart-форма: разрешён один файл `file`.",
                400,
            )

        uploaded = form.get("file")
        if not isinstance(uploaded, UploadFile) or not uploaded.filename:
            await form.close()
            return _json_error("Файл не выбран.", 400)

        name = Path(uploaded.filename).name
        suffix = Path(name).suffix.lower()
        if suffix not in (".zip", ".hbk"):
            await form.close()
            return _json_error("Принимаются только .zip и .hbk.", 400)

        directory = None
        job = None
        handed_off = False
        try:
            directory = tempfile.mkdtemp()
            target = Path(directory) / name
            job = dashboard_backend.start_job(name, 0)
            # Копирование до 500 МБ из spool в файл — синхронный дисковый I/O.
            # В event loop оно на медленном томе останавливало `/health` и MCP.
            # Поток не прерывается отменой запроса: `finally` ниже закроет
            # spool только после того, как копирование вернёт управление.
            await run_in_threadpool(
                _copy_upload, uploaded.file, target, job
            )
            task = heavy.spawn(
                heavy.run(
                    dashboard_backend.run_job,
                    registry, job, directory, target, suffix,
                )
            )
            handed_off = True
            dashboard_backend.ФОНОВЫЕ.add(task)
            task.add_done_callback(dashboard_backend.ФОНОВЫЕ.discard)
            return JSONResponse({"job": _job_payload(job)}, status_code=202)
        except dashboard_backend.UploadTooLarge:
            return _json_error(
                f"Файл больше {dashboard_backend.MAX_UPLOAD // 1024 // 1024} МБ.",
                413,
            )
        except OSError as error:
            return _json_error(
                dashboard_backend.storage_error_text("Не удалось принять файл", error),
                500,
            )
        finally:
            # До handoff каталог принадлежит запросу, после — worker. Отмена
            # чтения не должна оставить ни частичный файл, ни вечную job.
            if not handed_off:
                if directory is not None:
                    shutil.rmtree(directory, ignore_errors=True)
                if job is not None and job in dashboard_backend.JOBS:
                    dashboard_backend.JOBS.remove(job)
            uploaded.file.close()

    @admin_endpoint("Разбор", mutation=True)
    @heavy_endpoint
    async def parse_incoming_api(request: Request) -> JSONResponse:
        return _json_error(
            "Частичная загрузка Source B отключена; используйте полный intake update_full.",
            410,
        )

    @admin_endpoint("Очистка журнала", mutation=True)
    async def clear_jobs_api(request: Request) -> JSONResponse:
        completed = [
            job
            for job in dashboard_backend.JOBS
            if job["state"]
            in (dashboard_backend.JOB_DONE, dashboard_backend.JOB_FAILED)
        ]
        for job in completed:
            dashboard_backend.JOBS.remove(job)
        return JSONResponse({"cleared": len(completed)})

    @admin_endpoint("Удаление", mutation=True)
    async def remove_source_api(request: Request) -> JSONResponse:
        payload = await _json_body(request)
        source_id = str(payload.get("id", ""))
        if not source_id or payload.get("confirmation") != source_id:
            return _json_error("Не подтверждено точное имя источника.", 400)
        configuration = source_ids.configuration_of_modules(source_id)
        async with intake_start_lock:
            if any(not task.done() for task in intake_tasks):
                return _json_error(
                    "Нельзя удалять конфигурацию во время операции intake.",
                    409,
                )
            try:
                service = (
                    (await intake_service())
                    if context.intake is not None
                    or (registry.data_dir / "intake-v2").exists()
                    else None
                )
                has_intake_jobs = bool(
                    service is not None
                    and await run_in_threadpool(
                        service.has_configuration_jobs,
                        configuration,
                    )
                )
                if has_intake_jobs:
                    await run_in_threadpool(
                        service.ensure_configuration_purgeable,
                        configuration,
                    )
                removed = await run_in_threadpool(
                    registry.remove_if_registered, source_id, missing_ok=has_intake_jobs,
                )
                if removed:
                    await run_in_threadpool(registry.save)
                if has_intake_jobs:
                    await run_in_threadpool(
                        service.purge_configuration,
                        configuration,
                    )
                if service is not None and configuration not in (
                    await run_in_threadpool(registry.snapshot)
                ).configuration_names:
                    await run_in_threadpool(service.unbind_directory, configuration)
            except RegistryError as error:
                return _json_error(str(error), 400)
            except (
                IntakeApiError,
                LifecycleError,
                OperationError,
                CandidateStoreError,
            ) as error:
                return _json_error(str(error), 409)
            finally:
                if role_tools_refresh is not None:
                    await role_tools_refresh()
        return JSONResponse({"removed": source_id})

    @admin_endpoint("Удаление файла", mutation=True)
    async def forget_source_api(request: Request) -> JSONResponse:
        payload = await _json_body(request)
        given = str(payload.get("path", ""))
        if not given or payload.get("confirmation") != given:
            return _json_error("Не подтверждено точное имя файла.", 400)
        try:
            await run_in_threadpool(registry.forget_source, given)
        except SourceInUseError as error:
            return _json_error(str(error), 409)
        except FileNotFoundError:
            return _json_error("Такого неиспользуемого файла нет.", 404)
        except RegistryError as error:
            return _json_error(str(error), 400)
        except OSError as error:
            return _json_error(
                dashboard_backend.storage_error_text("Не удалось удалить файл", error),
                400,
            )
        return JSONResponse({"forgotten": given})

    return {
        "bootstrap": bootstrap,
        "sources_api": sources_api,
        "platform_set_api": platform_set_api,
        "platform_clear_api": platform_clear_api,
        "admin_sources_api": admin_sources_api,
        "directory_sources_api": directory_sources_api,
        "directory_change_api": directory_change_api,
        "intake_snapshot_api": intake_snapshot_api,
        "intake_upload_api": intake_upload_api,
        "intake_start_api": intake_start_api,
        "intake_jobs_api": intake_jobs_api,
        "intake_job_api": intake_job_api,
        "intake_confirm_api": intake_confirm_api,
        "intake_discard_api": intake_discard_api,
        "intake_delete_candidate_api": intake_delete_candidate_api,
        "upload_source_api": upload_source_api,
        "parse_incoming_api": parse_incoming_api,
        "clear_jobs_api": clear_jobs_api,
        "remove_source_api": remove_source_api,
        "forget_source_api": forget_source_api,
    }
