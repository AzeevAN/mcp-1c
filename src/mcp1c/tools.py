"""Инструменты сервера: чистые функции над реестром.

Здесь нет ни строки, зависящей от MCP. Причина не в чистоте ради чистоты:
эти же функции понадобятся дашборду и отладочному CLI, а протокольный слой
имеет свойство меняться. `server.py` — тонкая обёртка, которая только
регистрирует их и раздаёт описания.

Правило набора инструментов: их мало и список фиксирован. Каждый инструмент
висит в контексте агента постоянно, независимо от того, пользуется он им или
нет. Поэтому инструмент добавляется только под отдельную пользовательскую
задачу, а не под каждый новый внутренний индекс. Код конфигурации — отдельная
область поиска, для которой служит `search_procedures`.
"""

from __future__ import annotations

import base64
import binascii
import difflib
import hashlib
import heapq
import json
from bisect import bisect_right
from dataclasses import dataclass

from . import coverage_log, index_cache, replacements, structure_origin
from .bsl_lex import Процедура, прочитать_модуль, разобрать
from .intake_v2 import ExportIdentity
from .module_content import ModuleLocator, read_bsl
from .module_address import путь_модуля
from .registry import (
    KIND_EXTENSION,
    KIND_MODULES,
    STATUS_ERROR,
    LoadedModules,
    Registry,
    RegistrySnapshot,
    RegistryError,
    SourceSnapshot,
)
from .render import (
    BRIEF,
    CallerSite,
    DETAIL_LEVELS,
    FIELDS,
    FULL,
    FormHandlerBinding,
    MetadataBinding,
    ProcedureMatch,
    ProcedureOutline,
    render_callers,
    render_module_toc,
    render_object,
    render_procedure_card,
    render_procedure_search,
    render_standard_procedure_search,
    render_syntax_item,
)
from .search import FIELD_KIND_TITLES
from .standard_procedure_intents import recognize_standard_procedure_intent
from .syntax_model import KIND_TITLES, SyntaxItem, parse_version, release
from .virtual_tables import virtual_tables


_ИСХОДНЫЙ_ЧИТАТЕЛЬ_МОДУЛЯ = прочитать_модуль


def health(registry: Registry, *, detailed: bool) -> dict:
    """Тело ответа `/health`: живость и состав источников.

    `detailed` — прошёл ли запрос проверку на чтение. Имена конфигураций уже
    сведения о клиенте: кто у него внедрён и как называются доработки, — без
    токена отдаются только счётчики.

    """
    snapshot = registry.snapshot()
    площадки = list(snapshot.syntax.syntax.platforms) if snapshot.syntax else []
    тело = {
        "status": "ok",
        "configurations_total": len(snapshot.configurations),
        "syntax_loaded": bool(площадки),
    }
    if detailed:
        тело["configurations"] = list(snapshot.configuration_names)
        тело["syntax"] = площадки
    return тело


MAX_LIMIT = 50


def _render_notes(notes: list[str]) -> str:
    if not notes:
        return ""
    lines = ["", "---", "> **Оговорки по источнику данных**"]
    lines += [f"> - {note}" for note in notes]
    return "\n".join(lines) + "\n"


def _notes_block(
    context, *, critical_only: bool = True, include_code: bool = True
) -> str:
    return _render_notes(
        context.notes(
            critical_only=critical_only,
            include_code=include_code,
        )
    )


def _code_notes_block(context) -> str:
    return _render_notes(context.code_notes())


def _clamp(limit: int) -> int:
    return max(1, min(int(limit or 10), MAX_LIMIT))


# --------------------------------------------------------------- обзор


@dataclass(frozen=True, slots=True)
class _CodeCapture:
    source: SourceSnapshot | None
    loaded: LoadedModules | None
    ready: bool
    status: str
    error: str
    stage: tuple[int, int]
    stage_title: str
    progress: tuple[int, int]


@dataclass(frozen=True, slots=True)
class CodeProblemRow:
    """Одна обезличенная проблема покрытия готового корпуса кода."""

    category: str
    address: str | None
    ordinal: int
    reason: str
    marker: int | None = None


@dataclass(frozen=True, slots=True)
class CodeCoverage:
    """Точные агрегаты одного поколения; список проблем ограничен двадцатью."""

    modules_total: int
    modules_source_available: int
    modules_empty: int
    modules_partial: int
    modules_unreadable: int
    modules_conflict: int
    modules_compiled_without_source: int
    procedures_total: int
    procedures_full: int
    procedures_partial: int
    forms_total: int
    form_structures_full: int
    form_structures_partial: int
    form_structures_unread: int
    form_modules_read: int
    form_modules_empty: int
    form_modules_missing: int
    form_modules_unread: int
    unknown_markers: int
    known_markers_incomplete: int
    unsupported_addresses: int
    broken_containers: int
    unreadable_bodies: int
    budget_exceeded: int
    body_conflicts: int
    compiled_without_source: int
    problem_categories: tuple[tuple[str, int], ...]
    problems_total: int
    problems: tuple[CodeProblemRow, ...]

    @property
    def problems_omitted(self) -> int:
        return self.problems_total - len(self.problems)

    @property
    def has_limitations(self) -> bool:
        return any(
            (
                self.problem_categories,
                self.modules_partial,
                self.modules_unreadable,
                self.modules_conflict,
                self.procedures_partial,
                self.form_structures_partial,
                self.form_structures_unread,
                self.unsupported_addresses,
                self.broken_containers,
                self.unreadable_bodies,
                self.budget_exceeded,
                self.body_conflicts,
                self.compiled_without_source,
            )
        )


@dataclass(frozen=True, slots=True)
class _CodeSummary:
    modules: tuple[str, ...]
    compiled_modules: tuple[str, ...]
    forms: tuple[str, ...]
    procedures: int
    own_procedures: int
    overrides: tuple[tuple[str, int], ...]
    overridden_modules: frozenset[str]
    coverage: CodeCoverage


@dataclass(frozen=True, slots=True)
class _CodeView:
    capture: _CodeCapture
    summary: _CodeSummary | None


@dataclass(frozen=True, slots=True)
class _ConfigurationCodeSnapshot:
    context: object
    modules: _CodeView
    extensions: tuple[tuple[str, _CodeView], ...]


@dataclass(frozen=True, slots=True)
class _ListConfigurationsCapture:
    registry: RegistrySnapshot
    configurations: tuple[tuple[str, object], ...]
    syntax: object | None
    syntax_versions: tuple[tuple[str, object], ...]
    sources: tuple[tuple[object, "SourceStateRow", str], ...]
    rows: tuple[_ConfigurationCodeSnapshot, ...]


@dataclass(frozen=True, slots=True)
class CodeStateRow:
    """Одна атомарная строка состояния кода для человеческих оболочек."""

    configuration: str
    corpus: str
    state: str
    coverage: CodeCoverage | None = None
    source_id: str = ""
    journal: str = ""
    phase: str = "missing"
    diagnostic: str = ""
    kind: str = KIND_MODULES
    native_generation: bool = False


@dataclass(frozen=True, slots=True)
class SourceStateRow:
    """Учётная строка источника, скопированная под замком реестра."""

    id: str
    kind: str
    platform: str
    items_total: int
    warnings: tuple[str, ...]
    status: str = ""
    loaded_at: str = ""
    code_version: str = ""
    incomplete: bool = False


@dataclass(frozen=True, slots=True)
class SourcesSnapshot:
    """Единый неизменяемый снимок таблицы источников и состояний кода."""

    configuration_names: tuple[str, ...]
    sources: tuple[SourceStateRow, ...]
    code: tuple[CodeStateRow, ...]
    configurations: tuple["ConfigurationStateRow", ...] = ()


@dataclass(frozen=True, slots=True)
class ConfigurationStateRow:
    """Структурированная строка ``reg-list`` одного поколения."""

    name: str
    version: str
    platform: str
    objects: int
    edges: int
    loaded_at: str
    syntax_present: bool
    syntax_platform: str
    syntax_relation: str
    syntax_hidden: int
    notes: tuple[str, ...]
    code: tuple[CodeStateRow, ...]
    extension_runtime: SourceStateRow | None = None
    native_generation: bool = False
    compatibility_mode: str = ""
    predefined_available: bool = True


@dataclass(frozen=True, slots=True)
class ConfigurationsSnapshot:
    """Всё, что печатает ``reg-list``, после одного финального CAS."""

    rows: tuple[ConfigurationStateRow, ...]
    syntax_platforms: tuple[str, ...]
    syntax_source_platform: str
    syntax_items: int


_OVERRIDE_KINDS = ("Вместо", "После", "Перед", "ИзменениеИКонтроль")


def _capture_code(snapshot: RegistrySnapshot, source_id: str) -> _CodeCapture:
    code = snapshot.modules.get(source_id)
    if code is None:
        return _CodeCapture(
            source=snapshot.sources.get(source_id),
            loaded=None,
            ready=False,
            status="",
            error="",
            stage=(0, 0),
            stage_title="",
            progress=(0, 0),
        )
    return _CodeCapture(
        source=code.source,
        loaded=code.loaded,
        ready=code.ready,
        status=code.status,
        error=code.error,
        stage=code.stage,
        stage_title=code.stage_title,
        progress=code.progress,
    )


def _capture_is_current(
    snapshot: RegistrySnapshot, source_id: str, capture: _CodeCapture
) -> bool:
    current = _capture_code(snapshot, source_id)
    return (
        current.source == capture.source
        and current.loaded is capture.loaded
        and current.ready == capture.ready
        and current.status == capture.status
        and current.error == capture.error
        and current.stage == capture.stage
        and current.stage_title == capture.stage_title
        and current.progress == capture.progress
    )


def _safe_problem_reason(reason: str) -> str:
    if not reason:
        return "причина не записана"
    if "/" in reason or "\\" in reason:
        return "подробности доступны в журнале сервера"
    return reason


