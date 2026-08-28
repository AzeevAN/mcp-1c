"""Переключаемая оболочка дашборда: off, classic или React SPA.

Предметные данные и запись остаются в том же процессе ``Registry``. React
получает только HTTP API и никогда не монтирует ``data/`` самостоятельно.
"""

from __future__ import annotations

import os
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from urllib.parse import quote, urlencode

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import (
    FileResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
)
from starlette.routing import Route

from . import coverage_log, tools
from .dashboard import _authorized, _session_level, can_read
from .registry import (
    KIND_EXTENSION,
    KIND_MODULES,
    KIND_QUERY,
    KIND_SYNTAX,
    Registry,
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
