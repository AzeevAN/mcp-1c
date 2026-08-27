"""Переключаемая оболочка дашборда: off, classic или React SPA.

Предметные данные и запись остаются в том же процессе ``Registry``. React
получает только HTTP API и никогда не монтирует ``data/`` самостоятельно.
"""

from __future__ import annotations

import os
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse
from starlette.routing import Route

from .dashboard import _authorized, can_read
from .registry import KIND_EXTENSION, KIND_MODULES, Registry

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
                "summary": {
                    "configurations": len(snapshot.configurations),
                    "metadata_objects": metadata_objects,
                    "code_corpora": code_corpora,
                    "reference_sources": len(snapshot.syntax_versions)
                    + int(snapshot.query_source is not None),
                },
            }
        )

    async def spa_page(request: Request):
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
