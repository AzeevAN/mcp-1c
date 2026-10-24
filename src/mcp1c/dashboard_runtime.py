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
                    "error": corpus.diagnostic if admin else "",
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


def _json_error(message: str, status_code: int) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status_code)


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


def _secure_cookie(request: Request) -> bool:
    """`Secure` нужен и за TLS proxy, даже если до backend дошла схема http.

    Схема запроса за proxy верна, только когда ему доверены X-Forwarded-*;
    режим `https-proxy` сам по себе означает, что браузер видит HTTPS.
    """
    if request.url.scheme == "https":
        return True
    try:
        return access_mode() == ACCESS_HTTPS_PROXY
    except AccessModeError:
        return False


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


def _dictionary_payload(
    registry: Registry,
    *,
    config: str,
    admin: bool,
) -> dict:
    """Эффективные правила с точным слоем из общего Dictionary."""
    names = list(registry.snapshot().configuration_names)
    selected = config if config in names else (names[0] if names else "")
    dictionary = registry.dictionary
    aliases = []
    for phrase, (targets, source) in sorted(
        dictionary.aliases_with_source(selected or None).items()
    ):
        scope = None
        if selected and phrase in dictionary.aliases.get(selected, {}):
            scope = selected
        elif phrase in dictionary.aliases.get(ANY_CONFIGURATION, {}):
            scope = ANY_CONFIGURATION
        aliases.append(
            {
                "phrase": phrase,
                "targets": list(targets),
                "source": source,
                "scope": scope,
                "removable": source != SOURCE_BUILTIN,
            }
        )
    stats = dictionary.stats()
    return {
        "api_version": "v1",
        "permissions": {"read": True, "admin": admin},
        "configuration_names": names,
        "configuration": selected,
        "aliases": aliases,
        # Встроенные группы не раскрываются построчно и не снимаются на месте —
        # Встроенные правила показываются только агрегированным числом.
        "synonym_groups": [list(group) for group in dictionary.synonym_groups],
        "stats": {
            "local_synonym_groups": stats["своих групп синонимов"],
            "builtin_synonym_groups": stats["встроенных групп синонимов"],
            "builtin_aliases": stats["встроенных псевдонимов"],
            "configurations_with_aliases": stats[
                "конфигураций с псевдонимами"
            ],
            "local_aliases": stats["псевдонимов"],
        },
    }


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