def _iter_code_problems(loaded: LoadedModules):
    """Уникальные обезличенные проблемы без сортировки и общего списка."""
    if loaded.каталог is None or loaded.формы is None:
        raise RegistryError("Готовый индекс кода неполон; перезагрузите источник.")
    # ``form_structure_missing`` — итоговое следствие. Когда та же форма уже
    # имеет точную причину (битый XML, синтаксис контейнера, бюджет), второй
    # ряд с тем же адресом только съедает лимит первых двадцати адресов и не
    # добавляет знания. Для module-only формы без иной причины он остаётся.
    точные_причины_форм = {
        problem.адрес.casefold()
        for problems in loaded.формы.object_problems.values()
        for problem in problems
        if problem.категория != "form_structure_missing"
    }
    seen: set[tuple[str, str | None, int, str, int | None]] = set()

    for problems in (loaded.каталог.object_problems or {}).values():
        for problem in problems:
            item = CodeProblemRow(
                problem.category,
                problem.address,
                problem.ordinal,
                _safe_problem_reason(problem.reason),
            )
            key = (
                item.category,
                item.address,
                item.ordinal,
                item.reason,
                item.marker,
            )
            if key not in seen:
                seen.add(key)
                yield item
    for outcome in loaded.каталог.outcomes:
        if outcome.category != "unknown_address":
            continue
        item = CodeProblemRow(
            "unknown_address",
            None,
            outcome.ordinal,
            "канонический адрес не доказан",
        )
        key = (
            item.category,
            item.address,
            item.ordinal,
            item.reason,
            item.marker,
        )
        if key not in seen:
            seen.add(key)
            yield item
    for problems in loaded.формы.object_problems.values():
        for problem in problems:
            if (
                problem.категория == "form_structure_missing"
                and problem.адрес.casefold() in точные_причины_форм
            ):
                continue
            item = CodeProblemRow(
                problem.категория,
                problem.адрес,
                0,
                _safe_problem_reason(problem.причина),
                problem.маркер,
            )
            key = (
                item.category,
                item.address,
                item.ordinal,
                item.reason,
                item.marker,
            )
            if key not in seen:
                seen.add(key)
                yield item
    for entry in loaded.каталог.entries.values():
        if not entry.compiled:
            continue
        item = CodeProblemRow(
            "compiled_without_source",
            entry.address,
            0,
            "исходный текст модуля поставлен скомпилированным",
        )
        key = (
            item.category,
            item.address,
            item.ordinal,
            item.reason,
            item.marker,
        )
        if key not in seen:
            seen.add(key)
            yield item


def _all_code_problems(loaded: LoadedModules) -> tuple[CodeProblemRow, ...]:
    """Первые 20 строк без материализации полного корпуса проблем."""
    return tuple(
        heapq.nsmallest(
            20,
            _iter_code_problems(loaded),
            key=lambda item: (
                item.address is None,
                item.address.casefold() if item.address else "",
                item.address or "",
                item.category,
                item.ordinal,
                item.reason,
                -1 if item.marker is None else item.marker,
            ),
        )
    )


def _code_coverage(
    loaded: LoadedModules, *, include_problem_rows: bool = True
) -> CodeCoverage:
    if loaded.каталог is None or loaded.формы is None:
        raise RegistryError("Готовый индекс кода неполон; перезагрузите источник.")
    каталог = loaded.каталог
    формы = loaded.формы
    if loaded.оглавление is None:
        raise RegistryError("Готовый индекс кода неполон; перезагрузите источник.")
    процедур_всего, процедур_частично, частичные_модули = (
        loaded.оглавление.покрытие_процедур()
    )
    категории: dict[str, set[str]] = {}
    for outcome in каталог.outcomes:
        if outcome.address is not None:
            категории.setdefault(outcome.address.casefold(), set()).add(
                outcome.category
            )

    модулей_с_исходником = 0
    модулей_пусто_всего = 0
    модулей_частично = 0
    модулей_непрочитано_всего = 0
    модулей_конфликт = 0
    модулей_скомпилировано = 0
    for entry in каталог.entries.values():
        # XML-дескриптор или Form.xml доказывает существование формы, но не
        # модуля. Такой адрес относится только к таблице модулей форм.
        есть_кандидат_модуля = (
            not entry.is_form
            or entry.locator is not None
            or bool(set(entry.form_evidence) & {"module", "container", "form_bin"})
        )
        if not есть_кандидат_модуля:
            continue
        состояния = категории.get(entry.address.casefold(), set())
        if entry.conflict:
            модулей_конфликт += 1
        elif entry.compiled:
            модулей_скомпилировано += 1
        elif entry.locator is None:
            модулей_непрочитано_всего += 1
        elif "empty" in состояния and "indexed" not in состояния:
            модулей_пусто_всего += 1
        elif entry.address in частичные_модули:
            модулей_частично += 1
        else:
            модулей_с_исходником += 1
    модулей_всего = sum(
        (
            модулей_с_исходником,
            модулей_пусто_всего,
            модулей_частично,
            модулей_непрочитано_всего,
            модулей_конфликт,
            модулей_скомпилировано,
        )
    )

    модулей_прочитано = 0
    модулей_пусто = 0
    модулей_нет = 0
    модулей_непрочитано = 0
    for entry in каталог.entries.values():
        if not entry.is_form:
            continue
        состояния = категории.get(entry.address.casefold(), set())
        if entry.conflict:
            модулей_непрочитано += 1
        elif "empty" in состояния:
            модулей_пусто += 1
        elif entry.locator is not None and not entry.compiled:
            модулей_прочитано += 1
        elif состояния & {"broken_container", "unreadable_body"}:
            модулей_непрочитано += 1
        else:
            модулей_нет += 1

    проблемы = _all_code_problems(loaded) if include_problem_rows else ()
    категории_проблем = dict(каталог.problem_counts)
    for category, count in формы.problem_counts:
        категории_проблем[category] = категории_проблем.get(category, 0) + count
    if каталог.coverage.compiled:
        категории_проблем["compiled_without_source"] = (
            категории_проблем.get("compiled_without_source", 0)
            + каталог.coverage.compiled
        )
    проблем_всего = sum(категории_проблем.values())
    конфликты = len(
        {
            entry.address.casefold()
            for entry in каталог.entries.values()
            if entry.conflict
        }
    )
    return CodeCoverage(
        modules_total=модулей_всего,
        modules_source_available=модулей_с_исходником,
        modules_empty=модулей_пусто_всего,
        modules_partial=модулей_частично,
        modules_unreadable=модулей_непрочитано_всего,
        modules_conflict=модулей_конфликт,
        modules_compiled_without_source=модулей_скомпилировано,
        procedures_total=процедур_всего,
        procedures_full=процедур_всего - процедур_частично,
        procedures_partial=процедур_частично,
        forms_total=len(формы.модули),
        form_structures_full=формы.полных,
        form_structures_partial=формы.частичных,
        form_structures_unread=формы.непрочитанных,
        form_modules_read=модулей_прочитано,
        form_modules_empty=модулей_пусто,
        form_modules_missing=модулей_нет,
        form_modules_unread=модулей_непрочитано,
        unknown_markers=формы.неизвестных_маркеров,
        known_markers_incomplete=формы.известных_неполных,
        unsupported_addresses=каталог.coverage.unknown_address,
        broken_containers=каталог.coverage.broken_container,
        unreadable_bodies=каталог.coverage.unreadable_body,
        budget_exceeded=формы.превышений_бюджета,
        body_conflicts=конфликты,
        compiled_without_source=каталог.coverage.compiled,
        problem_categories=tuple(sorted(категории_проблем.items())),
        problems_total=проблем_всего,
        problems=проблемы if include_problem_rows else (),
    )


def _summarize_code(loaded: LoadedModules) -> _CodeSummary:
    """Агрегаты готового пакета без чтения модулей и списка ``Запись``."""
    if loaded.оглавление is None or loaded.формы is None:
        raise RegistryError("Готовый индекс кода неполон; перезагрузите источник.")
    toc = loaded.оглавление.сводка()
    порядок = lambda value: (value.casefold(), value)
    return _CodeSummary(
        modules=tuple(sorted(loaded.оглавление.модули, key=порядок)),
        compiled_modules=tuple(
            sorted(
                (
                    module
                    for module in loaded.оглавление.модули
                    if loaded.оглавление.скомпилирован(module)
                ),
                key=порядок,
            )
        ),
        forms=tuple(sorted(loaded.формы.модули, key=порядок)),
        procedures=toc.процедур,
        own_procedures=toc.собственных,
        overrides=tuple((kind, toc.перекрытия[kind]) for kind in _OVERRIDE_KINDS),
        overridden_modules=toc.перекрытые_модули,
        coverage=_code_coverage(loaded),
    )


def _configuration_code_snapshot(
    registry: Registry, name: str
) -> _ConfigurationCodeSnapshot:
    """Согласованный снимок конфигурации и всех её корпусов кода.

    Агрегаты считаются вне замка. Перед публикацией проверяются личности всех
    источников и скалярные поля прогресса; при reparse/remove весь снимок
    собирается заново, а не смешивает поколения.
    """
    prefix = f"{name}:ext:"
    base_id = f"{name}:modules"
    for _ in range(2):
        snapshot = registry.snapshot()
        context = registry.resolve(name)
        if (
            snapshot.configurations.get(name) is not context.configuration
            or snapshot.syntax is not context.syntax
        ):
            continue
        base = _capture_code(snapshot, base_id)
        if base.loaded is not context.modules:
            continue
        extension_ids = tuple(
            prefix + extension
            for extension in snapshot.extension_names(name)
        )
        extensions = tuple(
            (source_id, _capture_code(snapshot, source_id))
            for source_id in extension_ids
        )

        base_summary = (
            _summarize_code(base.loaded)
            if base.loaded is not None and base.ready
            else None
        )
        extension_views = tuple(
            (
                source_id[len(prefix):],
                _CodeView(
                    capture,
                    _summarize_code(capture.loaded)
                    if capture.loaded is not None and capture.ready
                    else None,
                ),
            )
            for source_id, capture in extensions
        )

        if (
            not registry.snapshot_is_current(snapshot)
            or not _capture_is_current(snapshot, base_id, base)
            or not all(
                _capture_is_current(snapshot, source_id, capture)
                for source_id, capture in extensions
            )
        ):
            continue
        return _ConfigurationCodeSnapshot(
            context=context,
            modules=_CodeView(base, base_summary),
            extensions=extension_views,
        )
    raise RegistryError(
        "Источники кода изменились дважды; повторите запрос после завершения "
        "загрузки."
    )


def _structure_origin_view(
    snapshot: _ConfigurationCodeSnapshot,
) -> structure_origin.StructureOriginView:
    """Происхождение из тех же поколений, что уже захватил ``get_object``."""
    base_capture = snapshot.modules.capture
    base_sha256 = base_capture.source.sha256 if base_capture.source else ""
    base_catalog = (
        base_capture.loaded.структура if base_capture.loaded is not None else None
    )
    extensions = tuple(
        (
            name,
            view.capture.source.sha256 if view.capture.source else "",
            view.capture.loaded.структура
            if view.capture.loaded is not None
            else None,
        )
        for name, view in snapshot.extensions
    )
    return structure_origin.resolve(
        base_sha256=base_sha256,
        base=base_catalog,
        extensions=extensions,
    )


def _list_capture_is_current(
    registry: Registry, capture: _ListConfigurationsCapture
) -> bool:
    return registry.snapshot_is_current(capture.registry)


def _source_state_row(source) -> SourceStateRow:
    """Скопировать изменяемую ``Source`` в значение для публикации."""
    return SourceStateRow(
        id=source.id,
        kind=source.kind,
        platform=source.platform,
        items_total=source.items_total,
        warnings=tuple(source.warnings),
        status=source.status,
        loaded_at=source.loaded_at,
        code_version=source.code_version,
        incomplete=source.incomplete,
    )


