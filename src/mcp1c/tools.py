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

import difflib as difflib
import threading
import weakref
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path as Path
from typing import cast

from . import (
    code_coverage,
    coverage_log,
    source_ids,
)
from .bsl_lex import разобрать
# Реэкспорты сохраняют прежние имена DTO и вспомогательных функций.
from .code_coverage import (
    _CodeCoveragePayload,
    _all_code_problems,
    _iter_code_problems as _iter_code_problems,
    CodeCoverage,
    CodeProblemRow as CodeProblemRow,
)
from .intake_v2 import ExportIdentity
from .module_content import read_bsl
from .source_modes import ActivationStatus
from .registry import (
    KIND_EXTENSION,
    KIND_MODULES,
    LoadedModules,
    Registry,
    RegistrySnapshot,
    RegistryError,
    SourcesChanged,
)
from .render import (
    FIELDS,
    ProcedureMatch,
)
from .syntax_model import SyntaxItem as SyntaxItem, parse_version, release
from .xdto import read_package_member

from . import tools_code, tools_syntax
from .tools_context import (
    CodeToolsContext,
    _CodeCapture,
    _CodeSummary,
    _ObjectCodeIndex,
    _CodeView,
    _ConfigurationCodeSnapshot,
    _CodeAggregates,
    _РазборМодуля,
)
from .tools_code import (
    MAX_MODULE_SOURCE_FILE_BYTES as MAX_MODULE_SOURCE_FILE_BYTES,
    ModuleSourceFile,
    _belongs_to_object as _belongs_to_object,
    _build_owner_boundaries as _build_owner_boundaries,
    _caller_site as _caller_site,
    _close_matches as _close_matches,
    _module_owner_lookup as _module_owner_lookup,
)
from .tools_syntax import (
    _поисковый_элемент_справки as _поисковый_элемент_справки,
)


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










# --------------------------------------------------------------- обзор












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
    kind: str = KIND_MODULES
    native_generation: bool = False
    last_successful_load_at: str | None = None


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
    last_successful_load_at: str | None = None


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
    native_generation: bool = False
    compatibility_mode: str = ""
    predefined_available: bool = True
    activation_mode: str = "unknown"
    activation_status: str = "unknown"
    platform_declaration: dict[str, str] | None = None
    source_origin: str = "unknown"
    last_successful_load_at: str | None = None


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
        )
    return _CodeCapture(source=code.source, loaded=code.loaded)


def _capture_is_current(
    snapshot: RegistrySnapshot, source_id: str, capture: _CodeCapture
) -> bool:
    current = _capture_code(snapshot, source_id)
    return current.source == capture.source and current.loaded is capture.loaded




# Пакет кода после публикации не меняется, а `get_object`, `list_configurations`,
# `get_related` и все инструменты по коду раньше пересчитывали его агрегаты на
# каждый вызов: на конфигурации из 2 867 модулей это 9 мс из 13 мс `get_object`.
# Ключ — поисковый индекс пакета, потому что `LoadedModules` и его структуры
# объявлены со `__slots__` без слабых ссылок, а индекс живёт ровно столько же,
# сколько пакет. Слабый ключ отпускает агрегаты вместе со старым поколением.
_CODE_AGGREGATES: "weakref.WeakKeyDictionary[object, _CodeAggregates]" = (
    weakref.WeakKeyDictionary()
)
_CODE_AGGREGATES_LOCK = threading.Lock()


def _code_aggregates(loaded: LoadedModules) -> _CodeAggregates:
    ключ = loaded.поиск
    with _CODE_AGGREGATES_LOCK:
        запись = _CODE_AGGREGATES.get(ключ)
        if (
            запись is None
            or запись.оглавление is not loaded.оглавление
            or запись.формы is not loaded.формы
            or запись.каталог is not loaded.каталог
            or запись.корень != loaded.корень
        ):
            if запись is not None:
                _РАЗБОРЫ.забыть(запись.пакет)
            запись = _CodeAggregates(
                loaded.оглавление, loaded.формы, loaded.каталог,
                корень=loaded.корень,
            )
            _CODE_AGGREGATES[ключ] = запись
            # Финализатор не берёт замок: сборка мусора может сработать прямо
            # внутри `_РАЗБОРЫ` под его же замком. Он только помечает пакет,
            # а разборы снимаются при следующем обращении к кэшу.
            weakref.finalize(ключ, _РАЗБОРЫ.забыть, запись.пакет)
        return запись


