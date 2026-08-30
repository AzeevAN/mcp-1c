"""Доказанное происхождение объектов и реквизитов расширений.

Файловая выгрузка нужна только во время приёма.  В опубликованном каталоге
кода остаётся малый gzip-снимок семантических адресов; исходные XML и ZIP
сервер не сохраняет.  Для расширения снимок содержит только разность с
конкретным поколением основной выгрузки, поэтому смена её SHA делает вывод
неизвестным, а не заставляет угадывать происхождение повторно.
"""

from __future__ import annotations

import gzip
import json
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from pathlib import Path

from .intake import карта_архива, карта_каталога


CATALOG_FILE = ".structure-origin.json.gz"
FORMAT_VERSION = 1
ROLE_BASE = "base"
ROLE_EXTENSION = "extension"
_MAX_CATALOG_BYTES = 64 * 1024 * 1024

_NS = "http://v8.1c.ru/8.3/MDClasses"

# Только виды, которые schema v1 умеет показать как самостоятельный объект.
# Физический каталог нужен для точного выбора XML и не публикуется наружу.
_KINDS: dict[str, tuple[str, str]] = {
    "Catalog": ("Catalogs", "Справочник"),
    "Document": ("Documents", "Документ"),
    "InformationRegister": ("InformationRegisters", "РегистрСведений"),
    "AccumulationRegister": ("AccumulationRegisters", "РегистрНакопления"),
    "AccountingRegister": ("AccountingRegisters", "РегистрБухгалтерии"),
    "CalculationRegister": ("CalculationRegisters", "РегистрРасчета"),
    "Constant": ("Constants", "Константа"),
    "Enum": ("Enums", "Перечисление"),
    "ChartOfCharacteristicTypes": (
        "ChartsOfCharacteristicTypes",
        "ПланВидовХарактеристик",
    ),
    "ChartOfAccounts": ("ChartsOfAccounts", "ПланСчетов"),
    "ChartOfCalculationTypes": (
        "ChartsOfCalculationTypes",
        "ПланВидовРасчета",
    ),
    "ExchangePlan": ("ExchangePlans", "ПланОбмена"),
    "BusinessProcess": ("BusinessProcesses", "БизнесПроцесс"),
    "Task": ("Tasks", "Задача"),
    "DefinedType": ("DefinedTypes", "ОпределяемыйТип"),
    "CommonModule": ("CommonModules", "ОбщийМодуль"),
    "EventSubscription": ("EventSubscriptions", "ПодпискаНаСобытие"),
    "ScheduledJob": ("ScheduledJobs", "РегламентноеЗадание"),
    "Report": ("Reports", "Отчет"),
    "DataProcessor": ("DataProcessors", "Обработка"),
}