def _capture_configurations_list(
    registry: Registry,
) -> _ListConfigurationsCapture | None:
    snapshot = registry.snapshot()
    configurations = tuple(snapshot.configurations.items())
    syntax = snapshot.syntax
    syntax_versions = tuple(snapshot.syntax_versions.items())
    sources = tuple(
        (source, _source_state_row(source), source.stored_path)
        for source in snapshot.sources.values()
    )
    try:
        rows = tuple(
            _configuration_code_snapshot(registry, name)
            for name, _ in configurations
        )
    except RegistryError:
        return None
    if not registry.snapshot_is_current(snapshot):
        return None
    return _ListConfigurationsCapture(
        registry=snapshot,
        configurations=configurations,
        syntax=syntax,
        syntax_versions=syntax_versions,
        sources=sources,
        rows=rows,
    )


def _safe_code_error(error: str) -> str:
    if not error:
        return "причина не записана"
    lowered = error.casefold()
    unsafe_fragments = (
        "/",
        "\\",
        "\n",
        "\r",
        "traceback",
        "errno",
        "permission denied",
        ".bsl",
        ".form",
        ".zip",
        ".xml",
    )
    if len(error) > 300 or any(
        fragment in lowered for fragment in unsafe_fragments
    ):
        return "подробности ошибки доступны в журнале сервера"
    return error


def _safe_admin_code_error(error: str) -> str:
    """Оставить техническую причину, но убрать приватное имя origin."""
    marker = "индекс кода не построился — "
    _origin, separator, reason = error.partition(marker)
    if separator:
        return _safe_code_error(marker + reason)
    return _safe_code_error(error)


def _loaded_partial_warning(
    loaded: LoadedModules, *, corpus: str = ""
) -> str | None:
    if loaded.оглавление is None or loaded.формы is None or loaded.каталог is None:
        return None
    coverage = _code_coverage(loaded, include_problem_rows=False)
    if not coverage.has_limitations:
        return None
    suffix = f" {corpus}" if corpus else ""
    categories = ", ".join(
        f"{category}={count}"
        for category, count in coverage.problem_categories
    ) or "нет"
    warning = (
        f"Покрытие кода{suffix} неполно: структуры форм: полностью "
        f"{coverage.form_structures_full}, частично "
        f"{coverage.form_structures_partial}, не прочитано "
        f"{coverage.form_structures_unread}; модули форм: прочитано "
        f"{coverage.form_modules_read}, пусто {coverage.form_modules_empty}, "
        f"отсутствует {coverage.form_modules_missing}, не прочитано "
        f"{coverage.form_modules_unread}; неизвестных маркеров: "
        f"{coverage.unknown_markers}, известных маркеров с неполной "
        f"семантикой: {coverage.known_markers_incomplete}, неподдержанных "
        f"адресов: {coverage.unsupported_addresses}, битых контейнеров: "
        f"{coverage.broken_containers}, непрочитанных тел: "
        f"{coverage.unreadable_bodies}, превышений бюджета: "
        f"{coverage.budget_exceeded}, конфликтов тел: "
        f"{coverage.body_conflicts}, скомпилированных без исходника: "
        f"{coverage.compiled_without_source}; категории проблем: "
        f"{categories}. Нулевой счётчик или пустой список не доказывает "
        "отсутствие скрытых данных."
    )
    if coverage.compiled_without_source:
        warning += (
            " Каждый такой модуль поставлен скомпилированным: сведения о "
            "процедурах, вызовах и перекрытиях приведены только по доступным "
            "исходникам; дополнительные процедуры, вызовы и перекрытия могут "
            "быть скрыты."
        )
    return warning


def _code_state_text(view: _CodeView) -> str:
    capture = view.capture
    if capture.source is None or capture.loaded is None:
        return "не загружен"
    if capture.ready and view.summary is not None:
        summary = view.summary
        readiness = (
            "готов с ограничениями"
            if summary.coverage.has_limitations
            else "готов"
        )
        procedures_label = (
            f"процедур {summary.procedures} по доступным исходникам"
            if summary.compiled_modules
            else f"процедур {summary.procedures}"
        )
        return (
            f"{readiness} — модулей {len(summary.modules)}, {procedures_label}, "
            f"форм {len(summary.forms)}, "
            f"скомпилированных без исходника {len(summary.compiled_modules)}"
        )
    if capture.status == STATUS_ERROR:
        return f"ошибка — {_safe_code_error(capture.error)}"
    stage, stages = capture.stage
    done, total = capture.progress
    return (
        f"строится: этап {stage}/{stages} «{capture.stage_title}», "
        f"обработано {done} из {total} элементов этапа"
    )


def code_coverage_lines(coverage: CodeCoverage | None) -> tuple[str, ...]:
    """Одинаковая диагностика для MCP, CLI и страницы ``/sources``."""
    if coverage is None:
        return ()
    lines: list[str] = []
    if coverage.has_limitations:
        lines.append(
            "ВНИМАНИЕ: покрытие кода неполно; нулевой счётчик или пустой "
            "список не доказывает отсутствие скрытых данных."
        )
    lines.extend(
        [
            f"Форм обнаружено: {coverage.forms_total}",
            "Структуры форм: "
            f"полностью {coverage.form_structures_full}, "
            f"частично {coverage.form_structures_partial}, "
            f"не прочитано {coverage.form_structures_unread}",
            "Модули форм: "
            f"прочитано {coverage.form_modules_read}, "
            f"пусто {coverage.form_modules_empty}, "
            f"отсутствует {coverage.form_modules_missing}, "
            f"не прочитано {coverage.form_modules_unread}",
            "Причины: "
            f"неизвестных маркеров {coverage.unknown_markers}, "
            "известных маркеров с неполной семантикой "
            f"{coverage.known_markers_incomplete}, "
            f"неподдержанных адресов {coverage.unsupported_addresses}, "
            f"битых контейнеров {coverage.broken_containers}, "
            f"непрочитанных тел {coverage.unreadable_bodies}, "
            f"превышений бюджета {coverage.budget_exceeded}, "
            f"конфликтов тел {coverage.body_conflicts}, "
            "скомпилированных без исходника "
            f"{coverage.compiled_without_source}",
        ]
    )
    if coverage.problem_categories:
        lines.append(
            "Категории проблем: "
            + ", ".join(
                f"{category}={count}"
                for category, count in coverage.problem_categories
            )
        )
    for problem in coverage.problems:
        target = (
            f"`{problem.address}`"
            if problem.address is not None
            else f"неадресуемый кандидат #{problem.ordinal}"
        )
        lines.append(
            f"Проблема [{problem.category}] {target} — {problem.reason}"
        )
    if coverage.problems_omitted:
        lines.append(f"Ещё проблем: {coverage.problems_omitted}")
    return tuple(lines)


def list_configurations(registry: Registry) -> str:
    """Какие конфигурации загружены и что по ним доступно."""
    for _ in range(2):
        capture = _capture_configurations_list(registry)
        if capture is None:
            continue
        answer = _render_configurations_list(capture)
        if _list_capture_is_current(registry, capture):
            return answer
    raise RegistryError(
        "Источники списка конфигураций изменились дважды; повторите запрос "
        "после завершения загрузки."
    )


def list_extensions(registry: Registry, config: str | None = None) -> str:
    """Фактическое состояние расширений по отдельному сеансовому снимку."""
    for _ in range(2):
        context = registry.resolve(config)
        snapshot = registry.snapshot()
        configuration = context.configuration
        assert configuration is not None
        name = configuration.config.name
        if snapshot.configurations.get(name) is not configuration:
            continue
        runtime = snapshot.extension_runtime.get(name)
        if context.extension_runtime is None:
            if runtime is not None:
                continue
        elif (
            runtime is None
            or runtime.snapshot is not context.extension_runtime.snapshot
            or runtime.source != SourceSnapshot.capture(
                context.extension_runtime.source
            )
        ):
            continue
        code_names = snapshot.extension_names(name)
        if not registry.snapshot_is_current(snapshot):
            continue
        return _render_extensions_state(
            configuration.config,
            runtime,
            code_names,
        )
    raise RegistryError(
        "Источники состояния расширений изменились дважды; повторите запрос "
        "после завершения загрузки."
    )


def _render_extensions_state(config, runtime, code_names: tuple[str, ...]) -> str:
    out = [f"# Расширения: {config.name}", ""]
    if runtime is None:
        out.extend(
            [
                "- Состояние: **unknown**",
                "- Фактическая активность и порядок неизвестны: отдельный "
                "снимок текущего сеанса не загружен.",
            ]
        )
        if code_names:
            out.extend(
                [
                    "",
                    "## Загруженный код",
                    "",
                    *(
                        f"- `{name}` — активность **unknown**"
                        for name in code_names
                    ),
                ]
            )
        return "\n".join(out).rstrip() + "\n"

    state = runtime.snapshot
    stale_reasons = _extension_runtime_stale_reasons(config, state)

    status = "stale" if stale_reasons else "snapshot"
    out.extend(
        [
            f"- Состояние: **{status}**",
            f"- Снято: `{state.captured_at}` · снимок `{state.snapshot_id}`",
            f"- Identity источника: `sha256:{runtime.source.sha256}`",
            "- Область наблюдения: текущий сеанс и его текущая область данных.",
        ]
    )
    if state.database_changed_since_session_start is None:
        out.append(
            "- Признак изменения набора после старта: **unknown** — API не "
            "получен в снятом сеансе; метод существует с платформы 8.3.22."
        )
    for reason in stale_reasons:
        out.append(f"- ⚠ {reason}.")

    out.extend(
        [
            "",
            "## Действуют в снятом сеансе",
            "",
            "> Позиция — порядок элементов в ответе API платформы; сама по "
            "себе она не является доказанным порядком исполнения модулей.",
            "",
        ]
    )
    if state.session_active:
        for item in state.session_active:
            details = [
                value
                for value in (
                    f"назначение {item.purpose}" if item.purpose else "",
                    f"область {item.scope}" if item.scope else "",
                    f"версия {item.version}" if item.version else "",
                )
                if value
            ]
            suffix = f" — {', '.join(details)}" if details else ""
            out.append(f"{item.session_position}. `{item.name}`{suffix}")
    else:
        out.append("Расширений, действующих в снятом сеансе, нет.")

    out.extend(["", "## Не применены в этом сеансе", ""])
    if state.session_disabled:
        for item in state.session_disabled:
            enabled = (
                "включено для следующего запуска"
                if item.enabled
                else "отключено в базе"
            )
            out.append(f"- `{item.name}` — {enabled}")
    else:
        out.append("Платформа не вернула не применённых расширений.")

    runtime_by_code_name: dict[str, list[object]] = {}
    for item in state.by_uuid.values():
        runtime_by_code_name.setdefault(
            index_cache.safe_name(item.name).casefold(), []
        ).append(item)
    if code_names:
        out.extend(["", "## Связь с загруженным кодом", ""])
        for code_name in code_names:
            matches = runtime_by_code_name.get(code_name.casefold(), [])
            if len(matches) != 1:
                status_text = "unknown"
            else:
                activity = matches[0].active_in_session
                status_text = (
                    "действует"
                    if activity is True
                    else "не применено" if activity is False else "unknown"
                )
            out.append(f"- `{code_name}` — {status_text}")
    return "\n".join(out).rstrip() + "\n"