async def _json_body(request: Request) -> dict:
    try:
        payload = await request.json()
    except (ValueError, RecursionError):
        return {}
    return payload if isinstance(payload, dict) else {}


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
    current_intake = intake
    intake_tasks: set[asyncio.Task[None]] = set()
    intake_start_lock = asyncio.Lock()

    # Ставится под `admin_endpoint(..., mutation=True)`: права и CSRF
    # проверяются снаружи, до захвата допуска, поэтому чужой запрос не
    # занимает слот тяжёлой операции даже на мгновение.
    def heavy_endpoint(endpoint):
        @wraps(endpoint)
        async def admitted(request):
            if restart.requested:
                return _json_error("Перезапуск уже запрошен.", 409)
            lease = heavy.acquire()
            if lease is None:
                return _json_error(
                    "Одновременно допускается только одна тяжёлая операция: "
                    "уже принимается или выполняется загрузка/intake. "
                    "Дождитесь её завершения и повторите запрос.", 409,
                )
            token = heavy.current.set(lease)
            try:
                return await endpoint(request)
            finally:
                heavy.current.reset(token)
                lease.release()
        return admitted

    intake_create_lock = threading.Lock()

    def create_intake_service() -> IntakeApiService:
        nonlocal current_intake
        with intake_create_lock:
            if current_intake is None:
                current_intake = IntakeApiService.for_registry(registry)
            return current_intake

    async def intake_service() -> IntakeApiService:
        # Первое создание сервиса закрывает оборванные job прежнего процесса и
        # дочищает их work синхронным rmtree; на гигабайтной выгрузке это
        # секунды, и в event loop они остановили бы все запросы сервера.
        if current_intake is not None:
            return current_intake
        return await run_in_threadpool(create_intake_service)

    def intake_task_done(task: asyncio.Task[None]) -> None:
        intake_tasks.discard(task)
        if not task.cancelled():
            task.exception()

    async def run_intake_work(
        service: IntakeApiService, work: IntakeWork
    ) -> None:
        try:
            await heavy.run(service.prepare, work)
        except Exception:
            # Application service уже сохранил bounded error в durable job.
            return

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
    async def dictionary_api(request: Request) -> JSONResponse:
        payload = await run_in_threadpool(
            _dictionary_payload,
            registry,
            config=request.query_params.get("config", ""),
            admin=admin_authorized(request),
        )
        return JSONResponse(payload)

    @admin_endpoint("Правка словаря", mutation=True)
    async def dictionary_alias_add_api(request: Request) -> JSONResponse:
        payload = await _json_body(request)
        phrase = payload.get("phrase")
        config = payload.get("config", "")
        raw_targets = payload.get("targets")
        if not isinstance(phrase, str) or not isinstance(config, str):
            return _json_error("phrase и config должны быть строками.", 422)
        if not isinstance(raw_targets, list) or not all(
            isinstance(target, str) for target in raw_targets
        ):
            return _json_error("targets должен быть списком строк.", 422)
        targets = [target.strip() for target in raw_targets if target.strip()]
        try:
            normalized, saved_targets = await run_in_threadpool(
                dashboard_backend.apply_dictionary_change,
                registry,
                lambda dictionary: dictionary.add_alias(
                    phrase, targets, config or None
                ),
            )
        except DictionaryError as error:
            return _json_error(str(error), 409)
        except ValueError as error:
            return _json_error(str(error), 422)
        except OSError as error:
            return _json_error(
                dashboard_backend.storage_error_text("Не удалось сохранить словарь", error),
                409,
            )
        return JSONResponse(
            {
                "changed": {
                    "phrase": normalized,
                    "targets": saved_targets,
                    "scope": config or ANY_CONFIGURATION,
                }
            }
        )

    @admin_endpoint("Правка словаря", mutation=True)
    async def dictionary_alias_remove_api(request: Request) -> JSONResponse:
        payload = await _json_body(request)
        phrase = payload.get("phrase")
        scope = payload.get("scope")
        if not isinstance(phrase, str) or not isinstance(scope, str):
            return _json_error("phrase и scope должны быть строками.", 422)
        if scope != ANY_CONFIGURATION and scope not in (
            await run_in_threadpool(registry.snapshot)
        ).configurations:
            return _json_error("Неизвестная область псевдонима.", 422)
        try:
            removed = await run_in_threadpool(
                dashboard_backend.apply_dictionary_change,
                registry,
                lambda dictionary: dictionary.remove_alias(
                    phrase, None if scope == ANY_CONFIGURATION else scope
                ),
            )
        except DictionaryError as error:
            return _json_error(str(error), 409)
        except OSError as error:
            return _json_error(
                dashboard_backend.storage_error_text("Не удалось сохранить словарь", error),
                409,
            )
        if not removed:
            return _json_error("Такого локального псевдонима нет.", 404)
        return JSONResponse({"changed": {"phrase": phrase, "scope": scope}})

    @admin_endpoint("Правка словаря", mutation=True)
    async def dictionary_synonyms_add_api(request: Request) -> JSONResponse:
        payload = await _json_body(request)
        words = payload.get("words")
        if not isinstance(words, list) or not all(
            isinstance(word, str) for word in words
        ):
            return _json_error("words должен быть списком строк.", 422)
        try:
            group = await run_in_threadpool(
                dashboard_backend.apply_dictionary_change,
                registry,
                lambda dictionary: dictionary.add_synonyms(words),
            )
        except DictionaryError as error:
            return _json_error(str(error), 409)
        except ValueError as error:
            return _json_error(str(error), 422)
        except OSError as error:
            return _json_error(
                dashboard_backend.storage_error_text("Не удалось сохранить словарь", error),
                409,
            )
        return JSONResponse({"changed": {"words": group}})

    @admin_endpoint("Правка словаря", mutation=True)
    async def dictionary_synonyms_remove_api(request: Request) -> JSONResponse:
        payload = await _json_body(request)
        words = payload.get("words")
        if not isinstance(words, list) or not all(
            isinstance(word, str) for word in words
        ):
            return _json_error("words должен быть списком строк.", 422)
        try:
            removed = await run_in_threadpool(
                dashboard_backend.apply_dictionary_change,
                registry,
                lambda dictionary: dictionary.remove_synonyms(words),
            )
        except DictionaryError as error:
            return _json_error(str(error), 409)
        except OSError as error:
            return _json_error(
                dashboard_backend.storage_error_text("Не удалось сохранить словарь", error),
                409,
            )
        if not removed:
            return _json_error(
                "Такой локальной группы нет. Встроенные группы снимаются "
                "только изменением кода.",
                404,
            )
        return JSONResponse({"changed": {"words": words}})

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
            snapshot = await run_in_threadpool(registry.snapshot)
            registry_target_exists = (
                configuration in snapshot.configuration_names
                or source_id in snapshot.sources
                or configuration in snapshot.sources
            )
            try:
                service = (
                    (await intake_service())
                    if current_intake is not None
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
                if registry_target_exists or not has_intake_jobs:
                    await run_in_threadpool(registry.remove, source_id)
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
            "/api/v1/sources/platform/set",
            platform_set_api,
            methods=["POST"],
            name="dashboard_platform_set",
        ),
        Route(
            "/api/v1/sources/platform/clear",
            platform_clear_api,
            methods=["POST"],
            name="dashboard_platform_clear",
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
            "/api/v1/cards/object",
            object_card_api,
            methods=["GET"],
            name="dashboard_object_card",
        ),
        Route(
            "/api/v1/cards/syntax",
            syntax_card_api,
            methods=["GET"],
            name="dashboard_syntax_card",
        ),
        Route(
            "/api/v1/graph",
            graph_api,
            methods=["GET"],
            name="dashboard_graph",
        ),
        Route(
            "/api/v1/roles",
            roles_catalog_api,
            methods=["GET"],
            name="dashboard_roles_catalog",
        ),
        Route(
            "/api/v1/roles/find",
            roles_find_api,
            methods=["GET"],
            name="dashboard_roles_find",
        ),
        Route(
            "/api/v1/roles/access",
            roles_access_api,
            methods=["GET"],
            name="dashboard_roles_access",
        ),
        Route(
            "/api/v1/roles/objects",
            role_objects_api,
            methods=["GET"],
            name="dashboard_role_objects",
        ),
        Route(
            "/api/v1/roles/restriction",
            roles_restriction_api,
            methods=["GET"],
            name="dashboard_roles_restriction",
        ),
        Route(
            "/api/v1/dictionary",
            dictionary_api,
            methods=["GET"],
            name="dashboard_dictionary",
        ),
        Route(
            "/api/v1/dictionary/aliases",
            dictionary_alias_add_api,
            methods=["POST"],
            name="dashboard_dictionary_alias_add",
        ),
        Route(
            "/api/v1/dictionary/aliases/remove",
            dictionary_alias_remove_api,
            methods=["POST"],
            name="dashboard_dictionary_alias_remove",
        ),
        Route(
            "/api/v1/dictionary/synonyms",
            dictionary_synonyms_add_api,
            methods=["POST"],
            name="dashboard_dictionary_synonyms_add",
        ),
        Route(
            "/api/v1/dictionary/synonyms/remove",
            dictionary_synonyms_remove_api,
            methods=["POST"],
            name="dashboard_dictionary_synonyms_remove",
        ),
        Route(
            "/api/v1/sources/admin",
            admin_sources_api,
            methods=["GET"],
            name="dashboard_sources_admin",
        ),
        Route(
            "/api/v1/capabilities",
            capabilities_api,
            methods=["GET"],
            name="dashboard_capabilities",
        ),
        Route(
            "/api/v1/capabilities",
            capabilities_update_api,
            methods=["PUT"],
            name="dashboard_capabilities_update",
        ),
        Route("/api/v1/sources/directories", directory_sources_api, methods=["GET"]),
        Route("/api/v1/sources/directories/{operation}", directory_change_api, methods=["POST"]),
        Route(
            "/api/v1/sources/intake",
            intake_snapshot_api,
            methods=["GET"],
            name="dashboard_intake_snapshot",
        ),
        Route(
            "/api/v1/sources/intake/upload",
            intake_upload_api,
            methods=["POST"],
            name="dashboard_intake_upload",
        ),
        Route(
            "/api/v1/sources/intake/start",
            intake_start_api,
            methods=["POST"],
            name="dashboard_intake_start",
        ),
        Route(
            "/api/v1/sources/intake/jobs",
            intake_jobs_api,
            methods=["GET"],
            name="dashboard_intake_jobs",
        ),
        Route(
            "/api/v1/sources/intake/jobs/{job_id}",
            intake_job_api,
            methods=["GET"],
            name="dashboard_intake_job",
        ),
        Route(
            "/api/v1/sources/intake/confirm",
            intake_confirm_api,
            methods=["POST"],
            name="dashboard_intake_confirm",
        ),
        Route(
            "/api/v1/sources/intake/discard",
            intake_discard_api,
            methods=["POST"],
            name="dashboard_intake_discard",
        ),
        Route(
            "/api/v1/sources/intake/candidates/delete",
            intake_delete_candidate_api,
            methods=["POST"],
            name="dashboard_intake_delete_candidate",
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
        Route("/favicon.svg", public_asset, methods=["GET"], name="dashboard_favicon"),
        Route("/mcp-1c-logo.svg", public_asset, methods=["GET"], name="dashboard_logo"),
    ]
    result.extend(
        Route(path, spa_page, methods=["GET"], name=f"dashboard_spa_{index}")
        for index, path in enumerate(SPA_PAGE_PATHS)
    )
    async def login(request: Request):
        """Обменять токен на серверную cookie-сессию SPA."""
        if not admin_token() and not api_token():
            return PlainTextResponse(
                "Вход выключен: не задан ни ADMIN_TOKEN, ни API_TOKEN.", 404
            )
        if login_from_foreign_site(request):
            # До чтения формы и сверки: такая попытка не тратит лимит
            # перебора, общий у всех клиентов за одним адресом.
            return PlainTextResponse(
                "Вход принимается только со страницы этого сервера.", 403
            )
        form = await request.form()
        given = str(form.get("token", ""))
        # Лимит проверяется после чтения формы и вместе со сверкой: иначе
        # запросы, придержанные на теле, прошли бы проверку все сразу.
        level, retry_after = check_token(request, given)
        if retry_after is not None:
            return PlainTextResponse(
                "Слишком много неудачных попыток входа. Повторите позже.",
                status_code=429,
                headers={"Retry-After": str(retry_after)},
            )
        if level is None:
            return PlainTextResponse("Неверный токен.", status_code=403)

        session = create_session(level)
        response = RedirectResponse(
            "/sources" if level == LEVEL_ADMIN else "/", status_code=303
        )
        response.set_cookie(
            COOKIE,
            session,
            max_age=SESSION_TTL_SECONDS,
            httponly=True,
            samesite="strict",
            secure=_secure_cookie(request),
            path="/",
        )
        return response

    async def logout(request: Request):
        """Закрыть cookie-сессию с обязательной same-origin проверкой."""
        denied = csrf_denied(request)
        if denied is not None:
            return denied
        drop_session(request.cookies.get(COOKIE, ""))
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(
            COOKIE,
            path="/",
            secure=_secure_cookie(request),
            httponly=True,
            samesite="strict",
        )
        return response

    result.extend(
        (
            Route("/login", login, methods=["POST"], name="dashboard_login"),
            Route("/logout", logout, methods=["POST"], name="dashboard_logout"),
        )
    )
    return result


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