def _tag(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


@dataclass(frozen=True, slots=True)
class DeclaredStructure:
    """Адреса, непосредственно прочитанные из одного ZIP."""

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


def _descriptor(
    stream,
    *,
    expected_kind: str,
    expected_name: str,
) -> tuple[str, frozenset[str]]:
    actual_name = ""
    field_names: set[str] = set()
    stack: list[str] = []
    for event, element in ET.iterparse(stream, events=("start", "end")):
        tag = _tag(element)
        if event == "start":
            stack.append(tag)
            if len(stack) == 2 and tag != expected_kind:
                raise ValueError("XML-дескриптор содержит другой вид объекта")
            continue
        if stack == ["MetaDataObject", expected_kind, "Properties", "Name"]:
            actual_name = (element.text or "").strip()
        elif stack == [
            "MetaDataObject",
            expected_kind,
            "ChildObjects",
            "Attribute",
            "Properties",
            "Name",
        ]:
            field_name = (element.text or "").strip()
            if field_name:
                field_names.add(field_name)
        element.clear()
        stack.pop()
    if actual_name != expected_name:
        raise ValueError("имя в XML-дескрипторе не совпало с манифестом")

    public_kind = _KINDS[expected_kind][1]
    object_address = f"{public_kind}.{actual_name}"
    # Табличные части, измерения и ресурсы здесь намеренно не выводятся:
    # на безопасном корпусе доказаны только прямые Attribute.
    fields = {f"{object_address}.{field_name}" for field_name in field_names}
    return object_address, frozenset(fields)


def _manifest(stream) -> tuple[bool, list[tuple[str, str]]]:
    """Потоково прочитать перечень корневых объектов Configuration.xml."""
    found_children = False
    expected: list[tuple[str, str]] = []
    stack: list[str] = []
    for event, element in ET.iterparse(stream, events=("start", "end")):
        tag = _tag(element)
        if event == "start":
            stack.append(tag)
            if stack == ["MetaDataObject", "Configuration", "ChildObjects"]:
                found_children = True
            continue
        if (
            len(stack) == 4
            and stack[:3]
            == ["MetaDataObject", "Configuration", "ChildObjects"]
            and tag in _KINDS
        ):
            name = (element.text or "").strip()
            if name:
                expected.append((tag, name))
        element.clear()
        stack.pop()
    return found_children, expected


def _capture_from_map(
    archive: dict[str, object],
    open_member,
) -> DeclaredStructure:
    problems: list[str] = []
    objects: set[str] = set()
    fields: set[str] = set()
    configuration_info = archive.get("Configuration.xml")
    if configuration_info is None:
        return DeclaredStructure(
            False, frozenset(), frozenset(), ("нет Configuration.xml",)
        )
    with open_member(configuration_info) as stream:
        found_children, expected = _manifest(stream)
    if not found_children:
        return DeclaredStructure(
            False,
            frozenset(),
            frozenset(),
            ("в Configuration.xml нет ChildObjects",),
        )

    for kind, name in expected:
        folder, _public_kind = _KINDS[kind]
        info = archive.get(f"{folder}/{name}.xml")
        if info is None:
            info = archive.get(f"{kind}.{name}.xml")
        if info is None:
            problems.append("нет XML-дескриптора поддерживаемого объекта")
            continue
        try:
            with open_member(info) as stream:
                object_address, object_fields = _descriptor(
                    stream, expected_kind=kind, expected_name=name
                )
        except (
            ET.ParseError,
            OSError,
            ValueError,
            RuntimeError,
            NotImplementedError,
        ):
            problems.append("не прочитан XML-дескриптор поддерживаемого объекта")
            continue
        objects.add(object_address)
        fields.update(object_fields)
    return DeclaredStructure(
        not problems,
        frozenset(objects),
        frozenset(fields),
        tuple(problems),
    )


def capture_archive(path: Path) -> DeclaredStructure:
    """Прочитать только корневые объекты и их прямые реквизиты.

    Полнота доказывается через ``Configuration/ChildObjects``: пустое или
    отсутствующее перечисление нельзя подменять выводом «объектов нет».
    Каждому поддержанному элементу манифеста должен соответствовать ровно
    разбираемый XML в иерархической либо плоской раскладке.
    """
    try:
        if path.is_dir():
            archive = карта_каталога(path)
            return _capture_from_map(archive, lambda member: member.open("rb"))
        with zipfile.ZipFile(path) as zf:
            archive = карта_архива(zf)
            return _capture_from_map(archive, zf.open)
    except (
        ET.ParseError,
        OSError,
        RuntimeError,
        NotImplementedError,
        zipfile.BadZipFile,
    ):
        return DeclaredStructure(
            False,
            frozenset(),
            frozenset(),
            ("файловая выгрузка недоступна для каталога происхождения",),
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


def extension_catalog(
    raw: DeclaredStructure,
    source_sha256: str,
    base: StructureCatalog | None,
) -> StructureCatalog:
    """Сохранить только доказанную разность с одним поколением базы."""
    if base is None or base.role != ROLE_BASE or not base.complete:
        return StructureCatalog(
            role=ROLE_EXTENSION,
            source_sha256=source_sha256,
            base_sha256=base.source_sha256 if base is not None else "",
            complete=False,
            objects=frozenset(),
            fields=frozenset(),
            problems=("нет полного каталога основной файловой выгрузки",),
        )

    base_object_keys = {address.casefold() for address in base.objects}
    base_field_keys = {address.casefold() for address in base.fields}
    own_objects = {
        address for address in raw.objects if address.casefold() not in base_object_keys
    }
    own_fields = {
        address
        for address in raw.fields
        if address.casefold() not in base_field_keys
        and address.rsplit(".", 1)[0].casefold() in base_object_keys
    }
    return StructureCatalog(
        role=ROLE_EXTENSION,
        source_sha256=source_sha256,
        base_sha256=base.source_sha256,
        complete=raw.complete,
        objects=frozenset(own_objects),
        fields=frozenset(own_fields),
        problems=raw.problems,
    )


def save(root: Path, catalog: StructureCatalog) -> None:
    payload = {
        "schema_version": FORMAT_VERSION,
        "role": catalog.role,
        "source_sha256": catalog.source_sha256,
        "base_sha256": catalog.base_sha256,
        "complete": catalog.complete,
        "objects": sorted(catalog.objects, key=lambda value: (value.casefold(), value)),
        "fields": sorted(catalog.fields, key=lambda value: (value.casefold(), value)),
        "problems": list(catalog.problems),
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    (root / CATALOG_FILE).write_bytes(gzip.compress(encoded, compresslevel=1, mtime=0))


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