def _extension_runtime_stale_reasons(config, state) -> list[str]:
    stale_reasons: list[str] = []
    if (
        state.configuration.version
        and config.version
        and state.configuration.version != config.version
    ):
        stale_reasons.append(
            "версия конфигурации снимка "
            f"{state.configuration.version} не совпадает с загруженной "
            f"версией {config.version}"
        )
    if (
        state.configuration.platform
        and config.platform
        and state.configuration.platform != config.platform
    ):
        stale_reasons.append(
            "платформа снимка "
            f"{state.configuration.platform} не совпадает с платформой "
            f"структурной выгрузки {config.platform}"
        )
    if state.database_changed_since_session_start is True:
        stale_reasons.append(
            "набор расширений изменён после запуска снятого сеанса"
        )
    return stale_reasons


def _code_state_rows(
    capture: _ListConfigurationsCapture,
    registry: Registry | None = None,
) -> tuple[CodeStateRow, ...]:
    def journal(view: _CodeView) -> tuple[str, str]:
        source = view.capture.source
        if source is None:
            return "", ""
        relative = ""
        if (
            registry is not None
            and view.summary is not None
            # Startup уже сверил полный payload с текущими индексами. Если
            # заменить или удалить расходящийся файл не удалось, старый JSON
            # может физически остаться, но публиковать путь к нему нельзя.
            and coverage_log.WRITE_WARNING not in source.warnings
            and coverage_log.load_current(registry.data_dir, source) is not None
        ):
            relative = coverage_log.relative_path(source.id)
        return source.id, relative

    def phase(view: _CodeView) -> str:
        if view.capture.source is None or view.capture.loaded is None:
            return "missing"
        if view.capture.ready and view.summary is not None:
            return "limited" if view.summary.coverage.has_limitations else "ready"
        if view.capture.status == STATUS_ERROR:
            return "error"
        return "building"

    def diagnostic(view: _CodeView) -> str:
        if view.capture.status != STATUS_ERROR:
            return ""
        return _safe_admin_code_error(view.capture.error)

    rows: list[CodeStateRow] = []
    for snapshot in capture.rows:
        native_base = capture.registry.generation(
            ExportIdentity.configuration(snapshot.context.name)
        ) is not None
        source_id, journal_path = journal(snapshot.modules)
        rows.append(
            CodeStateRow(
                snapshot.context.name,
                "Основная конфигурация",
                _code_state_text(snapshot.modules),
                (
                    snapshot.modules.summary.coverage
                    if snapshot.modules.summary is not None
                    else None
                ),
                source_id,
                journal_path,
                phase(snapshot.modules),
                diagnostic(snapshot.modules),
                KIND_MODULES,
                native_base,
            )
        )
        for extension_name, view in snapshot.extensions:
            native_extension = capture.registry.generation(
                ExportIdentity.extension(
                    extension_name,
                    parent_configuration=snapshot.context.name,
                )
            ) is not None
            source_id, journal_path = journal(view)
            rows.append(CodeStateRow(
                snapshot.context.name,
                f"Расширение {extension_name}",
                _code_state_text(view),
                view.summary.coverage if view.summary is not None else None,
                source_id,
                journal_path,
                phase(view),
                diagnostic(view),
                KIND_EXTENSION,
                native_extension,
            ))
    return tuple(rows)


def _configurations_result(
    capture: _ListConfigurationsCapture,
    registry: Registry | None = None,
) -> ConfigurationsSnapshot:
    code = _code_state_rows(capture, registry)
    rows = []
    for snapshot in capture.rows:
        context = snapshot.context
        configuration = context.configuration
        config = configuration.config
        runtime = capture.registry.extension_runtime.get(config.name)
        rows.append(
            ConfigurationStateRow(
                name=config.name,
                version=config.version,
                platform=config.platform,
                compatibility_mode=config.compatibility_mode,
                predefined_available=config.predefined_available,
                objects=len(config),
                edges=len(configuration.graph.edges),
                loaded_at=configuration.source.loaded_at,
                syntax_present=context.syntax is not None,
                syntax_platform=context.syntax_platform,
                syntax_relation=context.syntax_relation,
                syntax_hidden=context.syntax_hidden,
                notes=tuple(context.notes()),
                code=tuple(
                    state for state in code if state.configuration == config.name
                ),
                extension_runtime=(
                    _source_state_row(runtime.source) if runtime is not None else None
                ),
                native_generation=(
                    capture.registry.generation(
                        ExportIdentity.configuration(config.name)
                    )
                    is not None
                ),
            )
        )
    syntax = capture.syntax
    return ConfigurationsSnapshot(
        rows=tuple(rows),
        syntax_platforms=(
            tuple(syntax.syntax.platforms) if syntax is not None else ()
        ),
        syntax_source_platform=(
            syntax.source.platform if syntax is not None else ""
        ),
        syntax_items=len(syntax.syntax) if syntax is not None else 0,
    )


def configurations_snapshot(registry: Registry) -> ConfigurationsSnapshot:
    """Метаданные, справки и все корпуса кода одним снимком для CLI."""
    for _ in range(2):
        capture = _capture_configurations_list(registry)
        if capture is None:
            continue
        result = _configurations_result(capture, registry)
        if _list_capture_is_current(registry, capture):
            return result
    raise RegistryError(
        "Источники списка конфигураций изменились дважды; повторите запрос "
        "после завершения загрузки."
    )


def sources_snapshot(registry: Registry) -> SourcesSnapshot:
    """Источники и состояния основной конфигурации/расширений одним поколением.

    Тяжёлые агрегаты считаются снаружи ``_lock``. Финальный CAS проверяет
    личности и скалярные поля всех Source, конфигураций и корпусов кода, так
    что таблица источников не может показать новое поколение рядом со старыми
    счётчиками.
    """
    for _ in range(2):
        prepared = _capture_sources_snapshot(registry)
        if prepared is None:
            continue
        result, capture = prepared
        if _sources_snapshot_is_current(registry, capture):
            return result
    raise RegistryError(
        "Источники изменились дважды; повторите запрос после завершения "
        "загрузки."
    )


def _capture_sources_snapshot(
    registry: Registry,
) -> tuple[SourcesSnapshot, _ListConfigurationsCapture] | None:
    """Подготовить снимок и маркер для позднего CAS после файловых обходов."""
    capture = _capture_configurations_list(registry)
    if capture is None:
        return None
    configurations = _configurations_result(capture, registry).rows
    return (
        SourcesSnapshot(
            configuration_names=tuple(name for name, _ in capture.configurations),
            sources=tuple(row for _, row, _ in capture.sources),
            code=tuple(
                code for configuration in configurations for code in configuration.code
            ),
            configurations=configurations,
        ),
        capture,
    )


def _sources_snapshot_is_current(
    registry: Registry, capture: _ListConfigurationsCapture
) -> bool:
    """Финальный CAS подготовленного снимка; тяжёлой работы внутри нет."""
    return _list_capture_is_current(registry, capture)


def configuration_code_states(registry: Registry) -> tuple[CodeStateRow, ...]:
    """Состояния основной конфигурации и расширений из общего снимка."""
    return sources_snapshot(registry).code


def _render_configurations_list(capture: _ListConfigurationsCapture) -> str:
    if not capture.rows:
        if capture.syntax is not None:
            return _syntax_only_overview(capture.syntax)
        return (
            "Не загружено ни одной конфигурации и нет справки платформы.\n\n"
            "Выгрузите структуру обработкой из `exporter-1c/` и загрузите архив."
        )

    out = ["# Загруженные конфигурации", ""]
    for snapshot in capture.rows:
        context = snapshot.context
        config = context.configuration.config
        out.append(f"## {config.name}")
        if config.synonym:
            out.append(f"*{config.synonym}*")
        out.append("")
        out.append(
            f"- Версия: {config.version} · платформа **{config.platform or 'неизвестна'}**\n"
            f"- Объектов: {len(config)}, связей: "
            f"{len(context.configuration.graph.edges)}"
        )
        if config.compatibility_mode:
            out.append(f"- Режим совместимости: {config.compatibility_mode}")
        out.append(f"- Метаданные: да")
        if context.syntax is not None and context.syntax_platform:
            relation = {
                "exact": "версия совпадает с конфигурацией",
                "newer": (
                    f"новее конфигурации, скрыто {context.syntax_hidden} элементов"
                ),
                "older": "**старее конфигурации**",
                "unknown": "сравнение невозможно — фактическая версия платформы неизвестна",
            }.get(context.syntax_relation, context.syntax_relation)
            out.append(
                f"- Синтаксис платформы: справка {context.syntax_platform} — "
                f"{relation}"
            )
        else:
            out.append("- Синтаксис платформы: не подключён")
        out.append(f"- Индекс кода: {_code_state_text(snapshot.modules)}")
        out.extend(
            f"> {line}"
            for line in code_coverage_lines(
                snapshot.modules.summary.coverage
                if snapshot.modules.summary is not None
                else None
            )
        )
        for note in context.notes():
            out.append(f"- ⚠ {note}")
        for extension_name, extension in snapshot.extensions:
            out.extend(
                [
                    "",
                    f"### Расширение `{extension_name}`",
                    "",
                    f"- Индекс кода: {_code_state_text(extension)}",
                ]
            )
            out.extend(
                f"> {line}"
                for line in code_coverage_lines(
                    extension.summary.coverage
                    if extension.summary is not None
                    else None
                )
            )
            summary = extension.summary
            if summary is not None:
                overrides = sum(count for _, count in summary.overrides)
                доступность = (
                    " по доступным исходникам"
                    if summary.compiled_modules
                    else ""
                )
                out.append(
                    f"- Перекрытий{доступность}: {overrides} в "
                    f"{len(summary.overridden_modules)} модулях"
                )
                out.append(
                    f"- По видам{доступность}: "
                    + ", ".join(
                        f"{kind}: {count}" for kind, count in summary.overrides
                    )
                )
                out.append(
                    f"- Собственных процедур{доступность}: "
                    f"{summary.own_procedures}"
                )
                out.append(
                    "- Адресация: укажите "
                    f"`extension=\"{extension_name}\"` в инструментах кода."
                )
        out.append("")

    out += _coverage_section(capture)

    if len(capture.rows) > 1:
        out.append(
            "> В запросах указывайте `config` явно — конфигурация по умолчанию "
            "не подставляется."
        )
    return "\n".join(out).rstrip() + "\n"


