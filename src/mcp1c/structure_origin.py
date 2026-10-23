"""Доказанное происхождение объектов и реквизитов расширений.

Native-поколение выводит каталог основной выгрузки из своей проекции
(`capture_configuration`). Каталога расширения — разности с конкретным
поколением основной выгрузки — native-поколение не хранит, поэтому
происхождение объектов расширения остаётся неизвестным.
"""

from __future__ import annotations

from dataclasses import dataclass

from .model import Configuration


ROLE_BASE = "base"
ROLE_EXTENSION = "extension"

# Только виды, которые schema v1 умеет показать как самостоятельный объект.
_PUBLIC_KINDS = frozenset(
    {
        "Справочник",
        "Документ",
        "РегистрСведений",
        "РегистрНакопления",
        "РегистрБухгалтерии",
        "РегистрРасчета",
        "Константа",
        "Перечисление",
        "ПланВидовХарактеристик",
        "ПланСчетов",
        "ПланВидовРасчета",
        "ПланОбмена",
        "БизнесПроцесс",
        "Задача",
        "ОпределяемыйТип",
        "ОбщийМодуль",
        "ПодпискаНаСобытие",
        "РегламентноеЗадание",
        "Отчет",
        "Обработка",
    }
)


@dataclass(frozen=True, slots=True)
class DeclaredStructure:
    """Адреса, доказанные одной выгрузкой."""

    complete: bool
    objects: frozenset[str]
    fields: frozenset[str]
    problems: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class StructureCatalog:
    """Опубликованный снимок одного поколения кода."""

    role: str
    source_sha256: str
    base_sha256: str
    complete: bool
    objects: frozenset[str]
    fields: frozenset[str]
    problems: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class StructureOriginView:
    """Готовая для ответа агрегация нескольких доказательств."""

    objects: tuple[tuple[str, tuple[str, ...]], ...] = ()
    fields: tuple[tuple[str, tuple[str, ...]], ...] = ()
    unknown: tuple[str, ...] = ()

    def object_sources(self, address: str) -> tuple[str, ...]:
        normalized = address.casefold()
        return next(
            (sources for key, sources in self.objects if key.casefold() == normalized),
            (),
        )

    def field_sources(self, address: str) -> tuple[str, ...]:
        normalized = address.casefold()
        return next(
            (sources for key, sources in self.fields if key.casefold() == normalized),
            (),
        )


def base_catalog(raw: DeclaredStructure, source_sha256: str) -> StructureCatalog:
    return StructureCatalog(
        role=ROLE_BASE,
        source_sha256=source_sha256,
        base_sha256="",
        complete=raw.complete,
        objects=raw.objects,
        fields=raw.fields,
        problems=raw.problems,
    )


def capture_configuration(configuration: Configuration) -> DeclaredStructure:
    """Снять тот же каталог с уже проверенной native-проекции Source B.

    Generation runtime восстанавливает структуру из защищённого хешами
    ``base_structure`` и не хранит исходный ZIP. Поэтому происхождение нужно
    выводить из проекции до добавления стандартных реквизитов: иначе после
    первого restart полный Source B ошибочно выглядел бы как отсутствующий.
    """
    objects = {
        obj.full_name
        for obj in configuration.objects.values()
        if obj.kind in _PUBLIC_KINDS
    }
    fields = {
        f"{obj.full_name}.{field.name}"
        for obj in configuration.objects.values()
        if obj.kind in _PUBLIC_KINDS
        for field in obj.attributes
        if not field.standard
    }
    return DeclaredStructure(
        True,
        frozenset(objects),
        frozenset(fields),
    )


def resolve(
    *,
    base_sha256: str,
    base: StructureCatalog | None,
    extensions: tuple[tuple[str, str, StructureCatalog | None], ...],
) -> StructureOriginView:
    """Свести доказательства одного согласованного снимка Registry."""
    if not extensions:
        return StructureOriginView()
    if (
        base is None
        or base.role != ROLE_BASE
        or base.source_sha256 != base_sha256
        or not base.complete
    ):
        return StructureOriginView(
            unknown=("нет полного каталога основной файловой выгрузки",)
        )

    objects: dict[str, tuple[str, list[str]]] = {}
    fields: dict[str, tuple[str, list[str]]] = {}
    unknown: list[str] = []
    for name, source_sha256, catalog in extensions:
        if (
            catalog is None
            or catalog.role != ROLE_EXTENSION
            or catalog.source_sha256 != source_sha256
        ):
            unknown.append(f"нет каталога происхождения расширения «{name}»")
            continue
        if catalog.base_sha256 != base_sha256:
            if catalog.base_sha256:
                unknown.append(
                    "поколение основной файловой выгрузки изменилось после "
                    f"разбора расширения «{name}»"
                )
            else:
                unknown.append(
                    f"расширение «{name}» разобрано без каталога основной "
                    "файловой выгрузки"
                )
            continue
        for address in catalog.objects:
            objects.setdefault(address.casefold(), (address, []))[1].append(name)
        for address in catalog.fields:
            fields.setdefault(address.casefold(), (address, []))[1].append(name)
        if not catalog.complete:
            unknown.append(
                f"каталог происхождения расширения «{name}» неполон"
            )

    order = lambda value: (value.casefold(), value)
    return StructureOriginView(
        objects=tuple(
            (address, tuple(sorted(names, key=order)))
            for address, names in sorted(objects.values(), key=lambda item: order(item[0]))
        ),
        fields=tuple(
            (address, tuple(sorted(names, key=order)))
            for address, names in sorted(fields.values(), key=lambda item: order(item[0]))
        ),
        unknown=tuple(dict.fromkeys(unknown)),
    )
