"""Инструменты конфигурации и кода; зависимости передаёт фасад."""

from __future__ import annotations

import difflib
import hashlib
import heapq
import json
import sys
from array import array
from bisect import bisect_right
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Callable, Iterator

from . import cursor as cursor_codec, source_ids
from .bsl_lex import Процедура
from .code_coverage import (
    _NEUTRAL_COVERAGE_CATEGORIES,
    _safe_problem_reason,
    CodeProblemRow,
)
from .module_address import путь_модуля
from .registry_types import (
    KIND_EXTENSION,
    LoadedModules,
    RegistryError,
    SourceSnapshot,
    SourcesChanged,
)
from .render import (
    BRIEF,
    CallerSite,
    DETAIL_LEVELS,
    FIELDS,
    FULL,
    FormHandlerBinding,
    HTTPHandlerResolution,
    MetadataBinding,
    ProcedureMatch,
    ProcedureOutline,
    http_service_path,
    render_callers,
    render_http_service,
    render_subsystem,
    render_module_toc,
    render_object,
    render_procedure_card,
    render_procedure_search,
    render_standard_procedure_search,
    render_xdto,
)
from .search import FIELD_KIND_TITLES
from .platform_events import module_events
from .standard_procedure_intents import recognize_standard_procedure_intent
from .virtual_tables import analyze_virtual_tables
from .xdto import XDTOReadError, member_details
from .tools_common import MAX_LIMIT, _clamp, _notes_block
from .tools_context import (
    CodeToolsContext,
    _CodeSummary,
    _CodeView,
    _ConfigurationCodeSnapshot,
    _ObjectCodeIndex,
    _SignatureError,
    _РазборМодуля,
    _РазобраннаяПроцедура,
)

if TYPE_CHECKING:
    from .registry import Registry


def search_objects(
    registry: Registry,
    query: str,
    config: str | None = None,
    kind: str | None = None,
    limit: int = 10,
) -> str:
    """Найти объект конфигурации по описанию или части имени."""
    context = registry.resolve(config)
    if kind:
        unsupported = _unsupported_object_kind(context, kind)
        if unsupported:
            return unsupported + _notes_block(context)
    kinds = [kind] if kind else None
    hits = context.configuration.index.search(query, limit=_clamp(limit), kinds=kinds)

    if not hits:
        return (
            f"По запросу «{query}» в конфигурации {context.name} ничего не найдено."
            + _notes_block(context)
        )

    out = [f"# Найдено в {context.name}: «{query}»", ""]
    for hit in hits:
        obj = hit.doc.payload
        title = f" — {obj.synonym}" if obj.synonym else ""
        out.append(f"- `{obj.full_name}`{title}")
        summary = []
        if obj.attributes:
            summary.append(f"реквизитов {len(obj.attributes)}")
        if obj.tabular_parts:
            summary.append(f"ТЧ {len(obj.tabular_parts)}")
        if obj.movements:
            summary.append(f"движений {len(obj.movements)}")
        if summary:
            out.append(f"  {', '.join(summary)}")

    out += _fields_section(context, query)
    return "\n".join(out) + "\n" + _notes_block(context)


# Как агенты называют роль в `kind`. Роли не попадают в индекс объектов:
# структура приходит из источника A, где ролей нет, а объявленные права
# читаются отдельным ролевым слоем источника B.
_ROLE_KIND_NAMES = frozenset({"роль", "роли", "role", "roles"})


def _unsupported_object_kind(context, kind: str) -> str:
    """Отказ для вида, которого нет среди объектов конфигурации.

    Фильтр поискового индекса отбрасывает неизвестный вид молча, и пустая
    выдача читалась как факт о конфигурации: при независимой проверке
    инструментов агент искал роль через `kind="Роль"` и решил, что ролей с
    таким именем нет. Поэтому
    неизвестный вид называется прямо, с видами, которые здесь есть.
    """
    present = {obj.kind for obj in context.configuration.config.objects.values()}
    if kind in present:
        return ""
    if kind.strip().casefold() in _ROLE_KIND_NAMES:
        return (
            f"Роли не входят в поиск объектов конфигурации {context.name}: "
            "вид «Роль» здесь не индексируется. Роли по объекту и операциям "
            "подбирает `find_roles_for_access`, доступы роли по имени читает "
            "`get_role_access` — если эти инструменты есть в списке "
            "инструментов сервера."
        )
    return (
        f"Вида «{kind}» среди объектов конфигурации {context.name} нет, "
        "поиск с этим ограничением не выполнялся. Доступные виды: "
        + ", ".join(sorted(present))
        + ". Повторите поиск с одним из них или без `kind`."
    )


def _fields_section(context, query: str, limit: int = 5) -> list[str]:
    """Совпадения в реквизитах объектов.

    Отдельным разделом, потому что отвечает на другой вопрос: не «какой объект
    мне нужен», а «где хранится это значение». Запрос «номер телефона
    контрагента» описывает поле, а не объект, и по объектам не находится вовсе.
    """
    hits = context.configuration.field_index.search(query, limit=limit)
    if not hits:
        return []

    out = ["", f"## Найдено в реквизитах ({len(hits)})", ""]
    for hit in hits:
        ref = hit.doc.payload
        item = ref.field
        title = item.synonym or item.name
        out.append(f"- `{ref.full_name}` — {title}")
        out.append(
            f"  {item.type_spec()} · "
            f"{FIELD_KIND_TITLES.get(ref.kind, ref.kind)} объекта {ref.object_title}"
        )
    return out