def _syntax_only_overview(syntax) -> str:
    """Что доступно без единой загруженной конфигурации."""
    total = f"{len(syntax.syntax):,}".replace(",", "\u00a0")
    доступно = (
        f"справка платформы **{syntax.source.platform}** ({total} элементов)"
    )

    return (
        "# Конфигурации не загружены\n\n"
        f"Доступно: {доступно}. Работают `search_syntax` и "
        "`get_syntax`, параметр `config` указывать не нужно.\n\n"
        "Фильтрации по версии платформы нет — выдача содержит всё, что "
        "описано в подключённых справках. Загрузите выгрузку структуры "
        "конфигурации, чтобы лишнее отсекалось автоматически.\n"
    )


def _coverage_section(capture: _ListConfigurationsCapture) -> list[str]:
    """Каких справок не хватает и какие лишние.

    Знает об этом только сервер: он один видит и платформы конфигураций, и
    версии справок. Расхождение в один релиз стоит примерно 10–15 сигнатур и
    35–45 контекстов доступности — молчать об этом нельзя.
    """
    платформы_справок = {
        release(parse_version(source.platform)): source.platform
        for _, source in capture.syntax_versions
    }
    нужные: dict[tuple[int, ...], tuple[str, list[str]]] = {}
    unknown_platform = False
    for row in capture.rows:
        platform = row.context.configuration.config.platform
        key = release(parse_version(platform))
        if key:
            нужные.setdefault(key, (platform, []))[1].append(row.context.name)
        else:
            unknown_platform = True
    покрытие = {
        "loaded": [platform for _, platform in sorted(платформы_справок.items())],
        "missing": [
            {"platform": platform, "configurations": names}
            for key, (platform, names) in sorted(нужные.items())
            if key not in платформы_справок
        ],
        "unused": [
            platform
            for key, platform in sorted(платформы_справок.items())
            if key not in нужные and not unknown_platform
        ],
    }
    if not покрытие["loaded"] and not покрытие["missing"]:
        return []

    out = ["## Справки платформы", ""]
    if покрытие["loaded"]:
        out.append(f"Загружены: {', '.join(покрытие['loaded'])}.")
    else:
        out.append("Не загружено ни одной справки.")

    for пропуск in покрытие["missing"]:
        конфигурации = ", ".join(пропуск["configurations"])
        # «Собраны из соседних версий» верно, только когда соседние есть. При
        # нуле загруженных справок ответов по платформе нет вовсе, и обещать
        # приблизительные — врать (найдено живой проверкой 2026-08-19).
        чем_отвечаем = (
            "Ответы по ней собраны из соседних версий: наличие элементов "
            "отфильтровано, сигнатуры и доступность могут отличаться."
            if покрытие["loaded"]
            else "Методы и свойства платформы недоступны совсем — загрузите "
            "`shcntx_ru.hbk` этой версии."
        )
        out.append(
            f"- Не хватает справки **{пропуск['platform']}** — на ней работает "
            f"{конфигурации}. {чем_отвечаем}"
        )
    for лишняя in покрытие["unused"]:
        out.append(
            f"- Справка **{лишняя}** не используется: конфигураций на этой "
            "платформе нет."
        )
    out.append("")
    return out


# --------------------------------------------------------------- метаданные


def search_objects(
    registry: Registry,
    query: str,
    config: str | None = None,
    kind: str | None = None,
    limit: int = 10,
) -> str:
    """Найти объект конфигурации по описанию или части имени."""
    context = registry.resolve(config)
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


def get_object(
    registry: Registry,
    full_name: str,
    config: str | None = None,
    detail: str = FIELDS,
) -> str:
    """Структура объекта: реквизиты, табличные части, движения, связи."""
    if detail not in DETAIL_LEVELS:
        detail = FIELDS

    snapshot = None
    if detail == FIELDS or detail == FULL:
        name = registry.resolve(config).name
        snapshot = _configuration_code_snapshot(registry, name)
        context = snapshot.context
    else:
        context = registry.resolve(config)
    obj = context.configuration.config.get(full_name)

    if obj is None:
        hits = context.configuration.index.search(full_name, limit=5)
        suggestion = "\n".join(f"- `{h.doc.id}`" for h in hits)
        return (
            f"В конфигурации {context.name} нет объекта `{full_name}`.\n\n"
            + (f"Возможно, имелось в виду:\n{suggestion}\n" if suggestion else "")
            + _notes_block(context, include_code=False)
        )

    # Виртуальные таблицы собираются здесь, а не в рендере: они соединяют
    # метаданные конфигурации со справкой платформы, а рендер про справку
    # ничего не знает и знать не должен.
    # Предел нумерации субконто — свойство плана счетов, а спрашивают про
    # регистр бухгалтерии. Без него поля вида `Субконто1` назвать нечем.
    chart = context.configuration.config.get(str(obj.props.get("chart_of_accounts", "")))
    ext_dimensions = chart.props.get("max_ext_dimension_count", 0) if chart else 0

    # `ДанныеГрафика` описывает ресурсы графика — отдельного регистра сведений,
    # а не самого регистра расчёта.
    schedule = context.configuration.config.get(str(obj.props.get("schedule", "")))

    tables = virtual_tables(
        obj,
        context.syntax.tables if context.syntax else None,
        ext_dimension_count=ext_dimensions if isinstance(ext_dimensions, int) else 0,
        schedule_resources=[f.name for f in schedule.resources] if schedule else None,
    )

    body = render_object(
        obj,
        detail,
        graph=context.configuration.graph,
        virtual_tables=tables,
        origins=_structure_origin_view(snapshot) if snapshot is not None else None,
    )
    code = (
        _object_code_block(snapshot.modules, obj.full_name, detail)
        if snapshot is not None
        else ""
    )
    if snapshot is None:
        return body + _notes_block(context, include_code=False)
    return (
        body
        + _code_notes_block(context)
        + code
        + _notes_block(context, include_code=False)
    )


def _belongs_to_object(module: str, full_name: str) -> bool:
    lower_module = module.casefold()
    lower_name = full_name.casefold()
    return lower_module == lower_name or lower_module.startswith(lower_name + ".")


