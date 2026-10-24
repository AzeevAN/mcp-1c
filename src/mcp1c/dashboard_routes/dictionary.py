"""Чтение и административная правка локального словаря."""

from __future__ import annotations

from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import JSONResponse

from .. import dashboard_backend
from ..dashboard_backend import admin_authorized
from ..dictionary import ANY_CONFIGURATION, SOURCE_BUILTIN, DictionaryError
from ..registry import Registry
from .context import (
    DashboardContext,
    Endpoint,
    _json_body,
    _json_error,
    admin_endpoint,
    read_endpoint,
)


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


def handlers(context: DashboardContext) -> dict[str, Endpoint]:
    registry = context.registry

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

    return {
        "dictionary_api": dictionary_api,
        "dictionary_alias_add_api": dictionary_alias_add_api,
        "dictionary_alias_remove_api": dictionary_alias_remove_api,
        "dictionary_synonyms_add_api": dictionary_synonyms_add_api,
        "dictionary_synonyms_remove_api": dictionary_synonyms_remove_api,
    }
