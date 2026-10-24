"""Общие агрегаты и безопасные проекции покрытия готового корпуса кода."""

from __future__ import annotations

import heapq
import threading
import weakref
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, TypedDict

if TYPE_CHECKING:
    from .registry_types import LoadedModules


class _CodeProblemPayload(TypedDict):
    category: str
    address: str | None
    ordinal: int
    reason: str
    marker: int | None


class _CodeCoverageLimitations(TypedDict):
    categories: dict[str, int]
    occurrences_total: int
    unknown_markers: int
    known_markers_incomplete: int
    unsupported_addresses: int
    broken_containers: int
    unreadable_bodies: int
    budget_exceeded: int
    body_conflicts: int
    compiled_without_source: int


class _CodeCoveragePayload(TypedDict):
    modules: dict[str, int]
    procedures: dict[str, int]
    form_structures: dict[str, int]
    form_modules: dict[str, int]
    limitations: _CodeCoverageLimitations
    has_limitations: bool
    problems: list[_CodeProblemPayload]
    problems_omitted: int


@dataclass(frozen=True, slots=True)
class CodeProblemRow:
    """Одна обезличенная проблема покрытия готового корпуса кода."""

    category: str
    address: str | None
    ordinal: int
    reason: str
    marker: int | None = None

    def to_dict(self) -> _CodeProblemPayload:
        return {
            "category": self.category,
            "address": self.address,
            "ordinal": self.ordinal,
            "reason": self.reason,
            "marker": self.marker,
        }


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

    def to_dict(self) -> dict[str, object]:
        """Новый JSON-снимок агрегатов и доступной части списка проблем.

        Число строк полного журнала здесь неизвестно: количество вхождений
        проблем может отличаться от количества обезличенных строк.
        """
        return {
            "modules": {
                "total": self.modules_total,
                "source_available": self.modules_source_available,
                "empty": self.modules_empty,
                "partial": self.modules_partial,
                "unreadable": self.modules_unreadable,
                "conflict": self.modules_conflict,
                "compiled_without_source": self.modules_compiled_without_source,
            },
            "procedures": {
                "total": self.procedures_total,
                "full": self.procedures_full,
                "partial": self.procedures_partial,
            },
            "form_structures": {
                "total": self.forms_total,
                "full": self.form_structures_full,
                "partial": self.form_structures_partial,
                "unavailable": self.form_structures_unread,
            },
            "form_modules": {
                "total": self.forms_total,
                "read": self.form_modules_read,
                "empty": self.form_modules_empty,
                "missing": self.form_modules_missing,
                "unreadable": self.form_modules_unread,
            },
            "limitations": {
                "categories": dict(self.problem_categories),
                "occurrences_total": self.problems_total,
                "unknown_markers": self.unknown_markers,
                "known_markers_incomplete": self.known_markers_incomplete,
                "unsupported_addresses": self.unsupported_addresses,
                "broken_containers": self.broken_containers,
                "unreadable_bodies": self.unreadable_bodies,
                "budget_exceeded": self.budget_exceeded,
                "body_conflicts": self.body_conflicts,
                "compiled_without_source": self.compiled_without_source,
            },
            "has_limitations": self.has_limitations,
            "problems": [problem.to_dict() for problem in self.problems],
            "problems_omitted": self.problems_omitted,
        }

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
                self.form_structures_unread,
                self.unsupported_addresses,
                self.broken_containers,
                self.unreadable_bodies,
                self.budget_exceeded,
                self.body_conflicts,
            )
        )


def _safe_problem_reason(reason: str) -> str:
    if not reason:
        return "причина не записана"
    if "/" in reason or "\\" in reason:
        return "подробности доступны в журнале сервера"
    return reason


_NEUTRAL_COVERAGE_CATEGORIES = frozenset(
    {
        "descriptor_only",
        "event_semantics_deferred",
        "form_structure_missing",
        "known_marker_semantics_deferred",
        "known_marker_semantics_incomplete",
        "missing_body",
        "unknown_marker",
        "unsupported_profile",
    }
)


def _iter_code_problems(loaded: LoadedModules):
    """Уникальные обезличенные проблемы без сортировки и общего списка."""
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
            if problem.category in _NEUTRAL_COVERAGE_CATEGORIES:
                continue
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
            if problem.категория in _NEUTRAL_COVERAGE_CATEGORIES:
                continue
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