def _object_code_details(
    view: _CodeView, full_name: str
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    summary = view.summary
    if summary is None:
        return (), ()
    form_addresses = set(summary.forms)
    form_addresses.update(
        module for module in summary.modules if _is_form_module(module)
    )
    forms = tuple(
        sorted(
            (
                form
                for form in form_addresses
                if _belongs_to_object(form, full_name)
            ),
            key=lambda value: (value.casefold(), value),
        )
    )
    form_keys = {form.casefold() for form in forms}
    modules = tuple(
        module
        for module in summary.modules
        if _belongs_to_object(module, full_name)
        and module.casefold() not in form_keys
    )
    return modules, forms


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


def _object_code_block(view: _CodeView, full_name: str, detail: str) -> str:
    if detail == BRIEF:
        return ""
    state = _code_state_text(view)
    if view.capture.source is None or view.capture.loaded is None:
        state = "код не загружен"
    if view.summary is None:
        return (
            f"\nКод объекта: {state}.\n"
            if detail == FIELDS
            else f"\n## Код объекта\n\nИндекс кода: {state}.\n"
        )

    modules, forms = _object_code_details(view, full_name)
    loaded = view.capture.loaded
    if loaded is None:
        raise RegistryError("Готовый индекс кода неполон; перезагрузите источник.")
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
        )
    object_rows.extend(
        CodeProblemRow(
            "compiled_without_source",
            entry.address,
            0,
            "исходный текст модуля поставлен скомпилированным",
        )
        for entry in loaded.каталог.entries.values()
        if entry.compiled and entry.address.casefold() in form_keys
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
            f", скомпилированных без исходника {compiled_count}"
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
                        " — поставлен скомпилированным, исходного текста нет"
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


def get_related(
    registry: Registry,
    full_name: str,
    config: str | None = None,
    limit: int = 40,
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
    snapshot = _configuration_code_snapshot(registry, name)
    context = snapshot.context
    graph = context.configuration.graph

    if full_name not in context.configuration.config.objects:
        return f"В конфигурации {context.name} нет объекта `{full_name}`." + _notes_block(context)

    overrides, unavailable = _extension_overrides(snapshot)
    out = [f"# Связи `{full_name}` в {context.name}", ""]
    for extension_name, view in snapshot.extensions:
        if view.summary is None or view.capture.loaded is None:
            continue
        warning = _loaded_partial_warning(
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
        and _loaded_partial_warning(view.capture.loaded) is not None
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

    outgoing = graph.outgoing(full_name, include_weak=False)
    if outgoing:
        out.append(f"## Ссылается на ({len(outgoing)})")
        out.append("")
        for edge in outgoing[: _clamp(limit)]:
            out.append(f"- {related_name(edge.target)} — {edge.title}")
        if len(outgoing) > limit:
            out.append(f"- … ещё {len(outgoing) - limit}")
        out.append("")

    incoming = graph.incoming(full_name)
    if incoming:
        out.append(f"## Ссылаются на него ({len(incoming)})")
        out.append("")
        for edge in incoming[: _clamp(limit)]:
            out.append(f"- {related_name(edge.source)} — {edge.title}")
        if len(incoming) > limit:
            out.append(f"- … ещё {len(incoming) - limit}")
        out.append("")

    return "\n".join(out) + "\n" + _notes_block(context)


def _extension_overrides(
    snapshot: _ConfigurationCodeSnapshot,
) -> tuple[dict[str, tuple[str, ...]], list[tuple[str, str]]]:
    config = snapshot.context.configuration.config
    objects = sorted(
        config.objects,
        key=lambda value: (-len(value), value.casefold(), value),
    )
    found: dict[str, list[str]] = {}
    unavailable: list[tuple[str, str]] = []
    for extension_name, view in snapshot.extensions:
        if view.summary is None:
            unavailable.append((extension_name, _code_state_text(view)))
            continue
        for module in view.summary.overridden_modules:
            owner = next(
                (
                    full_name
                    for full_name in objects
                    if _belongs_to_object(module, full_name)
                ),
                None,
            )
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
    # Оба объекта берутся из одного снимка, а не из двух независимых resolve:
    # параллельная публикация не должна смешать поколения внутри ответа.
    snapshot = registry.snapshot()
    names = list(snapshot.configuration_names) if configs is None else list(configs)
    if len(names) != 2 or any(not isinstance(name, str) or not name for name in names) or names[0] == names[1]:
        raise RegistryError(
            "Укажите ровно две разные конфигурации в configs. "
            "Без configs автоматический выбор возможен, только если загружены ровно две. "
            "Список имён доступен через list_configurations."
        )

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
            if not isinstance(cursor, str) or not 1 <= len(cursor) <= 2048:
                raise ValueError
            state = json.loads(base64.b64decode(
                cursor + "=" * (-len(cursor) % 4), altchars=b"-_", validate=True,
            ))
            if (not isinstance(state, dict) or set(state) != {"v", "sha256", "offset"}
                    or type(state["v"]) is not int or state["v"] != 1
                    or type(state["offset"]) is not int or state["offset"] <= 0):
                raise ValueError
        except (ValueError, TypeError, binascii.Error, RecursionError):
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
        next_cursor = base64.urlsafe_b64encode(json.dumps(
            {"v": 1, "sha256": digest, "offset": end}, separators=(",", ":"),
        ).encode("utf-8")).decode("ascii").rstrip("=")
        arguments = json.dumps({"full_name": full_name, "configs": names,
                                "cursor": next_cursor, "limit": limit}, ensure_ascii=False)
        out.extend(("", "Продолжение (аргументы следующего вызова):",
                    f"`compare_configurations({arguments})`"))
    else:
        out.extend(("", "Все различия имён реквизитов прочитаны."))

    return "\n".join(out) + "\n"


# --------------------------------------------------------------- код модулей


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


def _отрезок_процедуры(
    текст: str,
    *,
    начало_строка: int,
    начало_столбец: int,
    конец_строка: int,
    конец_столбец: int,
) -> list[str]:
    """Физические строки между точными позициями одноразового разбора."""
    строки = текст.split("\n")
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


def _сигнатура_из_текста(текст: str, процедура: Процедура) -> str:
    """Декларация точного вхождения из уже дочитанного снимка модуля."""
    части = _отрезок_процедуры(
        текст,
        начало_строка=процедура.строка,
        начало_столбец=процедура.начало_столбец,
        конец_строка=процедура.конец_сигнатуры_строка,
        конец_столбец=процедура.конец_сигнатуры_столбец,
    )
    return " ".join(часть.strip() for часть in части if часть.strip())


def _прочитать_тело_модуля(loaded: LoadedModules, модуль: str) -> str:
    """Единая граница чтения; каталог подменяет файловый fallback-локатор."""
    if loaded.каталог is not None:
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
        locator = entry.locator
    else:
        # Совместимость только для вручную собранных старых тестовых пакетов;
        # production LoadedModules всегда публикуется вместе с каталогом.
        locator = ModuleLocator.file(путь_модуля(модуль))
    # Старые проверки подменяют этот символ, чтобы синхронизировать смену
    # поколения посреди чтения. В рабочем процессе всегда действует
    # защищённый reader локатора; подмена остаётся узким тестовым seam.
    if locator.kind == "file" and прочитать_модуль is not _ИСХОДНЫЙ_ЧИТАТЕЛЬ_МОДУЛЯ:
        return прочитать_модуль(loaded.корень / locator.relative_path)
    return read_bsl(loaded.корень, модуль, locator)


def _сигнатура(
    loaded: LoadedModules,
    запись,
    снимки: dict[str, tuple[str, dict[int, Процедура]]] | None = None,
) -> str:
    """Дочитывает только декларацию по сохранённому номеру строки.

    В `LoadedModules` сигнатуры и тела не появляются: текст существует лишь
    на время одного ответа. Снимок позволяет не читать и не разбирать один
    файл по разу на каждую строку оглавления.
    """
    if снимки is None:
        снимки = {}
    снимок = снимки.get(запись.модуль)
    if снимок is None:
        текст = _прочитать_тело_модуля(loaded, запись.модуль)
        разбор = _parsed_procedures(
            loaded.оглавление.модуля(запись.модуль), текст
        )
        снимки[запись.модуль] = (текст, разбор)
    else:
        текст, разбор = снимок
    return _сигнатура_из_текста(
        текст, _parsed_procedure(разбор, запись)
    )


class _SignatureError(Exception):
    """Граница декларации не совпала с текущим файлом модуля."""


class _StaleModules(Exception):
    """LoadedModules сменился, пока инструмент читал canonical root."""


def _modules_are_current(registry: Registry, loaded: LoadedModules) -> bool:
    """Короткий CAS без дискового I/O под замком реестра."""
    current = registry.snapshot().modules.get(loaded.source.id)
    return current is not None and current.loaded is loaded


def _modules_state_snapshot(
    registry: Registry,
    loaded: LoadedModules,
) -> tuple[
    bool,
    str,
    str,
    tuple[int, int],
    str,
    tuple[int, int],
]:
    """Снимок готовности и прогресса одного поколения.

    Фоновый поток меняет эти поля последовательными
    присваиваниями под `_lock`. Читатель берёт их под тем же замком,
    иначе видимы невозможные сочетания вроде «этап 2,
    оглавление» или `status=error` без уже записанной причины. Диск под
    замком не читается.
    """
    current = registry.snapshot().modules.get(loaded.source.id)
    if current is None or current.loaded is not loaded:
        raise _StaleModules
    return (
        current.ready,
        current.status,
        current.error,
        current.stage,
        current.stage_title,
        current.progress,
    )


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
    готов, status, error, этап, название_этапа, прогресс = (
        _modules_state_snapshot(registry, loaded)
    )
    if готов:
        return None
    if status == STATUS_ERROR:
        причина = _safe_code_error(error)
        return f"Индекс кода не построен: {причина}"
    номер_этапа, этапов = этап
    обработано, всего = прогресс
    return (
        f"Индекс кода строится: этап {номер_этапа}/{этапов} "
        f"«{название_этапа}», обработано {обработано} из {всего} "
        "элементов этапа. Ответы про код пока недоступны."
    )


def _procedure_matches(
    loaded: LoadedModules,
    записи: list,
    снимки: dict[str, tuple[str, dict[int, Процедура]]] | None = None,
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
                signature=_сигнатура(loaded, запись, снимки),
                exported=запись.экспорт,
                function=запись.функция,
                line=запись.строка,
                calls=по_модулям.get(запись.модуль, 0),
                unresolved_calls=неразрешённых,
                annotated=запись.перекрыта,
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
            match = _procedure_matches(loaded, найденные)[0]
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
) -> str:
    """Точное имя и поиск словами по коду конфигурации или расширения."""
    limit = _procedure_limit(limit)
    запрос = query.strip()
    if not запрос:
        raise RegistryError("Запрос query не может быть пустым.")

    context, loaded = _selected_modules(registry, config, extension)
    состояние = _modules_availability_message(registry, context, loaded)
    if состояние is not None:
        return состояние
    if any(
        индекс is None
        for индекс in (loaded.оглавление, loaded.вызовы, loaded.поиск)
    ):
        raise RegistryError("Готовый индекс кода неполон; перезагрузите источник.")

    try:
        приоритетные = _scope_modules(loaded, scope)
    except RegistryError as error:
        if not _modules_are_current(registry, loaded):
            raise _StaleModules from error
        raise
    предупреждение = _loaded_partial_warning(loaded)
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
        снимки: dict[str, tuple[str, dict[int, Процедура]]] = {}
        точные_совпадения = _procedure_matches(loaded, точные, снимки)
        словесные_совпадения = _procedure_matches(loaded, слова, снимки)
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
) -> str:
    """Поиск с одним полным повтором при смене поколения кода."""
    for _ in range(2):
        try:
            return _search_procedures_once(
                registry, query, config, extension, scope, limit
            )
        except _StaleModules:
            continue
    raise RegistryError(
        "Код изменился во время поиска дважды; повторите запрос после "
        "завершения загрузки."
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
) -> str:
    """Текст одного поколения, без раскрытия локального пути при отказе."""
    try:
        текст = _прочитать_тело_модуля(loaded, module)
    except OSError as error:
        if not _modules_are_current(registry, loaded):
            raise _StaleModules from error
        raise RegistryError(
            "Не удалось прочитать текущий файл модуля: файл недоступен."
        ) from error
    if not _modules_are_current(registry, loaded):
        raise _StaleModules
    return текст


def _parsed_procedures(
    записи: list, текст: str
) -> dict[int, Процедура]:
    """Сопоставляет оглавление с последовательностью одноразового разбора."""
    процедуры = разобрать(текст)
    if len(записи) != len(процедуры):
        raise _SignatureError
    результат: dict[int, Процедура] = {}
    for запись, процедура in zip(записи, процедуры, strict=True):
        if (
            запись.строка != процедура.строка
            or запись.имя.casefold() != процедура.имя.casefold()
        ):
            raise _SignatureError
        результат[запись.позиция] = процедура
    return результат


def _parsed_procedure(разбор: dict[int, Процедура], запись) -> Процедура:
    try:
        return разбор[запись.позиция]
    except KeyError as error:
        raise _SignatureError from error


def _procedure_body(текст: str, процедура: Процедура) -> list[str]:
    """Физические строки точного вхождения без соседей на граничных строках."""
    строки = текст.split("\n")
    конец_строка = процедура.конец or len(строки)
    return _отрезок_процедуры(
        текст,
        начало_строка=процедура.строка,
        начало_столбец=процедура.начало_столбец,
        конец_строка=конец_строка,
        конец_столбец=процедура.конец_столбец,
    )


def _extension_delta(текст: str, процедура: Процедура) -> list[str]:
    """Блоки правки дословно, включая сами граничные директивы."""
    результат: list[str] = []
    внутри: str | None = None
    концы = {
        "#удаление": "#конецудаления",
        "#вставка": "#конецвставки",
    }
    for строка in _procedure_body(текст, процедура):
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


def _similar_address(loaded: LoadedModules, module: str, name: str | None) -> list[str]:
    if name is None:
        return difflib.get_close_matches(module, loaded.оглавление.модули, n=5, cutoff=0.45)
    кандидаты = [
        f"{module}::{item.имя}" for item in loaded.оглавление.модуля(module)
    ]
    return difflib.get_close_matches(f"{module}::{name}", кандидаты, n=5, cutoff=0.45)


def _foreign_extension_warnings(
    registry: Registry,
    configuration: str,
    module: str,
    target_name: str,
    selected: LoadedModules,
    observed: list[LoadedModules],
) -> list[str]:
    prefix = f"{configuration}:ext:"
    snapshot = registry.snapshot()
    кандидаты = sorted(
        (
            (source_id[len(prefix):], code.loaded)
            for source_id, code in snapshot.modules.items()
            if source_id.startswith(prefix)
            and code.loaded is not None
            and code.loaded is not selected
        ),
        key=lambda item: item[0],
    )
    warnings: list[str] = []
    for extension_name, foreign in кандидаты:
        try:
            готов, *_ = _modules_state_snapshot(registry, foreign)
        except _StaleModules:
            continue
        if not готов:
            continue
        записи = foreign.оглавление.модуля(module)
        if not записи:
            continue
        текст = _read_module_snapshot(registry, foreign, module)
        observed.append(foreign)
        разбор = _parsed_procedures(записи, текст)
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
) -> str:
    context, loaded = _selected_modules(registry, config, extension)
    состояние = _modules_availability_message(registry, context, loaded)
    if состояние is not None:
        return состояние
    if any(
        индекс is None
        for индекс in (loaded.оглавление, loaded.вызовы, loaded.формы)
    ):
        raise RegistryError("Готовый индекс кода неполон; перезагрузите источник.")
    partial_warning = _loaded_partial_warning(loaded)
    warning_prefix = f"> {partial_warning}\n\n" if partial_warning else ""

    модуль, разделитель, имя = address.partition("::")
    модуль = модуль.strip()
    имя = имя.strip()
    if not модуль or (разделитель and (not имя or "::" in имя)):
        raise RegistryError(
            "address должен быть адресом модуля или парой `Модуль::Имя`."
        )
    канонический_модуль = next(
        (
            item
            for item in loaded.оглавление.модули
            if item.casefold() == модуль.casefold()
        ),
        None,
    )
    if канонический_модуль is None:
        похожие = _similar_address(loaded, модуль, None)
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
            f"Модуль `{модуль}` поставлен скомпилированным — исходного "
            "текста и оглавления процедур в выгрузке нет. Не считайте, что "
            f"процедуры отсутствуют: их нельзя увидеть.{хвост}\n"
        )

    текст = _read_module_snapshot(registry, loaded, модуль)
    разбор = _parsed_procedures(записи, текст)
    observed = [loaded]
    warnings = _module_warnings(context, loaded, записи)
    if partial_warning:
        warnings.insert(0, partial_warning)

    if not разделитель:
        outlines: list[ProcedureOutline] = []
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
                    signature=_сигнатура_из_текста(текст, parsed),
                    exported=запись.экспорт,
                    function=запись.функция,
                    line=запись.строка,
                    calls=calls,
                    directive=parsed.директива,
                    events=loaded.формы.обработчик(модуль, запись.имя) or (),
                )
            )
        if not _modules_package_is_current(registry, observed):
            raise _StaleModules
        return render_module_toc(
            context.name, модуль, outlines, warnings=warnings, extension=extension
        )

    совпадения = [запись for запись in записи if запись.имя.casefold() == имя.casefold()]
    if not совпадения:
        похожие = _similar_address(loaded, модуль, имя)
        if not _modules_are_current(registry, loaded):
            raise _StaleModules
        хвост = "" if not похожие else "\n\nВозможно, имелось в виду:\n" + "\n".join(f"- `{item}`" for item in похожие)
        return warning_prefix + f"В модуле `{модуль}` нет процедуры `{имя}`.{хвост}\n"
    запись = совпадения[0]
    parsed = _parsed_procedure(разбор, запись)
    body = _procedure_body(текст, parsed)
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
        base_text = _read_module_snapshot(registry, base, модуль)
        observed.append(base)
        base_parsed = _parsed_procedures(
            base.оглавление.модуля(модуль), base_text
        )
        body = [
            "// Тело основной конфигурации",
            *_procedure_body(
                base_text, _parsed_procedure(base_parsed, base_records[0])
            ),
            "",
            f"// Дельта расширения {extension}",
            *_extension_delta(текст, parsed),
        ]
    elif extension and parsed.перекрытие and parsed.перекрытие[0].casefold() in ("вместо", "around"):
        warnings.append(
            "Показано тело `&Вместо`; типовое тело читайте отдельным запросом "
            "к основной конфигурации без `extension`."
        )

    warnings.extend(
        _foreign_extension_warnings(
            registry, context.name, модуль, target_name, loaded, observed
        )
    )
    events = loaded.формы.обработчик(модуль, запись.имя) or ()
    compilation = _compilation_context(context, модуль, parsed)
    if events:
        compilation.append("события формы: " + ", ".join(events))
    if not _modules_package_is_current(registry, observed):
        raise _StaleModules
    return render_procedure_card(
        context.name,
        f"{модуль}::{запись.имя}",
        signature=_сигнатура_из_текста(текст, parsed),
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
) -> str:
    """Оглавление модуля или карточка процедуры с дисковым телом."""
    start_line, lines = _procedure_window(start_line, lines)
    if not isinstance(address, str) or not address.strip():
        raise RegistryError("address не может быть пустым.")
    for _ in range(2):
        try:
            return _get_procedure_once(
                registry, address, config, extension, start_line, lines
            )
        except _StaleModules:
            continue
        except _SignatureError as error:
            raise RegistryError(
                "Не удалось сопоставить оглавление с текущим текстом модуля; "
                "перезагрузите источник кода."
            ) from error
    raise RegistryError(
        "Код изменился во время чтения дважды; повторите запрос после "
        "завершения загрузки."
    )


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
    return sorted(
        совпадения,
        key=lambda item: (item.kind, item.source.casefold(), item.source),
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
) -> str:
    context, loaded = _selected_modules(registry, config, extension)
    состояние = _modules_availability_message(registry, context, loaded)
    if состояние is not None:
        return состояние
    if any(
        индекс is None
        for индекс in (loaded.оглавление, loaded.вызовы, loaded.формы)
    ):
        raise RegistryError("Готовый индекс кода неполон; перезагрузите источник.")
    partial_warning = _loaded_partial_warning(loaded)
    warning_prefix = f"> {partial_warning}\n\n" if partial_warning else ""

    module, separator, name = address.partition("::")
    module = module.strip()
    name = name.strip()
    if not separator or not module or not name or "::" in name:
        raise RegistryError(
            "address должен быть точным адресом процедуры `Модуль::Имя`; "
            "адрес одного модуля для get_callers недостаточен."
        )
    canonical_module = next(
        (
            item
            for item in loaded.оглавление.модули
            if item.casefold() == module.casefold()
        ),
        None,
    )
    if canonical_module is None:
        похожие = _similar_address(loaded, module, None)
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
            f"Модуль `{module}` поставлен скомпилированным — исходного "
            "текста и оглавления процедур нет. Поэтому подтвердить или "
            f"опровергнуть вызовы `{name}` по этому адресу невозможно.\n"
        )
    записи_модуля = loaded.оглавление.модуля(module)
    совпадения = [
        запись for запись in записи_модуля if запись.имя.casefold() == name.casefold()
    ]
    if not совпадения:
        похожие = _similar_address(loaded, module, name)
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
    )


