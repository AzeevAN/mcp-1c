"""Переключаемая оболочка дашборда: off, classic или React SPA.

Предметные данные и запись остаются в том же процессе ``Registry``. React
получает только HTTP API и никогда не монтирует ``data/`` самостоятельно.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from pathlib import Path
from urllib.parse import quote, urlencode

from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException
from starlette.requests import Request
from starlette.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
)
from starlette.routing import Route

from . import __version__, coverage_log, dashboard as classic_dashboard, tools
from .dashboard import _authorized, _csrf_denied, _session_level, can_read
from .dictionary import ANY_CONFIGURATION, SOURCE_BUILTIN
from .graph_view import DEFAULT_LIMIT as DEFAULT_GRAPH_LIMIT
from .graph_view import bounds as graph_bounds
from .graph_view import neighbourhood
from .registry import (
    KIND_EXTENSION,
    KIND_MODULES,
    KIND_SYNTAX,
    Registry,
    RegistryError,
)
from .process_restart import RestartController
from .reference_provider import (
    MAX_REFERENCE_DB_BYTES,
    ReferenceService,
    ReferenceValidationError,
)
from .render import DETAIL_LEVELS

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


def _card_payload(
    registry: Registry,
    *,
    kind: str,
    config: str,
    name: str,
    detail: str,
) -> dict:
    """Карточка тем же вызовом и тем же Markdown-рендерером, что в classic."""
    normalized_detail = detail if detail in DETAIL_LEVELS else "fields"
    markdown = classic_dashboard._card_text(
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
        "html": classic_dashboard.render_markdown(markdown),
    }


_GRAPH_LIMIT_OPTIONS = (15, 30, 60, 150, 400)


def _graph_payload(
    registry: Registry,
    *,
    config: str,
    name: str,
    limit: int,
) -> dict:
    """Окрестность с готовой серверной раскладкой для classic и SPA."""
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
            "color": classic_dashboard.KIND_COLORS.get(
                node.kind, classic_dashboard.KIND_FALLBACK
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
                "color": classic_dashboard.KIND_COLORS.get(
                    kind, classic_dashboard.KIND_FALLBACK
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
        # classic UI показывает для них только число по тому же контракту.
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
) -> list[Route]:
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
                classic_dashboard._apply_dictionary_change,
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
                classic_dashboard._apply_dictionary_change,
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
                classic_dashboard._apply_dictionary_change,
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
                classic_dashboard._apply_dictionary_change,
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
        payload = _admin_sources_payload(prepared)
        payload["reference"] = reference.payload(detailed=True)
        payload["runtime"] = {"self_restart": restart.enabled}
        return JSONResponse(payload)

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
        if suffix not in (".zip", ".hbk", ".json"):
            await form.close()
            return _json_error("Принимаются только .zip, .hbk и .json.", 400)

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
        for route in classic_routes(registry, reference=reference)
        if (route.path == "/login" and "POST" in (route.methods or set()))
        or route.path == "/logout"
    )
    return result


def _reference_routes(
    reference: ReferenceService,
    restart: RestartController,
) -> list[Route]:
    """Общий API статуса, файла и controlled restart для classic и SPA."""

    async def reference_status_api(request: Request) -> JSONResponse:
        if not can_read(request):
            return _json_error("Нужен токен чтения.", 401)
        return JSONResponse(reference.payload(detailed=_authorized(request)))

    async def reference_upload_api(request: Request) -> JSONResponse:
        wants_html = "text/html" in request.headers.get("accept", "")

        def denied_response(message: str, status_code: int):
            if wants_html:
                return PlainTextResponse(message, status_code=status_code)
            return _json_error(message, status_code)

        denied = _mutation_denied(request, action="Загрузка общей справки")
        if denied is not None:
            return denied
        if not reference.managed_upload_available:
            return denied_response(
                "Загрузка выключена: база подключена по внешнему пути.", 409
            )
        try:
            form = await classic_dashboard._limited_upload_form(
                request,
                MAX_REFERENCE_DB_BYTES,
                allowed_fields=frozenset(),
            )
        except classic_dashboard._UploadTooLarge:
            return denied_response(
                f"Файл больше {MAX_REFERENCE_DB_BYTES // 1024 // 1024} МБ.",
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
        if Path(name).suffix.lower() != ".sqlite3":
            await form.close()
            return denied_response("Принимается только файл .sqlite3 schema v1.", 400)

        directory = reference.managed_path.parent
        temporary: Path | None = None
        try:
            directory.mkdir(parents=True, exist_ok=True)
            descriptor, raw_path = tempfile.mkstemp(
                dir=directory,
                prefix=".reference-upload-",
                suffix=".sqlite3",
            )
            os.close(descriptor)
            temporary = Path(raw_path)
            size = 0
            with temporary.open("wb") as output:
                while True:
                    chunk = await uploaded.read(classic_dashboard.CHUNK)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > MAX_REFERENCE_DB_BYTES:
                        raise classic_dashboard._UploadTooLarge
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            await form.close()
            pending = await run_in_threadpool(
                reference.install_candidate, temporary
            )
            temporary = None
            if wants_html:
                return RedirectResponse("/sources", status_code=303)
            return JSONResponse(
                {
                    "reference": reference.payload(detailed=True),
                    "pending": pending.payload(detailed=True),
                },
                status_code=201,
            )
        except classic_dashboard._UploadTooLarge:
            return denied_response(
                f"Файл больше {MAX_REFERENCE_DB_BYTES // 1024 // 1024} МБ.",
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
        wants_html = "text/html" in request.headers.get("accept", "")

        def denied_response(message: str, status_code: int):
            if wants_html:
                return PlainTextResponse(message, status_code=status_code)
            return _json_error(message, status_code)

        denied = _mutation_denied(request, action="Удаление общей справки")
        if denied is not None:
            return denied
        if not reference.managed_upload_available:
            return denied_response(
                "Удаление выключено: база подключена по внешнему пути.", 409
            )
        if wants_html:
            form = await request.form()
            confirmation = str(form.get("confirmation", ""))
        else:
            payload = await _json_body(request)
            confirmation = str(payload.get("confirmation", ""))
        if confirmation != "reference.sqlite3":
            return denied_response(
                "Для удаления подтвердите точное имя reference.sqlite3.", 400
            )
        try:
            pending = await run_in_threadpool(reference.remove_managed)
        except ReferenceValidationError as error:
            status_code = 404 if error.state == "missing" else 409
            return denied_response(str(error), status_code)
        except OSError:
            return denied_response("Не удалось удалить каноническую базу.", 500)
        if wants_html:
            return RedirectResponse("/sources", status_code=303)
        return JSONResponse(
            {
                "removed": "reference.sqlite3",
                "reference": reference.payload(detailed=True),
                "pending": (
                    pending.payload(detailed=True) if pending is not None else None
                ),
            }
        )

    async def restart_api(request: Request):
        wants_html = "text/html" in request.headers.get("accept", "")

        def denied_response(message: str, status_code: int):
            if wants_html:
                return PlainTextResponse(message, status_code=status_code)
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
        if wants_html:
            page = classic_dashboard._layout(
                "Перезапуск",
                "<h2>Сервер перезапускается</h2>"
                "<p>MCP-сеансы и вход в дашборд будут созданы заново. "
                "После восстановления откройте страницу «Источники».</p>"
                "<p><a href='/login?next=/sources'>Проверить состояние</a></p>",
            )
            return HTMLResponse(page.body, status_code=202, background=background)
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
) -> list[Route]:
    """Вернуть ровно один UI-контур, не затрагивая ``/mcp`` и ``/health``."""
    selected = dashboard_mode() if mode is None else mode
    if selected not in DASHBOARD_MODES:
        raise DashboardModeError(
            "Режим дашборда должен быть одним из: off, classic, spa."
        )
    if selected == DASHBOARD_OFF:
        return []
    if reference is None:
        reference = ReferenceService.discover(registry.data_dir)
    if restart is None:
        restart = RestartController(enabled=False)
    if selected == DASHBOARD_CLASSIC:
        from .dashboard import routes as classic_routes

        return [
            *classic_routes(
                registry,
                reference=reference,
                restart_available=restart.enabled,
            ),
            *_reference_routes(reference, restart),
        ]
    root = static_dir or Path(
        os.environ.get("MCP1C_DASHBOARD_DIST", "dashboard/dist")
    )
    return [
        *_spa_routes(registry, root, reference, restart),
        *_reference_routes(reference, restart),
    ]