@dataclass(slots=True)
class _CoverageCacheEntry:
    """Покрытие одного пакета без ссылки на владельца слабого ключа."""

    оглавление: object
    формы: object
    каталог: object
    корень: object
    coverage: CodeCoverage | None = None
    coverage_without_rows: CodeCoverage | None = None


# LoadedModules не поддерживает слабые ссылки; поисковый индекс живёт столько
# же, сколько пакет. Значение не держит ни loaded, ни индекс — иначе слабый
# ключ никогда не освободил бы запись старого поколения.
_CODE_COVERAGE: "weakref.WeakKeyDictionary[object, _CoverageCacheEntry]" = (
    weakref.WeakKeyDictionary()
)
_CODE_COVERAGE_LOCK = threading.Lock()


def _coverage_cache_entry(loaded: LoadedModules) -> _CoverageCacheEntry:
    ключ = loaded.поиск
    with _CODE_COVERAGE_LOCK:
        запись = _CODE_COVERAGE.get(ключ)
        if (
            запись is None
            or запись.оглавление is not loaded.оглавление
            or запись.формы is not loaded.формы
            or запись.каталог is not loaded.каталог
            or запись.корень != loaded.корень
        ):
            запись = _CoverageCacheEntry(
                loaded.оглавление, loaded.формы, loaded.каталог, loaded.корень
            )
            _CODE_COVERAGE[ключ] = запись
        return запись


def get_code_coverage(
    loaded: LoadedModules, *, include_problem_rows: bool = True,
    all_code_problems: Callable[[LoadedModules], tuple[CodeProblemRow, ...]],
) -> CodeCoverage:
    # Два варианта кэшируются раздельно: короткому предупреждению список
    # проблем не нужен, и строить его ради кэша значило бы платить за то, от
    # чего вариант без строк и избавлен. Гонка двух потоков на промахе лишь
    # посчитает одно и то же дважды — результат неизменяем.
    запись = _coverage_cache_entry(loaded)
    готовое = (
        запись.coverage
        if include_problem_rows
        else запись.coverage_without_rows
    )
    if готовое is not None:
        return готовое
    результат = _compute_code_coverage(
        loaded, include_problem_rows=include_problem_rows,
        all_code_problems=all_code_problems,
    )
    if include_problem_rows:
        запись.coverage = результат
    else:
        запись.coverage_without_rows = результат
    return результат


def _compute_code_coverage(
    loaded: LoadedModules, *, include_problem_rows: bool,
    all_code_problems: Callable[[LoadedModules], tuple[CodeProblemRow, ...]],
) -> CodeCoverage:
    каталог = loaded.каталог
    формы = loaded.формы
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
        состояния = категории.get(entry.address.casefold(), set())
        есть_кандидат_модуля = (
            not entry.is_form
            or entry.locator is not None
            or "module" in entry.form_evidence
            or bool(
                состояния
                & {"broken_container", "unreadable_body", "conflict"}
            )
        )
        if not есть_кандидат_модуля:
            continue
        if entry.conflict:
            модулей_конфликт += 1
        elif entry.compiled:
            модулей_скомпилировано += 1
        elif состояния & {"broken_container", "unreadable_body"}:
            модулей_непрочитано_всего += 1
        elif entry.locator is None:
            модулей_непрочитано_всего += 1
        elif "empty" in состояния:
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
        elif состояния & {"broken_container", "unreadable_body"}:
            модулей_непрочитано += 1
        elif "empty" in состояния:
            модулей_пусто += 1
        elif entry.locator is not None and not entry.compiled:
            модулей_прочитано += 1
        else:
            модулей_нет += 1

    проблемы = all_code_problems(loaded) if include_problem_rows else ()
    категории_проблем = {
        category: count
        for category, count in каталог.problem_counts
        if category not in _NEUTRAL_COVERAGE_CATEGORIES
    }
    for category, count in формы.problem_counts:
        if category in _NEUTRAL_COVERAGE_CATEGORIES:
            continue
        категории_проблем[category] = категории_проблем.get(category, 0) + count
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
