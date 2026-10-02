"""Доказанное происхождение объектов и реквизитов расширений.

Native-поколение выводит каталог основной выгрузки из своей проекции
(`capture_configuration`). Каталог расширения — разность с конкретным
поколением основной выгрузки — писал только удалённый приём кода отдельно от
структуры (`add_modules`) в файл `.structure-origin.json.gz` своего корня.
`load` читает этот файл у строк кода прежней версии в окне совместимости
(удалить после выпуска 4.3.0 вместе с их подъёмом); у native-расширения
каталога нет, и происхождение для него остаётся неизвестным.
"""

from __future__ import annotations

import gzip
import json
from dataclasses import dataclass
from pathlib import Path

from .model import Configuration


CATALOG_FILE = ".structure-origin.json.gz"
FORMAT_VERSION = 1
ROLE_BASE = "base"
ROLE_EXTENSION = "extension"
_MAX_CATALOG_BYTES = 64 * 1024 * 1024

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


def load(root: Path) -> StructureCatalog | None:
    """Поднять расходный снимок; любая порча означает unknown."""
    try:
        path = root / CATALOG_FILE
        if path.stat().st_size > _MAX_CATALOG_BYTES:
            return None
        with gzip.open(path, "rb") as stream:
            encoded = stream.read(_MAX_CATALOG_BYTES + 1)
        if len(encoded) > _MAX_CATALOG_BYTES:
            return None
        raw = json.loads(encoded)
        if not isinstance(raw, dict):
            return None
        if (
            type(raw.get("schema_version")) is not int
            or raw.get("schema_version") != FORMAT_VERSION
            or raw.get("role") not in {ROLE_BASE, ROLE_EXTENSION}
            or not isinstance(raw.get("source_sha256"), str)
            or not isinstance(raw.get("base_sha256"), str)
            or type(raw.get("complete")) is not bool
        ):
            return None
        objects = raw.get("objects")
        fields = raw.get("fields")
        problems = raw.get("problems")
        if not all(
            isinstance(items, list) and all(isinstance(item, str) for item in items)
            for items in (objects, fields, problems)
        ):
            return None
        return StructureCatalog(
            role=raw["role"],
            source_sha256=raw["source_sha256"],
            base_sha256=raw["base_sha256"],
            complete=raw["complete"],
            objects=frozenset(objects),
            fields=frozenset(fields),
            problems=tuple(problems),
        )
    except (
        OSError,
        EOFError,
        UnicodeError,
        gzip.BadGzipFile,
        json.JSONDecodeError,
        TypeError,
    ):
        return None


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
