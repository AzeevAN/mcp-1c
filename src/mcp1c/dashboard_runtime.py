"""Переключаемая оболочка дашборда: off, classic или React SPA.

Предметные данные и запись остаются в том же процессе ``Registry``. React
получает только HTTP API и никогда не монтирует ``data/`` самостоятельно.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from urllib.parse import quote, urlencode

from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException
from starlette.requests import Request
from starlette.responses import (
    FileResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
)
from starlette.routing import Route

from . import coverage_log, dashboard as classic_dashboard, tools
from .dashboard import _authorized, _csrf_denied, _session_level, can_read
from .registry import (
    KIND_EXTENSION,
    KIND_MODULES,
    KIND_QUERY,
    KIND_SYNTAX,
    Registry,
    RegistryError,
)

DASHBOARD_OFF = "off"
DASHBOARD_CLASSIC = "classic"
DASHBOARD_SPA = "spa"
DASHBOARD_MODES = (DASHBOARD_OFF, DASHBOARD_CLASSIC, DASHBOARD_SPA)

SPA_PAGE_PATHS = (
    "/",
    "/login",
    "/sources",
    "/queries",
    "/graph",
    "/dictionary",
    "/object",
    "/syntax",
)


class DashboardModeError(ValueError):
    """В окружении указан неизвестный режим дашборда."""


def dashboard_mode() -> str:
    """Прочитать режим один раз при сборке приложения.

    ``classic`` остаётся значением по умолчанию на переходный период: обычное
    обновление сервера не должно неожиданно убирать знакомый интерфейс.
    """
    mode = os.environ.get("MCP1C_DASHBOARD", DASHBOARD_CLASSIC).strip().lower()
    if mode not in DASHBOARD_MODES:
        raise DashboardModeError(
            "MCP1C_DASHBOARD должен быть одним из: off, classic, spa. "
            f"Получено: {mode or '<пусто>'}."
        )
    return mode


def _package_version() -> str:
    try:
        return version("mcp1c")
    except PackageNotFoundError:  # pragma: no cover - только запуск из исходников
        return "0.0.0+local"


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
    return {
        "has_limitations": coverage.has_limitations,
        "modules": {
            "total": coverage.modules_total,
            "source_available": coverage.modules_source_available,
            "empty": coverage.modules_empty,
            "partial": coverage.modules_partial,
            "unreadable": coverage.modules_unreadable,
            "conflict": coverage.modules_conflict,
            "compiled_without_source": coverage.modules_compiled_without_source,
        },
        "procedures": {
            "total": coverage.procedures_total,
            "full": coverage.procedures_full,
            "partial": coverage.procedures_partial,
        },
        "form_structures": {
            "total": coverage.forms_total,
            "full": coverage.form_structures_full,
            "partial": coverage.form_structures_partial,
            "unreadable": coverage.form_structures_unread,
        },
        "form_modules": {
            "total": coverage.forms_total,
            "read": coverage.form_modules_read,
            "empty": coverage.form_modules_empty,
            "missing": coverage.form_modules_missing,
            "unreadable": coverage.form_modules_unread,
        },
        "problems_total": coverage.problems_total,
        "problem_categories": [
            {"category": category, "count": count}
            for category, count in coverage.problem_categories
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
                    "id": corpus.source_id or f"{configuration.name}:modules",
                    "label": corpus.corpus,
                    "kind": source.kind if source is not None else KIND_MODULES,
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
                }
            )
        configurations.append(
            {
                "id": configuration.name,
                "version": configuration.version,
                "platform": configuration.platform,
                "objects": configuration.objects,
                "edges": configuration.edges,
                "loaded_at": configuration.loaded_at,
                "notes": list(configuration.notes),
                "source": _source_payload(sources_by_id.get(configuration.name)),
                "corpora": corpora,
            }
        )
    references = [
        _source_payload(source)
        for source in snapshot.sources
        if source.kind in (KIND_SYNTAX, KIND_QUERY)
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
    from .incoming import (
        STATE_FAILED,
        STATE_NEW,
        STATE_STALE,
        STATE_UPDATED,
    )

    actionable = {STATE_NEW, STATE_UPDATED, STATE_STALE, STATE_FAILED}
    configurations = list(prepared.sources.configuration_names)
    incoming = []
    for row in prepared.incoming:
        can_parse = (
            row.state in actionable
            and not row.settling
            and bool(configurations)
        )
        incoming.append(
            {
                "name": row.name,
                "size": row.size,
                "state": row.state,
                "detail": row.detail,
                "settling": row.settling,
                "can_parse": can_parse,
                "action": (
                    "reparse"
                    if row.state in (STATE_UPDATED, STATE_STALE)
                    else "parse"
                ),
            }
        )
    return {
        "api_version": "v1",
        "limits": {"upload_bytes": classic_dashboard.MAX_UPLOAD},
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


def _json_error(message: str, status_code: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status_code)


_QUERY_SCOPE_LABELS = {
    "objects": "Объекты",
    "fields": "Реквизиты",
    "syntax": "Справка и язык запросов",
}


def _queries_setup_payload(registry: Registry) -> dict:
    """Стабильные настройки формы и доступность живых поисковых корпусов."""
    snapshot = registry.snapshot()
    names = list(snapshot.configuration_names)
    return {
        "api_version": "v1",
        "configuration_names": names,
        "default_configuration": names[0] if names else "",
        "scopes": [
            {
                "id": scope,
                "label": _QUERY_SCOPE_LABELS[scope],
                "requires_configuration": scope != "syntax",
            }
            for scope in classic_dashboard.SCOPES
        ],
        "limits": {
            "phrases": classic_dashboard.MAX_QUERY_PHRASES,
            "phrase_chars": classic_dashboard.MAX_QUERY_CHARS,
            "results_per_phrase": 5,
        },
        "availability": {
            "configurations": bool(names),
            "syntax": snapshot.syntax is not None,
            "query_language": snapshot.query_source is not None,
        },
    }


def _query_results_payload(
    config: str,
    scope: str,
    phrases: list[str],
    results: list[tuple[str, list, list]],
) -> dict:
    """Перевести результат общего SearchIndex в JSON без пересчёта выдачи."""
    serialized = []
    for phrase, hits, hidden in results:
        alias_url = None
        if scope != "syntax":
            alias_url = (
                f"/dictionary?config={quote(config)}&phrase={quote(phrase)}"
            )
        serialized.append(
            {
                "phrase": phrase,
                "alias_url": alias_url,
                "hits": [
                    {
                        "position": position,
                        "id": hit.doc.id,
                        "title": (
                            getattr(hit.doc.payload, "address", "") or hit.doc.id
                            if scope == "syntax"
                            else hit.doc.id
                        ),
                        "kind": classic_dashboard._kind_title(
                            scope, hit.doc.kind
                        ),
                        "score": hit.score,
                        "reason": hit.reason,
                        "card_url": classic_dashboard._card_link(
                            scope, config, hit
                        ),
                    }
                    for position, hit in enumerate(hits, start=1)
                ],
                "hidden": [
                    {
                        "title": getattr(hit.doc.payload, "address", "")
                        or hit.doc.id,
                        "reason": classic_dashboard._hidden_reason(
                            hit.doc.payload
                        ),
                    }
                    for hit in hidden
                ],
            }
        )
    return {
        "api_version": "v1",
        "request": {"config": config, "scope": scope, "phrases": phrases},
        "results": serialized,
    }


def _admin_denied(request: Request, *, action: str) -> JSONResponse | None:
    if not os.environ.get("ADMIN_TOKEN", ""):
        return _json_error(f"{action} выключено: не задан ADMIN_TOKEN.", 404)
    if not _authorized(request):
        return _json_error("Нужен вход администратора.", 403)
    return None


def _mutation_denied(request: Request, *, action: str) -> JSONResponse | None:
    denied = _admin_denied(request, action=action)
    if denied is not None:
        return denied
    csrf = _csrf_denied(request)
    if csrf is not None:
        return _json_error("Запрос отклонён проверкой источника.", csrf.status_code)
    return None


async def _json_body(request: Request) -> dict:
    try:
        payload = await request.json()
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _spa_routes(registry: Registry, static_dir: Path) -> list[Route]:
    static_dir = static_dir.resolve()

    async def bootstrap(request: Request) -> JSONResponse:
        if not can_read(request):
            return JSONResponse(
                {"error": "Нужен токен чтения."},
                status_code=401,
            )
        snapshot = registry.snapshot()
        metadata_objects = sum(
            len(loaded.config) for loaded in snapshot.configurations.values()
        )
        code_corpora = sum(
            source.kind in (KIND_MODULES, KIND_EXTENSION)
            for source in snapshot.sources.values()
        )
        return JSONResponse(
            {
                "api_version": "v1",
                "dashboard_mode": DASHBOARD_SPA,
                "server": {"status": "ok", "version": _package_version()},
                "permissions": {
                    "read": True,
                    "admin": _authorized(request),
                },
                "authentication": {
                    "read_required": bool(os.environ.get("API_TOKEN", "")),
                    "admin_available": bool(os.environ.get("ADMIN_TOKEN", "")),
                    "session_level": _session_level(request),
                },
                "summary": {
                    "configurations": len(snapshot.configurations),
                    "metadata_objects": metadata_objects,
                    "code_corpora": code_corpora,
                    "reference_sources": len(snapshot.syntax_versions)
                    + int(snapshot.query_source is not None),
                },
            }
        )

    async def sources_api(request: Request) -> JSONResponse:
        if not can_read(request):
            return JSONResponse({"error": "Нужен токен чтения."}, status_code=401)
        snapshot = await run_in_threadpool(tools.sources_snapshot, registry)
        return JSONResponse(
            _sources_payload(snapshot, admin=_authorized(request))
        )

    async def queries_setup_api(request: Request) -> JSONResponse:
        if not can_read(request):
            return _json_error("Нужен токен чтения.", 401)
        payload = await run_in_threadpool(_queries_setup_payload, registry)
        return JSONResponse(payload)

    async def queries_run_api(request: Request) -> JSONResponse:
        if not can_read(request):
            return _json_error("Нужен токен чтения.", 401)
        payload = await _json_body(request)
        config = payload.get("config", "")
        scope = payload.get("scope", "")
        raw_phrases = payload.get("phrases")
        if not isinstance(config, str) or not isinstance(scope, str):
            return _json_error("config и scope должны быть строками.", 422)
        if scope not in classic_dashboard.SCOPES:
            return _json_error(f"Неизвестная область поиска: {scope or '<пусто>'}.", 422)
        if not isinstance(raw_phrases, list) or not all(
            isinstance(phrase, str) for phrase in raw_phrases
        ):
            return _json_error("phrases должен быть списком строк.", 422)
        phrases = [phrase.strip() for phrase in raw_phrases if phrase.strip()]
        if not phrases:
            return _json_error("Не указано ни одной фразы.", 422)
        if len(phrases) > classic_dashboard.MAX_QUERY_PHRASES:
            return _json_error(
                "За один прогон принимается не более "
                f"{classic_dashboard.MAX_QUERY_PHRASES} фраз.",
                422,
            )
        if any(
            len(phrase) > classic_dashboard.MAX_QUERY_CHARS
            for phrase in phrases
        ):
            return _json_error(
                "Каждая поисковая фраза должна содержать не более "
                f"{classic_dashboard.MAX_QUERY_CHARS} символов.",
                422,
            )
        try:
            results = await run_in_threadpool(
                classic_dashboard._run_queries,
                registry,
                config or None,
                scope,
                phrases,
            )
        except RegistryError as error:
            return _json_error(str(error), 409)
        except ValueError as error:
            return _json_error(str(error), 422)
        return JSONResponse(_query_results_payload(config, scope, phrases, results))

    async def coverage_api(request: Request) -> JSONResponse:
        if not can_read(request):
            return JSONResponse({"error": "Нужен токен чтения."}, status_code=401)
        source_id = request.query_params.get("source_id", "")
        if not source_id:
            return JSONResponse(
                {"error": "Не указан source_id."}, status_code=400
            )
        snapshot = registry.snapshot()
        source = snapshot.sources.get(source_id)
        if source is None or source.kind not in (KIND_MODULES, KIND_EXTENSION):
            return JSONResponse({"error": "Журнал не найден."}, status_code=404)
        payload = await run_in_threadpool(
            coverage_log.load_current, registry.data_dir, source
        )
        if payload is None:
            return JSONResponse(
                {"error": "Актуальный журнал недоступен."}, status_code=404
            )
        return JSONResponse(payload)

    async def admin_sources_api(request: Request) -> JSONResponse:
        denied = _admin_denied(request, action="Управление источниками")
        if denied is not None:
            return denied
        prepared = await run_in_threadpool(
            classic_dashboard._prepare_sources_page,
            registry,
            authorized=True,
        )
        return JSONResponse(_admin_sources_payload(prepared))

    async def upload_source_api(request: Request) -> JSONResponse:
        denied = _mutation_denied(request, action="Загрузка")
        if denied is not None:
            return denied
        try:
            form = await classic_dashboard._limited_upload_form(request)
        except classic_dashboard._UploadTooLarge:
            return _json_error(
                f"Файл больше {classic_dashboard.MAX_UPLOAD // 1024 // 1024} МБ.",
                413,
            )
        except MultiPartException:
            return _json_error(
                "Некорректная multipart-форма: разрешены один файл `file` "
                "и флаг `allow_truncated`.",
                400,
            )

        uploaded = form.get("file")
        allow_truncated = str(form.get("allow_truncated", "")) == "1"
        if not isinstance(uploaded, UploadFile) or not uploaded.filename:
            await form.close()
            return _json_error("Файл не выбран.", 400)

        name = Path(uploaded.filename).name
        suffix = Path(name).suffix.lower()
        if suffix not in (".zip", ".hbk"):
            await form.close()
            return _json_error("Принимаются только .zip и .hbk.", 400)

        directory = tempfile.mkdtemp()
        target = Path(directory) / name
        job = classic_dashboard._start_job(name, 0)
        try:
            size = 0
            with target.open("wb") as output:
                while True:
                    chunk = await uploaded.read(classic_dashboard.CHUNK)
                    if not chunk:
                        break
                    size += len(chunk)
                    job["size"] = size
                    if size > classic_dashboard.MAX_UPLOAD:
                        raise classic_dashboard._UploadTooLarge
                    output.write(chunk)
        except classic_dashboard._UploadTooLarge:
            shutil.rmtree(directory, ignore_errors=True)
            classic_dashboard._JOBS.remove(job)
            await form.close()
            return _json_error(
                f"Файл больше {classic_dashboard.MAX_UPLOAD // 1024 // 1024} МБ.",
                413,
            )
        except OSError as error:
            shutil.rmtree(directory, ignore_errors=True)
            classic_dashboard._JOBS.remove(job)
            await form.close()
            return _json_error(f"Не удалось принять файл: {error}", 500)
        await form.close()

        task = asyncio.create_task(
            run_in_threadpool(
                classic_dashboard._run_job,
                registry,
                job,
                directory,
                target,
                suffix,
                allow_truncated=allow_truncated,
            )
        )
        classic_dashboard._ФОНОВЫЕ.add(task)
        task.add_done_callback(classic_dashboard._ФОНОВЫЕ.discard)
        return JSONResponse({"job": _job_payload(job)}, status_code=202)

    async def parse_incoming_api(request: Request) -> JSONResponse:
        denied = _mutation_denied(request, action="Разбор")
        if denied is not None:
            return denied

        from . import intake
        from .incoming import SETTLE_SECONDS

        payload = await _json_body(request)
        raw_name = str(payload.get("name", ""))
        name = Path(raw_name).name
        if not name or name != raw_name:
            return _json_error("Входящая выгрузка не найдена.", 404)
        scanner = classic_dashboard._scanner(registry)
        archive = registry.incoming_dir / name
        if not archive.is_file():
            return _json_error("Входящая выгрузка не найдена.", 404)

        busy = scanner.running
        if busy:
            job = classic_dashboard._start_job(name, archive.stat().st_size)
            job["state"] = classic_dashboard.JOB_FAILED
            job["error"] = (
                "уже идёт разбор другой выгрузки ("
                + ", ".join(sorted(busy))
                + ") — одновременно разбирается не больше одной"
            )
            return JSONResponse(
                {"error": job["error"], "job": _job_payload(job)},
                status_code=409,
            )
        if scanner.дописывается(archive):
            job = classic_dashboard._start_job(name, archive.stat().st_size)
            job["state"] = classic_dashboard.JOB_FAILED
            job["error"] = (
                f"{name}: файл изменялся только что — похоже, копирование ещё "
                f"идёт. Повторите через {int(SETTLE_SECONDS)} с."
            )
            return JSONResponse(
                {"error": job["error"], "job": _job_payload(job)},
                status_code=409,
            )

        job = classic_dashboard._start_job(name, archive.stat().st_size)
        try:
            await run_in_threadpool(intake.planned_size, archive)
        except Exception as error:
            job["state"] = classic_dashboard.JOB_FAILED
            job["error"] = f"{archive.name}: не похоже на zip-архив ({error})"
            await run_in_threadpool(scanner.note_failure, archive, job["error"])
            return JSONResponse(
                {"error": job["error"], "job": _job_payload(job)},
                status_code=400,
            )

        configuration = str(payload.get("configuration", "")).strip()
        if configuration and configuration not in registry.snapshot().configurations:
            job["state"] = classic_dashboard.JOB_FAILED
            job["error"] = (
                f"конфигурации «{configuration}» нет в реестре — выберите "
                "загруженную конфигурацию."
            )
            await run_in_threadpool(scanner.note_failure, archive, job["error"])
            return JSONResponse(
                {"error": job["error"], "job": _job_payload(job)},
                status_code=400,
            )

        started, busy = scanner.try_start(name)
        if not started:
            job["state"] = classic_dashboard.JOB_FAILED
            job["error"] = (
                "уже идёт разбор другой выгрузки ("
                + ", ".join(busy)
                + ") — одновременно разбирается не больше одной"
            )
            return JSONResponse(
                {"error": job["error"], "job": _job_payload(job)},
                status_code=409,
            )
        task = asyncio.create_task(
            run_in_threadpool(
                classic_dashboard._run_incoming,
                registry,
                scanner,
                job,
                archive,
                configuration or None,
            )
        )
        classic_dashboard._ФОНОВЫЕ.add(task)
        task.add_done_callback(classic_dashboard._ФОНОВЫЕ.discard)
        return JSONResponse({"job": _job_payload(job)}, status_code=202)

    async def clear_jobs_api(request: Request) -> JSONResponse:
        denied = _mutation_denied(request, action="Очистка журнала")
        if denied is not None:
            return denied
        completed = [
            job
            for job in classic_dashboard._JOBS
            if job["state"]
            in (classic_dashboard.JOB_DONE, classic_dashboard.JOB_FAILED)
        ]
        for job in completed:
            classic_dashboard._JOBS.remove(job)
        return JSONResponse({"cleared": len(completed)})

    async def remove_source_api(request: Request) -> JSONResponse:
        denied = _mutation_denied(request, action="Удаление")
        if denied is not None:
            return denied
        payload = await _json_body(request)
        source_id = str(payload.get("id", ""))
        if not source_id or payload.get("confirmation") != source_id:
            return _json_error("Не подтверждено точное имя источника.", 400)
        try:
            await run_in_threadpool(registry.remove, source_id)
            await run_in_threadpool(registry.save)
        except RegistryError as error:
            return _json_error(str(error), 400)
        return JSONResponse({"removed": source_id})

    async def forget_source_api(request: Request) -> JSONResponse:
        denied = _mutation_denied(request, action="Удаление файла")
        if denied is not None:
            return denied
        payload = await _json_body(request)
        given = str(payload.get("path", ""))
        if not given or payload.get("confirmation") != given:
            return _json_error("Не подтверждено точное имя файла.", 400)
        orphan_sources = await run_in_threadpool(registry.orphan_sources)
        allowed = {
            path.relative_to(registry.data_dir).as_posix(): path
            for path, _ in orphan_sources
        }
        target = allowed.get(given)
        if target is None:
            return _json_error("Такого неиспользуемого файла нет.", 404)
        try:
            await run_in_threadpool(target.unlink)
        except OSError as error:
            return _json_error(str(error), 400)
        return JSONResponse({"forgotten": given})

    async def spa_page(request: Request):
        if request.url.path != "/login" and not can_read(request):
            target = request.url.path
            if request.url.query:
                target += "?" + request.url.query
            return RedirectResponse(
                "/login?" + urlencode({"next": target}),
                status_code=303,
            )
        index = static_dir / "index.html"
        if not index.is_file():
            return PlainTextResponse(
                "React-дашборд не собран. Выполните npm run build в dashboard/.",
                status_code=503,
            )
        return FileResponse(index)

    async def asset(request: Request):
        relative = request.path_params.get("path", "")
        candidate = (static_dir / "assets" / relative).resolve()
        assets_root = (static_dir / "assets").resolve()
        if assets_root not in candidate.parents or not candidate.is_file():
            return PlainTextResponse("Файл не найден.", status_code=404)
        return FileResponse(candidate)

    result = [
        Route(
            "/api/v1/dashboard/bootstrap",
            bootstrap,
            methods=["GET"],
            name="dashboard_bootstrap",
        ),
        Route(
            "/api/v1/sources",
            sources_api,
            methods=["GET"],
            name="dashboard_sources",
        ),
        Route(
            "/api/v1/sources/coverage",
            coverage_api,
            methods=["GET"],
            name="dashboard_source_coverage",
        ),
        Route(
            "/api/v1/queries",
            queries_setup_api,
            methods=["GET"],
            name="dashboard_queries_setup",
        ),
        Route(
            "/api/v1/queries",
            queries_run_api,
            methods=["POST"],
            name="dashboard_queries_run",
        ),
        Route(
            "/api/v1/sources/admin",
            admin_sources_api,
            methods=["GET"],
            name="dashboard_sources_admin",
        ),
        Route(
            "/api/v1/sources/upload",
            upload_source_api,
            methods=["POST"],
            name="dashboard_source_upload",
        ),
        Route(
            "/api/v1/sources/incoming/parse",
            parse_incoming_api,
            methods=["POST"],
            name="dashboard_incoming_parse",
        ),
        Route(
            "/api/v1/sources/jobs/clear",
            clear_jobs_api,
            methods=["POST"],
            name="dashboard_source_jobs_clear",
        ),
        Route(
            "/api/v1/sources/remove",
            remove_source_api,
            methods=["POST"],
            name="dashboard_source_remove",
        ),
        Route(
            "/api/v1/sources/forget",
            forget_source_api,
            methods=["POST"],
            name="dashboard_source_forget",
        ),
        Route(
            "/assets/{path:path}",
            asset,
            methods=["GET"],
            name="dashboard_asset",
        ),
    ]
    result.extend(
        Route(path, spa_page, methods=["GET"], name=f"dashboard_spa_{index}")
        for index, path in enumerate(SPA_PAGE_PATHS)
    )
    # Сессионная cookie пока остаётся общим контрактом двух интерфейсов.
    # Страницу входа рисует SPA, а проверку токена и logout выполняет прежний
    # серверный код: так новый UI не заводит второй набор полномочий.
    from .dashboard import routes as classic_routes

    result.extend(
        route
        for route in classic_routes(registry)
        if (route.path == "/login" and "POST" in (route.methods or set()))
        or route.path == "/logout"
    )
    return result


def routes(
    registry: Registry,
    *,
    mode: str | None = None,
    static_dir: Path | None = None,
) -> list[Route]:
    """Вернуть ровно один UI-контур, не затрагивая ``/mcp`` и ``/health``."""
    selected = dashboard_mode() if mode is None else mode
    if selected not in DASHBOARD_MODES:
        raise DashboardModeError(
            "Режим дашборда должен быть одним из: off, classic, spa."
        )
    if selected == DASHBOARD_OFF:
        return []
    if selected == DASHBOARD_CLASSIC:
        from .dashboard import routes as classic_routes

        return classic_routes(registry)
    root = static_dir or Path(
        os.environ.get("MCP1C_DASHBOARD_DIST", "dashboard/dist")
    )
    return _spa_routes(registry, root)
