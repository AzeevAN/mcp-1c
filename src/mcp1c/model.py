"""Внутренняя модель конфигурации 1С.

Формат-независима: и XML-, и JSON-выгрузка приводятся сюда, дальше по коду
никто не знает, откуда пришли данные.

Контракт выгрузки — docs/schema-v1.md.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterator

SCHEMA_VERSION = "1"

# Виды метаданных, у которых бывают предопределённые элементы.
KINDS_WITH_PREDEFINED = frozenset(
    {"Справочник", "ПланВидовХарактеристик", "ПланСчетов", "ПланВидовРасчета"}
)

# Виды, чьи объекты адресуются в коде через менеджер (Справочники.X, Документы.X).
KIND_TO_MANAGER = {
    "Справочник": "Справочники",
    "Документ": "Документы",
    "РегистрСведений": "РегистрыСведений",
    "РегистрНакопления": "РегистрыНакопления",
    "РегистрБухгалтерии": "РегистрыБухгалтерии",
    "РегистрРасчета": "РегистрыРасчета",
    "Константа": "Константы",
    "Перечисление": "Перечисления",
    "ПланВидовХарактеристик": "ПланыВидовХарактеристик",
    "ПланСчетов": "ПланыСчетов",
    "ПланВидовРасчета": "ПланыВидовРасчета",
    "ПланОбмена": "ПланыОбмена",
    "БизнесПроцесс": "БизнесПроцессы",
    "Задача": "Задачи",
    "ОбщийМодуль": "ОбщиеМодули",
    "Отчет": "Отчеты",
    "Обработка": "Обработки",
}

_COMMON_MODULE_PREFIX = "ОбщийМодуль."


def normalize_common_module_binding(value: str) -> str:
    """Привести привязку процедуры общего модуля к `Модуль.Процедура`.

    Exporter всегда писал короткую форму. Публичная schema раньше ошибочно
    добавляла префикс вида метаданных, поэтому принимаем и эту историческую
    запись. Проверка точки сохраняет корректное короткое значение для модуля,
    который сам называется `ОбщийМодуль`.
    """
    if value.startswith(_COMMON_MODULE_PREFIX):
        without_prefix = value.removeprefix(_COMMON_MODULE_PREFIX)
        if "." in without_prefix:
            return without_prefix
    return value


@dataclass(slots=True)
class Field:
    """Реквизит, измерение, ресурс или реквизит табличной части."""

    name: str
    synonym: str = ""
    comment: str = ""
    indexing: str = ""
    types: list[str] = field(default_factory=list)
    string_length: int | None = None
    digits: int | None = None
    fraction_digits: int | None = None
    date_parts: str = ""

    @property
    def is_composite(self) -> bool:
        return len(self.types) > 1

    @property
    def is_unlimited_string(self) -> bool:
        """Строка без ограничения длины — та, что не годится в запрос как есть.

        Выгрузка пишет `string_length` только когда длина больше нуля
        (`exporter-1c/src/core.bsl`), поэтому у неограниченной ключа нет вовсе.
        Отсутствие ключа у строкового поля и означает «неограниченная»:
        `КвалификаторыСтроки.Длина` там ноль, а сами квалификаторы платформа
        отдаёт всегда. Ноль тоже принимаем — на случай, если выгрузка когда-то
        станет писать его явно.
        """
        return "Строка" in self.types and not self.string_length

    @property
    def title(self) -> str:
        return self.synonym or self.name

    def type_spec(self, collapse_after: int = 5) -> str:
        """Человекочитаемое описание типа с учётом квалификаторов.

        Составные типы схлопываются: у Регистратора или Субконто их бывают
        сотни, и в markdown они не нужны. Полный список остаётся в модели.
        """
        if not self.types:
            return "?"

        if len(self.types) > collapse_after:
            head = ", ".join(self.types[:collapse_after])
            return f"составной, {len(self.types)} типов: {head}, …"

        parts = []
        for type_name in self.types:
            if type_name == "Строка" and self.string_length:
                parts.append(f"Строка({self.string_length})")
            elif type_name == "Строка":
                # Пробел перед скобкой намеренно: `Строка(200)` — это длина,
                # `Строка (неогр.)` — пометка. Слитно они читались бы как одно
                # и то же, а разница здесь стоит невыполнимого запроса.
                #
                # Рецепт стоит здесь, а не только в оговорке под карточкой.
                # Проверено 2026-08-18 на живом агенте: он получил карточку с
                # оговоркой целиком и всё равно сгруппировал по такому полю —
                # оговорка была последним абзацем, на 721 токен позже строки
                # поля, а решение принимается там, где имя копируют.
                parts.append("Строка (неогр. — только через ПОДСТРОКА)")
            elif type_name == "Число" and self.digits:
                fraction = self.fraction_digits or 0
                parts.append(f"Число({self.digits},{fraction})")
            else:
                parts.append(type_name)
        return ", ".join(parts)

    def object_types(self) -> list[str]:
        """Только ссылочные типы — те, что дают рёбра графа."""
        return [t for t in self.types if "." in t]


@dataclass(slots=True)
class TabularPart:
    name: str
    synonym: str = ""
    attributes: list[Field] = field(default_factory=list)

    @property
    def title(self) -> str:
        return self.synonym or self.name


@dataclass(slots=True, frozen=True)
class ObjectRelation:
    """Типизированная связь из дополнительного metadata-слоя Source B."""

    kind: str
    target: str
    state: str
    properties: tuple[tuple[str, str], ...] = ()


@dataclass(slots=True)
class MetadataObject:
    full_name: str
    kind: str
    name: str
    synonym: str = ""
    comment: str = ""

    props: dict[str, object] = field(default_factory=dict)

    attributes: list[Field] = field(default_factory=list)
    dimensions: list[Field] = field(default_factory=list)
    resources: list[Field] = field(default_factory=list)
    tabular_parts: list[TabularPart] = field(default_factory=list)

    movements: list[str] = field(default_factory=list)
    based_on: list[str] = field(default_factory=list)
    owners: list[str] = field(default_factory=list)

    predefined: list[str] = field(default_factory=list)
    enum_values: list[tuple[str, str]] = field(default_factory=list)
    value_type: Field | None = None

    # Эти сведения не входят в schema v1 и не должны копироваться в
    # base_structure. Они образуют единый resolved-view только после чтения
    # отдельного extended_structure поколения Source B.
    code_address: str = ""
    forms: list[str] = field(default_factory=list)
    extended: dict[str, object] = field(default_factory=dict)
    relations: list[ObjectRelation] = field(default_factory=list)

    @property
    def title(self) -> str:
        return self.synonym or self.name

    @property
    def manager_path(self) -> str:
        """Как объект адресуется в коде: Справочники.Номенклатура."""
        manager = KIND_TO_MANAGER.get(self.kind)
        if manager:
            return f"{manager}.{self.name}"
        return self.code_address or self.full_name

    def all_fields(self) -> Iterator[tuple[str, Field]]:
        """Все поля объекта вместе с путём до них."""
        for attribute in self.attributes:
            yield attribute.name, attribute
        for dimension in self.dimensions:
            yield dimension.name, dimension
        for resource in self.resources:
            yield resource.name, resource
        for part in self.tabular_parts:
            for attribute in part.attributes:
                yield f"{part.name}.{attribute.name}", attribute
        if self.value_type is not None:
            yield "ТипЗначения", self.value_type

    def field_count(self) -> int:
        return sum(1 for _ in self.all_fields())


@dataclass(slots=True)
class Configuration:
    name: str = ""
    synonym: str = ""
    version: str = ""
    vendor: str = ""
    platform: str = ""
    exported_at: str = ""
    exporter_version: str = ""
    schema_version: str = SCHEMA_VERSION
    source_format: str = ""

    truncated: bool = False
    predefined_available: bool = True
    warnings: list[str] = field(default_factory=list)

    objects: dict[str, MetadataObject] = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.objects)

    def get(self, full_name: str) -> MetadataObject | None:
        return self.objects.get(full_name)

    def by_kind(self, kind: str) -> list[MetadataObject]:
        return [o for o in self.objects.values() if o.kind == kind]

    def kinds(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for obj in self.objects.values():
            counts[obj.kind] = counts.get(obj.kind, 0) + 1
        return dict(sorted(counts.items(), key=lambda kv: -kv[1]))

    @property
    def is_complete(self) -> bool:
        """Можно ли считать выгрузку пригодной для рабочей коллекции."""
        return not self.truncated and self.predefined_available
