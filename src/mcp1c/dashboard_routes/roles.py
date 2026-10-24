"""Чтение каталога ролей, прав и ограничений доступа."""

from __future__ import annotations

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse

from ..registry import RegistryError
from ..role_access_service import (
    MAX_ACCESS_LIMIT as MAX_ROLE_ACCESS_LIMIT,
    MAX_FIND_LIMIT as MAX_ROLE_FIND_LIMIT,
    MAX_PAGE_CHARS as MAX_ROLE_PAGE_CHARS,
    MAX_OBJECT_QUERY_CHARS,
    MAX_ROLE_QUERY_CHARS,
    MAX_ROLE_LIST_LIMIT,
    MIN_PAGE_CHARS as MIN_ROLE_PAGE_CHARS,
    RoleAccessQueryError,
    find_roles_payload,
    get_role_access_payload,
    http_status as role_http_status,
    role_objects_payload,
    roles_catalog_payload,
)
from .context import DashboardContext, Endpoint, _json_error, read_endpoint


def handlers(context: DashboardContext) -> dict[str, Endpoint]:
    registry = context.registry

    def role_param(request: Request, name: str, *, maximum: int) -> str | None:
        values = request.query_params.getlist(name)
        if len(values) > 1:
            raise RoleAccessQueryError(
                f"Параметр {name} должен быть указан один раз."
            )
        if not values:
            return None
        value = values[0]
        if len(value) > maximum:
            raise RoleAccessQueryError(
                f"Параметр {name} должен содержать не более {maximum} символов."
            )
        return value

    def role_integer(
        request: Request,
        name: str,
        *,
        default: int,
        minimum: int,
        maximum: int,
    ) -> int:
        raw = role_param(request, name, maximum=20)
        if raw is None:
            return default
        try:
            value = int(raw)
        except ValueError as error:
            raise RoleAccessQueryError(
                f"Параметр {name} должен быть целым числом."
            ) from error
        if not minimum <= value <= maximum:
            raise RoleAccessQueryError(
                f"Параметр {name} должен быть от {minimum} до {maximum}."
            )
        return value

    def role_boolean(request: Request, name: str) -> bool:
        raw = role_param(request, name, maximum=1)
        if raw is None or raw == "0":
            return False
        if raw == "1":
            return True
        raise RoleAccessQueryError(f"Параметр {name} должен быть 0 или 1.")

    @read_endpoint
    async def roles_catalog_api(request: Request) -> JSONResponse:
        try:
            payload = await run_in_threadpool(
                roles_catalog_payload,
                registry,
                config=role_param(request, "config", maximum=512) or None,
                query=role_param(
                    request,
                    "query",
                    maximum=MAX_ROLE_QUERY_CHARS,
                ) or "",
                cursor=role_param(request, "cursor", maximum=2048) or None,
                limit=role_integer(
                    request,
                    "limit",
                    default=50,
                    minimum=1,
                    maximum=MAX_ROLE_LIST_LIMIT,
                ),
            )
        except (RoleAccessQueryError, RegistryError) as error:
            return _json_error(str(error), 400)
        return JSONResponse(payload, status_code=role_http_status(payload))

    @read_endpoint
    async def role_objects_api(request: Request) -> JSONResponse:
        try:
            role = role_param(request, "role", maximum=512)
            if not role:
                raise RoleAccessQueryError("Параметр role обязателен.")
            payload = await run_in_threadpool(
                role_objects_payload,
                registry,
                role,
                config=role_param(request, "config", maximum=512) or None,
                kind=role_param(request, "kind", maximum=512) or "",
                query=role_param(
                    request,
                    "query",
                    maximum=MAX_OBJECT_QUERY_CHARS,
                ) or "",
                cursor=role_param(request, "cursor", maximum=2048) or None,
                limit=role_integer(
                    request,
                    "limit",
                    default=50,
                    minimum=1,
                    maximum=MAX_ROLE_ACCESS_LIMIT,
                ),
            )
        except (RoleAccessQueryError, RegistryError) as error:
            return _json_error(str(error), 400)
        return JSONResponse(payload, status_code=role_http_status(payload))

    @read_endpoint
    async def roles_find_api(request: Request) -> JSONResponse:
        try:
            full_name = role_param(request, "full_name", maximum=1024)
            if not full_name:
                raise RoleAccessQueryError("Параметр full_name обязателен.")
            operations = request.query_params.getlist("operation")
            if not 1 <= len(operations) <= 16 or any(
                not operation or len(operation) > 32 for operation in operations
            ):
                raise RoleAccessQueryError(
                    "Нужно указать от одной до шестнадцати операций operation."
                )
            payload = await run_in_threadpool(
                find_roles_payload,
                registry,
                full_name,
                operations,
                config=role_param(request, "config", maximum=512) or None,
                child_path=role_param(request, "child_path", maximum=1024) or "",
                include_conditional=role_boolean(request, "include_conditional"),
                cursor=role_param(request, "cursor", maximum=2048) or None,
                limit=role_integer(
                    request,
                    "limit",
                    default=10,
                    minimum=1,
                    maximum=MAX_ROLE_FIND_LIMIT,
                ),
            )
        except (RoleAccessQueryError, RegistryError) as error:
            return _json_error(str(error), 400)
        return JSONResponse(payload, status_code=role_http_status(payload))

    @read_endpoint
    async def roles_access_api(request: Request) -> JSONResponse:
        try:
            role = role_param(request, "role", maximum=512)
            if not role:
                raise RoleAccessQueryError("Параметр role обязателен.")
            payload = await run_in_threadpool(
                get_role_access_payload,
                registry,
                role,
                config=role_param(request, "config", maximum=512) or None,
                full_name=role_param(request, "full_name", maximum=1024) or "",
                detail=role_param(request, "detail", maximum=16) or "summary",
                cursor=role_param(request, "cursor", maximum=2048) or None,
                limit=role_integer(
                    request,
                    "limit",
                    default=50,
                    minimum=1,
                    maximum=MAX_ROLE_ACCESS_LIMIT,
                ),
            )
        except (RoleAccessQueryError, RegistryError) as error:
            return _json_error(str(error), 400)
        return JSONResponse(payload, status_code=role_http_status(payload))

    @read_endpoint
    async def roles_restriction_api(request: Request) -> JSONResponse:
        try:
            role = role_param(request, "role", maximum=512)
            restriction_ref = role_param(
                request,
                "restriction_ref",
                maximum=2048,
            )
            if not role or not restriction_ref:
                raise RoleAccessQueryError(
                    "Параметры role и restriction_ref обязательны."
                )
            payload = await run_in_threadpool(
                get_role_access_payload,
                registry,
                role,
                config=role_param(request, "config", maximum=512) or None,
                restriction_ref=restriction_ref,
                restriction_cursor=(
                    role_param(request, "cursor", maximum=2048) or None
                ),
                max_chars=role_integer(
                    request,
                    "max_chars",
                    default=2000,
                    minimum=MIN_ROLE_PAGE_CHARS,
                    maximum=MAX_ROLE_PAGE_CHARS,
                ),
            )
        except (RoleAccessQueryError, RegistryError) as error:
            return _json_error(str(error), 400)
        return JSONResponse(payload, status_code=role_http_status(payload))

    return {
        "roles_catalog_api": roles_catalog_api,
        "role_objects_api": role_objects_api,
        "roles_find_api": roles_find_api,
        "roles_access_api": roles_access_api,
        "roles_restriction_api": roles_restriction_api,
    }
