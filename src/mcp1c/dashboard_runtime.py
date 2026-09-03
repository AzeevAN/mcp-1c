"""Переключаемая оболочка современного дашборда: on или off.

Предметные данные и запись остаются в том же процессе ``Registry``. React
получает только HTTP API и никогда не монтирует ``data/`` самостоятельно.
"""

from __future__ import annotations

import asyncio
import os
import secrets
import shutil
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
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

from . import __version__, coverage_log, dashboard_backend, tools
from .auth import same_token
from .dashboard_backend import (
    COOKIE,
    LEVEL_ADMIN,
    LEVEL_READ,
    _SESSIONS,
    _admin_token,
    _api_token,
    _authorized,
    _csrf_denied,
    _session_level,
    can_read,
)
from .dictionary import ANY_CONFIGURATION, SOURCE_BUILTIN
from .graph_view import DEFAULT_LIMIT as DEFAULT_GRAPH_LIMIT
from .graph_view import bounds as graph_bounds
from .graph_view import neighbourhood
from .intake_v2_api import (
    IntakeApiConflict,
    IntakeApiError,
    IntakeApiNotFound,
    IntakeApiService,
    IntakeWork,
)
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
    RegistryError,
)
from .process_restart import RestartController
from .reference_provider import (
    DEFAULT_PAGE_CHARS,
    MAX_PAGE_CHARS,
    MAX_REFERENCE_ARTIFACT_BYTES,
    MIN_PAGE_CHARS,
    REFERENCE_ARTIFACT_NAME,
    REFERENCE_ARTIFACT_SUFFIX,
    ReferenceQueryError,
    ReferenceService,
    ReferenceValidationError,
)
from .role_access_service import (
    MAX_ACCESS_LIMIT as MAX_ROLE_ACCESS_LIMIT,
    MAX_FIND_LIMIT as MAX_ROLE_FIND_LIMIT,
    MAX_PAGE_CHARS as MAX_ROLE_PAGE_CHARS,
    MAX_ROLE_LIST_LIMIT,
    MIN_PAGE_CHARS as MIN_ROLE_PAGE_CHARS,
    RoleAccessQueryError,
    find_roles_payload,
    get_role_access_payload,
    http_status as role_http_status,
    roles_catalog_payload,
)
from .render import DETAIL_LEVELS
from .runtime_config import (
    DASHBOARD_MODES,
    DASHBOARD_OFF,
    DASHBOARD_ON,
    DashboardModeError,
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
                    "error": corpus.diagnostic if admin else "",
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
                "extension_runtime": _source_payload(
                    sources_by_id.get(
                        f"{configuration.name}:extension-runtime"
                    )
                ),
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
            "phrase_chars": dashboard_backend.MAX_QUERY_CHARS,
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
                        "kind": dashboard_backend._kind_title(
                            scope, hit.doc.kind
                        ),
                        "score": hit.score,
                        "reason": hit.reason,
                        "card_url": dashboard_backend._card_link(
                            scope, config, hit
                        ),
                    }
                    for position, hit in enumerate(hits, start=1)
                ],
                "hidden": [
                    {
                        "title": getattr(hit.doc.payload, "address", "")
                        or hit.doc.id,
                        "reason": dashboard_backend._hidden_reason(
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
) -> dict:
    """Карточка тем же вызовом и тем же Markdown-рендерером, что получает MCP."""
    normalized_detail = detail if detail in DETAIL_LEVELS else "fields"
    markdown = dashboard_backend._card_text(
        registry, kind, config, name, normalized_detail
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
        "detail_levels": list(DETAIL_LEVELS),
        "markdown": markdown,
        "html": dashboard_backend.render_markdown(markdown),
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


def _spa_routes(
    registry: Registry,
    static_dir: Path,
    reference: ReferenceService,
    restart: RestartController,
    intake: IntakeApiService | None,
    role_tools_refresh: Callable[[], Awaitable[bool]] | None,
) -> list[Route]:
    static_dir = static_dir.resolve()
    current_intake = intake
    intake_tasks: set[asyncio.Task[None]] = set()
    intake_start_lock = asyncio.Lock()

    def intake_service() -> IntakeApiService:
        nonlocal current_intake
        if current_intake is None:
            configured = os.environ.get("MCP1C_CONFIG_SOURCE", "").strip()
            current_intake = IntakeApiService.for_registry(
                registry,
                local_source=Path(configured) if configured else None,
            )
        return current_intake

    def intake_task_done(task: asyncio.Task[None]) -> None:
        intake_tasks.discard(task)
        if not task.cancelled():
            task.exception()

    async def run_intake_work(
        service: IntakeApiService, work: IntakeWork
    ) -> None:
        try:
            await run_in_threadpool(service.prepare, work)
        except Exception:
            # Application service уже сохранил bounded error в durable job.
            return

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
                "dashboard_mode": DASHBOARD_ON,
                "server": {"status": "ok", "version": __version__},
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
                    "reference_sources": len(snapshot.syntax_versions),
                },
            }
        )

    async def sources_api(request: Request) -> JSONResponse:
        if not can_read(request):
            return JSONResponse({"error": "Нужен токен чтения."}, status_code=401)
        try:
            snapshot = await run_in_threadpool(tools.sources_snapshot, registry)
        except RegistryError as error:
            # Публикация нового поколения может дважды обогнать CAS снимка.
            # Это ожидаемый конфликт чтения, а не авария сервера.
            if str(error).startswith("Источники изменились дважды;"):
                return _json_error(str(error), 409)
            raise
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
            len(phrase) > dashboard_backend.MAX_QUERY_CHARS
            for phrase in phrases
        ):
            return _json_error(
                "Каждая поисковая фраза должна содержать не более "
                f"{dashboard_backend.MAX_QUERY_CHARS} символов.",
                422,
            )
        try:
            results = await run_in_threadpool(
                dashboard_backend._run_queries,
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

    async def card_api(request: Request, *, kind: str) -> JSONResponse:
        if not can_read(request):
            return _json_error("Нужен токен чтения.", 401)
        config = request.query_params.get("config", "")
        name = request.query_params.get("name", "")
        detail = request.query_params.get("detail", "fields")
        if not name.strip():
            return _json_error("Не указано имя карточки.", 422)
        try:
            payload = await run_in_threadpool(
                _card_payload,
                registry,
                kind=kind,
                config=config,
                name=name,
                detail=detail,
            )
        except RegistryError as error:
            return _json_error(str(error), 409)
        return JSONResponse(payload)

    async def object_card_api(request: Request) -> JSONResponse:
        return await card_api(request, kind="object")

    async def syntax_card_api(request: Request) -> JSONResponse:
        return await card_api(request, kind="syntax")

    async def graph_api(request: Request) -> JSONResponse:
        if not can_read(request):
            return _json_error("Нужен токен чтения.", 401)
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

    async def dictionary_api(request: Request) -> JSONResponse:
        if not can_read(request):
            return _json_error("Нужен токен чтения.", 401)
        payload = await run_in_threadpool(
            _dictionary_payload,
            registry,
            config=request.query_params.get("config", ""),
            admin=_authorized(request),
        )
        return JSONResponse(payload)

    async def dictionary_alias_add_api(request: Request) -> JSONResponse:
        denied = _mutation_denied(request, action="Правка словаря")
        if denied is not None:
            return denied
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
                dashboard_backend._apply_dictionary_change,
                registry,
                lambda dictionary: dictionary.add_alias(
                    phrase, targets, config or None
                ),
            )
        except ValueError as error:
            return _json_error(str(error), 422)
        except OSError as error:
            return _json_error(f"Не удалось сохранить словарь: {error}", 409)
        return JSONResponse(
            {
                "changed": {
                    "phrase": normalized,
                    "targets": saved_targets,
                    "scope": config or ANY_CONFIGURATION,
                }
            }
        )

    async def dictionary_alias_remove_api(request: Request) -> JSONResponse:
        denied = _mutation_denied(request, action="Правка словаря")
        if denied is not None:
            return denied
        payload = await _json_body(request)
        phrase = payload.get("phrase")
        scope = payload.get("scope")
        if not isinstance(phrase, str) or not isinstance(scope, str):
            return _json_error("phrase и scope должны быть строками.", 422)
        if scope != ANY_CONFIGURATION and scope not in registry.snapshot().configurations:
            return _json_error("Неизвестная область псевдонима.", 422)
        try:
            removed = await run_in_threadpool(
                dashboard_backend._apply_dictionary_change,
                registry,
                lambda dictionary: dictionary.remove_alias(
                    phrase, None if scope == ANY_CONFIGURATION else scope
                ),
            )
        except OSError as error:
            return _json_error(f"Не удалось сохранить словарь: {error}", 409)
        if not removed:
            return _json_error("Такого локального псевдонима нет.", 404)
        return JSONResponse({"changed": {"phrase": phrase, "scope": scope}})

    async def dictionary_synonyms_add_api(request: Request) -> JSONResponse:
        denied = _mutation_denied(request, action="Правка словаря")
        if denied is not None:
            return denied
        payload = await _json_body(request)
        words = payload.get("words")
        if not isinstance(words, list) or not all(
            isinstance(word, str) for word in words
        ):
            return _json_error("words должен быть списком строк.", 422)
        try:
            group = await run_in_threadpool(
                dashboard_backend._apply_dictionary_change,
                registry,
                lambda dictionary: dictionary.add_synonyms(words),
            )
        except ValueError as error:
            return _json_error(str(error), 422)
        except OSError as error:
            return _json_error(f"Не удалось сохранить словарь: {error}", 409)
        return JSONResponse({"changed": {"words": group}})

    async def dictionary_synonyms_remove_api(request: Request) -> JSONResponse:
        denied = _mutation_denied(request, action="Правка словаря")
        if denied is not None:
            return denied
        payload = await _json_body(request)
        words = payload.get("words")
        if not isinstance(words, list) or not all(
            isinstance(word, str) for word in words
        ):
            return _json_error("words должен быть списком строк.", 422)
        try:
            removed = await run_in_threadpool(
                dashboard_backend._apply_dictionary_change,
                registry,
                lambda dictionary: dictionary.remove_synonyms(words),
            )
        except OSError as error:
            return _json_error(f"Не удалось сохранить словарь: {error}", 409)
        if not removed:
            return _json_error(
                "Такой локальной группы нет. Встроенные группы снимаются "
                "только изменением кода.",
                404,
            )
        return JSONResponse({"changed": {"words": words}})

    async def coverage_api(request: Request) -> JSONResponse:
        if not can_read(request):
            return JSONResponse({"error": "Нужен токен чтения."}, status_code=401)
        source_id = request.query_params.get("source_id", "")
        if not source_id:
            return JSONResponse(
                {"error": "Не указан source_id."}, status_code=400
            )
        snapshot = registry.snapshot()
        code = snapshot.modules.get(source_id)
        source = (
            code.source
            if code is not None and code.source is not None
            else snapshot.sources.get(source_id)
        )
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

    async def roles_catalog_api(request: Request) -> JSONResponse:
        if not can_read(request):
            return _json_error("Нужен токен чтения.", 401)
        try:
            payload = await run_in_threadpool(
                roles_catalog_payload,
                registry,
                config=role_param(request, "config", maximum=512) or None,
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

    async def roles_find_api(request: Request) -> JSONResponse:
        if not can_read(request):
            return _json_error("Нужен токен чтения.", 401)
        try:
            full_name = role_param(request, "full_name", maximum=1024)
            if not full_name:
                raise RoleAccessQueryError("Параметр full_name обязателен.")
            operations = request.query_params.getlist("operation")
            if not 1 <= len(operations) <= 6 or any(
                not operation or len(operation) > 32 for operation in operations
            ):
                raise RoleAccessQueryError(
                    "Нужно указать от одной до шести операций operation."
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

    async def roles_access_api(request: Request) -> JSONResponse:
        if not can_read(request):
            return _json_error("Нужен токен чтения.", 401)
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

    async def roles_restriction_api(request: Request) -> JSONResponse:
        if not can_read(request):
            return _json_error("Нужен токен чтения.", 401)
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

    async def admin_sources_api(request: Request) -> JSONResponse:
        denied = _admin_denied(request, action="Управление источниками")
        if denied is not None:
            return denied
        prepared = await run_in_threadpool(
            dashboard_backend._prepare_sources_page,
            registry,
            authorized=True,
        )
        payload = _admin_sources_payload(prepared)
        payload["reference"] = reference.payload(detailed=True)
        payload["runtime"] = {"self_restart": restart.enabled}
        return JSONResponse(payload)

    async def intake_snapshot_api(request: Request) -> JSONResponse:
        denied = _admin_denied(request, action="Просмотр кандидатов")
        if denied is not None:
            return denied
        try:
            payload = await run_in_threadpool(intake_service().snapshot)
        except (
            IntakeApiError,
            LifecycleError,
            OperationError,
            RegistryError,
        ) as error:
            return _json_error(str(error), 409)
        return JSONResponse(payload)

    async def intake_upload_api(request: Request) -> JSONResponse:
        denied = _mutation_denied(request, action="Загрузка кандидата")
        if denied is not None:
            return denied
        service = intake_service()
        try:
            form = await dashboard_backend._limited_upload_form(
                request,
                service.lifecycle.browser.max_upload_bytes,
                allowed_fields=frozenset(),
            )
        except dashboard_backend._UploadTooLarge:
            limit = service.lifecycle.browser.max_upload_bytes // 1024 // 1024
            return _json_error(f"Файл больше {limit} МБ.", 413)
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
            candidate = await run_in_threadpool(
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

    async def intake_start_api(request: Request) -> JSONResponse:
        denied = _mutation_denied(request, action="Запуск разбора")
        if denied is not None:
            return denied
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
        service = intake_service()
        async with intake_start_lock:
            if any(not task.done() for task in intake_tasks):
                return _json_error(
                    "Одновременно выполняется только одна тяжёлая операция intake.",
                    409,
                )
            try:
                work = await run_in_threadpool(
                    service.start,
                    candidate_id,
                    action,
                    job_id=job_id,
                    parent_configuration=parent,
                )
            except IntakeApiNotFound as error:
                return _json_error(str(error), 404)
            except (IntakeApiConflict, LifecycleError) as error:
                return _json_error(str(error), 409)
            except IntakeApiError as error:
                return _json_error(str(error), 422)
            task = asyncio.create_task(run_intake_work(service, work))
            intake_tasks.add(task)
            task.add_done_callback(intake_task_done)
        return JSONResponse(
            {"job": service.job_payload(work.job_id)},
            status_code=202,
        )

    async def intake_job_api(request: Request) -> JSONResponse:
        denied = _admin_denied(request, action="Просмотр операции")
        if denied is not None:
            return denied
        try:
            payload = await run_in_threadpool(
                intake_service().job_payload,
                request.path_params.get("job_id", ""),
            )
        except IntakeApiNotFound as error:
            return _json_error(str(error), 404)
        except (IntakeApiError, LifecycleError, OperationError) as error:
            return _json_error(str(error), 409)
        return JSONResponse({"job": payload})

    async def intake_confirm_api(request: Request) -> JSONResponse:
        denied = _mutation_denied(request, action="Публикация preview")
        if denied is not None:
            return denied
        payload = await _json_body(request)
        if set(payload) != {"job_id"} or not isinstance(
            payload.get("job_id"), str
        ):
            return _json_error("Нужен единственный строковый job_id.", 422)
        async with intake_start_lock:
            if any(not task.done() for task in intake_tasks):
                return _json_error(
                    "Нельзя публиковать preview во время другой тяжёлой операции.",
                    409,
                )
            try:
                job = await run_in_threadpool(
                    intake_service().confirm,
                    payload["job_id"],
                )
            except IntakeApiNotFound as error:
                return _json_error(str(error), 404)
            except (IntakeApiConflict, OperationConflict) as error:
                return _json_error(str(error), 409)
            except (IntakeApiError, LifecycleError, OperationError) as error:
                return _json_error(str(error), 422)
        if role_tools_refresh is not None:
            await role_tools_refresh()
        return JSONResponse({"job": job})

    async def upload_source_api(request: Request) -> JSONResponse:
        denied = _mutation_denied(request, action="Загрузка")
        if denied is not None:
            return denied
        try:
            form = await dashboard_backend._limited_upload_form(request)
        except dashboard_backend._UploadTooLarge:
            return _json_error(
                f"Файл больше {dashboard_backend.MAX_UPLOAD // 1024 // 1024} МБ.",
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
        if suffix not in (".zip", ".hbk", ".json"):
            await form.close()
            return _json_error("Принимаются только .zip, .hbk и .json.", 400)

        directory = tempfile.mkdtemp()
        target = Path(directory) / name
        job = dashboard_backend._start_job(name, 0)
        try:
            size = 0
            with target.open("wb") as output:
                while True:
                    chunk = await uploaded.read(dashboard_backend.CHUNK)
                    if not chunk:
                        break
                    size += len(chunk)
                    job["size"] = size
                    if size > dashboard_backend.MAX_UPLOAD:
                        raise dashboard_backend._UploadTooLarge
                    output.write(chunk)
        except dashboard_backend._UploadTooLarge:
            shutil.rmtree(directory, ignore_errors=True)
            dashboard_backend._JOBS.remove(job)
            await form.close()
            return _json_error(
                f"Файл больше {dashboard_backend.MAX_UPLOAD // 1024 // 1024} МБ.",
                413,
            )
        except OSError as error:
            shutil.rmtree(directory, ignore_errors=True)
            dashboard_backend._JOBS.remove(job)
            await form.close()
            return _json_error(f"Не удалось принять файл: {error}", 500)
        await form.close()

        task = asyncio.create_task(
            run_in_threadpool(
                dashboard_backend._run_job,
                registry,
                job,
                directory,
                target,
                suffix,
                allow_truncated=allow_truncated,
            )
        )
        dashboard_backend._ФОНОВЫЕ.add(task)
        task.add_done_callback(dashboard_backend._ФОНОВЫЕ.discard)
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
        scanner = dashboard_backend._scanner(registry)
        archive = registry.incoming_dir / name
        if not archive.is_file():
            return _json_error("Входящая выгрузка не найдена.", 404)

        busy = scanner.running
        if busy:
            job = dashboard_backend._start_job(name, archive.stat().st_size)
            job["state"] = dashboard_backend.JOB_FAILED
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
            job = dashboard_backend._start_job(name, archive.stat().st_size)
            job["state"] = dashboard_backend.JOB_FAILED
            job["error"] = (
                f"{name}: файл изменялся только что — похоже, копирование ещё "
                f"идёт. Повторите через {int(SETTLE_SECONDS)} с."
            )
            return JSONResponse(
                {"error": job["error"], "job": _job_payload(job)},
                status_code=409,
            )

        job = dashboard_backend._start_job(name, archive.stat().st_size)
        try:
            await run_in_threadpool(intake.planned_size, archive)
        except Exception as error:
            job["state"] = dashboard_backend.JOB_FAILED
            job["error"] = f"{archive.name}: не похоже на zip-архив ({error})"
            await run_in_threadpool(scanner.note_failure, archive, job["error"])
            return JSONResponse(
                {"error": job["error"], "job": _job_payload(job)},
                status_code=400,
            )

        configuration = str(payload.get("configuration", "")).strip()
        if configuration and configuration not in registry.snapshot().configurations:
            job["state"] = dashboard_backend.JOB_FAILED
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
            job["state"] = dashboard_backend.JOB_FAILED
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
                dashboard_backend._run_incoming,
                registry,
                scanner,
                job,
                archive,
                configuration or None,
            )
        )
        dashboard_backend._ФОНОВЫЕ.add(task)
        task.add_done_callback(dashboard_backend._ФОНОВЫЕ.discard)
        return JSONResponse({"job": _job_payload(job)}, status_code=202)

    async def clear_jobs_api(request: Request) -> JSONResponse:
        denied = _mutation_denied(request, action="Очистка журнала")
        if denied is not None:
            return denied
        completed = [
            job
            for job in dashboard_backend._JOBS
            if job["state"]
            in (dashboard_backend.JOB_DONE, dashboard_backend.JOB_FAILED)
        ]
        for job in completed:
            dashboard_backend._JOBS.remove(job)
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
        if role_tools_refresh is not None:
            await role_tools_refresh()
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
    async def login(request: Request):
        """Обменять токен на серверную cookie-сессию SPA."""
        if not _admin_token() and not _api_token():
            return PlainTextResponse(
                "Вход выключен: не задан ни ADMIN_TOKEN, ни API_TOKEN.", 404
            )
        form = await request.form()
        given = str(form.get("token", ""))
        if same_token(given, _admin_token()):
            level = LEVEL_ADMIN
        elif same_token(given, _api_token()):
            level = LEVEL_READ
        else:
            return PlainTextResponse("Неверный токен.", status_code=403)

        session = secrets.token_urlsafe(32)
        _SESSIONS[session] = level
        response = RedirectResponse(
            "/sources" if level == LEVEL_ADMIN else "/", status_code=303
        )
        response.set_cookie(
            COOKIE,
            session,
            httponly=True,
            samesite="strict",
            secure=request.url.scheme == "https",
            path="/",
        )
        return response

    async def logout(request: Request):
        """Закрыть cookie-сессию с обязательной same-origin проверкой."""
        denied = _csrf_denied(request)
        if denied is not None:
            return denied
        _SESSIONS.pop(request.cookies.get(COOKIE, ""), None)
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(
            COOKIE,
            path="/",
            secure=request.url.scheme == "https",
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
    restart: RestartController,
) -> list[Route]:
    """API статуса, файла и управляемого перезапуска для SPA."""

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

    async def reference_search_api(request: Request) -> JSONResponse:
        if not can_read(request):
            return _json_error("Нужен токен чтения.", 401)
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

    async def reference_item_api(request: Request) -> JSONResponse:
        if not can_read(request):
            return _json_error("Нужен токен чтения.", 401)
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

    async def reference_status_api(request: Request) -> JSONResponse:
        if not can_read(request):
            return _json_error("Нужен токен чтения.", 401)
        return JSONResponse(reference.payload(detailed=_authorized(request)))

    async def reference_upload_api(request: Request) -> JSONResponse:
        def denied_response(message: str, status_code: int):
            return _json_error(message, status_code)

        denied = _mutation_denied(request, action="Загрузка общей справки")
        if denied is not None:
            return denied
        if not reference.managed_upload_available:
            return denied_response(
                "Загрузка выключена: база подключена по внешнему пути.", 409
            )
        try:
            form = await dashboard_backend._limited_upload_form(
                request,
                MAX_REFERENCE_ARTIFACT_BYTES,
                allowed_fields=frozenset(),
            )
        except dashboard_backend._UploadTooLarge:
            return denied_response(
                f"Файл больше {MAX_REFERENCE_ARTIFACT_BYTES // 1024 // 1024} МБ.",
                413,
            )
        except MultiPartException:
            return denied_response(
                "Некорректная multipart-форма: разрешён один файл `file`.",
                400,
            )
        uploaded = form.get("file")
        if not isinstance(uploaded, UploadFile) or not uploaded.filename:
            await form.close()
            return denied_response("Файл не выбран.", 400)
        name = Path(uploaded.filename).name
        if Path(name).suffix.lower() != REFERENCE_ARTIFACT_SUFFIX:
            await form.close()
            return denied_response(
                "Принимается только подписанный файл .mcp1cref.", 400
            )

        directory = reference.managed_path.parent
        temporary: Path | None = None
        try:
            directory.mkdir(parents=True, exist_ok=True)
            descriptor, raw_path = tempfile.mkstemp(
                dir=directory,
                prefix=".reference-upload-",
                suffix=REFERENCE_ARTIFACT_SUFFIX,
            )
            os.close(descriptor)
            temporary = Path(raw_path)
            size = 0
            with temporary.open("wb") as output:
                while True:
                    chunk = await uploaded.read(dashboard_backend.CHUNK)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_REFERENCE_ARTIFACT_BYTES:
                        raise dashboard_backend._UploadTooLarge
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            await form.close()
            pending = await run_in_threadpool(
                reference.install_candidate, temporary
            )
            temporary = None
            return JSONResponse(
                {
                    "reference": reference.payload(detailed=True),
                    "pending": pending.payload(detailed=True),
                },
                status_code=201,
            )
        except dashboard_backend._UploadTooLarge:
            return denied_response(
                f"Файл больше {MAX_REFERENCE_ARTIFACT_BYTES // 1024 // 1024} МБ.",
                413,
            )
        except ReferenceValidationError as error:
            return denied_response(str(error), 422)
        except OSError:
            return denied_response("Не удалось сохранить каноническую базу.", 500)
        finally:
            await form.close()
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    async def reference_remove_api(request: Request):
        def denied_response(message: str, status_code: int):
            return _json_error(message, status_code)

        denied = _mutation_denied(request, action="Удаление общей справки")
        if denied is not None:
            return denied
        if not reference.managed_upload_available:
            return denied_response(
                "Удаление выключено: база подключена по внешнему пути.", 409
            )
        payload = await _json_body(request)
        confirmation = str(payload.get("confirmation", ""))
        if confirmation != REFERENCE_ARTIFACT_NAME:
            return denied_response(
                f"Для удаления подтвердите точное имя {REFERENCE_ARTIFACT_NAME}.", 400
            )
        try:
            pending = await run_in_threadpool(reference.remove_managed)
        except ReferenceValidationError as error:
            status_code = 404 if error.state == "missing" else 409
            return denied_response(str(error), status_code)
        except OSError:
            return denied_response("Не удалось удалить каноническую базу.", 500)
        return JSONResponse(
            {
                "removed": REFERENCE_ARTIFACT_NAME,
                "reference": reference.payload(detailed=True),
                "pending": (
                    pending.payload(detailed=True) if pending is not None else None
                ),
            }
        )

    async def restart_api(request: Request):
        def denied_response(message: str, status_code: int):
            return _json_error(message, status_code)

        denied = _mutation_denied(request, action="Перезапуск сервера")
        if denied is not None:
            return denied
        if not restart.enabled:
            return denied_response(
                "Перезапуск из дашборда выключен оператором.", 404
            )
        if reference.pending_status is None:
            return denied_response(
                "Нет ожидающего изменения общей справки.", 409
            )
        if not restart.reserve():
            return denied_response("Перезапуск уже запрошен.", 409)

        background = BackgroundTask(restart.terminate_after_response)
        return JSONResponse(
            {"state": "restarting", "runtime_id": restart.runtime_id},
            status_code=202,
            background=background,
        )

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
        Route(
            "/api/v1/reference/upload",
            reference_upload_api,
            methods=["POST"],
            name="dashboard_reference_upload",
        ),
        Route(
            "/api/v1/reference/remove",
            reference_remove_api,
            methods=["POST"],
            name="dashboard_reference_remove",
        ),
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
    configured_dist = os.environ.get("MCP1C_DASHBOARD_DIST", "")
    root = static_dir or (
        Path(configured_dist) if configured_dist else DEFAULT_DASHBOARD_DIST
    )
    return [
        *_spa_routes(
            registry,
            root,
            reference,
            restart,
            intake,
            role_tools_refresh,
        ),
        *_reference_routes(reference, restart),
    ]