def get_callers(
    registry: Registry,
    address: str,
    config: str | None = None,
    extension: str | None = None,
    limit: int = 20,
) -> str:
    """Подтверждённые вызовы, привязки метаданных и обработчики формы."""
    limit = _procedure_limit(limit)
    if not isinstance(address, str) or not address.strip():
        raise RegistryError("address не может быть пустым.")
    for _ in range(2):
        try:
            return _get_callers_once(registry, address, config, extension, limit)
        except _StaleModules:
            continue
    raise RegistryError(
        "Код изменился во время обратного поиска дважды; повторите запрос "
        "после завершения загрузки."
    )


# --------------------------------------------------------------- справка


def _syntax_context(registry: Registry, config: str | None):
    context = registry.resolve(config, require_configuration=False)
    if context.syntax is None:
        raise RegistryError(
            "Справка платформы не подключена. Загрузите `shcntx_ru.hbk` "
            "из каталога установки 1С."
        )
    return context


def search_syntax(
    registry: Registry,
    query: str,
    config: str | None = None,
    kind: str | None = None,
    limit: int = 10,
) -> str:
    """Найти метод, свойство или объект платформы.

    Результаты отфильтрованы по версии платформы конфигурации: того, чего в
    ней ещё нет, в выдаче не будет.
    """
    context = _syntax_context(registry, config)
    keep = context.syntax_filter()
    kinds = [kind] if kind else None

    limit = _clamp(limit)
    hits = context.syntax.index.search(
        query, limit=limit, kinds=kinds,
        predicate=lambda doc: keep(doc.payload),
    )
    # Окно нужно только для ограниченных подсказок о скрытом. Оно не может
    # ограничивать поиск доступных элементов: все верхние места бывают новее.
    raw = context.syntax.index.search(query, limit=limit * 4, kinds=kinds)
    filtered_out = [h for h in raw if not keep(h.doc.payload)]

    if not hits:
        where = (
            f" (платформа конфигурации {context.platform})"
            if context.configuration is not None
            else ""
        )
        # Пусто по двум разным причинам, и ответ у них разный. Если выдачу
        # обнулил фильтр версии, «ничего не найдено» — прямая ложь: агент
        # пойдёт искать опечатку в имени, которое существует. Поймано на живой
        # справке 2026-08-17 запросом «ПолучитьБуферДвоичныхДанных» под
        # конфигурацией 8.3.5.
        скрытое = _hidden_block(context, filtered_out)
        if скрытое:
            head = [
                f"# Справка платформы: «{query}»",
                f"В версии платформы {context.platform} доступного ничего нет, "
                "но подходящие элементы есть в других версиях.",
            ]
            return "\n".join(head + скрытое) + "\n" + _notes_block(context)
        return (
            f"По запросу «{query}» в справке платформы "
            f"ничего не найдено{where}."
            + _notes_block(context)
        )

    out = [f"# Справка платформы: «{query}»"]
    if context.configuration is not None:
        out.append(f"Для конфигурации {context.name}, платформа {context.platform}")
    else:
        out.append(
            f"Справка {context.syntax.source.platform}, "
            "конфигурация не выбрана — фильтрации по версии нет"
        )
    out.append("")
    for hit in hits:
        item: SyntaxItem = hit.doc.payload
        out.append(f"- `{item.address}` / `{item.full_en}`")
        facts = [KIND_TITLES.get(item.kind, item.kind)]
        if item.since:
            facts.append(f"с {item.since}")
        # Доступность берётся по версии конфигурации: мобильных контекстов в
        # старых платформах не существовало, а справка приписала их задним
        # числом тысячам элементов.
        resolution = (
            context.syntax.syntax.facts_for(item, context.platform)
            if context.platform
            else None
        )
        availability = (
            resolution.availability
            if resolution is not None and resolution.availability
            else item.availability
        )
        if availability:
            facts.append(", ".join(availability))
        if resolution is not None and not resolution.exact:
            facts.append(f"сведения по справке {resolution.platform}")
        out.append(f"  {' · '.join(facts)}")
    return "\n".join(out + _hidden_block(context, filtered_out)) + "\n" + _notes_block(context)


