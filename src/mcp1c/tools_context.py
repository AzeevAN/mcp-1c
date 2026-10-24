"""Общие типы ответов и явные зависимости инструментов по коду."""

from __future__ import annotations

from array import array
from bisect import bisect_left
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Mapping, Protocol

if TYPE_CHECKING:
    from .bsl_lex import Процедура
    from .code_coverage import CodeCoverage
    from .module_content import ModuleLocator
    from .registry import Registry
    from .registry_types import LoadedModules, SourceSnapshot


@dataclass(frozen=True, slots=True)
class _CodeCapture:
    source: SourceSnapshot | None
    loaded: LoadedModules | None


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
class _ObjectCodeIndex:
    """Производные адреса пакета без ссылок на поисковый индекс."""

    form_keys: frozenset[str]
    modules_by_owner: Mapping[str, tuple[str, ...]]
    forms_by_owner: Mapping[str, tuple[str, ...]]


@dataclass(frozen=True, slots=True)
class _CodeView:
    capture: _CodeCapture
    summary: _CodeSummary | None


@dataclass(frozen=True, slots=True)
class _ConfigurationCodeSnapshot:
    context: object
    modules: _CodeView
    extensions: tuple[tuple[str, _CodeView], ...]


@dataclass(slots=True)
class _CodeAggregates:
    """Агрегаты одного неизменяемого пакета кода, посчитанные по требованию.

    Структуры, из которых они выведены, держатся здесь же: по ним проверяется,
    что запись относится к тому же пакету, а не к случайно совпавшему ключу.
    """

    оглавление: object
    формы: object
    каталог: object
    summary: _CodeSummary | None = None
    object_index: _ObjectCodeIndex | None = None
    # Корень сверяется отдельно: тело модуля читается с диска по нему.
    корень: object = None
    # Ключ пакета в общем кэше разборов (`_РАЗБОРЫ`): у каждой записи
    # агрегатов свой, поэтому замена записи отрезает и её разборы.
    пакет: object = field(default_factory=object)


class _SignatureError(Exception):
    """Граница декларации не совпала с текущим файлом модуля."""


@dataclass(frozen=True, slots=True)
class _РазобраннаяПроцедура:
    """Поля одного объявления, которые нужны ответу, без текста модуля.

    Атрибуты названы как у `Процедура`: карточка, оглавление и поиск читают
    их одинаково, откуда бы ни пришла запись — из кэша или из свежего разбора.
    """

    имя: str
    сигнатура: str
    строка: int
    начало_столбец: int
    конец: int
    конец_столбец: int
    директива: str
    перекрытие: tuple[str, str] | tuple[()]


@dataclass(frozen=True, slots=True)
class _РазборМодуля:
    """Компактный итог разбора одного модуля одного пакета кода.

    Текст модуля здесь не держится: тело карточка дочитывает с диска. Числа
    лежат в `array` по четыре на процедуру, а не объектами `int`: номера
    строк за 256 стоили бы по 28 байт каждый. Записи упорядочены по позиции
    в оглавлении — по ней и ищутся двоичным поиском.
    """

    позиции: array
    границы: array
    имена: tuple[str, ...]
    сигнатуры: tuple[str, ...]
    директивы: tuple[str, ...]
    перекрытия: tuple[tuple[str, str] | tuple[()], ...]
    # Оценка памяти записи для бюджета кэша. Имена — ссылки на строки
    # оглавления, директивы интернированы: ни те, ни другие не считаются.
    размер: int = 0

    def процедура(self, запись) -> _РазобраннаяПроцедура:
        i = bisect_left(self.позиции, запись.позиция)
        if i == len(self.позиции) or self.позиции[i] != запись.позиция:
            raise _SignatureError
        строка, начало_столбец, конец, конец_столбец = self.границы[4 * i:4 * i + 4]
        return _РазобраннаяПроцедура(
            имя=self.имена[i],
            сигнатура=self.сигнатуры[i],
            строка=строка,
            начало_столбец=начало_столбец,
            конец=конец,
            конец_столбец=конец_столбец,
            директива=self.директивы[i],
            перекрытие=self.перекрытия[i],
        )


class _ParseCache(Protocol):
    def взять(self, пакет: object, модуль: str) -> _РазборМодуля | None: ...

    def положить(
        self, пакет: object, модуль: str, разбор: _РазборМодуля
    ) -> _РазборМодуля: ...


class _PackageMemberReader(Protocol):
    def __call__(
        self, root: str | Path, relative_path: str, *,
        expected_size: int, expected_sha256: str,
    ) -> bytes: ...


class _PartialWarning(Protocol):
    def __call__(self, loaded: LoadedModules, *, corpus: str = "") -> str | None: ...


@dataclass(frozen=True, slots=True)
class CodeToolsContext:
    """Зависимости одного вызова фасада; не хранится в производных кэшах."""

    read_bsl: Callable[[Path, str, ModuleLocator], str]
    разобрать: Callable[[str], list[Процедура]]
    read_package_member: _PackageMemberReader
    _сигнатура: Callable[[LoadedModules, object, dict[str, _РазборМодуля] | None], str]
    _similar_address: Callable[[LoadedModules, str, str | None], list[str]]
    _scope_modules: Callable[[LoadedModules, str | None], frozenset[str]]
    _selected_modules: Callable[
        [Registry, str | None, str | None], tuple[object, LoadedModules | None]
    ]
    _modules_package_is_current: Callable[[Registry, list[LoadedModules]], bool]
    _is_form_module: Callable[[str], bool]
    _РАЗБОРЫ: _ParseCache
    _configuration_code_snapshot: Callable[[Registry, str], _ConfigurationCodeSnapshot]
    _loaded_partial_warning: _PartialWarning
    _code_state_text: Callable[[_CodeView], str]
    _code_aggregates: Callable[[LoadedModules], _CodeAggregates]
    _read_module_snapshot: Callable[[Registry, LoadedModules, str], str]