def _http_service_cursor_digest(
    context,
    obj,
    detail: str,
    bindings: dict[str, HTTPHandlerResolution],
    code_source: SourceSnapshot | None,
) -> str:
    """Отпечаток ровно той HTTP-карточки, которую разрешено дочитать."""
    source = SourceSnapshot.capture(context.configuration.source)
    state = {
        "configuration": context.name,
        "source": {
            "id": source.id,
            "sha256": source.sha256,
            "stored_path": source.stored_path,
            "locator_generation": source.locator_generation,
            "selection_version": source.selection_version,
        },
        "structure_sha256": context.configuration.structure_sha256,
        "code_source": (
            {
                "id": code_source.id,
                "sha256": code_source.sha256,
                "stored_path": code_source.stored_path,
                "locator_generation": code_source.locator_generation,
                "selection_version": code_source.selection_version,
            }
            if code_source is not None
            else None
        ),
        "object": obj.full_name,
        "detail": detail,
        "extended": obj.extended,
        "bindings": [
            [name, binding.state, binding.address]
            for name, binding in sorted(bindings.items())
        ],
    }
    return hashlib.sha256(
        json.dumps(
            state,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _decode_http_service_cursor(cursor: str, digest: str) -> tuple[int, int]:
    try:
        state = cursor_codec.decode(cursor)
        if (
            not isinstance(state, dict)
            or set(state) != {"v", "sha256", "template", "method"}
            or type(state["v"]) is not int
            or state["v"] != 1
            or not isinstance(state["sha256"], str)
            or type(state["template"]) is not int
            or state["template"] < 0
            or type(state["method"]) is not int
            or state["method"] < 0
            or (state["template"] == 0 and state["method"] == 0)
        ):
            raise ValueError
    except cursor_codec.DECODE_ERRORS:
        raise RegistryError(
            "Некорректный курсор HTTP-сервиса; начните чтение заново."
        ) from None
    if state["sha256"] != digest:
        raise RegistryError(
            "Курсор относится к другому объекту или изменившемуся поколению; "
            "начните чтение заново."
        )
    return state["template"], state["method"]


def _encode_http_service_cursor(
    digest: str, template_offset: int, method_offset: int
) -> str:
    return cursor_codec.encode(
        {
            "v": 1,
            "sha256": digest,
            "template": template_offset,
            "method": method_offset,
        }
    )


def _subsystem_cursor_digest(context, obj, detail: str) -> str:
    source = SourceSnapshot.capture(context.configuration.source)
    state = {
        "configuration": context.name,
        "source": {
            "id": source.id,
            "sha256": source.sha256,
            "stored_path": source.stored_path,
            "locator_generation": source.locator_generation,
            "selection_version": source.selection_version,
        },
        "structure_sha256": context.configuration.structure_sha256,
        "object": obj.full_name,
        "detail": detail,
        "extended": obj.extended,
    }
    return hashlib.sha256(
        json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _decode_subsystem_cursor(cursor: str, digest: str) -> int:
    try:
        state = cursor_codec.decode(cursor)
        if (
            not isinstance(state, dict)
            or set(state) != {"v", "sha256", "offset"}
            or state.get("v") != 1
            or not isinstance(state.get("sha256"), str)
            or type(state.get("offset")) is not int
            or state["offset"] <= 0
        ):
            raise ValueError
    except cursor_codec.DECODE_ERRORS:
        raise RegistryError("Некорректный курсор подсистемы; начните чтение заново.") from None
    if state["sha256"] != digest:
        raise RegistryError(
            "Курсор относится к другому объекту или изменившемуся поколению; "
            "начните чтение заново."
        )
    return state["offset"]


def _encode_subsystem_cursor(digest: str, offset: int) -> str:
    return cursor_codec.encode({"v": 1, "sha256": digest, "offset": offset})


def get_object(
    registry: Registry,
    full_name: str,
    config: str | None = None,
    detail: str = FIELDS,
    *,
    cursor: str | None = None,
    _ctx: CodeToolsContext,
) -> str:
    """Структура объекта: реквизиты, табличные части, движения, связи."""
    if detail not in DETAIL_LEVELS:
        if cursor is not None:
            raise RegistryError(
                "Курсор требует исходный detail fields/full без изменений."
            )
        detail = FIELDS

    snapshot = None
    if detail == FIELDS or detail == FULL:
        name = registry.resolve(config).name
        snapshot = _ctx._configuration_code_snapshot(registry, name)
        context = snapshot.context
    else:
        context = registry.resolve(config)
    obj = context.configuration.config.get(full_name)

    if obj is None:
        if cursor is not None:
            raise RegistryError(
                "Курсор относится к другому объекту или конфигурации; "
                "начните чтение заново."
            )
        suggestion = "\n".join(
            f"- `{item}`" for item in similar_objects(context.configuration, full_name)
        )
        return (
            f"В конфигурации {context.name} нет объекта `{full_name}`.\n\n"
            + (f"Возможно, имелось в виду:\n{suggestion}\n" if suggestion else "")
            + _notes_block(context)
        )
    if cursor is not None and (obj.kind not in {"HTTPСервис", "Подсистема"} or detail == BRIEF):
        raise RegistryError(
            "Курсор поддерживается только для HTTP-сервиса или подсистемы "
            "с detail fields/full."
        )

    # Виртуальные таблицы собираются здесь, а не в рендере: они соединяют
    # метаданные конфигурации со справкой платформы, а рендер про справку
    # ничего не знает и знать не должен.
    table_report = analyze_virtual_tables(
        obj,
        context.syntax.tables if context.syntax else None,
        configuration=context.configuration.config,
    )

    body = ""
    if cursor is None:
        body = render_object(
            obj,
            detail,
            graph=context.configuration.graph,
            include_relations=obj.kind != "Подсистема",
            virtual_tables=table_report.tables,
            table_availability=table_report.availability,
            virtual_table_notes=table_report.notes,
        )
    if obj.kind == "HTTPСервис":
        bindings = (
            _http_handler_resolutions(snapshot.modules, obj)
            if snapshot is not None
            else {}
        )
        digest = _http_service_cursor_digest(
            context,
            obj,
            detail,
            bindings,
            snapshot.modules.capture.source if snapshot is not None else None,
        )
        template_offset = 0
        method_offset = 0
        if cursor is not None:
            template_offset, method_offset = _decode_http_service_cursor(
                cursor, digest
            )
            templates = obj.extended.get("url_templates", [])
            if not isinstance(templates, list) or template_offset >= len(templates):
                raise RegistryError(
                    "Курсор содержит недопустимое смещение; начните чтение заново."
                )
        try:
            page = render_http_service(
                obj,
                detail,
                bindings,
                platform=context.platform,
                template_offset=template_offset,
                method_offset=method_offset,
            )
        except ValueError:
            raise RegistryError(
                "Курсор содержит недопустимое смещение; начните чтение заново."
            ) from None
        body += page.text
        if page.next_template is not None:
            next_cursor = _encode_http_service_cursor(
                digest, page.next_template, page.next_method
            )
            arguments = {
                "full_name": obj.full_name,
                "config": context.name,
                "detail": detail,
                "cursor": next_cursor,
            }
            body += (
                "\nПродолжение (аргументы следующего вызова):\n"
                f"`get_object({json.dumps(arguments, ensure_ascii=False)})`\n"
            )
    if obj.kind == "Подсистема":
        digest = _subsystem_cursor_digest(context, obj, detail)
        offset = _decode_subsystem_cursor(cursor, digest) if cursor is not None else 0
        try:
            page = render_subsystem(obj, detail, offset=offset)
        except ValueError:
            raise RegistryError(
                "Курсор содержит недопустимое смещение; начните чтение заново."
            ) from None
        body += page.text
        if page.next_offset is not None:
            arguments = {
                "full_name": obj.full_name,
                "config": context.name,
                "detail": detail,
                "cursor": _encode_subsystem_cursor(digest, page.next_offset),
            }
            body += (
                "\nПродолжение (аргументы следующего вызова):\n"
                f"`get_object({json.dumps(arguments, ensure_ascii=False)})`\n"
            )
    if obj.kind == "ПакетXDTO" or obj.kind.startswith("ПакетXDTO."):
        relative_path = obj.extended.get("_member_relative_path")
        member_size = obj.extended.get("_member_size")
        member_sha256 = obj.extended.get("_member_sha256")
        if (
            detail != BRIEF
            and isinstance(relative_path, str)
            and isinstance(member_size, int)
            and isinstance(member_sha256, str)
        ):
            try:
                payload = _ctx.read_package_member(
                    Path(registry.data_dir) / context.configuration.source.stored_path,
                    relative_path,
                    expected_size=member_size,
                    expected_sha256=member_sha256,
                )
                body += render_xdto(
                    obj,
                    detail,
                    member_details(
                        payload,
                        obj,
                        context.configuration.config,
                    ),
                )
            except XDTOReadError as error:
                body += f"\n> Полная модель XDTO недоступна: {error}.\n"
    code = (
        _object_code_block(snapshot.modules, obj.full_name, detail, _ctx=_ctx)
        if snapshot is not None and cursor is None
        else ""
    )
    return body + code + _notes_block(context)


def _belongs_to_object(module: str, full_name: str) -> bool:
    lower_module = module.casefold()
    lower_name = full_name.casefold()
    return lower_module == lower_name or lower_module.startswith(lower_name + ".")


def _http_handlers(obj) -> set[str]:
    handlers: set[str] = set()
    templates = obj.extended.get("url_templates", [])
    if not isinstance(templates, list):
        return handlers
    for template in templates:
        if not isinstance(template, dict):
            continue
        methods = template.get("methods", [])
        if not isinstance(methods, list):
            continue
        for method in methods:
            if not isinstance(method, dict):
                continue
            handler = method.get("handler")
            if isinstance(handler, str):
                handlers.add(handler)
    return handlers


def _http_handler_resolutions(view: _CodeView, obj) -> dict[str, HTTPHandlerResolution]:
    handlers = _http_handlers(obj)
    if not handlers:
        return {}
    loaded = view.capture.loaded
    if loaded is None:
        return {
            handler.casefold(): HTTPHandlerResolution("unavailable")
            for handler in handlers
        }
    requested_module = obj.code_address or obj.full_name
    module = loaded.оглавление.канонический_модуль(requested_module)
    if module is None:
        return {
            handler.casefold(): HTTPHandlerResolution("module_missing")
            for handler in handlers
        }
    if loaded.оглавление.скомпилирован(module):
        return {
            handler.casefold(): HTTPHandlerResolution("unresolved")
            for handler in handlers
        }
    rows = loaded.оглавление.модуля(module)
    result: dict[str, HTTPHandlerResolution] = {}
    for handler in handlers:
        if not handler:
            result[handler.casefold()] = HTTPHandlerResolution("unresolved")
            continue
        matches = [row for row in rows if row.имя.casefold() == handler.casefold()]
        if not matches:
            result[handler.casefold()] = HTTPHandlerResolution("procedure_missing")
        elif len(matches) != 1 or not matches[0].функция:
            result[handler.casefold()] = HTTPHandlerResolution("unresolved")
        else:
            result[handler.casefold()] = HTTPHandlerResolution(
                "resolved",
                f"{module}::{matches[0].имя}",
            )
    return result


def _object_code_details(
    view: _CodeView, full_name: str,
    *, _ctx: CodeToolsContext,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    summary = view.summary
    if summary is None:
        return (), ()
    loaded = view.capture.loaded
    assert loaded is not None
    aggregates = _ctx._code_aggregates(loaded)
    index = aggregates.object_index
    if index is None:
        index = _build_object_code_index(summary, _ctx=_ctx)
        # Как у summary: гонка может посчитать один неизменяемый ответ
        # дважды, но публикуется сразу весь готовый пакет индексов.
        aggregates.object_index = index
    owner = full_name.casefold()
    return (
        index.modules_by_owner.get(owner, ()),
        index.forms_by_owner.get(owner, ()),
    )


def _address_owners(address: str) -> Iterator[str]:
    """Все владельцы по точному правилу `_belongs_to_object`."""
    key = address.casefold()
    yield key
    position = key.find(".")
    while position != -1:
        yield key[:position]
        position = key.find(".", position + 1)


def _build_object_code_index(summary: _CodeSummary, *, _ctx: CodeToolsContext) -> _ObjectCodeIndex:
    form_addresses = set(summary.forms)
    form_addresses.update(
        module for module in summary.modules if _ctx._is_form_module(module)
    )
    form_keys = frozenset(form.casefold() for form in form_addresses)
    modules_by_owner: dict[str, list[str]] = {}
    forms_by_owner: dict[str, list[str]] = {}
    for module in summary.modules:
        if module.casefold() in form_keys:
            continue
        for owner in _address_owners(module):
            modules_by_owner.setdefault(owner, []).append(module)
    for form in sorted(form_addresses, key=lambda value: (value.casefold(), value)):
        for owner in _address_owners(form):
            forms_by_owner.setdefault(owner, []).append(form)
    return _ObjectCodeIndex(
        form_keys,
        MappingProxyType({
            owner: tuple(values) for owner, values in modules_by_owner.items()
        }),
        MappingProxyType({
            owner: tuple(values) for owner, values in forms_by_owner.items()
        }),
    )


def _is_form_module(address: str) -> bool:
    """Форма по канонической грамматике адреса, без догадки по подстроке."""
    try:
        path = путь_модуля(address)
    except ValueError:
        return False
    return (
        "/Forms/" in path or path.startswith("CommonForms/")
    ) and path.endswith("/Ext/Form/Module.bsl")


def _problem_text(problem: CodeProblemRow) -> str:
    target = (
        f"`{problem.address}`"
        if problem.address is not None
        else f"неадресуемый кандидат #{problem.ordinal}"
    )
    return f"[{problem.category}] {target} — {problem.reason}"


def _object_code_block(view: _CodeView, full_name: str, detail: str, *, _ctx: CodeToolsContext) -> str:
    if detail == BRIEF:
        return ""
    state = _ctx._code_state_text(view)
    if view.capture.source is None or view.capture.loaded is None:
        state = "код не загружен"
    if view.summary is None:
        return (
            f"\nКод объекта: {state}.\n"
            if detail == FIELDS
            else f"\n## Код объекта\n\nИндекс кода: {state}.\n"
        )

    modules, forms = _object_code_details(view, full_name, _ctx=_ctx)
    loaded = view.capture.loaded
    assert loaded is not None
    form_keys = {form.casefold() for form in forms}
    object_rows: list[CodeProblemRow] = []
    for address, problems in (loaded.каталог.object_problems or {}).items():
        if address.casefold() not in form_keys:
            continue
        object_rows.extend(
            CodeProblemRow(
                problem.category,
                problem.address,
                problem.ordinal,
                _safe_problem_reason(problem.reason),
            )
            for problem in problems
            if problem.category not in _NEUTRAL_COVERAGE_CATEGORIES
        )
    for address, problems in loaded.формы.object_problems.items():
        if address.casefold() not in form_keys:
            continue
        object_rows.extend(
            CodeProblemRow(
                problem.категория,
                problem.адрес,
                0,
                _safe_problem_reason(problem.причина),
                problem.маркер,
            )
            for problem in problems
            if problem.категория not in _NEUTRAL_COVERAGE_CATEGORIES
        )
    проблемы_форм = tuple(
        sorted(
            {
                (item.category, item.address, item.ordinal, item.reason): item
                for item in object_rows
            }.values(),
            key=lambda item: (
                item.address.casefold() if item.address else "",
                item.address or "",
                item.category,
                item.ordinal,
                item.reason,
            ),
        )
    )
    предупреждения: list[str] = []
    if проблемы_форм:
        предупреждения.append(
            "> **Покрытие форм объекта неполно.** Нулевые счётчики внутри "
            "частично прочитанной формы не доказывают отсутствие данных."
        )
        предупреждения.extend(f"> - {_problem_text(item)}" for item in проблемы_форм)
    if view.summary.coverage.unsupported_addresses:
        предупреждения.append(
            "> **Список форм объекта может быть неполон:** неподдержанных "
            f"адресов: {view.summary.coverage.unsupported_addresses}; их "
            "принадлежность конкретному объекту не доказана."
        )
    префикс = ""
    if предупреждения:
        префикс = "\n" + "\n".join(предупреждения) + "\n"
    compiled = {
        module.casefold() for module in view.summary.compiled_modules
    }
    if detail == FIELDS:
        compiled_count = sum(
            1 for module in modules if module.casefold() in compiled
        )
        suffix = (
            f", с недоступным исходным текстом {compiled_count}"
            if compiled_count
            else ""
        )
        return префикс + (
            f"\nКод объекта: модулей {len(modules)}, форм {len(forms)}"
            f"{suffix}.\n"
        )

    out: list[str] = предупреждения.copy()
    if modules:
        out.extend(
            [
                "",
                "## Модули объекта",
                "",
                *(
                    f"- `{item}`"
                    + (
                        " — исходный текст недоступен"
                        if item.casefold() in compiled
                        else ""
                    )
                    for item in modules
                ),
            ]
        )
    else:
        out.extend(["", "## Модули объекта", "", "Модулей нет."])
    if forms:
        out.extend(
            ["", "## Формы объекта", "", *(f"- `{item}`" for item in forms)]
        )
    else:
        out.extend(["", "## Формы объекта", "", "Форм нет."])
    return "\n".join(out).rstrip() + "\n"


def similar_objects(
    configuration, full_name: str, limit: int = 5, *, same_kind_only: bool = False
) -> list[str]:
    """Объекты, которые могли иметь в виду под несуществующим именем.

    Полнотекстовый поиск по строке `Документ.НетТакого` поднимал объекты
    любых видов по слову «документ». Сначала — объекты того же вида с
    близким именем, и только если их не хватило — поиск по самому имени.
    """
    objects = configuration.config.objects
    вид, _, имя = full_name.strip().partition(".")
    if not имя:
        вид, имя = "", вид
    того_же_вида = {
        candidate.partition(".")[2].casefold(): candidate
        for candidate in objects
        if candidate.partition(".")[0].casefold() == вид.casefold()
    }
    найденные = [
        того_же_вида[key]
        for key in difflib.get_close_matches(
            имя.casefold(), list(того_же_вида), n=limit, cutoff=0.6
        )
    ]
    # Сравнение конфигураций ищет тот же объект: другой вид там — шум.
    if len(найденные) < limit and имя and not same_kind_only:
        for hit in configuration.index.search(имя, limit=limit * 2):
            if hit.doc.id in objects and hit.doc.id not in найденные:
                найденные.append(hit.doc.id)
    return [item for item in найденные if item != full_name][:limit]


def get_related(
    registry: Registry,
    full_name: str,
    config: str | None = None,
    limit: int = 40,
    cursor: str | None = None,
    *, _ctx: CodeToolsContext,
) -> str:
    """Что задевает задача: движения, ссылки, зависимости объекта.

    Только прямые связи, и это решение, а не недоделка. Раздел «в радиусе N»
    здесь был и снят 2026-08-18: он обещал соседей соседей, а отдавал
    продолжение списка прямых соседей — обход упирался в свой лимит в 200
    объектов раньше, чем делал второй шаг (у `Справочник.Номенклатура` одних
    прямых соседей 264). Замер: 199 строк раздела, все до одной с расстоянием
    1, ценой 3 000 лишних токенов на вызов.

    Поднимать лимит смысла нет: на двух шагах достаётся тысяча объектов, на
    трёх — 1 700 из 5 637, то есть треть конфигурации. Список имён без
    объяснения, через что идёт связь, бесполезен и показанный целиком —
    информацию несёт ребро, а не узел. Отвечать «через что» должны пути, а не
    окрестность; такой инструмент потребует живого случая и проверки точности.
    """
    name = registry.resolve(config).name
    snapshot = _ctx._configuration_code_snapshot(registry, name)
    context = snapshot.context
    graph = context.configuration.graph

    if full_name not in context.configuration.config.objects:
        похожие = similar_objects(context.configuration, full_name)
        return (
            f"В конфигурации {context.name} нет объекта `{full_name}`."
            + (
                "\n\nВозможно, имелось в виду:\n"
                + "\n".join(f"- `{item}`" for item in похожие)
                + "\n"
                if похожие
                else ""
            )
            + _notes_block(context)
        )

    overrides, unavailable = _extension_overrides(snapshot, _ctx=_ctx)
    out = [f"# Связи `{full_name}` в {context.name}", ""]
    for extension_name, view in snapshot.extensions:
        if view.summary is None or view.capture.loaded is None:
            continue
        warning = _ctx._loaded_partial_warning(
            view.capture.loaded,
            corpus=f"расширения `{extension_name}`",
        )
        if warning:
            out.append(f"> {warning}")
    for extension_name, state in unavailable:
        out.append(
            f"> Перекрытия расширения `{extension_name}` недоступны: {state}."
        )
    if unavailable or any(
        view.summary is not None
        and view.capture.loaded is not None
        and _ctx._loaded_partial_warning(view.capture.loaded) is not None
        for _name, view in snapshot.extensions
    ):
        out.append("")

    def related_name(value: str) -> str:
        extensions = overrides.get(value.casefold(), ())
        if not extensions:
            return f"`{value}`"
        label = "расширением" if len(extensions) == 1 else "расширениями"
        names = ", ".join(f"`{item}`" for item in extensions)
        return f"`{value}` *(перекрыто {label} {names})*"

    # Один список: сначала исходящие, затем входящие. Обрезка «… ещё 75»
    # без продолжения прятала большую часть ответа на «что сломается»: у
    # документа со 115 входящими связями агент видел 40.
    outgoing = graph.outgoing(full_name, include_weak=False)
    incoming = graph.incoming(full_name)
    rows = [(0, edge.target, edge.title) for edge in outgoing] + [
        (1, edge.source, edge.title) for edge in incoming
    ]
    # Свой предел: общий `_clamp` (50) рассчитан на поиск, а связи читаются
    # списком до конца.
    limit = max(1, min(int(limit), 200))
    digest = hashlib.sha256(
        json.dumps([context.name, full_name, rows], ensure_ascii=False).encode("utf-8")
    ).hexdigest()
    offset = 0
    if cursor is not None:
        try:
            state = cursor_codec.decode(cursor)
        except cursor_codec.DECODE_ERRORS:
            state = None
        if (
            not isinstance(state, dict)
            or set(state) != {"v", "sha256", "offset"}
            or state["v"] != 1
            or state["sha256"] != digest
            or type(state["offset"]) is not int
            or not 0 < state["offset"] < len(rows)
        ):
            raise RegistryError(
                "Курсор относится к другому объекту, конфигурации или устаревшему "
                "списку связей; начните чтение заново."
            )
        offset = state["offset"]

    end = min(offset + limit, len(rows))
    titles = (
        f"## Ссылается на ({len(outgoing)})",
        f"## Ссылаются на него ({len(incoming)})",
    )
    previous = None
    for side, name, title in rows[offset:end]:
        if side != previous:
            if previous is not None:
                out.append("")
            out.extend((titles[side], ""))
            previous = side
        out.append(f"- {related_name(name)} — {title}")
    if previous is not None:
        out.append("")
    if end < len(rows):
        next_cursor = cursor_codec.encode({"v": 1, "sha256": digest, "offset": end})
        arguments = json.dumps(
            {"full_name": full_name, "config": context.name,
             "cursor": next_cursor, "limit": limit},
            ensure_ascii=False,
        )
        out.extend((
            f"Связей: {len(rows)}. Показаны {offset + 1}–{end}.",
            "",
            "Продолжение (аргументы следующего вызова):",
            f"`get_related({arguments})`",
            "",
        ))

    return "\n".join(out) + "\n" + _notes_block(context)


def _module_owner_lookup(objects):
    """Владелец модуля по объектам конфигурации — без перебора всех объектов.

    Раньше на каждый перекрытый модуль перебирались все объекты с `casefold`:
    на 1 887 объектах это 22 мс на 50 перекрытых модулей и 87 мс на 200 при
    каждом `get_related`. Владельцем может быть только объект, чьё имя без
    учёта регистра равно адресу модуля или его префиксу до точки, поэтому
    достаточно словаря по этим префиксам. Из нескольких подходящих выбирается
    тот же, что первым шёл в прежнем порядке: длинное имя раньше короткого.
    """
    по_ключу: dict[str, list[str]] = {}
    for full_name in objects:
        по_ключу.setdefault(full_name.casefold(), []).append(full_name)

    def owner_of(module: str) -> str | None:
        ключ = module.casefold()
        кандидаты = list(по_ключу.get(ключ, ()))
        позиция = ключ.find(".")
        while позиция != -1:
            кандидаты.extend(по_ключу.get(ключ[:позиция], ()))
            позиция = ключ.find(".", позиция + 1)
        if not кандидаты:
            return None
        return min(
            кандидаты, key=lambda value: (-len(value), value.casefold(), value)
        )

    return owner_of


def _extension_overrides(
    snapshot: _ConfigurationCodeSnapshot,
    *, _ctx: CodeToolsContext,
) -> tuple[dict[str, tuple[str, ...]], list[tuple[str, str]]]:
    config = snapshot.context.configuration.config
    owner_of = _module_owner_lookup(config.objects)
    found: dict[str, list[str]] = {}
    unavailable: list[tuple[str, str]] = []
    for extension_name, view in snapshot.extensions:
        if view.summary is None:
            unavailable.append((extension_name, _ctx._code_state_text(view)))
            continue
        for module in view.summary.overridden_modules:
            owner = owner_of(module)
            if owner is not None:
                found.setdefault(owner.casefold(), []).append(extension_name)
    result = {
        owner: tuple(sorted(set(names), key=lambda value: (value.casefold(), value)))
        for owner, names in found.items()
    }
    return result, unavailable


def compare_configurations(
    registry: Registry,
    full_name: str,
    configs: list[str] | None = None,
    *,
    cursor: str | None = None,
    limit: int = 40,
) -> str:
    """Имена реквизитов одного объекта в явной паре; различия без потерь."""
    if type(limit) is not int or not 1 <= limit <= 100:
        raise RegistryError("limit должен быть целым числом от 1 до 100.")
    for _ in range(2):
        # Resolve применяет barrier, а CAS не даёт ответить по поколению,
        # которое сменилось между проверкой доступа и построением различий.
        snapshot = registry.snapshot()
        names = list(snapshot.configuration_names) if configs is None else list(configs)
        if len(names) != 2 or any(not isinstance(name, str) or not name for name in names) or names[0] == names[1]:
            raise RegistryError(
                "Укажите ровно две разные конфигурации в configs. "
                "Без configs автоматический выбор возможен, только если загружены ровно две. "
                "Список имён доступен через list_configurations."
            )
        if any(name not in snapshot.configurations for name in names):
            missing = next(name for name in names if name not in snapshot.configurations)
            raise RegistryError(f"Неизвестная конфигурация: {missing}. Проверьте list_configurations.")
        contexts = [registry.resolve(name) for name in names]
        if any(
            context.configuration is not snapshot.configurations[name]
            for name, context in zip(names, contexts)
        ):
            continue
        answer = _compare_configurations_snapshot(
            snapshot, full_name, names, cursor=cursor, limit=limit
        )
        if registry.snapshot_is_current(snapshot):
            return answer
    raise SourcesChanged("Поколение конфигураций изменилось дважды; повторите сравнение.")


def _compare_configurations_snapshot(
    snapshot, full_name: str, names: list[str], *, cursor: str | None, limit: int
) -> str:
    """Построить страницу сравнения из одного проверяемого снимка."""

    out = [f"# Сравнение `{full_name}`", "",
           "Сравниваются только имена реквизитов. Типы, квалификаторы и содержимое "
           "табличных частей не сравниваются.", ""]
    objects = []
    rows = []

    for name in names:
        loaded = snapshot.configurations.get(name)
        if loaded is None:
            raise RegistryError(f"Неизвестная конфигурация: {name}. Проверьте list_configurations.")
        obj = loaded.config.get(full_name)
        objects.append(obj)
        if obj is None:
            out.append(f"- **{name}** — объекта нет")
            похожие = similar_objects(loaded, full_name, limit=3, same_kind_only=True)
            if похожие:
                out.append("  Похожие: " + ", ".join(f"`{item}`" for item in похожие))
            rows.append(None)
            continue
        rows.append([sorted({a.name for a in obj.attributes}), len(obj.tabular_parts), len(obj.movements)])
        out.append(
            f"- **{name}** — реквизитов {len(obj.attributes)}, "
            f"ТЧ {len(obj.tabular_parts)}, движений {len(obj.movements)}"
        )
    out.append("")

    digest = hashlib.sha256(json.dumps(
        [full_name, names, rows], ensure_ascii=False, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    offset = 0
    if cursor is not None:
        try:
            state = cursor_codec.decode(cursor)
            if (not isinstance(state, dict) or set(state) != {"v", "sha256", "offset"}
                    or type(state["v"]) is not int or state["v"] != 1
                    or type(state["offset"]) is not int or state["offset"] <= 0):
                raise ValueError
        except cursor_codec.DECODE_ERRORS:
            raise RegistryError("Некорректный курсор сравнения; начните чтение заново.") from None
        if state["sha256"] != digest:
            raise RegistryError("Курсор относится к другой паре, объекту или изменившемуся сравнению; начните заново.")
        offset = state["offset"]

    if any(obj is None for obj in objects):
        if cursor is not None:
            raise RegistryError("Курсор недоступен: объект отсутствует в выбранной паре.")
        out.append("Сравнение имён реквизитов не выполнено: объект должен существовать в обеих конфигурациях.")
        return "\n".join(out) + "\n"

    left, right = objects
    left_attrs = {a.name for a in left.attributes}
    right_attrs = {a.name for a in right.attributes}

    only_left = sorted(left_attrs - right_attrs)
    only_right = sorted(right_attrs - left_attrs)

    differences = [(0, name) for name in only_left] + [(1, name) for name in only_right]
    if cursor is not None and offset >= len(differences):
        raise RegistryError("Курсор содержит недопустимое смещение; начните чтение заново.")
    if not differences:
        out.append("Имена реквизитов совпадают в выбранной паре.")
        return "\n".join(out) + "\n"

    end = min(offset + limit, len(differences))
    out.extend((f"Различий: {len(differences)}. Показаны {offset + 1}–{end}.", ""))
    previous_side = None
    for side, name in differences[offset:end]:
        if side != previous_side:
            out.extend((f"## Реквизиты только в {names[side]}", ""))
            previous_side = side
        out.append(f"- `{name}`")
    if end < len(differences):
        next_cursor = cursor_codec.encode(
            {"v": 1, "sha256": digest, "offset": end}
        )
        arguments = json.dumps({"full_name": full_name, "configs": names,
                                "cursor": next_cursor, "limit": limit}, ensure_ascii=False)
        out.extend(("", "Продолжение (аргументы следующего вызова):",
                    f"`compare_configurations({arguments})`"))
    else:
        out.extend(("", "Все различия имён реквизитов прочитаны."))

    return "\n".join(out) + "\n"


# --------------------------------------------------------------- код модулей


# Предел ответа скачивания, по нормализованному тексту. Приём ограничивает
# модуль отдельно и выше — `resource_limits.INTAKE_MAX_MODULE_BYTES` (17 МиБ
# сырой записи, по замеру крупных выгрузок): модуль 16–17 МиБ доступен всем
# инструментам, но файлом целиком не отдаётся. Числа разные намеренно.
MAX_MODULE_SOURCE_FILE_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class ModuleSourceFile:
    """Нормализованный исходник и идентичность прочитанного поколения."""

    address: str
    config: str
    extension: str | None
    content: bytes
    sha256: str
    revision: str

    @property
    def size_bytes(self) -> int:
        return len(self.content)

    @property
    def state(self) -> str:
        return "empty" if not self.content else "readable"


def _module_source_file_once(
    registry: Registry,
    address: str,
    config: str | None,
    extension: str | None,
    *, _ctx: CodeToolsContext,
) -> ModuleSourceFile:
    context, loaded = _ctx._selected_modules(registry, config, extension)
    состояние = _modules_availability_message(registry, context, loaded)
    if состояние is not None:
        raise RegistryError(состояние)
    assert loaded is not None
    canonical = loaded.оглавление.канонический_модуль(address)
    if canonical is None:
        if not _modules_are_current(registry, loaded):
            raise _StaleModules
        raise RegistryError(f"Модуль `{address}` в загруженном коде не найден.")
    if loaded.оглавление.скомпилирован(canonical):
        if not _modules_are_current(registry, loaded):
            raise _StaleModules
        raise RegistryError("Исходный текст модуля недоступен: модуль скомпилирован.")
    текст = _ctx._read_module_snapshot(registry, loaded, canonical)
    content = текст.encode("utf-8")
    if len(content) > MAX_MODULE_SOURCE_FILE_BYTES:
        raise RegistryError("Модуль превышает предел файловой выдачи 16 МиБ.")
    digest = hashlib.sha256(content).hexdigest()
    identity = loaded.каталог.identity
    revision = hashlib.sha256(
        "\0".join((
            identity.source_id, identity.source_sha256,
            str(identity.generation), canonical, digest,
        )).encode("utf-8")
    ).hexdigest()
    if not _modules_are_current(registry, loaded):
        raise _StaleModules
    return ModuleSourceFile(
        address=canonical, config=context.name, extension=extension,
        content=content, sha256=digest, revision=revision,
    )


def get_module_source_file(
    registry: Registry,
    address: str,
    config: str | None = None,
    extension: str | None = None,
    *, _ctx: CodeToolsContext,
) -> ModuleSourceFile:
    """Прочитать точный модуль одного актуального поколения для скачивания."""
    if not isinstance(address, str) or not address.strip() or "::" in address:
        raise RegistryError("address должен быть точным адресом модуля без `::`.")
    address = address.strip()
    return _retry_stale_modules(
        lambda: _module_source_file_once(registry, address, config, extension, _ctx=_ctx),
        "Код изменился во время чтения; повторите запрос.",
    )


def _procedure_limit(limit: int) -> int:
    """У поиска по коду предел — часть контракта, не молчаливый clamp."""
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= MAX_LIMIT
    ):
        raise RegistryError(f"limit должен быть целым числом от 1 до {MAX_LIMIT}.")
    return limit


def _selected_modules(
    registry: Registry,
    config: str | None,
    extension: str | None,
) -> tuple[object, LoadedModules | None]:
    context = registry.resolve(config, extension=extension)
    if extension is None:
        return context, context.modules
    if context.extension is not None:
        return context, context.extension

    доступные = registry.snapshot().extension_names(context.name)
    хвост = (
        f" Доступны: {', '.join(доступные)}."
        if доступные
        else " Загруженных расширений нет."
    )
    raise RegistryError(
        f"Расширение {extension} с кодом для конфигурации {context.name} "
        f"не загружено.{хвост}"
    )


def _scope_modules(loaded: LoadedModules, scope: str | None) -> frozenset[str]:
    if scope is None:
        return frozenset()
    область = scope.strip()
    if not область or "::" in область:
        raise RegistryError(
            "Область поиска scope должна быть именем объекта или точным "
            "адресом модуля без имени процедуры."
        )

    модули = loaded.оглавление.модули
    низкое = область.casefold()
    точные = [модуль for модуль in модули if модуль.casefold() == низкое]
    if точные:
        return frozenset(точные)

    начало = низкое + "."
    объектные = [
        модуль for модуль in модули if модуль.casefold().startswith(начало)
    ]
    if объектные:
        return frozenset(объектные)
    raise RegistryError(
        f"Область поиска `{scope}` не найдена в загруженном коде. "
        "Укажите имя объекта метаданных или точный адрес модуля из выдачи."
    )


def _строки_модуля(текст: str) -> list[str]:
    """Физические строки снимка модуля — режутся один раз на ответ.

    Оглавление модуля берёт сигнатуру каждой процедуры, и резка всего текста
    на каждую из них была квадратичной: на модуле из 348 процедур это 180 мс
    из 260 мс ответа.
    """
    return текст.split("\n")


def _отрезок_процедуры(
    строки: list[str],
    *,
    начало_строка: int,
    начало_столбец: int,
    конец_строка: int,
    конец_столбец: int,
) -> list[str]:
    """Физические строки между точными позициями одноразового разбора."""
    начало = начало_строка - 1
    конец = конец_строка - 1
    if not (0 <= начало <= конец < len(строки)):
        raise _SignatureError
    if начало == конец:
        return [строки[начало][начало_столбец:конец_столбец]]
    return [
        строки[начало][начало_столбец:],
        *строки[начало + 1:конец],
        строки[конец][:конец_столбец],
    ]


def _сигнатура_из_текста(строки: list[str], процедура: Процедура) -> str:
    """Декларация точного вхождения из уже дочитанного снимка модуля."""
    части = _отрезок_процедуры(
        строки,
        начало_строка=процедура.строка,
        начало_столбец=процедура.начало_столбец,
        конец_строка=процедура.конец_сигнатуры_строка,
        конец_столбец=процедура.конец_сигнатуры_столбец,
    )
    return " ".join(часть.strip() for часть in части if часть.strip())


def _прочитать_тело_модуля(loaded: LoadedModules, модуль: str, *, _ctx: CodeToolsContext) -> str:
    """Прочитать модуль через каталог локаторов опубликованного поколения."""
    identity = loaded.каталог.identity
    if (
        identity.source_id != loaded.source.id
        or identity.source_sha256 != loaded.source.sha256
        or identity.generation != loaded.source.locator_generation
    ):
        raise OSError("каталог локаторов принадлежит другому поколению")
    entry = loaded.каталог.entries.get(модуль)
    if entry is None or entry.locator is None:
        raise OSError("для адреса нет читаемого локатора")
    return _ctx.read_bsl(loaded.корень, модуль, entry.locator)


def _сигнатура(
    loaded: LoadedModules,
    запись,
    снимки: "dict[str, _РазборМодуля] | None" = None,
    *, _ctx: CodeToolsContext,
) -> str:
    """Декларация из компактного разбора модуля.

    В `LoadedModules` сигнатуры и тела не появляются: их держит отдельный
    кэш разборов пакета (`_разбор_модуля`), текст модуля читается только при
    промахе. Снимки ответа позволяют повторно использовать уже найденный
    разбор для каждой строки оглавления.
    """
    if снимки is None:
        снимки = {}
    разбор = снимки.get(запись.модуль)
    if разбор is None:
        разбор = _разбор_модуля(
            loaded,
            запись.модуль,
            lambda: _прочитать_тело_модуля(loaded, запись.модуль, _ctx=_ctx),
            _ctx=_ctx,
        )
        снимки[запись.модуль] = разбор
    return _parsed_procedure(разбор, запись).сигнатура




class _StaleModules(Exception):
    """LoadedModules сменился, пока инструмент читал canonical root."""


def _retry_stale_modules(attempt, message: str):
    """Одна попытка и один полный повтор, если поколение кода сменилось.

    Второй смены подряд не ждём дольше: это идёт загрузка, и честнее
    сказать агенту повторить позже, чем крутиться. Прочие исключения
    попытки выходят наружу сразу, с первой же попытки.
    """
    for _ in range(2):
        try:
            return attempt()
        except _StaleModules:
            continue
    raise RegistryError(message)


def _modules_are_current(registry: Registry, loaded: LoadedModules) -> bool:
    """Короткий CAS без дискового I/O под замком реестра."""
    current = registry.snapshot().modules.get(loaded.source.id)
    return current is not None and current.loaded is loaded


def _modules_availability_message(
    registry: Registry,
    context,
    loaded: LoadedModules | None,
) -> str | None:
    """Одинаковый контракт состояния для всех инструментов по коду."""
    if loaded is None:
        return (
            f"Для конфигурации {context.name} выгрузка в файлы не загружена. "
            "Инструменты про код ответить не могут."
        )
    if not _modules_are_current(registry, loaded):
        raise _StaleModules
    return None


def _события_платформы(context) -> Callable[[str, str], str]:
    """Событие платформы, которое вызывает процедуру модуля, или "".

    Источник — справка, загруженная для конфигурации, с фильтром её версии;
    без справки событий не знаем и ничего не помечаем.
    """
    syntax = getattr(context, "syntax", None)
    if syntax is None:
        return lambda модуль, имя: ""
    keep = context.syntax_filter()
    по_модулям: dict[str, dict[str, str]] = {}

    def событие(модуль: str, имя: str) -> str:
        имена = по_модулям.get(модуль)
        if имена is None:
            имена = {
                событие.casefold(): событие
                for событие in module_events(syntax.events, модуль, keep)
            }
            по_модулям[модуль] = имена
        return имена.get(имя.casefold(), "")

    return событие


def _procedure_matches(
    loaded: LoadedModules,
    записи: list,
    снимки: "dict[str, _РазборМодуля] | None" = None,
    *, _ctx: CodeToolsContext,
    события: Callable[[str, str], str] | None = None,
) -> list[ProcedureMatch]:
    счётчики: dict[str, tuple[dict[str, int], int]] = {}
    результат: list[ProcedureMatch] = []
    for запись in записи:
        ключ_имени = запись.имя.casefold()
        сведения = счётчики.get(ключ_имени)
        if сведения is None:
            по_модулям = {}
            неразрешённых = 0
            for место in loaded.вызовы.места(запись.имя):
                if место.цель is None:
                    неразрешённых += 1
                else:
                    по_модулям[место.цель] = по_модулям.get(место.цель, 0) + 1
            сведения = по_модулям, неразрешённых
            счётчики[ключ_имени] = сведения
        по_модулям, неразрешённых = сведения
        результат.append(
            ProcedureMatch(
                address=f"{запись.модуль}::{запись.имя}",
                signature=_ctx._сигнатура(loaded, запись, снимки),
                exported=запись.экспорт,
                function=запись.функция,
                line=запись.строка,
                calls=по_модулям.get(запись.модуль, 0),
                unresolved_calls=неразрешённых,
                annotated=запись.перекрыта,
                platform_event=(
                    события(запись.модуль, запись.имя) if события else ""
                ),
            )
        )
    return результат


def _standard_procedure_response(
    registry: Registry,
    loaded: LoadedModules,
    *,
    configuration: str,
    query: str,
    procedure: str,
    scope: str | None,
    scope_modules: frozenset[str],
    extension: str | None,
    _ctx: CodeToolsContext,
) -> str:
    """Разрешить типовое имя через TOC, не ранжируя сотни реализаций."""
    найденные = loaded.оглавление.по_имени(procedure)
    if scope is not None:
        найденные = [
            запись for запись in найденные if запись.модуль in scope_modules
        ]

    match = None
    if scope is not None and len(найденные) == 1:
        try:
            match = _procedure_matches(loaded, найденные, _ctx=_ctx)[0]
        except (OSError, _SignatureError) as ошибка:
            if not _modules_are_current(registry, loaded):
                raise _StaleModules from ошибка
            raise RegistryError(
                "Не удалось прочитать сигнатуру из текущей выгрузки кода: "
                "файл модуля недоступен."
            ) from ошибка

    if not _modules_are_current(registry, loaded):
        raise _StaleModules
    return render_standard_procedure_search(
        configuration,
        query,
        procedure,
        found_count=len(найденные),
        scope=scope,
        match=match,
        extension=extension,
    )


def _search_procedures_once(
    registry: Registry,
    query: str,
    config: str | None = None,
    extension: str | None = None,
    scope: str | None = None,
    limit: int = 10,
    *, _ctx: CodeToolsContext,
) -> str:
    """Точное имя и поиск словами по коду конфигурации или расширения."""
    limit = _procedure_limit(limit)
    запрос = query.strip()
    if not запрос:
        raise RegistryError("Запрос query не может быть пустым.")

    context, loaded = _ctx._selected_modules(registry, config, extension)
    состояние = _modules_availability_message(registry, context, loaded)
    if состояние is not None:
        return состояние
    assert loaded is not None

    try:
        приоритетные = _ctx._scope_modules(loaded, scope)
    except RegistryError as error:
        if not _modules_are_current(registry, loaded):
            raise _StaleModules from error
        raise
    предупреждение = _ctx._loaded_partial_warning(loaded)
    предупреждение_скомпилированных = (
        f"> {предупреждение}\n\n" if предупреждение else ""
    )
    типовая = recognize_standard_procedure_intent(запрос)
    # Точное каноническое имя сохраняет прежний полный контракт поиска. Только
    # расширенная формулировка включает осторожное разрешение намерения.
    if типовая is not None and запрос.casefold() != типовая.casefold():
        return предупреждение_скомпилированных + _standard_procedure_response(
            registry,
            loaded,
            configuration=context.name,
            query=query,
            procedure=типовая,
            scope=scope,
            scope_modules=приоритетные,
            extension=extension,
            _ctx=_ctx,
        )
    точные_все = loaded.оглавление.по_имени(запрос)

    def категория(запись) -> int:
        if приоритетные and запись.модуль in приоритетные:
            return 0
        сдвиг = 1 if приоритетные else 0
        if запись.модуль.startswith("ОбщийМодуль."):
            return сдвиг
        return сдвиг + 1

    точные_все = sorted(точные_все, key=категория)
    точные = точные_все[:limit]
    точные_ключи = {
        (запись.модуль.casefold(), запись.имя.casefold())
        for запись in точные_все
    }

    def не_точная(doc) -> bool:
        запись = doc.payload
        return (запись.модуль.casefold(), запись.имя.casefold()) not in точные_ключи

    # Категории ищутся отдельно: scope не может потеряться за глобальным
    # top-N, а ради счётчика не материализуются десятки тысяч Hit. Берётся
    # ровно limit+1 — последний нужен только для честного «есть ещё».
    hits = []
    категорий = 3 if приоритетные else 2
    for номер_категории in range(категорий):
        осталось = limit + 1 - len(hits)
        if осталось <= 0:
            break
        hits.extend(
            loaded.поиск.search(
                запрос,
                limit=осталось,
                predicate=lambda doc, номер=номер_категории: (
                    не_точная(doc) and категория(doc.payload) == номер
                ),
            )
        )

    слова_все = [hit.doc.payload for hit in hits]
    слова = слова_все[:limit]
    if not точные and not слова:
        выбранное = (
            f"расширении {extension} конфигурации {context.name}"
            if extension
            else f"конфигурации {context.name}"
        )
        if not _modules_are_current(registry, loaded):
            raise _StaleModules
        return предупреждение_скомпилированных + (
            f"По исходным текстам по запросу «{query}» в {выбранное} "
            "совпадений нет. "
            "Поиск по словам выполняется только по экспортным процедурам; "
            "неэкспортная находится по точному имени. Если известен точный "
            "адрес, используйте `get_procedure(address=\"Модуль::Имя\")`."
        )

    try:
        снимки: dict[str, _РазборМодуля] = {}
        события = _события_платформы(context)
        точные_совпадения = _procedure_matches(
            loaded, точные, снимки, _ctx=_ctx, события=события
        )
        словесные_совпадения = _procedure_matches(
            loaded, слова, снимки, _ctx=_ctx, события=события
        )
    except (OSError, _SignatureError) as ошибка:
        # Canonical root мог смениться или исчезнуть между resolve и чтением.
        # Сначала identity CAS: ошибка старого поколения — повод повторить,
        # а не показывать путь или файловую причину пользователю.
        if not _modules_are_current(registry, loaded):
            raise _StaleModules from ошибка
        raise RegistryError(
            "Не удалось прочитать сигнатуру из текущей выгрузки кода: "
            "файл модуля недоступен."
        ) from ошибка

    # Последняя проверка непосредственно перед render: оглавление, вызовы,
    # номера строк и прочитанные сигнатуры обязаны принадлежать одному
    # объекту LoadedModules. Дискового I/O под `_lock` здесь нет.
    if not _modules_are_current(registry, loaded):
        raise _StaleModules

    ответ = render_procedure_search(
        context.name,
        query,
        exact=точные_совпадения,
        exact_total=len(точные_все),
        exact_more_modules=len(
            {запись.модуль for запись in точные_все[len(точные):]}
        ),
        words=словесные_совпадения,
        words_more=len(слова_все) > limit,
        limit=limit,
        extension=extension,
    )
    return предупреждение_скомпилированных + ответ


def search_procedures(
    registry: Registry,
    query: str,
    config: str | None = None,
    extension: str | None = None,
    scope: str | None = None,
    limit: int = 10,
    *, _ctx: CodeToolsContext,
) -> str:
    """Поиск с одним полным повтором при смене поколения кода."""
    return _retry_stale_modules(
        lambda: _search_procedures_once(
            registry, query, config, extension, scope, limit,
            _ctx=_ctx,
        ),
        "Код изменился во время поиска дважды; повторите запрос после "
        "завершения загрузки.",
    )


def _procedure_window(start_line: int, lines: int) -> tuple[int, int]:
    """Строгая граница окна: ошибка вызова не маскируется обрезкой."""
    if (
        isinstance(start_line, bool)
        or not isinstance(start_line, int)
        or start_line < 0
    ):
        raise RegistryError("start_line должен быть целым числом от 0.")
    if (
        isinstance(lines, bool)
        or not isinstance(lines, int)
        or not 1 <= lines <= 200
    ):
        raise RegistryError("lines должен быть целым числом от 1 до 200.")
    return start_line, lines


def _modules_package_is_current(
    registry: Registry, loaded_modules: list[LoadedModules]
) -> bool:
    """Один CAS для всех корпусов, чьи части попадут в один ответ."""
    уникальные = {item.source.id: item for item in loaded_modules}
    snapshot = registry.snapshot()
    return all(
        snapshot.modules.get(source_id) is not None
        and snapshot.modules[source_id].loaded is item
        for source_id, item in уникальные.items()
    )


def _read_module_snapshot(
    registry: Registry,
    loaded: LoadedModules,
    module: str,
    *, _ctx: CodeToolsContext,
) -> str:
    """Текст одного поколения, без раскрытия локального пути при отказе."""
    try:
        текст = _прочитать_тело_модуля(loaded, module, _ctx=_ctx)
    except OSError as error:
        if not _modules_are_current(registry, loaded):
            raise _StaleModules from error
        raise RegistryError(
            "Не удалось прочитать текущий файл модуля: файл недоступен."
        ) from error
    if not _modules_are_current(registry, loaded):
        raise _StaleModules
    return текст






def _parsed_procedures(записи: list, текст: str, *, _ctx: CodeToolsContext) -> _РазборМодуля:
    """Сопоставляет оглавление с разбором и сжимает его до нужных полей."""
    процедуры = _ctx.разобрать(текст)
    if len(записи) != len(процедуры):
        raise _SignatureError
    строки = _строки_модуля(текст)
    пары = []
    for запись, процедура in zip(записи, процедуры, strict=True):
        if (
            запись.строка != процедура.строка
            or запись.имя.casefold() != процедура.имя.casefold()
        ):
            raise _SignatureError
        пары.append((запись.позиция, запись, процедура))
    пары.sort(key=lambda item: item[0])
    границы = array("q")
    for _, _, процедура in пары:
        границы.extend((
            процедура.строка, процедура.начало_столбец,
            процедура.конец, процедура.конец_столбец,
        ))
    разбор = _РазборМодуля(
        позиции=array("q", (позиция for позиция, _, _ in пары)),
        границы=границы,
        # Имя оглавления уже лежит в памяти; своя копия строки нужна, только
        # если разбор написал его иначе (регистр сверяется выше без учёта).
        имена=tuple(
            запись.имя if запись.имя == процедура.имя else процедура.имя
            for _, запись, процедура in пары
        ),
        сигнатуры=tuple(
            _сигнатура_из_текста(строки, процедура) for _, _, процедура in пары
        ),
        # Директив единицы видов на весь корпус: одна строка на вид.
        директивы=tuple(sys.intern(процедура.директива) for _, _, процедура in пары),
        перекрытия=tuple(процедура.перекрытие for _, _, процедура in пары),
    )
    размер = (
        _ЦЕНА_ЗАПИСИ_КЭША
        + sys.getsizeof(разбор)
        + sys.getsizeof(разбор.позиции)
        + sys.getsizeof(разбор.границы)
        + sum(
            sys.getsizeof(часть)
            for часть in (
                разбор.имена, разбор.сигнатуры, разбор.директивы, разбор.перекрытия
            )
        )
        + sum(sys.getsizeof(сигнатура) for сигнатура in разбор.сигнатуры)
        + sum(
            sys.getsizeof(перекрытие) + sum(sys.getsizeof(часть) for часть in перекрытие)
            for перекрытие in разбор.перекрытия
            if перекрытие
        )
    )
    object.__setattr__(разбор, "размер", размер)
    return разбор


# Слот `OrderedDict` с ключом-кортежем на запись кэша; ключ модуля — строка
# оглавления, она уже в памяти.
_ЦЕНА_ЗАПИСИ_КЭША = 200



def _разбор_модуля(loaded: LoadedModules, модуль: str, прочитать, *, _ctx: CodeToolsContext) -> _РазборМодуля:
    """Разбор модуля один раз на пакет кода; `прочитать` зовётся при промахе.

    Разбор — 75–95% времени `get_procedure` и `search_procedures`, а пакет
    после публикации не меняется. Ключ кэша — пакет из агрегатов: новое
    поколение приходит с новым ключом, старое уходит вместе со своим
    `LoadedModules`. Гонка двух потоков на промахе лишь разберёт модуль
    дважды; в кэше останется первая запись, обе равны.
    """
    агрегаты = _ctx._code_aggregates(loaded)
    готовый = _ctx._РАЗБОРЫ.взять(агрегаты.пакет, модуль)
    if готовый is not None:
        return готовый
    разбор = _parsed_procedures(loaded.оглавление.модуля(модуль), прочитать(), _ctx=_ctx)
    return _ctx._РАЗБОРЫ.положить(агрегаты.пакет, модуль, разбор)


def _parsed_procedure(разбор: _РазборМодуля, запись) -> _РазобраннаяПроцедура:
    return разбор.процедура(запись)


def _procedure_body(строки: list[str], процедура: _РазобраннаяПроцедура) -> list[str]:
    """Физические строки точного вхождения без соседей на граничных строках."""
    конец_строка = процедура.конец or len(строки)
    return _отрезок_процедуры(
        строки,
        начало_строка=процедура.строка,
        начало_столбец=процедура.начало_столбец,
        конец_строка=конец_строка,
        конец_столбец=процедура.конец_столбец,
    )


def _extension_delta(строки: list[str], процедура: _РазобраннаяПроцедура) -> list[str]:
    """Блоки правки дословно, включая сами граничные директивы."""
    результат: list[str] = []
    внутри: str | None = None
    концы = {
        "#удаление": "#конецудаления",
        "#вставка": "#конецвставки",
    }
    for строка in _procedure_body(строки, процедура):
        голая = строка.strip().casefold()
        if внутри is None and голая in концы:
            внутри = концы[голая]
        if внутри is not None:
            результат.append(строка)
            if голая == внутри:
                внутри = None
    return результат


_MODULE_CONTEXT_TITLES = {
    "global": "Глобальный",
    "server": "Сервер",
    "client_managed": "Клиент (управляемое приложение)",
    "server_call": "Вызов сервера",
    "privileged": "Привилегированный",
}


def _compilation_context(context, module: str, parsed) -> list[str]:
    результат: list[str] = []
    if parsed.директива:
        результат.append(f"&{parsed.директива}")
    if module.startswith("ОбщийМодуль.") and context.configuration is not None:
        объект = context.configuration.config.get(module)
        if объект is not None:
            for ключ, подпись in _MODULE_CONTEXT_TITLES.items():
                if ключ in объект.props:
                    значение = "да" if объект.props[ключ] is True else "нет"
                    результат.append(f"{подпись}: {значение}")
    return результат


def _module_warnings(context, loaded: LoadedModules, записи: list) -> list[str]:
    warnings: list[str] = []
    частичные = [запись for запись in записи if запись.частичный]
    if частичные:
        первая = min(item.строка for item in частичные)
        warnings.append(
            f"Модуль разобран не до конца: с процедуры на строке {первая} "
            "граница конца не найдена; оглавление может быть неполным."
        )
    if (
        loaded.source.kind != KIND_EXTENSION
        and context.configuration is not None
        and loaded.версия_кода
        and context.configuration.config.version
        and loaded.версия_кода != context.configuration.config.version
    ):
        warnings.append(
            f"Код модулей выгружен для версии {loaded.версия_кода}, "
            f"загруженные метаданные — версии {context.configuration.config.version}. "
            "Строить правку на этом ответе нельзя без сверки."
        )
    return warnings


def _close_matches(
    word: str, possibilities, *, n: int, cutoff: float
) -> list[str]:
    """Тот же ответ, что `difflib.get_close_matches`, без лишних `ratio()`.

    Адреса модулей похожи друг на друга по составу букв («Справочник.»,
    «.МодульОбъекта»), поэтому дешёвые фильтры difflib пропускают почти всех,
    и дорогой `ratio()` считался для каждого из 2 867 модулей — 150 мс на одну
    опечатку. `quick_ratio()` — точная верхняя граница `ratio()`: кандидаты
    перебираются по ней от большей к меньшей, и перебор останавливается, когда
    граница опускается ниже пятого лучшего счёта. Равная граница ещё
    проверяется: ничья по счёту разрешается строкой, как в `heapq.nlargest`.
    """
    matcher = difflib.SequenceMatcher()
    matcher.set_seq2(word)
    кандидаты: list[tuple[float, str]] = []
    for possibility in possibilities:
        matcher.set_seq1(possibility)
        if matcher.real_quick_ratio() >= cutoff:
            граница = matcher.quick_ratio()
            if граница >= cutoff:
                кандидаты.append((граница, possibility))
    кандидаты.sort(key=lambda item: item[0], reverse=True)
    лучшие: list[tuple[float, str]] = []
    for граница, possibility in кандидаты:
        if len(лучшие) == n and граница < лучшие[0][0]:
            break
        matcher.set_seq1(possibility)
        счёт = matcher.ratio()
        if счёт < cutoff:
            continue
        if len(лучшие) < n:
            heapq.heappush(лучшие, (счёт, possibility))
        else:
            heapq.heappushpop(лучшие, (счёт, possibility))
    return [possibility for _, possibility in sorted(лучшие, reverse=True)]


def _similar_address(loaded: LoadedModules, module: str, name: str | None) -> list[str]:
    if name is None:
        kind = module.partition(".")[0].casefold()
        candidates = [
            item for item in loaded.оглавление.модули
            if item.partition(".")[0].casefold() == kind
        ]
        return _close_matches(
            module, candidates or loaded.оглавление.модули, n=5, cutoff=0.45
        )
    кандидаты = [
        f"{module}::{item.имя}" for item in loaded.оглавление.модуля(module)
    ]
    return _close_matches(f"{module}::{name}", кандидаты, n=5, cutoff=0.45)


def _foreign_extension_warnings(
    registry: Registry,
    configuration: str,
    module: str,
    target_name: str,
    selected: LoadedModules,
    observed: list[LoadedModules],
    *, _ctx: CodeToolsContext,
) -> list[str]:
    prefix = source_ids.extension_prefix(configuration)
    snapshot = registry.snapshot()
    кандидаты = sorted(
        (
            (source_id[len(prefix):], code.loaded)
            for source_id, code in snapshot.modules.items()
            if source_id.startswith(prefix)
            and code.loaded is not selected
        ),
        key=lambda item: item[0],
    )
    warnings: list[str] = []
    for extension_name, foreign in кандидаты:
        if not _modules_are_current(registry, foreign):
            continue
        записи = foreign.оглавление.модуля(module)
        if not записи:
            continue
        # При попадании в кэш файл не читается, и CAS чтения не случается:
        # чужой пакет всё равно попадает в `observed` и сверяется перед render.
        observed.append(foreign)
        разбор = _разбор_модуля(
            foreign,
            module,
            lambda: _ctx._read_module_snapshot(registry, foreign, module),
            _ctx=_ctx,
        )
        for запись in записи:
            parsed = _parsed_procedure(разбор, запись)
            if not parsed.перекрытие:
                continue
            вид, цель = parsed.перекрытие
            if (цель or parsed.имя).casefold() != target_name.casefold():
                continue
            вид_низкое = вид.casefold()
            if вид_низкое in ("вместо", "around"):
                warnings.append(
                    f"Процедуру уже перекрывает расширение `{extension_name}` "
                    f"аннотацией `&{вид}`. Какое расширение выиграет, зависит от "
                    "порядка расширений; текст чужого расширения не показан."
                )
            elif вид_низкое in (
                "изменениеиконтроль",
                "changeandvalidate",
            ):
                warnings.append(
                    f"Расширение `{extension_name}` аннотацией `&{вид}` "
                    "меняет типовое тело блоками вставки/удаления; текст "
                    "чужого расширения не показан."
                )
            else:
                warnings.append(
                    f"Расширение `{extension_name}` добавляет `&{вид}` для этой "
                    "процедуры; его код тоже выполняется, но текст чужого расширения "
                    "не показан."
                )
    return warnings


def _get_procedure_once(
    registry: Registry,
    address: str,
    config: str | None,
    extension: str | None,
    start_line: int,
    lines: int,
    *, _ctx: CodeToolsContext,
) -> str:
    context, loaded = _ctx._selected_modules(registry, config, extension)
    состояние = _modules_availability_message(registry, context, loaded)
    if состояние is not None:
        return состояние
    assert loaded is not None
    partial_warning = _ctx._loaded_partial_warning(loaded)
    warning_prefix = f"> {partial_warning}\n\n" if partial_warning else ""

    модуль, разделитель, имя = address.partition("::")
    модуль = модуль.strip()
    имя = имя.strip()
    if not модуль or (разделитель and (not имя or "::" in имя)):
        raise RegistryError(
            "address должен быть адресом модуля или парой `Модуль::Имя`."
        )
    канонический_модуль = loaded.оглавление.канонический_модуль(модуль)
    if канонический_модуль is None:
        похожие = _ctx._similar_address(loaded, модуль, None)
        if not _modules_are_current(registry, loaded):
            raise _StaleModules
        хвост = "" if not похожие else "\n\nВозможно, имелось в виду:\n" + "\n".join(f"- `{item}`" for item in похожие)
        return warning_prefix + f"Модуль `{модуль}` в загруженном коде не найден.{хвост}\n"
    модуль = канонический_модуль
    записи = loaded.оглавление.модуля(модуль)

    if loaded.оглавление.скомпилирован(модуль):
        if not _modules_are_current(registry, loaded):
            raise _StaleModules
        хвост = (
            f" Процедуру `{имя}` проверить невозможно."
            if разделитель
            else ""
        )
        return warning_prefix + (
            f"У модуля `{модуль}` исходный текст недоступен — оглавления "
            "процедур в выгрузке нет. Не считайте, что "
            f"процедуры отсутствуют: их нельзя увидеть.{хвост}\n"
        )

    # Текст нужен разбору только при промахе кэша, а оглавлению — вовсе нет:
    # сигнатуры лежат в разборе. Карточка дочитывает тело ниже.
    прочитанный: list[str] = []

    def прочитать() -> str:
        прочитанный.append(_ctx._read_module_snapshot(registry, loaded, модуль))
        return прочитанный[0]

    разбор = _разбор_модуля(loaded, модуль, прочитать, _ctx=_ctx)
    observed = [loaded]
    warnings = _module_warnings(context, loaded, записи)
    if partial_warning:
        warnings.insert(0, partial_warning)

    if not разделитель:
        outlines: list[ProcedureOutline] = []
        событие_платформы = _события_платформы(context)
        for запись in записи:
            parsed = _parsed_procedure(разбор, запись)
            calls = sum(
                1
                for место in loaded.вызовы.места(запись.имя)
                if место.цель == модуль
            )
            outlines.append(
                ProcedureOutline(
                    address=f"{модуль}::{запись.имя}",
                    signature=parsed.сигнатура,
                    exported=запись.экспорт,
                    function=запись.функция,
                    line=запись.строка,
                    calls=calls,
                    directive=parsed.директива,
                    events=loaded.формы.обработчик(модуль, запись.имя) or (),
                    platform_event=событие_платформы(модуль, запись.имя),
                )
            )
        if not _ctx._modules_package_is_current(registry, observed):
            raise _StaleModules
        return render_module_toc(
            context.name, модуль, outlines, warnings=warnings, extension=extension
        )

    совпадения = [запись for запись in записи if запись.имя.casefold() == имя.casefold()]
    if not совпадения:
        похожие = _ctx._similar_address(loaded, модуль, имя)
        if not _modules_are_current(registry, loaded):
            raise _StaleModules
        хвост = "" if not похожие else "\n\nВозможно, имелось в виду:\n" + "\n".join(f"- `{item}`" for item in похожие)
        return warning_prefix + f"В модуле `{модуль}` нет процедуры `{имя}`.{хвост}\n"
    запись = совпадения[0]
    parsed = _parsed_procedure(разбор, запись)
    текст = прочитанный[0] if прочитанный else _ctx._read_module_snapshot(
        registry, loaded, модуль
    )
    строки = _строки_модуля(текст)
    # Границы из кэша, а текст прочитан заново: если файл под опубликованным
    # пакетом когда-нибудь изменится на месте, рассогласованное тело не уйдёт
    # в ответ — строка объявления обязана содержать имя процедуры.
    if not прочитанный and (
        parsed.строка > len(строки)
        or parsed.имя.casefold()
        not in строки[parsed.строка - 1][parsed.начало_столбец:].casefold()
    ):
        raise _SignatureError
    body = _procedure_body(строки, parsed)
    target_name = (
        parsed.перекрытие[1] or parsed.имя
        if parsed.перекрытие
        else parsed.имя
    )

    if extension and parsed.перекрытие and parsed.перекрытие[0].casefold() in (
        "изменениеиконтроль",
        "changeandvalidate",
    ):
        base = context.modules
        base_state = _modules_availability_message(registry, context, base)
        if base_state is not None:
            return base_state
        base_records = [
            item
            for item in base.оглавление.модуля(модуль)
            if item.имя.casefold() == target_name.casefold()
        ]
        if not base_records:
            raise RegistryError(
                f"Аннотация `&{parsed.перекрытие[0]}` ссылается на "
                f"`{модуль}::{target_name}`, но в коде основной конфигурации её нет."
            )
        base_text = _ctx._read_module_snapshot(registry, base, модуль)
        observed.append(base)
        base_parsed = _разбор_модуля(base, модуль, lambda: base_text, _ctx=_ctx)
        body = [
            "// Тело основной конфигурации",
            *_procedure_body(
                _строки_модуля(base_text),
                _parsed_procedure(base_parsed, base_records[0]),
            ),
            "",
            f"// Дельта расширения {extension}",
            *_extension_delta(строки, parsed),
        ]
    elif extension and parsed.перекрытие and parsed.перекрытие[0].casefold() in ("вместо", "around"):
        warnings.append(
            "Показано тело `&Вместо`; типовое тело читайте отдельным запросом "
            "к основной конфигурации без `extension`."
        )

    warnings.extend(
        _foreign_extension_warnings(
            registry, context.name, модуль, target_name, loaded, observed,
            _ctx=_ctx,
        )
    )
    events = loaded.формы.обработчик(модуль, запись.имя) or ()
    compilation = _compilation_context(context, модуль, parsed)
    if events:
        compilation.append("события формы: " + ", ".join(events))
    событие_платформы = _события_платформы(context)(модуль, запись.имя)
    if событие_платформы:
        compilation.append(
            f"событие платформы: {событие_платформы} — вызывается платформой, "
            "мест вызова в коде у него может не быть"
        )
    if not _ctx._modules_package_is_current(registry, observed):
        raise _StaleModules
    return render_procedure_card(
        context.name,
        f"{модуль}::{запись.имя}",
        signature=parsed.сигнатура,
        compilation=compilation,
        body=body,
        start_line=start_line,
        lines=lines,
        warnings=warnings,
        annotation=parsed.перекрытие,
        extension=extension,
    )


def get_procedure(
    registry: Registry,
    address: str,
    config: str | None = None,
    extension: str | None = None,
    start_line: int = 0,
    lines: int = 200,
    *, _ctx: CodeToolsContext,
) -> str:
    """Оглавление модуля или карточка процедуры с дисковым телом."""
    start_line, lines = _procedure_window(start_line, lines)
    if not isinstance(address, str) or not address.strip():
        raise RegistryError("address не может быть пустым.")
    try:
        return _retry_stale_modules(
            lambda: _get_procedure_once(
                registry, address, config, extension, start_line, lines,
                _ctx=_ctx,
            ),
            "Код изменился во время чтения дважды; повторите запрос после "
            "завершения загрузки.",
        )
    except _SignatureError as error:
        raise RegistryError(
            "Не удалось сопоставить оглавление с текущим текстом модуля; "
            "перезагрузите источник кода."
        ) from error


_AMBIGUOUS_OWNER = object()


@dataclass(slots=True)
class _OwnerBoundaries:
    начала: list[int]
    состояния: list[object | None]
    частичные: list[int]


def _build_owner_boundaries(records: list) -> _OwnerBoundaries:
    """Кусочно-постоянное состояние владельца для двоичного поиска.

    Перекрытия сворачиваются один раз при подготовке. Запрос по строке после
    этого не идёт назад по тысячам подходящих процедур: одно состояние явно
    говорит, что владелец единственный, отсутствует или неоднозначен.
    """
    записи = sorted(records, key=lambda item: (item.строка, item.позиция))
    частичные = sorted(item.строка for item in записи if item.частичный)
    события: dict[int, tuple[list[int], list[int]]] = {}
    for номер, запись in enumerate(записи):
        конец = запись.конец
        if not конец:
            continue
        начала, удаления = события.setdefault(запись.строка, ([], []))
        начала.append(номер)
        начала, удаления = события.setdefault(конец + 1, ([], []))
        удаления.append(номер)

    координаты: list[int] = []
    состояния: list[object | None] = []
    активные: set[int] = set()
    for строка in sorted(события):
        добавления, удаления = события[строка]
        активные.difference_update(удаления)
        активные.update(добавления)
        координаты.append(строка)
        if len(активные) == 1:
            состояния.append(записи[next(iter(активные))])
        elif активные:
            состояния.append(_AMBIGUOUS_OWNER)
        else:
            состояния.append(None)
    return _OwnerBoundaries(координаты, состояния, частичные)


def _caller_boundaries(
    loaded: LoadedModules, modules: set[str]
) -> dict[str, _OwnerBoundaries]:
    """Интервальные состояния модулей для O(log P) поиска владельца."""
    return {
        module: _build_owner_boundaries(loaded.оглавление.модуля(module))
        for module in modules
    }


def _caller_site(boundaries, место) -> CallerSite:
    """Владелец места по непересекающимся строковым границам оглавления.

    Вызов хранит только номер строки. Если на одной физической строке лежат
    две процедуры, выбирать первую или последнюю было бы догадкой: обе
    границы подходят. Частичная процедура тоже не даёт правой границы.
    """
    границы = boundaries[место.модуль]
    позиция = bisect_right(границы.начала, место.строка) - 1
    состояние = границы.состояния[позиция] if позиция >= 0 else None
    if состояние is not None and состояние is not _AMBIGUOUS_OWNER:
        запись = состояние
        return CallerSite(
            module=место.модуль,
            line=место.строка,
            owner=f"{место.модуль}::{запись.имя}",
        )
    if состояние is _AMBIGUOUS_OWNER:
        return CallerSite(
            module=место.модуль,
            line=место.строка,
            owner=None,
            ambiguous_owner=True,
        )

    return CallerSite(
        module=место.модуль,
        line=место.строка,
        owner=None,
        partial_owner=bisect_right(границы.частичные, место.строка) > 0,
    )


def _metadata_bindings(context, module: str, name: str) -> list[MetadataBinding]:
    if context.configuration is None:
        return []
    совпадения = [
        MetadataBinding(kind=edge.kind, source=edge.source)
        for edge in context.configuration.graph.edges
        if edge.kind in ("handler", "method")
        and edge.target.casefold() == module.casefold()
        and edge.via.casefold() == name.casefold()
    ]
    for obj in context.configuration.config.objects.values():
        if (
            obj.kind != "HTTPСервис"
            or (obj.code_address or obj.full_name).casefold() != module.casefold()
        ):
            continue
        root_url = obj.extended.get("root_url")
        templates = obj.extended.get("url_templates")
        if not isinstance(root_url, str) or not isinstance(templates, list):
            continue
        for template in templates:
            if not isinstance(template, dict):
                continue
            template_value = template.get("template")
            methods = template.get("methods")
            if not isinstance(template_value, str) or not isinstance(methods, list):
                continue
            for method in methods:
                if not isinstance(method, dict):
                    continue
                handler = method.get("handler")
                http_method = method.get("http_method")
                if (
                    isinstance(handler, str)
                    and handler.casefold() == name.casefold()
                    and isinstance(http_method, str)
                ):
                    совпадения.append(
                        MetadataBinding(
                            kind="http_method",
                            source=obj.full_name,
                            method=http_method,
                            path=http_service_path(root_url, template_value),
                        )
                    )
    return sorted(
        совпадения,
        key=lambda item: (
            item.kind,
            item.source.casefold(),
            item.source,
            item.method,
            item.path,
        ),
    )


def _form_bindings(loaded: LoadedModules, module: str, name: str):
    lower_module = module.casefold()
    if ".форма." not in lower_module and not lower_module.startswith("общаяформа."):
        return [], "not_form"
    форма = loaded.формы.состав(module)
    if форма is None:
        return [], "missing"
    if форма.состояние_xml == "broken":
        return [], "broken"
    if форма.состояние_xml == "ready":
        привязки = [
            FormHandlerBinding(element=item.элемент, event=item.событие)
            for item in loaded.формы.привязки(module, name)
        ]
        return привязки, "ready"
    if форма.битая:
        return [], "partial_broken"
    if форма.структура_частична:
        return [], "partial"
    if not форма.структура_доступна:
        return [], "missing"
    return [], "partial"


def _get_callers_once(
    registry: Registry,
    address: str,
    config: str | None,
    extension: str | None,
    limit: int,
    *, _ctx: CodeToolsContext,
) -> str:
    context, loaded = _ctx._selected_modules(registry, config, extension)
    состояние = _modules_availability_message(registry, context, loaded)
    if состояние is not None:
        return состояние
    assert loaded is not None
    partial_warning = _ctx._loaded_partial_warning(loaded)
    warning_prefix = f"> {partial_warning}\n\n" if partial_warning else ""

    module, separator, name = address.partition("::")
    module = module.strip()
    name = name.strip()
    if not separator or not module or not name or "::" in name:
        raise RegistryError(
            "address должен быть точным адресом процедуры `Модуль::Имя`; "
            "адрес одного модуля для get_callers недостаточен."
        )
    canonical_module = loaded.оглавление.канонический_модуль(module)
    if canonical_module is None:
        похожие = _ctx._similar_address(loaded, module, None)
        хвост = (
            ""
            if not похожие
            else "\n\nВозможно, имелось в виду:\n"
            + "\n".join(f"- `{item}`" for item in похожие)
        )
        if not _modules_are_current(registry, loaded):
            raise _StaleModules
        return warning_prefix + (
            f"Модуль `{module}` в загруженном коде не найден.{хвост}\n"
        )
    module = canonical_module
    if loaded.оглавление.скомпилирован(module):
        if not _modules_are_current(registry, loaded):
            raise _StaleModules
        return warning_prefix + (
            f"У модуля `{module}` исходный текст недоступен — оглавления "
            "процедур нет. Поэтому подтвердить или "
            f"опровергнуть вызовы `{name}` по этому адресу невозможно.\n"
        )
    записи_модуля = loaded.оглавление.модуля(module)
    совпадения = [
        запись for запись in записи_модуля if запись.имя.casefold() == name.casefold()
    ]
    if not совпадения:
        похожие = _ctx._similar_address(loaded, module, name)
        хвост = (
            ""
            if not похожие
            else "\n\nВозможно, имелось в виду:\n"
            + "\n".join(f"- `{item}`" for item in похожие)
        )
        if not _modules_are_current(registry, loaded):
            raise _StaleModules
        return warning_prefix + (
            f"В модуле `{module}` нет процедуры `{name}`.{хвост}\n"
        )
    canonical_name = совпадения[0].имя

    выбор = loaded.вызовы.выбрать(canonical_name, module, limit=limit)
    показанные_места = выбор.подтверждённые
    остаток_бюджета = limit - len(показанные_места)
    показанные_неразрешённые_места = выбор.неразрешённые[:остаток_бюджета]
    границы = _caller_boundaries(
        loaded,
        {
            item.модуль
            for item in [*показанные_места, *показанные_неразрешённые_места]
        },
    )
    показанные = [_caller_site(границы, item) for item in показанные_места]
    показанные_неразрешённые = [
        _caller_site(границы, item) for item in показанные_неразрешённые_места
    ]
    metadata = _metadata_bindings(context, module, canonical_name)
    form_bindings, form_state = _form_bindings(loaded, module, canonical_name)
    warnings = _module_warnings(context, loaded, записи_модуля)
    if partial_warning:
        warnings.insert(0, partial_warning)

    # Все структуры ответа принадлежат одному объекту LoadedModules. Между
    # чтением массивов и этой проверкой reparse/remove может заменить пакет;
    # тогда весь запрос повторяется, а не смешивает два поколения.
    if not _modules_are_current(registry, loaded):
        raise _StaleModules
    return render_callers(
        context.name,
        f"{module}::{canonical_name}",
        code_sites=показанные,
        confirmed_total=выбор.подтверждённых_всего,
        omitted_modules=выбор.пропущено_в_модулях,
        unresolved_sites=показанные_неразрешённые,
        unresolved_total=выбор.неразрешённых_всего,
        metadata=metadata,
        form_bindings=form_bindings,
        form_state=form_state,
        warnings=warnings,
        extension=extension,
        platform_event=_события_платформы(context)(module, canonical_name),
    )


def get_callers(
    registry: Registry,
    address: str,
    config: str | None = None,
    extension: str | None = None,
    limit: int = 20,
    *, _ctx: CodeToolsContext,
) -> str:
    """Подтверждённые вызовы, привязки метаданных и обработчики формы."""
    limit = _procedure_limit(limit)
    if not isinstance(address, str) or not address.strip():
        raise RegistryError("address не может быть пустым.")
    return _retry_stale_modules(
        lambda: _get_callers_once(registry, address, config, extension, limit, _ctx=_ctx),
        "Код изменился во время обратного поиска дважды; повторите запрос "
        "после завершения загрузки.",
    )