def _hidden_block(context, filtered_out: list) -> list[str]:
    """Что фильтр версии убрал из выдачи и почему.

    Причины две и они противоположные: одно ещё не появилось, другого уже нет.
    Блок собирается отдельно, потому что нужен и там, где выдача есть, и там,
    где фильтр не оставил ничего — во втором случае молчание превращается в
    «такого имени нет».
    """
    if not filtered_out:
        return []

    target = parse_version(context.platform)
    позже = [h for h in filtered_out if _appears_later(h.doc.payload, target)]
    удалены = [h for h in filtered_out if not _appears_later(h.doc.payload, target)]

    out: list[str] = []
    if позже:
        out.append("")
        out.append(
            f"> Ещё {len(позже)} подходящих элементов скрыто: они появились "
            f"в платформе позже {context.platform}."
        )
        с_заменой = []
        for hit in позже[:3]:
            item = hit.doc.payload
            # Сам рецепт в выдачу не идёт: десять фрагментов кода в списке из
            # десяти строк съели бы контекст агента, а без оговорки «чем замена
            # отличается» рецепт превращает невыполнимый код в неверный.
            замена = " · есть замена" if replacements.find(item.name_ru) else ""
            if замена:
                с_заменой.append(item.name_ru)
            out.append(f"> - `{item.address}` — с {item.since}{замена}")

        # Отметки мало: агент отвечает на вопрос буквально («есть ли метод?») и
        # за рецептом сам не идёт — проверено на живом агенте 2026-08-17.
        # Работает указание, а не намёк, как со строкой про `config` в
        # `list_configurations`.
        if с_заменой:
            имена = ", ".join(f"`{имя}`" for имя in с_заменой)
            out.append(
                f"> Для помеченных есть чем заменить: вызовите `get_syntax` "
                f"({имена}) и покажите рецепт вместе с ответом."
            )

    if удалены:
        out.append("")
        out.append(
            f"> Ещё {len(удалены)} подходящих элементов скрыто: в платформе "
            f"{context.platform} их уже нет."
        )
        for hit in удалены[:3]:
            item = hit.doc.payload
            out.append(f"> - `{item.address}` — по версию {item.until} включительно")

    return out


def _appears_later(item: SyntaxItem, target: tuple[int, ...]) -> bool:
    """Элемент недоступен потому, что ещё не появился, а не потому, что удалён.

    Проверять `until` на непустоту нельзя: элемент, живший только в средней
    справке, имеет обе границы сразу (`since=8.3.18`, `until=8.3.19`), и для
    конфигурации 8.3.5 верная причина — «ещё не появился».
    """
    return bool(item.since) and release(item.since_tuple) > release(target)


def _replacement_block(item: SyntaxItem, target: tuple[int, ...], platform: str) -> str:
    """Рецепт замены, если он написан. Пусто — значит сказать нечего.

    Только для элементов, которые ещё не появились. Удалённому рецепт «для
    старых платформ» не подходит: там нужен путь вперёд, а не назад, и
    подставить один вместо другого — соврать ровно в ту сторону, из-за которой
    затевалось слияние справок.
    """
    if not _appears_later(item, target):
        return ""
    рецепт = replacements.find(item.name_ru)
    if рецепт is None:
        return ""

    out = ["", f"**Чем заменить в {platform}:** {рецепт.instead}"]
    if рецепт.code:
        out += ["", "```bsl", рецепт.code, "```"]
    out += ["", f"**Отличие:** {рецепт.note}"]
    return "\n".join(out)


def _unavailable_reason(item: SyntaxItem, target: tuple[int, ...]) -> str:
    if _appears_later(item, target):
        return f"появился в **{item.since}**"
    if item.until:
        return f"описан по версию **{item.until}** включительно, дальше его нет"
    return "недоступен в этой версии"


def _отсечённые_однофамильцы(context, отсечённые: list[SyntaxItem], *,
                             подробно: bool) -> str:
    """Одноимённые, которых фильтр версии убрал, — названные вслух.

    Фильтр молчит, и это правильно, пока он убирает всё: тогда срабатывает
    `_unavailable_here` с причиной и заменой. Но если рядом остался доступный
    одноимённый член другого объекта, отсечённое исчезало бесследно.

    `подробно` — отдана карточка одного элемента, места хватает на причину и
    рецепт. Иначе печатается список одноимённых, и туда идёт короткая сводка:
    у имени вроде «количество» отсечённых бывает полсотни.
    """
    if not отсечённые or not context.platform:
        return ""

    target = parse_version(context.platform)
    показать = отсечённые[:3] if подробно else отсечённые[:5]
    сколько = len(отсечённые)
    # Согласование: «Ещё 1 одноимённых недоступны» читается как опечатка, а
    # текст этот агент видит чаще всего именно в единственном числе.
    заголовок = (
        f"> **Ещё один одноимённый недоступен в {context.platform}:**"
        if сколько == 1
        else f"> **Ещё {сколько} одноимённых недоступны в {context.platform}:**"
    )
    out = ["", заголовок]
    for item in показать:
        out.append(f"> - `{item.address}` — {_unavailable_reason(item, target)}")
    if len(отсечённые) > len(показать):
        out.append(f"> - …и ещё {len(отсечённые) - len(показать)}")

    if подробно:
        for item in показать:
            блок = _replacement_block(item, target, context.platform)
            if блок:
                out.append(f"\n## Замена для `{item.address}`{блок}")
    else:
        out.append("> ")
        out.append("> Спросите по полному адресу, чтобы увидеть причину и замену.")
    return "\n".join(out) + "\n"


def _unavailable_here(context, matching: list[SyntaxItem]) -> str:
    """Элемент есть в платформе, но не в версии этой конфигурации.

    Причины две и они противоположные: либо он появился позже, либо был
    удалён раньше. Сказать «появился в версии X» про удалённый — соврать в
    ту же сторону, из-за которой затевалось слияние справок. У однофамильцев
    причины могут различаться, поэтому перечисляются все: умолчать про второй
    элемент — значит скрыть, что после обновления платформы он появится.
    """
    target = parse_version(context.platform)

    if len(matching) > 1:
        out = [
            f"# Ни один из одноимённых элементов не доступен в {context.platform}",
            "",
        ]
        рецепты = []
        for item in sorted(matching, key=lambda i: i.full_ru):
            out.append(f"- `{item.address}` — {_unavailable_reason(item, target)}")
            блок = _replacement_block(item, target, context.platform)
            if блок:
                рецепты.append(f"\n## Замена для `{item.address}`{блок}")
        out.append("")
        out.append(
            f"Конфигурация {context.name} работает на **{context.platform}**; "
            "использовать их нельзя — код не скомпилируется."
        )
        return "\n".join(out + рецепты) + "\n" + _notes_block(context)

    item = matching[0]
    if _appears_later(item, target):
        рецепт = _replacement_block(item, target, context.platform)
        # Без рецепта заканчиваем прежней фразой: сказать «нужен другой способ»
        # честнее, чем промолчать, но хуже, чем назвать способ.
        хвост = (
            f"\n{рецепт}" if рецепт else f" Нужен способ, доступный в {context.platform}."
        )
        return (
            f"# `{item.address}` недоступен в этой конфигурации\n\n"
            f"Элемент существует в платформе, но появился в версии "
            f"**{item.since}**, а конфигурация {context.name} работает на "
            f"**{context.platform}**.\n\n"
            f"Использовать его нельзя — код не скомпилируется."
            f"{хвост}\n"
            + _notes_block(context)
        )

    return (
        f"# `{item.address}` в этой конфигурации не существует\n\n"
        f"Элемент описан в справке по версию **{item.until}** включительно "
        f"и в более поздних справках отсутствует, а конфигурация "
        f"{context.name} работает на **{context.platform}**.\n\n"
        f"Использовать его нельзя — код не скомпилируется.\n"
        + _notes_block(context)
    )


def get_syntax(
    registry: Registry,
    name: str,
    config: str | None = None,
    detail: str = FIELDS,
) -> str:
    """Полное описание элемента платформы: сигнатура, параметры, доступность."""
    context = _syntax_context(registry, config)
    keep = context.syntax_filter()
    wanted = name.strip().lower()

    matching = context.syntax.find_exact(wanted)
    exact = [item for item in matching if keep(item)]
    отсечённые = [item for item in matching if not keep(item)]

    # Элемент найден, но в платформе конфигурации его нет. Молчать об этом
    # нельзя: агент решит, что перепутал имя, и будет искать несуществующее.
    # Правильный вывод — что метод есть, но для этой версии нужен другой путь.
    if matching and not exact:
        return _unavailable_here(context, matching)

    if not exact:
        raw = context.syntax.index.search(name, limit=20)
        hits = [h for h in raw if keep(h.doc.payload)]
        # Похожее, отсеянное по версии, — тоже подсказка, и на старой платформе
        # самая нужная: новые API агент помнит лучше, чем 8.3.5, и ошибается в
        # именах чаще именно там. Выбросить их значит ответить «такого нет»
        # там, где верно «есть, но не в вашей версии».
        скрытое = _hidden_block(context, [h for h in raw if not keep(h.doc.payload)])
        if not hits:
            if скрытое:
                head = [
                    f"Точного совпадения нет, и всё похожее недоступно "
                    f"в {context.platform}."
                ]
                return "\n".join(head + скрытое) + "\n" + _notes_block(context)
            # Называть надо ту справку, по которой ответ строился, а не
            # объединённый источник: тот всегда назовётся самой свежей из
            # загруженных, и отказ выходит подписан чужой версией.
            справка = context.syntax_platform
            если_совпало = справка == context.platform
            return (
                f"В справке платформы {справка} нет элемента `{name}`"
                + ("." if если_совпало else f", доступного в версии {context.platform}.")
                + _notes_block(context)
            )
        suggestion = "\n".join(f"- `{h.doc.payload.full_ru}`" for h in hits[:5])
        return (
            "\n".join([f"Точного совпадения нет. Возможно:\n{suggestion}"] + скрытое)
            + "\n"
            + _notes_block(context)
        )

    if len(exact) > 1:
        out = [f"# Одноимённых элементов: {len(exact)}", ""]
        for item in exact[:15]:
            out.append(
                f"- `{item.address}` — {KIND_TITLES.get(item.kind, item.kind)}"
                + (f", с {item.since}" if item.since else "")
            )
        out.append("")
        out.append("Повторите вызов с адресом из списка.")
        return (
            "\n".join(out)
            + "\n"
            + _отсечённые_однофамильцы(context, отсечённые, подробно=False)
            + _notes_block(context)
        )

    # Карточка собирается под версию конфигурации: сигнатура, доступность и
    # имя между версиями менялись, и разница в один параметр — ошибка
    # компиляции, а не мелочь.
    resolution = (
        context.syntax.syntax.facts_for(exact[0], context.platform)
        if context.platform
        else None
    )
    return (
        render_syntax_item(exact[0], detail, resolution)
        + _отсечённые_однофамильцы(context, отсечённые, подробно=True)
        + _notes_block(context)
    )