def _code_coverage(
    loaded: LoadedModules, *, include_problem_rows: bool = True
) -> CodeCoverage:
    return code_coverage.get_code_coverage(
        loaded, include_problem_rows=include_problem_rows,
        all_code_problems=_all_code_problems,
    )


def _compute_code_coverage(
    loaded: LoadedModules, *, include_problem_rows: bool
) -> CodeCoverage:
    return code_coverage._compute_code_coverage(
        loaded, include_problem_rows=include_problem_rows,
        all_code_problems=_all_code_problems,
    )


def _summarize_code(loaded: LoadedModules) -> _CodeSummary:
    """Агрегаты готового пакета без чтения модулей и списка ``Запись``."""
    агрегаты = _code_aggregates(loaded)
    if агрегаты.summary is not None:
        return агрегаты.summary
    результат = _compute_code_summary(loaded)
    агрегаты.summary = результат
    return результат


def _compute_code_summary(loaded: LoadedModules) -> _CodeSummary:
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

    Агрегаты считаются вне замка. Перед публикацией проверяются снимки
    источников и личности пакетов; при reparse/remove весь снимок
    собирается заново, а не смешивает поколения.
    """
    prefix = source_ids.extension_prefix(name)
    base_id = source_ids.modules_id(name)
    for _ in range(2):
        snapshot = registry.snapshot()
        context = registry.resolve(name, diagnostic=True)
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
            if base.loaded is not None
            else None
        )
        extension_views = tuple(
            (
                source_id[len(prefix):],
                _CodeView(
                    capture,
                    _summarize_code(capture.loaded)
                    if capture.loaded is not None
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
    raise SourcesChanged(
        "Источники кода изменились дважды; повторите запрос после завершения "
        "загрузки."
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
        last_successful_load_at=source.last_successful_load_at,
        code_version=source.code_version,
        incomplete=source.incomplete,
    )


def _capture_configurations_list(
    registry: Registry,
    *,
    names: tuple[str, ...] | None = None,
) -> _ListConfigurationsCapture | None:
    snapshot = registry.snapshot()
    configurations = tuple(
        (name, loaded)
        for name, loaded in snapshot.configurations.items()
        if names is None or name in names
    )
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


def _loaded_partial_warning(
    loaded: LoadedModules, *, corpus: str = ""
) -> str | None:
    projection = cast(
        _CodeCoveragePayload,
        _code_coverage(loaded, include_problem_rows=False).to_dict(),
    )
    structures = projection["form_structures"]
    modules = projection["form_modules"]
    limitations = projection["limitations"]
    suffix = f" {corpus}" if corpus else ""
    evidence_note = ""
    if limitations["compiled_without_source"]:
        evidence_note = (
            f"В корпусе кода{suffix} исходный текст недоступен у "
            f"{limitations['compiled_without_source']} модулей. Сведения о "
            "процедурах, вызовах и перекрытиях приведены только по доступным "
            "исходникам; пустой результат не доказывает отсутствие кода в "
            "таких модулях."
        )
    if not projection["has_limitations"]:
        return evidence_note or None
    categories = ", ".join(
        f"{category}={count}"
        for category, count in limitations["categories"].items()
    ) or "нет"
    warning = (
        f"Покрытие кода{suffix} неполно: структуры форм: полностью "
        f"{structures['full']}, семантика отложена "
        f"{structures['partial']}, не прочитано "
        f"{structures['unavailable']}; модули форм: прочитано "
        f"{modules['read']}, пусто {modules['empty']}, "
        f"отсутствует {modules['missing']}, не прочитано "
        f"{modules['unreadable']}; неизвестных маркеров: "
        f"{limitations['unknown_markers']}, известных маркеров с неполной "
        f"семантикой: {limitations['known_markers_incomplete']}, неподдержанных "
        f"адресов: {limitations['unsupported_addresses']}, битых контейнеров: "
        f"{limitations['broken_containers']}, непрочитанных тел: "
        f"{limitations['unreadable_bodies']}, превышений бюджета: "
        f"{limitations['budget_exceeded']}, конфликтов тел: "
        f"{limitations['body_conflicts']}; категории проблем: "
        f"{categories}. Нулевой счётчик или пустой список не доказывает "
        "отсутствие скрытых данных."
    )
    if evidence_note:
        warning += " " + evidence_note
    return warning


def _code_state_text(view: _CodeView) -> str:
    summary = view.summary
    if summary is None:
        return "не загружен"
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
        "с недоступным исходным текстом "
        f"{len(summary.compiled_modules)}"
    )


def code_coverage_lines(coverage: CodeCoverage | None) -> tuple[str, ...]:
    """Одинаковая диагностика для MCP, CLI и страницы ``/sources``."""
    if coverage is None:
        return ()
    projection = cast(_CodeCoveragePayload, coverage.to_dict())
    structures = projection["form_structures"]
    modules = projection["form_modules"]
    limitations = projection["limitations"]
    lines: list[str] = []
    if projection["has_limitations"]:
        lines.append(
            "ВНИМАНИЕ: покрытие кода неполно; нулевой счётчик или пустой "
            "список не доказывает отсутствие скрытых данных."
        )
    lines.extend(
        [
            f"Форм обнаружено: {structures['total']}",
            "Структуры форм: "
            f"полностью {structures['full']}, "
            f"семантика отложена {structures['partial']}, "
            f"не прочитано {structures['unavailable']}",
            "Модули форм: "
            f"прочитано {modules['read']}, "
            f"пусто {modules['empty']}, "
            f"отсутствует {modules['missing']}, "
            f"не прочитано {modules['unreadable']}",
            "Причины: "
            f"неизвестных маркеров {limitations['unknown_markers']}, "
            "известных маркеров с неполной семантикой "
            f"{limitations['known_markers_incomplete']}, "
            f"неподдержанных адресов {limitations['unsupported_addresses']}, "
            f"битых контейнеров {limitations['broken_containers']}, "
            f"непрочитанных тел {limitations['unreadable_bodies']}, "
            f"превышений бюджета {limitations['budget_exceeded']}, "
            f"конфликтов тел {limitations['body_conflicts']}, "
            "модулей с недоступным исходным текстом "
            f"{limitations['compiled_without_source']}",
        ]
    )
    if limitations["categories"]:
        lines.append(
            "Категории проблем: "
            + ", ".join(
                f"{category}={count}"
                for category, count in limitations["categories"].items()
            )
        )
    for problem in projection["problems"]:
        target = (
            f"`{problem['address']}`"
            if problem["address"] is not None
            else f"неадресуемый кандидат #{problem['ordinal']}"
        )
        lines.append(
            f"Проблема [{problem['category']}] {target} — {problem['reason']}"
        )
    if projection["problems_omitted"]:
        lines.append(f"Ещё проблем: {projection['problems_omitted']}")
    return tuple(lines)


def list_configurations(registry: Registry) -> str:
    """Какие конфигурации загружены и что по ним доступно."""
    for _ in range(2):
        state = registry.snapshot()
        names = state.configuration_names
        blocked = frozenset(
            name
            for name in names
            if (
                (activation := registry.active_activation(
                    ExportIdentity.configuration(name)
                )) is not None
                and activation.status is ActivationStatus.RELOAD_REQUIRED
            )
        )
        available = tuple(name for name in names if name not in blocked)
        if blocked and not available:
            if registry.snapshot_is_current(state):
                return _blocked_configurations_text(names, blocked)
            continue
        capture = _capture_configurations_list(registry, names=available)
        if capture is None:
            continue
        answer = _render_configurations_list(capture)
        if registry.snapshot_is_current(state) and _list_capture_is_current(registry, capture):
            if blocked:
                answer = _blocked_configurations_text(names, blocked) + "\n\n" + answer
            return answer
    raise SourcesChanged(
        "Источники списка конфигураций изменились дважды; повторите запрос "
        "после завершения загрузки."
    )


def _blocked_configurations_text(
    names: tuple[str, ...], blocked: frozenset[str]
) -> str:
    """Краткая диагностика без структуры заблокированных поколений."""
    return (
        "# Состояние конфигураций\n\n"
        + "\n".join(f"- {name}: RELOAD_REQUIRED" for name in names if name in blocked)
        + "\n\nЗагрузите полный Source A или Source B через intake. "
        "Конфигурационные данные старого поколения недоступны."
    )


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
        if view.summary is None:
            return "missing"
        return "limited" if view.summary.coverage.has_limitations else "ready"

    rows: list[CodeStateRow] = []
    for snapshot in capture.rows:
        native_base = capture.registry.generation(
            ExportIdentity.configuration(snapshot.context.name)
        )
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
                KIND_MODULES,
                native_base is not None,
                (
                    native_base.pointer.last_successful_load_at
                    if native_base is not None
                    and snapshot.modules.capture.source is not None
                    else (
                        snapshot.modules.capture.source.last_successful_load_at
                        if snapshot.modules.capture.source is not None
                        else None
                    )
                ),
            )
        )
        for extension_name, view in snapshot.extensions:
            native_extension = capture.registry.generation(
                ExportIdentity.extension(
                    extension_name,
                    parent_configuration=snapshot.context.name,
                )
            )
            source_id, journal_path = journal(view)
            rows.append(CodeStateRow(
                snapshot.context.name,
                f"Расширение {extension_name}",
                _code_state_text(view),
                view.summary.coverage if view.summary is not None else None,
                source_id,
                journal_path,
                phase(view),
                KIND_EXTENSION,
                native_extension is not None,
                (
                    native_extension.pointer.last_successful_load_at
                    if native_extension is not None
                    else (
                        view.capture.source.last_successful_load_at
                        if view.capture.source is not None
                        else None
                    )
                ),
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
        activation = (
            registry.active_activation(ExportIdentity.configuration(config.name))
            if registry is not None
            else None
        )
        declaration = (
            registry.platform_declaration(config.name)
            if registry is not None
            else None
        )
        rows.append(
            ConfigurationStateRow(
                name=config.name,
                source_origin=(
                    "source_a" if config.source_format in ("xml", "json")
                    else "source_b" if config.source_format == "source-b"
                    else "unknown"
                ),
                version=config.version,
                platform=declaration.version if declaration else config.platform,
                compatibility_mode=config.compatibility_mode,
                predefined_available=config.predefined_available,
                activation_mode=(activation.mode.value if activation and activation.mode else "unknown"),
                activation_status=(activation.status.value if activation else "unknown"),
                platform_declaration=(
                    declaration.to_dict()
                    if declaration
                    else None
                ),
                objects=len(config),
                edges=len(configuration.graph.edges),
                loaded_at=configuration.source.loaded_at,
                last_successful_load_at=(
                    generation.pointer.last_successful_load_at
                    if (generation := capture.registry.generation(
                        ExportIdentity.configuration(config.name)
                    )) is not None
                    else configuration.source.last_successful_load_at
                ),
                syntax_present=context.syntax is not None,
                syntax_platform=context.syntax_platform,
                syntax_relation=context.syntax_relation,
                syntax_hidden=context.syntax_hidden,
                notes=tuple(context.notes()),
                code=(
                    ()
                    if activation is not None
                    and activation.status is ActivationStatus.RELOAD_REQUIRED
                    else tuple(
                        state for state in code if state.configuration == config.name
                    )
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
    raise SourcesChanged(
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
    raise SourcesChanged(
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
            f"- Версия: {config.version} · платформа **{context.platform or 'неизвестна'}**\n"
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
        platform = row.context.platform
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


# --------------------------------------------------------------- кэш разборов и фасады


# Бюджет общего кэша разборов. Компактные разборы всех модулей крупнейшей из
# измеренных типовых конфигураций (Зарплата и управление персоналом с тремя
# расширениями: 8 543 модуля, 172 тыс. процедур) занимают 49,8 МиБ, две трети
# из них — сигнатуры. Без вытеснения это не десятки МиБ: предел — тот же
# максимум с запасом 2,5. Несколько конфигураций на сервере делят бюджет,
# дольше всех не тронутые модули вытесняются первыми.
PARSE_CACHE_MAX_BYTES = 125 * 1024 * 1024


class _КэшРазборов:
    """Общий LRU компактных разборов всех пакетов кода с бюджетом в байтах.

    Ключ — (пакет из агрегатов, адрес модуля). Новое поколение получает
    новые агрегаты и новый пакет, поэтому устаревший разбор не отдаётся;
    разборы снятого пакета снимаются при ближайшем обращении к кэшу.
    """

    def __init__(self, предел: int) -> None:
        self.предел = предел
        self.объём = 0
        self._замок = threading.Lock()
        self._записи: OrderedDict[tuple[object, str], _РазборМодуля] = OrderedDict()
        # Пополняется финализатором без замка: `list.append` атомарен.
        self._забытые: list[object] = []

    def забыть(self, пакет: object) -> None:
        self._забытые.append(пакет)
        # Снятый пакет освобождает память сразу, если замок свободен; захват
        # без ожидания безопасен и при сборке мусора в потоке, уже держащем
        # замок: тогда уборка просто останется до следующего обращения.
        if self._замок.acquire(blocking=False):
            try:
                self._убрать_забытые()
            finally:
                self._замок.release()

    def _убрать_забытые(self) -> None:
        if not self._забытые:
            return
        забытые = set()
        while self._забытые:
            забытые.add(self._забытые.pop())
        for ключ in [ключ for ключ in self._записи if ключ[0] in забытые]:
            self.объём -= self._записи.pop(ключ).размер

    def взять(self, пакет: object, модуль: str) -> _РазборМодуля | None:
        with self._замок:
            self._убрать_забытые()
            разбор = self._записи.get((пакет, модуль))
            if разбор is not None:
                self._записи.move_to_end((пакет, модуль))
            return разбор

    def положить(
        self, пакет: object, модуль: str, разбор: _РазборМодуля
    ) -> _РазборМодуля:
        if разбор.размер > self.предел:
            return разбор
        with self._замок:
            self._убрать_забытые()
            ключ = (пакет, модуль)
            готовый = self._записи.get(ключ)
            if готовый is not None:
                self._записи.move_to_end(ключ)
                return готовый
            self._записи[ключ] = разбор
            self.объём += разбор.размер
            while self.объём > self.предел:
                _, вытесненный = self._записи.popitem(last=False)
                self.объём -= вытесненный.размер
            return разбор


_РАЗБОРЫ = _КэшРазборов(PARSE_CACHE_MAX_BYTES)


def _code_tools_context() -> CodeToolsContext:
    """Снять текущие зависимости фасада без сохранения реестра или поколения."""
    return CodeToolsContext(
        _code_aggregates=_code_aggregates,
        _code_state_text=_code_state_text,
        _configuration_code_snapshot=_configuration_code_snapshot,
        _is_form_module=_is_form_module,
        _loaded_partial_warning=_loaded_partial_warning,
        _modules_package_is_current=_modules_package_is_current,
        _read_module_snapshot=_read_module_snapshot,
        _scope_modules=_scope_modules,
        _selected_modules=_selected_modules,
        _similar_address=_similar_address,
        _РАЗБОРЫ=_РАЗБОРЫ,
        _сигнатура=_сигнатура,
        read_bsl=read_bsl,
        read_package_member=read_package_member,
        разобрать=разобрать,
    )


def search_objects(
    registry: Registry,
    query: str,
    config: str | None = None,
    kind: str | None = None,
    limit: int = 10,
) -> str:
    """Найти объект конфигурации по описанию или части имени."""
    return tools_code.search_objects(registry, query, config, kind, limit)


def get_object(
    registry: Registry,
    full_name: str,
    config: str | None = None,
    detail: str = FIELDS,
    *,
    cursor: str | None = None,
) -> str:
    """Структура объекта: реквизиты, табличные части, движения, связи."""
    return tools_code.get_object(
        registry,
        full_name,
        config,
        detail,
        cursor=cursor,
        _ctx=_code_tools_context(),
    )


def _object_code_details(
    view: _CodeView, full_name: str
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    return tools_code._object_code_details(view, full_name, _ctx=_code_tools_context())


def _build_object_code_index(summary: _CodeSummary) -> _ObjectCodeIndex:
    return tools_code._build_object_code_index(summary, _ctx=_code_tools_context())


def _is_form_module(address: str) -> bool:
    """Форма по канонической грамматике адреса, без догадки по подстроке."""
    return tools_code._is_form_module(address)


def _object_code_block(view: _CodeView, full_name: str, detail: str) -> str:
    return tools_code._object_code_block(view, full_name, detail, _ctx=_code_tools_context())


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
    return tools_code.get_related(registry, full_name, config, limit, _ctx=_code_tools_context())


def _extension_overrides(
    snapshot: _ConfigurationCodeSnapshot,
) -> tuple[dict[str, tuple[str, ...]], list[tuple[str, str]]]:
    return tools_code._extension_overrides(snapshot, _ctx=_code_tools_context())


def compare_configurations(
    registry: Registry,
    full_name: str,
    configs: list[str] | None = None,
    *,
    cursor: str | None = None,
    limit: int = 40,
) -> str:
    """Имена реквизитов одного объекта в явной паре; различия без потерь."""
    return tools_code.compare_configurations(
        registry,
        full_name,
        configs,
        cursor=cursor,
        limit=limit,
    )


def _module_source_file_once(
    registry: Registry,
    address: str,
    config: str | None,
    extension: str | None,
) -> ModuleSourceFile:
    return tools_code._module_source_file_once(
        registry,
        address,
        config,
        extension,
        _ctx=_code_tools_context(),
    )


def get_module_source_file(
    registry: Registry,
    address: str,
    config: str | None = None,
    extension: str | None = None,
) -> ModuleSourceFile:
    """Прочитать точный модуль одного актуального поколения для скачивания."""
    return tools_code.get_module_source_file(
        registry,
        address,
        config,
        extension,
        _ctx=_code_tools_context(),
    )


def _selected_modules(
    registry: Registry,
    config: str | None,
    extension: str | None,
) -> tuple[object, LoadedModules | None]:
    return tools_code._selected_modules(registry, config, extension)


def _scope_modules(loaded: LoadedModules, scope: str | None) -> frozenset[str]:
    return tools_code._scope_modules(loaded, scope)


def _прочитать_тело_модуля(loaded: LoadedModules, модуль: str) -> str:
    """Прочитать модуль через каталог локаторов опубликованного поколения."""
    return tools_code._прочитать_тело_модуля(loaded, модуль, _ctx=_code_tools_context())


def _сигнатура(
    loaded: LoadedModules,
    запись,
    снимки: "dict[str, _РазборМодуля] | None" = None,
) -> str:
    """Декларация из компактного разбора модуля.

    В `LoadedModules` сигнатуры и тела не появляются: их держит отдельный
    кэш разборов пакета (`_разбор_модуля`), текст модуля читается только при
    промахе. Снимки ответа позволяют повторно использовать уже найденный
    разбор для каждой строки оглавления.
    """
    return tools_code._сигнатура(loaded, запись, снимки, _ctx=_code_tools_context())


def _procedure_matches(
    loaded: LoadedModules,
    записи: list,
    снимки: "dict[str, _РазборМодуля] | None" = None,
) -> list[ProcedureMatch]:
    return tools_code._procedure_matches(loaded, записи, снимки, _ctx=_code_tools_context())


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
    return tools_code._standard_procedure_response(
        registry,
        loaded,
        configuration=configuration,
        query=query,
        procedure=procedure,
        scope=scope,
        scope_modules=scope_modules,
        extension=extension,
        _ctx=_code_tools_context(),
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
    return tools_code._search_procedures_once(
        registry,
        query,
        config,
        extension,
        scope,
        limit,
        _ctx=_code_tools_context(),
    )


def search_procedures(
    registry: Registry,
    query: str,
    config: str | None = None,
    extension: str | None = None,
    scope: str | None = None,
    limit: int = 10,
) -> str:
    """Поиск с одним полным повтором при смене поколения кода."""
    return tools_code.search_procedures(
        registry,
        query,
        config,
        extension,
        scope,
        limit,
        _ctx=_code_tools_context(),
    )


def _modules_package_is_current(
    registry: Registry, loaded_modules: list[LoadedModules]
) -> bool:
    """Один CAS для всех корпусов, чьи части попадут в один ответ."""
    return tools_code._modules_package_is_current(registry, loaded_modules)


def _read_module_snapshot(
    registry: Registry,
    loaded: LoadedModules,
    module: str,
) -> str:
    """Текст одного поколения, без раскрытия локального пути при отказе."""
    return tools_code._read_module_snapshot(registry, loaded, module, _ctx=_code_tools_context())


def _parsed_procedures(записи: list, текст: str) -> _РазборМодуля:
    """Сопоставляет оглавление с разбором и сжимает его до нужных полей."""
    return tools_code._parsed_procedures(записи, текст, _ctx=_code_tools_context())


def _разбор_модуля(loaded: LoadedModules, модуль: str, прочитать) -> _РазборМодуля:
    """Разбор модуля один раз на пакет кода; `прочитать` зовётся при промахе.

    Разбор — 75–95% времени `get_procedure` и `search_procedures`, а пакет
    после публикации не меняется. Ключ кэша — пакет из агрегатов: новое
    поколение приходит с новым ключом, старое уходит вместе со своим
    `LoadedModules`. Гонка двух потоков на промахе лишь разберёт модуль
    дважды; в кэше останется первая запись, обе равны.
    """
    return tools_code._разбор_модуля(loaded, модуль, прочитать, _ctx=_code_tools_context())


def _similar_address(loaded: LoadedModules, module: str, name: str | None) -> list[str]:
    return tools_code._similar_address(loaded, module, name)


def _foreign_extension_warnings(
    registry: Registry,
    configuration: str,
    module: str,
    target_name: str,
    selected: LoadedModules,
    observed: list[LoadedModules],
) -> list[str]:
    return tools_code._foreign_extension_warnings(
        registry,
        configuration,
        module,
        target_name,
        selected,
        observed,
        _ctx=_code_tools_context(),
    )


def _get_procedure_once(
    registry: Registry,
    address: str,
    config: str | None,
    extension: str | None,
    start_line: int,
    lines: int,
) -> str:
    return tools_code._get_procedure_once(
        registry,
        address,
        config,
        extension,
        start_line,
        lines,
        _ctx=_code_tools_context(),
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
    return tools_code.get_procedure(
        registry,
        address,
        config,
        extension,
        start_line,
        lines,
        _ctx=_code_tools_context(),
    )


def _get_callers_once(
    registry: Registry,
    address: str,
    config: str | None,
    extension: str | None,
    limit: int,
) -> str:
    return tools_code._get_callers_once(
        registry,
        address,
        config,
        extension,
        limit,
        _ctx=_code_tools_context(),
    )


def get_callers(
    registry: Registry,
    address: str,
    config: str | None = None,
    extension: str | None = None,
    limit: int = 20,
) -> str:
    """Подтверждённые вызовы, привязки метаданных и обработчики формы."""
    return tools_code.get_callers(
        registry,
        address,
        config,
        extension,
        limit,
        _ctx=_code_tools_context(),
    )


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
    return tools_syntax.search_syntax(registry, query, config, kind, limit)


def get_syntax(
    registry: Registry,
    name: str,
    config: str | None = None,
    detail: str = FIELDS,
    links_offset: int = 0,
) -> str:
    """Полное описание элемента платформы: сигнатура, параметры, доступность."""
    return tools_syntax.get_syntax(registry, name, config, detail, links_offset)


def syntax_navigation_data(
    registry: Registry, name: str, config: str | None = None, links_offset: int = 0
) -> list[dict]:
    """Структурированные проверенные переходы для карточки дашборда."""
    return tools_syntax.syntax_navigation_data(registry, name, config, links_offset)
