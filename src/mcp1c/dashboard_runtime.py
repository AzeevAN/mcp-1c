"""Переключаемая оболочка современного дашборда: on или off.

Предметные данные и запись остаются в том же процессе ``Registry``. React
получает только HTTP API и никогда не монтирует ``data/`` самостоятельно.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import re
import secrets
import shutil
import tempfile
import threading
from collections.abc import Awaitable, Callable
from dataclasses import replace
from functools import wraps
from pathlib import Path
from typing import cast
from urllib.parse import quote, urlencode

from starlette.background import BackgroundTask
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

from . import __version__, coverage_log, dashboard_backend, source_ids, tools
from .dashboard_routes import auth, dictionary, roles, sources_intake
from .dashboard_routes.auth import _secure_cookie as _secure_cookie
from .dashboard_routes.context import (
    DashboardContext,
    _admin_denied as _admin_denied,
    _json_body as _json_body,
    _json_error as _json_error,
    _mutation_denied as _mutation_denied,
    admin_endpoint as admin_endpoint,
    read_endpoint as read_endpoint,
)
from .dashboard_routes.dictionary import _dictionary_payload as _dictionary_payload
from .dashboard_routes.sources_intake import (
    _INTAKE_CONFIRM_ERRORS as _INTAKE_CONFIRM_ERRORS,
    _INTAKE_DELETE_CANDIDATE_ERRORS as _INTAKE_DELETE_CANDIDATE_ERRORS,
    _INTAKE_DISCARD_ERRORS as _INTAKE_DISCARD_ERRORS,
    _INTAKE_JOB_ERRORS as _INTAKE_JOB_ERRORS,
    _INTAKE_READ_ERRORS as _INTAKE_READ_ERRORS,
    _INTAKE_START_ERRORS as _INTAKE_START_ERRORS,
    _IntakeErrorTable as _IntakeErrorTable,
    _admin_sources_payload as _admin_sources_payload,
    _copy_upload as _copy_upload,
    _coverage_payload as _coverage_payload,
    _intake_error_response as _intake_error_response,
    _job_payload as _job_payload,
    _source_payload as _source_payload,
    _sources_payload as _sources_payload,
)
from .capabilities import (
    CapabilityConfigurationError,
    CapabilityRuntime,
    CapabilitySettingsStore,
    MAX_SERVER_SETTINGS_BYTES,
)
from .dashboard_backend import (
    COOKIE,
    LEVEL_ADMIN,
    SESSION_TTL_SECONDS,
    admin_authorized,
    admin_token,
    api_token,
    can_read,
    check_token,
    create_session,
    login_from_foreign_site,
    csrf_denied,
    drop_session,
    session_level,
)
from .dictionary import ANY_CONFIGURATION, SOURCE_BUILTIN, DictionaryError
from .graph_view import DEFAULT_LIMIT as DEFAULT_GRAPH_LIMIT
from .graph_view import bounds as graph_bounds
from .graph_view import neighbourhood
from .heavy_operations import HeavyOperations
from .intake_v2_api import (
    IntakeApiConflict,
    IntakeApiError,
    IntakeApiInvalid,
    IntakeApiNotFound,
    IntakeApiService,
    IntakeWork,
)
from .intake_v2 import CandidateStoreError, ExportIdentity, IntakeV2ContractError
from .intake_v2_lifecycle import LifecycleError
from .intake_v2_operations import OperationConflict, OperationError
from .intake_v2_transport import (
    TransportError,
    TransportLimitError,
)
from .registry import (
    KIND_EXTENSION,
    KIND_MODULES,
    KIND_SYNTAX,
    Registry,
    RegistryChangedExternally,
    RegistryError,
    SourceInUseError,
    SourcesChanged,
)
from .source_modes import ActivationStatus
from .process_restart import RestartController
from .reference_provider import (
    DEFAULT_PAGE_CHARS,
    MAX_PAGE_CHARS,
    MIN_PAGE_CHARS,
    ReferenceQueryError,
    ReferenceService,
)
from .role_access_service import (
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
from .render import DETAIL_LEVELS
from .runtime_config import (
    ACCESS_HTTPS_PROXY,
    DASHBOARD_MODES,
    DASHBOARD_OFF,
    DASHBOARD_ON,
    AccessModeError,
    DashboardModeError,
    access_mode,
    dashboard_mode,
)
from .search import MAX_QUERY_CHARS

SPA_PAGE_PATHS = (
    "/",
    "/login",
    "/sources",
    "/queries",
    "/reference",
    "/graph",
    "/dictionary",
    "/object",
    "/syntax",
    "/roles",
    "/capabilities",
)
DEFAULT_DASHBOARD_DIST = Path(__file__).resolve().with_name("dashboard_dist")


# Порядок и имена маршрутов — часть HTTP-контура дашборда.
_SPA_ROUTE_TABLE = (
    ('/api/v1/dashboard/bootstrap', 'bootstrap', ('GET',), 'dashboard_bootstrap'),
    ('/api/v1/sources', 'sources_api', ('GET',), 'dashboard_sources'),
    ('/api/v1/sources/platform/set', 'platform_set_api', ('POST',), 'dashboard_platform_set'),
    ('/api/v1/sources/platform/clear', 'platform_clear_api', ('POST',), 'dashboard_platform_clear'),
    ('/api/v1/sources/coverage', 'coverage_api', ('GET',), 'dashboard_source_coverage'),
    ('/api/v1/queries', 'queries_setup_api', ('GET',), 'dashboard_queries_setup'),
    ('/api/v1/queries', 'queries_run_api', ('POST',), 'dashboard_queries_run'),
    ('/api/v1/cards/object', 'object_card_api', ('GET',), 'dashboard_object_card'),
    ('/api/v1/cards/syntax', 'syntax_card_api', ('GET',), 'dashboard_syntax_card'),
    ('/api/v1/graph', 'graph_api', ('GET',), 'dashboard_graph'),
    ('/api/v1/roles', 'roles_catalog_api', ('GET',), 'dashboard_roles_catalog'),
    ('/api/v1/roles/find', 'roles_find_api', ('GET',), 'dashboard_roles_find'),
    ('/api/v1/roles/access', 'roles_access_api', ('GET',), 'dashboard_roles_access'),
    ('/api/v1/roles/objects', 'role_objects_api', ('GET',), 'dashboard_role_objects'),
    ('/api/v1/roles/restriction', 'roles_restriction_api', ('GET',), 'dashboard_roles_restriction'),
    ('/api/v1/dictionary', 'dictionary_api', ('GET',), 'dashboard_dictionary'),
    ('/api/v1/dictionary/aliases', 'dictionary_alias_add_api', ('POST',), 'dashboard_dictionary_alias_add'),
    ('/api/v1/dictionary/aliases/remove', 'dictionary_alias_remove_api', ('POST',), 'dashboard_dictionary_alias_remove'),
    ('/api/v1/dictionary/synonyms', 'dictionary_synonyms_add_api', ('POST',), 'dashboard_dictionary_synonyms_add'),
    ('/api/v1/dictionary/synonyms/remove', 'dictionary_synonyms_remove_api', ('POST',), 'dashboard_dictionary_synonyms_remove'),
    ('/api/v1/sources/admin', 'admin_sources_api', ('GET',), 'dashboard_sources_admin'),
    ('/api/v1/capabilities', 'capabilities_api', ('GET',), 'dashboard_capabilities'),
    ('/api/v1/capabilities', 'capabilities_update_api', ('PUT',), 'dashboard_capabilities_update'),
    ('/api/v1/sources/directories', 'directory_sources_api', ('GET',), None),
    ('/api/v1/sources/directories/{operation}', 'directory_change_api', ('POST',), None),
    ('/api/v1/sources/intake', 'intake_snapshot_api', ('GET',), 'dashboard_intake_snapshot'),
    ('/api/v1/sources/intake/upload', 'intake_upload_api', ('POST',), 'dashboard_intake_upload'),
    ('/api/v1/sources/intake/start', 'intake_start_api', ('POST',), 'dashboard_intake_start'),
    ('/api/v1/sources/intake/jobs', 'intake_jobs_api', ('GET',), 'dashboard_intake_jobs'),
    ('/api/v1/sources/intake/jobs/{job_id}', 'intake_job_api', ('GET',), 'dashboard_intake_job'),
    ('/api/v1/sources/intake/confirm', 'intake_confirm_api', ('POST',), 'dashboard_intake_confirm'),
    ('/api/v1/sources/intake/discard', 'intake_discard_api', ('POST',), 'dashboard_intake_discard'),
    ('/api/v1/sources/intake/candidates/delete', 'intake_delete_candidate_api', ('POST',), 'dashboard_intake_delete_candidate'),
    ('/api/v1/sources/upload', 'upload_source_api', ('POST',), 'dashboard_source_upload'),
    ('/api/v1/sources/incoming/parse', 'parse_incoming_api', ('POST',), 'dashboard_incoming_parse'),
    ('/api/v1/sources/jobs/clear', 'clear_jobs_api', ('POST',), 'dashboard_source_jobs_clear'),
    ('/api/v1/sources/remove', 'remove_source_api', ('POST',), 'dashboard_source_remove'),
    ('/api/v1/sources/forget', 'forget_source_api', ('POST',), 'dashboard_source_forget'),
    ('/assets/{path:path}', 'asset', ('GET',), 'dashboard_asset'),
    ('/favicon.svg', 'public_asset', ('GET',), 'dashboard_favicon'),
    ('/mcp-1c-logo.svg', 'public_asset', ('GET',), 'dashboard_logo'),
    *((path, "spa_page", ("GET",), f"dashboard_spa_{index}")
      for index, path in enumerate(SPA_PAGE_PATHS)),
    ('/login', 'login', ('POST',), 'dashboard_login'),
    ('/logout', 'logout', ('POST',), 'dashboard_logout'),
)


_REVISION_SALT = secrets.token_hex(16)


_INLINE_SCRIPT_RE = re.compile(
    r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", re.IGNORECASE | re.DOTALL
)
_CSP_CACHE: dict[tuple[str, int, int], str] = {}


def _spa_csp(index: Path) -> str:
    """CSP для собранной SPA с хешами её встроенных скриптов.

    `index.html` содержит встроенный скрипт выбора темы до загрузки React;
    `'unsafe-inline'` для скриптов перечеркнул бы смысл политики, поэтому
    разрешается ровно этот текст по SHA-256. Хеши считаются по файлу при
    отдаче: новая сборка дашборда не требует правки сервера. Стили остаются
    `'unsafe-inline'`: React задаёт атрибуты `style`, а XSS через стиль без
    скрипта здесь не даёт доступа к данным.
    """
    stat = index.stat()
    key = (str(index), stat.st_mtime_ns, stat.st_size)
    cached = _CSP_CACHE.get(key)
    if cached is not None:
        return cached
    html = index.read_text(encoding="utf-8")
    hashes = " ".join(
        "'sha256-"
        + base64.b64encode(hashlib.sha256(body.encode("utf-8")).digest()).decode()
        + "'"
        for body in _INLINE_SCRIPT_RE.findall(html)
    )
    policy = "; ".join(
        (
            "default-src 'self'",
            f"script-src 'self' {hashes}".rstrip(),
            "style-src 'self' 'unsafe-inline'",
            "img-src 'self' data:",
            "font-src 'self' data:",
            "connect-src 'self'",
            "object-src 'none'",
            "base-uri 'none'",
            "form-action 'self'",
            "frame-ancestors 'none'",
        )
    )
    _CSP_CACHE.clear()
    _CSP_CACHE[key] = policy
    return policy


def _sources_revision(snapshot) -> str:
    """Непрозрачный маркер снимка, без передачи путей и служебных identity в SPA.

    Это общая ревизия источников, а не версия отдельной конфигурации.
    Новый процесс консервативно делает сохранённую выдачу исторической.
    """
    evidence = (
        _REVISION_SALT,
        tuple(
            (key, replace(value, last_successful_load_at=None))
            for key, value in snapshot.sources.items()
        ),
        tuple(
            (key, replace(value, last_successful_load_at=None))
            for key, value in snapshot.syntax_versions.items()
        ),
        tuple(
            (
                key,
                replace(
                    value.pointer,
                    last_successful_load_at=None,
                    last_successful_load_job_id=None,
                    pending_load_commits=(),
                ),
            )
            for key, value in snapshot.generations.items()
        ),
        snapshot.source_lifecycle_token,
    )
    return hashlib.sha256(repr(evidence).encode()).hexdigest()


def _run_revisioned_queries(registry, config, scope, phrases):
    snapshot = registry.snapshot()
    results = dashboard_backend.run_queries(registry, config or None, scope, phrases)
    payload = _query_results_payload(config, scope, phrases, results)
    # Гонка не должна приписать старой выдаче ревизию уже нового источника.
    payload["sources_revision"] = (
        _sources_revision(snapshot) if registry.snapshot_is_current(snapshot) else None
    )
    return payload


_QUERY_SCOPE_LABELS = {
    "objects": "Объекты",
    "fields": "Реквизиты",
    "syntax": "Справка платформы",
}


def _queries_setup_payload(registry: Registry) -> dict:
    """Стабильные настройки формы и доступность живых поисковых корпусов."""
    snapshot = registry.snapshot()
    names = list(snapshot.configuration_names)
    return {
        "api_version": "v1",
        "sources_revision": _sources_revision(snapshot),
        "configuration_names": names,
        "default_configuration": names[0] if names else "",
        "scopes": [
            {
                "id": scope,
                "label": _QUERY_SCOPE_LABELS[scope],
                "requires_configuration": scope != "syntax",
            }
            for scope in dashboard_backend.SCOPES
        ],
        "limits": {
            "phrases": dashboard_backend.MAX_QUERY_PHRASES,
            "phrase_chars": MAX_QUERY_CHARS,
            "results_per_phrase": 5,
        },
        "availability": {
            "configurations": bool(names),
            "syntax": snapshot.syntax is not None,
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
                        "kind": dashboard_backend.kind_title(
                            scope, hit.doc.kind
                        ),
                        "score": hit.score,
                        "reason": hit.reason,
                        "card_url": dashboard_backend.card_link(
                            scope, config, hit
                        ),
                    }
                    for position, hit in enumerate(hits, start=1)
                ],
                "hidden": [
                    {
                        "title": getattr(hit.doc.payload, "address", "")
                        or hit.doc.id,
                        "reason": dashboard_backend.hidden_reason(
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


def _card_payload(
    registry: Registry,
    *,
    kind: str,
    config: str,
    name: str,
    detail: str,
    links_offset: int = 0,
) -> dict:
    """Карточка тем же вызовом и тем же Markdown-рендерером, что получает MCP."""
    levels = [*DETAIL_LEVELS, "links"] if kind == "syntax" else list(DETAIL_LEVELS)
    normalized_detail = detail if detail in levels else "fields"
    markdown = dashboard_backend.card_text(
        registry, kind, config, name, normalized_detail,
        links_offset if kind == "syntax" and normalized_detail == "links" else 0,
    )
    navigation = (
        dashboard_backend.tools.syntax_navigation_data(
            registry, name, config or None,
            links_offset if normalized_detail == "links" else 0,
        )
        if kind == "syntax" and normalized_detail in ("full", "links") else []
    )
    names = list(registry.snapshot().configuration_names)
    resolved_configuration = config or (names[0] if len(names) == 1 else "")
    return {
        "api_version": "v1",
        "kind": kind,
        "name": name,
        "configuration": resolved_configuration,
        "configuration_names": names,
        "configuration_required": kind == "object",
        "detail": normalized_detail,
        "detail_levels": levels,
        "markdown": markdown,
        "html": dashboard_backend.render_markdown(markdown),
        "navigation": navigation,
    }


_GRAPH_LIMIT_OPTIONS = (15, 30, 60, 150, 400)


def _graph_payload(
    registry: Registry,
    *,
    config: str,
    name: str,
    limit: int,
) -> dict:
    """Окрестность с готовой серверной раскладкой для SPA."""
    names = list(registry.snapshot().configuration_names)
    selected = config if config in names else (names[0] if names else "")
    payload = {
        "api_version": "v1",
        "configuration_names": names,
        "configuration": selected,
        "name": name,
        "limit": limit,
        "limit_options": list(_GRAPH_LIMIT_OPTIONS),
        "state": "awaiting_object",
        "message": (
            "Введите полное имя объекта или возьмите его со страницы «Запросы»."
        ),
        "suggestions": [],
        "graph": None,
    }
    if not selected:
        payload["state"] = "empty_registry"
        payload["message"] = (
            "Не загружено ни одной конфигурации — граф строить не по чему."
        )
        return payload

    context = registry.resolve(selected)
    if not name:
        return payload

    if name not in context.configuration.config.objects:
        hits = context.configuration.index.search(name, limit=5)
        payload["state"] = "not_found"
        payload["message"] = (
            f"В конфигурации {context.name} нет объекта `{name}`."
        )
        payload["suggestions"] = [
            {
                "name": hit.doc.id,
                "graph_url": (
                    f"/graph?config={quote(context.name)}"
                    f"&name={quote(hit.doc.id)}&limit={limit}"
                ),
            }
            for hit in hits
        ]
        return payload

    area = neighbourhood(context.configuration.graph, name, limit=limit)

    def node_payload(node) -> dict:
        return {
            "name": node.name,
            "short": node.short,
            "kind": node.kind,
            "degree": node.degree,
            "x": node.x,
            "y": node.y,
            "color": dashboard_backend.KIND_COLORS.get(
                node.kind, dashboard_backend.KIND_FALLBACK
            ),
            "graph_url": (
                f"/graph?config={quote(context.name)}"
                f"&name={quote(node.name)}&limit={limit}"
            ),
            "object_url": (
                f"/object?config={quote(context.name)}&name={quote(node.name)}"
            ),
        }

    kinds = sorted({area.subject.kind} | {node.kind for node in area.nodes})
    payload["graph"] = {
        "depth": 1,
        "total": area.total,
        "shown": area.shown,
        "truncated": area.truncated,
        "bounds": list(graph_bounds(area)),
        "subject": node_payload(area.subject),
        "nodes": [node_payload(node) for node in area.nodes],
        "links": [
            {
                "source": link.source,
                "target": link.target,
                "title": link.title,
                "outgoing": link.outgoing,
            }
            for link in area.links
        ],
        "kinds": [
            {
                "kind": kind,
                "color": dashboard_backend.KIND_COLORS.get(
                    kind, dashboard_backend.KIND_FALLBACK
                ),
            }
            for kind in kinds
        ],
    }
    if area.total:
        payload["state"] = "ready"
        payload["message"] = ""
    else:
        payload["state"] = "isolated"
        payload["message"] = (
            f"`{name}` ни на что не ссылается и на него не ссылается никто."
        )
    return payload


def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    payload: dict[str, object] = {}
    for key, value in pairs:
        if key in payload:
            raise ValueError(f"Повторяющийся JSON-ключ: {key}")
        payload[key] = value
    return payload


def _reject_json_constant(value: str) -> object:
    raise ValueError(f"Недопустимое JSON-значение: {value}")


async def _strict_json_body(request: Request) -> dict | None:
    try:
        encoded = await request.body()
        if len(encoded) > MAX_SERVER_SETTINGS_BYTES:
            return None
        payload = json.loads(
            encoded,
            object_pairs_hook=_strict_json_object,
            parse_constant=_reject_json_constant,
        )
    except (RecursionError, UnicodeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _spa_routes(
    registry: Registry,
    static_dir: Path,
    reference: ReferenceService,
    restart: RestartController,
    capabilities: CapabilityRuntime,
    intake: IntakeApiService | None,
    role_tools_refresh: Callable[[], Awaitable[bool]] | None,
    heavy: HeavyOperations,
) -> list[Route]:
    static_dir = static_dir.resolve()
    context = DashboardContext(
        registry=registry,
        static_dir=static_dir,
        reference=reference,
        restart=restart,
        capabilities=capabilities,
        intake=intake,
        role_tools_refresh=role_tools_refresh,
        heavy=heavy,
    )

    @read_endpoint
    async def queries_setup_api(request: Request) -> JSONResponse:
        payload = await run_in_threadpool(_queries_setup_payload, registry)
        return JSONResponse(payload)

    @read_endpoint
    async def queries_run_api(request: Request) -> JSONResponse:
        payload = await _json_body(request)
        config = payload.get("config", "")
        scope = payload.get("scope", "")
        raw_phrases = payload.get("phrases")
        if not isinstance(config, str) or not isinstance(scope, str):
            return _json_error("config и scope должны быть строками.", 422)
        if scope not in dashboard_backend.SCOPES:
            return _json_error(f"Неизвестная область поиска: {scope or '<пусто>'}.", 422)
        if not isinstance(raw_phrases, list) or not all(
            isinstance(phrase, str) for phrase in raw_phrases
        ):
            return _json_error("phrases должен быть списком строк.", 422)
        phrases = [phrase.strip() for phrase in raw_phrases if phrase.strip()]
        if not phrases:
            return _json_error("Не указано ни одной фразы.", 422)
        if len(phrases) > dashboard_backend.MAX_QUERY_PHRASES:
            return _json_error(
                "За один прогон принимается не более "
                f"{dashboard_backend.MAX_QUERY_PHRASES} фраз.",
                422,
            )
        if any(
            len(phrase) > MAX_QUERY_CHARS
            for phrase in phrases
        ):
            return _json_error(
                "Каждая поисковая фраза должна содержать не более "
                f"{MAX_QUERY_CHARS} символов.",
                422,
            )
        try:
            result = await run_in_threadpool(
                _run_revisioned_queries,
                registry,
                config,
                scope,
                phrases,
            )
        except RegistryError as error:
            return _json_error(str(error), 409)
        except ValueError as error:
            return _json_error(str(error), 422)
        return JSONResponse(result)

    @read_endpoint
    async def card_api(request: Request, *, kind: str) -> JSONResponse:
        config = request.query_params.get("config", "")
        name = request.query_params.get("name", "")
        detail = request.query_params.get("detail", "fields")
        raw_offset = request.query_params.get("links_offset", "0")
        if not name.strip():
            return _json_error("Не указано имя карточки.", 422)
        try:
            links_offset = int(raw_offset)
        except ValueError:
            return _json_error("links_offset должен быть целым числом.", 422)
        if links_offset < 0 or links_offset > 1_000_000:
            return _json_error("links_offset вне допустимого диапазона.", 422)
        try:
            payload = await run_in_threadpool(
                _card_payload,
                registry,
                kind=kind,
                config=config,
                name=name,
                detail=detail,
                links_offset=links_offset,
            )
        except RegistryError as error:
            return _json_error(str(error), 409)
        return JSONResponse(payload)

    async def object_card_api(request: Request) -> JSONResponse:
        return await card_api(request, kind="object")

    async def syntax_card_api(request: Request) -> JSONResponse:
        return await card_api(request, kind="syntax")

    @read_endpoint
    async def graph_api(request: Request) -> JSONResponse:
        params = request.query_params
        try:
            raw_limit = int(params.get("limit") or 0)
        except ValueError:
            raw_limit = 0
        limit = (
            max(1, min(raw_limit, 400))
            if raw_limit
            else DEFAULT_GRAPH_LIMIT
        )
        try:
            payload = await run_in_threadpool(
                _graph_payload,
                registry,
                config=params.get("config", ""),
                name=params.get("name", "").strip(),
                limit=limit,
            )
        except RegistryError as error:
            return _json_error(str(error), 409)
        return JSONResponse(payload)

    @read_endpoint
    async def coverage_api(request: Request) -> JSONResponse:
        source_id = request.query_params.get("source_id", "")
        if not source_id:
            return JSONResponse(
                {"error": "Не указан source_id."}, status_code=400
            )
        def attempt() -> tuple[int, dict] | None:
            # Снимок, активация и поздний CAS берут `_lock` Registry, который
            # публикация держит и на fsync, — вся попытка идёт в потоке.
            snapshot = registry.snapshot()
            code = snapshot.modules.get(source_id)
            source = (
                code.source
                if code is not None and code.source is not None
                else snapshot.sources.get(source_id)
            )
            if source is None or source.kind not in (KIND_MODULES, KIND_EXTENSION):
                return 404, {"error": "Журнал не найден."}
            owner, _, _ = source_id.partition(":")
            activation = registry.active_activation(ExportIdentity.configuration(owner))
            if activation is not None and activation.status is ActivationStatus.RELOAD_REQUIRED:
                if registry.snapshot_is_current(snapshot):
                    return 409, {"error": "reload_required"}
                return None
            payload = coverage_log.load_current(registry.data_dir, source)
            if not registry.snapshot_is_current(snapshot):
                return None
            if payload is None:
                return 404, {"error": "Актуальный журнал недоступен."}
            return 200, payload

        for _ in range(2):
            result = await run_in_threadpool(attempt)
            if result is not None:
                status, body = result
                return JSONResponse(body, status_code=status)
        return JSONResponse({"error": "Источники изменились; повторите запрос."}, status_code=409)

    @admin_endpoint("Просмотр настроек модулей")
    async def capabilities_api(request: Request) -> JSONResponse:
        try:
            payload = await run_in_threadpool(capabilities.payload)
            payload["runtime"] = {"self_restart": restart.enabled}
            return JSONResponse(payload)
        except CapabilityConfigurationError as error:
            return _json_error(str(error), 409)

    @admin_endpoint("Изменение настроек модулей", mutation=True)
    async def capabilities_update_api(request: Request) -> JSONResponse:
        payload = await _strict_json_body(request)
        if payload is None:
            return _json_error(
                "Нужен однозначный JSON-объект без повторяющихся ключей.",
                422,
            )
        enabled = payload.get("enabled")
        if (
            set(payload) != {"enabled"}
            or not isinstance(enabled, list)
            or not all(isinstance(name, str) for name in enabled)
        ):
            return _json_error(
                "Нужен полный массив `enabled` из точных имён модулей.",
                422,
            )
        try:
            normalized = capabilities.store.normalize(enabled)
        except CapabilityConfigurationError as error:
            return _json_error(str(error), 422)
        try:
            result = await run_in_threadpool(
                capabilities.save_desired,
                normalized,
            )
        except CapabilityConfigurationError as error:
            return _json_error(str(error), 409)
        result["runtime"] = {"self_restart": restart.enabled}
        return JSONResponse(result)

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
        return FileResponse(
            index, headers={"Content-Security-Policy": _spa_csp(index)}
        )

    async def asset(request: Request):
        relative = request.path_params.get("path", "")
        candidate = (static_dir / "assets" / relative).resolve()
        assets_root = (static_dir / "assets").resolve()
        if assets_root not in candidate.parents or not candidate.is_file():
            return PlainTextResponse("Файл не найден.", status_code=404)
        return FileResponse(candidate)

    async def public_asset(request: Request):
        """Отдать небольшой public-asset, скопированный Vite в корень dist."""
        relative = request.path_params.get("path", "") or Path(request.url.path).name
        if relative not in {"favicon.svg", "mcp-1c-logo.svg"}:
            return PlainTextResponse("Файл не найден.", status_code=404)
        candidate = (static_dir / relative).resolve()
        if static_dir.resolve() not in candidate.parents or not candidate.is_file():
            return PlainTextResponse("Файл не найден.", status_code=404)
        return FileResponse(candidate)

    handlers = {
        **sources_intake.handlers(context),
        **roles.handlers(context),
        **dictionary.handlers(context),
        **auth.handlers(context),
        "asset": asset,
        "capabilities_api": capabilities_api,
        "capabilities_update_api": capabilities_update_api,
        "coverage_api": coverage_api,
        "graph_api": graph_api,
        "object_card_api": object_card_api,
        "public_asset": public_asset,
        "queries_run_api": queries_run_api,
        "queries_setup_api": queries_setup_api,
        "syntax_card_api": syntax_card_api,
        "spa_page": spa_page,
    }
    return [
        Route(path, handlers[key], methods=methods, name=name)
        for path, key, methods, name in _SPA_ROUTE_TABLE
    ]


def _reference_routes(
    reference: ReferenceService,
) -> list[Route]:
    """API статуса и чтения встроенной общей справки."""

    def unique_param(request: Request, name: str, *, maximum: int) -> str | None:
        values = request.query_params.getlist(name)
        if len(values) > 1:
            raise ReferenceQueryError(f"Параметр {name} должен быть указан один раз.")
        if not values:
            return None
        value = values[0]
        if len(value) > maximum:
            raise ReferenceQueryError(
                f"Параметр {name} должен содержать не более {maximum} символов."
            )
        return value

    def integer_param(
        request: Request,
        name: str,
        *,
        default: int,
        minimum: int,
        maximum: int,
    ) -> int:
        raw = unique_param(request, name, maximum=20)
        if raw is None:
            return default
        try:
            value = int(raw)
        except ValueError as error:
            raise ReferenceQueryError(f"Параметр {name} должен быть целым числом.") from error
        if not minimum <= value <= maximum:
            raise ReferenceQueryError(
                f"Параметр {name} должен быть от {minimum} до {maximum}."
            )
        return value

    def boolean_param(request: Request, name: str) -> bool:
        raw = unique_param(request, name, maximum=1)
        if raw is None or raw == "0":
            return False
        if raw == "1":
            return True
        raise ReferenceQueryError(f"Параметр {name} должен быть 0 или 1.")

    def unavailable() -> JSONResponse:
        return JSONResponse(
            {"error": reference.status.message, "state": reference.status.state},
            status_code=409,
        )

    @read_endpoint
    async def reference_search_api(request: Request) -> JSONResponse:
        if reference.provider is None:
            return unavailable()
        try:
            query = unique_param(request, "query", maximum=MAX_QUERY_CHARS)
            if query is None or not query.strip():
                raise ReferenceQueryError("Параметр query не должен быть пустым.")
            domain = unique_param(request, "domain", maximum=100) or None
            kind = unique_param(request, "kind", maximum=100) or None
            platform = unique_param(request, "platform", maximum=64) or None
            limit = integer_param(
                request, "limit", default=10, minimum=1, maximum=50
            )
            include_explicit = boolean_param(request, "include_explicit")
            include_hidden = boolean_param(request, "include_hidden")
            result = await run_in_threadpool(
                reference.provider.search,
                query,
                domain=domain,
                kind=kind,
                platform=platform,
                include_explicit=include_explicit,
                include_hidden=include_hidden,
                limit=limit,
            )
        except ReferenceQueryError as error:
            return _json_error(str(error), 400)
        return JSONResponse(result)

    @read_endpoint
    async def reference_item_api(request: Request) -> JSONResponse:
        if reference.provider is None:
            return unavailable()
        try:
            item_id = unique_param(request, "item_id", maximum=512)
            if item_id is None or not item_id:
                raise ReferenceQueryError("Параметр item_id не должен быть пустым.")
            section_id = unique_param(request, "section_id", maximum=512) or None
            cursor = unique_param(request, "cursor", maximum=2048) or None
            platform = unique_param(request, "platform", maximum=64) or None
            max_chars = integer_param(
                request,
                "max_chars",
                default=DEFAULT_PAGE_CHARS,
                minimum=MIN_PAGE_CHARS,
                maximum=MAX_PAGE_CHARS,
            )
            result = await run_in_threadpool(
                reference.provider.get,
                item_id,
                section_id=section_id,
                cursor=cursor,
                max_chars=max_chars,
                platform=platform,
            )
            result["html"] = dashboard_backend.render_markdown(result["content"])
        except ReferenceQueryError as error:
            return _json_error(str(error), 400)
        return JSONResponse(result)

    @read_endpoint
    async def reference_status_api(request: Request) -> JSONResponse:
        return JSONResponse(reference.payload(detailed=admin_authorized(request)))

    return [
        Route(
            "/api/v1/reference",
            reference_status_api,
            methods=["GET"],
            name="dashboard_reference_status",
        ),
        Route(
            "/api/v1/reference/search",
            reference_search_api,
            methods=["GET"],
            name="dashboard_reference_search",
        ),
        Route(
            "/api/v1/reference/item",
            reference_item_api,
            methods=["GET"],
            name="dashboard_reference_item",
        ),
    ]


def _server_routes(
    restart: RestartController,
    *,
    reference: ReferenceService,
    capabilities: CapabilityRuntime,
    heavy: HeavyOperations,
) -> list[Route]:
    """Общие process-level маршруты, не принадлежащие одному провайдеру."""

    @admin_endpoint("Перезапуск сервера", mutation=True)
    async def restart_api(request: Request):
        if not restart.enabled:
            return _json_error(
                "Перезапуск из дашборда выключен оператором.", 404
            )
        reasons = []
        try:
            if await run_in_threadpool(capabilities.pending_restart):
                reasons.append("capabilities")
        except CapabilityConfigurationError as error:
            return _json_error(str(error), 409)
        if not reasons:
            return _json_error(
                "Нет ожидающих изменений, требующих полного перезапуска.", 409
            )
        if restart.requested:
            return _json_error("Перезапуск уже запрошен.", 409)

        background = BackgroundTask(restart.terminate_after_response)
        response = JSONResponse(
            {
                "state": "restarting",
                "runtime_id": restart.runtime_id,
                "reasons": reasons,
            },
            status_code=202,
            background=background,
        )
        lease = heavy.acquire()
        if lease is None:
            return _json_error(
                "Идёт загрузка. Перезапуск возможен после её завершения.", 409,
            )
        reserved = False
        try:
            if not restart.reserve():
                return _json_error("Перезапуск уже запрошен.", 409)
            reserved = True
            # Принятый restart удерживает допуск до выхода процесса:
            # в задержке перед завершением новая загрузка не должна начаться.
            return response
        finally:
            if not reserved:
                lease.release()

    return [
        Route(
            "/api/v1/server/restart",
            restart_api,
            methods=["POST"],
            name="dashboard_server_restart",
        ),
    ]


def routes(
    registry: Registry,
    *,
    mode: str | None = None,
    static_dir: Path | None = None,
    reference: ReferenceService | None = None,
    restart: RestartController | None = None,
    capabilities: CapabilityRuntime | None = None,
    intake: IntakeApiService | None = None,
    role_tools_refresh: Callable[[], Awaitable[bool]] | None = None,
) -> list[Route]:
    """Вернуть ровно один UI-контур, не затрагивая ``/mcp`` и ``/health``."""
    selected = dashboard_mode() if mode is None else mode
    if selected not in DASHBOARD_MODES:
        raise DashboardModeError(
            "Режим дашборда должен быть одним из: on, off."
        )
    if selected == DASHBOARD_OFF:
        return []
    if reference is None:
        reference = ReferenceService.discover(registry.data_dir)
    if restart is None:
        restart = RestartController(enabled=False)
    if capabilities is None:
        capabilities = CapabilityRuntime(
            CapabilitySettingsStore(registry.data_dir),
            active=(),
        )
    configured_dist = os.environ.get("MCP1C_DASHBOARD_DIST", "")
    root = static_dir or (
        Path(configured_dist) if configured_dist else DEFAULT_DASHBOARD_DIST
    )
    heavy = HeavyOperations()
    return [
        *_spa_routes(
            registry,
            root,
            reference,
            restart,
            capabilities,
            intake,
            role_tools_refresh,
            heavy,
        ),
        *_reference_routes(reference),
        *_server_routes(
            restart,
            reference=reference,
            capabilities=capabilities,
            heavy=heavy,
        ),
    ]
