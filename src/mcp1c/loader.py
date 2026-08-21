"""Чтение выгрузки структуры конфигурации: ZIP или распакованный каталог.

Оба формата (XML и JSON) разбираются в один и тот же промежуточный словарь,
дальше единственная нормализация в модель. Конвенция «структура → XML»
описана в docs/schema-v1.md.

XML читается потоково (iterparse + clear), поэтому память не зависит от
размера чанка.
"""

from __future__ import annotations

import json
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, Iterator

from .model import Configuration, Field, MetadataObject, TabularPart

MANIFEST_XML = "manifest.xml"
MANIFEST_JSON = "manifest.json"
LEGACY_MARKER = "objects.csv"

_INT_KEYS = frozenset(
    {
        "string_length",
        "digits",
        "fraction_digits",
        "code_length",
        "description_length",
        "number_length",
        "objects_total",
        "chunk_size",
        "count",
        "chunk",
    }
)
_BOOL_KEYS = frozenset(
    {
        "truncated",
        "predefined_available",
        "hierarchical",
        "global",
        "server",
        "client_managed",
        "server_call",
        "privileged",
        "external_connection",
    }
)

# Секции объекта, которые загрузчик знает. Всё остальное уезжает в props.
_FIELD_SECTIONS = ("attributes", "dimensions", "resources")
_REF_SECTIONS = ("movements", "based_on", "owners")
_KNOWN_SECTIONS = frozenset(
    _FIELD_SECTIONS
    + _REF_SECTIONS
    + ("tabular_parts", "predefined", "enum_values", "value_type")
)
_OBJECT_HEAD = frozenset({"full_name", "type", "name", "synonym", "comment"})

# Сколько нарушений контракта показать поимённо. Больше в сообщение не влезет,
# а для починки хватает и нескольких: они всегда одного рода.
_MAX_CONTRACT_PROBLEMS = 10


class ExportError(Exception):
    """Выгрузка не может быть прочитана."""


# ---------------------------------------------------------------- источник


class _Source:
    """Доступ к файлам выгрузки — одинаковый для ZIP и каталога."""

    def __init__(self, path: Path):
        self.path = path
        self._zip: zipfile.ZipFile | None = None
        self._root = ""

        if path.is_dir():
            self._names = [
                str(p.relative_to(path)).replace("\\", "/")
                for p in path.rglob("*")
                if p.is_file()
            ]
        elif zipfile.is_zipfile(path):
            self._zip = zipfile.ZipFile(path)
            self._names = [n for n in self._zip.namelist() if not n.endswith("/")]
            self._root = _common_root(self._names)
        else:
            raise ExportError(f"Не ZIP и не каталог: {path}")

    def exists(self, name: str) -> bool:
        return (self._root + name) in self._names

    def open(self, name: str) -> IO[bytes]:
        full = self._root + name
        if self._zip is not None:
            return self._zip.open(full)
        return (self.path / full).open("rb")

    def close(self) -> None:
        if self._zip is not None:
            self._zip.close()


def _common_root(names: list[str]) -> str:
    """ZIP может содержать выгрузку внутри одной обёрточной папки."""
    for candidate in (MANIFEST_XML, MANIFEST_JSON, LEGACY_MARKER):
        for name in names:
            if name == candidate:
                return ""
            if name.endswith("/" + candidate):
                return name[: -len(candidate)]
    return ""


# ---------------------------------------------------------------- XML → dict


def _xml_value(element: ET.Element) -> Any:
    items = element.findall("item")
    if items:
        return [_xml_item(i) for i in items]
    return _xml_dict(element)


def _xml_item(item: ET.Element) -> Any:
    # Скаляр массива пишется как <item value="..."/> и других данных не несёт.
    if len(item) == 0 and set(item.attrib) == {"value"}:
        return item.get("value")
    return _xml_dict(item)


def _xml_dict(element: ET.Element) -> dict[str, Any]:
    data: dict[str, Any] = dict(element.attrib)
    for child in element:
        data[child.tag] = _xml_value(child)
    return data


def _read_objects_xml(stream: IO[bytes]) -> Iterator[dict[str, Any]]:
    """Потоковое чтение чанка: в памяти одновременно живёт один объект."""
    for event, element in ET.iterparse(stream, events=("end",)):
        if element.tag != "object":
            continue
        yield _xml_dict(element)
        element.clear()


def _read_manifest_xml(stream: IO[bytes]) -> dict[str, Any]:
    root = ET.parse(stream).getroot()
    return _xml_dict(root)


# ---------------------------------------------------------------- приведение


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except ValueError:
        return None


def _as_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "1", "истина", "yes"}


def _coerce(key: str, value: Any) -> Any:
    if key in _INT_KEYS:
        return _as_int(value)
    if key in _BOOL_KEYS:
        return _as_bool(value)
    return value


def _to_field(raw: dict[str, Any]) -> Field:
    types = raw.get("type") or []
    if isinstance(types, str):
        types = [types]
    return Field(
        name=raw.get("name", ""),
        synonym=raw.get("synonym", "") or "",
        comment=raw.get("comment", "") or "",
        indexing=raw.get("indexing", "") or "",
        types=[str(t) for t in types],
        string_length=_as_int(raw.get("string_length")),
        digits=_as_int(raw.get("digits")),
        fraction_digits=_as_int(raw.get("fraction_digits")),
        date_parts=raw.get("date_parts", "") or "",
    )


# Чего загрузчик ждёт от известных ключей. Проверяется до разбора: выгрузка
# приходит из обработки, которая живёт своей жизнью, и занятый по ошибке ключ
# роняет загрузку далеко от места ошибки. Так уже было дважды — `type`, куда
# попал список типов значения, и `predefined`, куда попало булево регламентного
# задания вместо списка предопределённых.
_EXPECTED_TYPES: dict[str, tuple[type, ...]] = {
    "full_name": (str,),
    "type": (str,),
    "name": (str,),
    "synonym": (str,),
    "comment": (str,),
    "attributes": (list,),
    "dimensions": (list,),
    "resources": (list,),
    "movements": (list,),
    "based_on": (list,),
    "owners": (list,),
    "tabular_parts": (list,),
    "predefined": (list,),
    "enum_values": (list,),
    "value_type": (dict,),
}


def contract_problems(raw: dict[str, Any]) -> list[str]:
    """Ключи объекта, чей тип не совпадает с контрактом schema v1.

    Возвращает список, а не бросает: собрать все несоответствия разом
    полезнее, чем чинить их по одному, каждый раз перевыгружая конфигурацию.
    """
    problems: list[str] = []
    for key, expected in _EXPECTED_TYPES.items():
        value = raw.get(key)
        if value is None or isinstance(value, expected):
            continue
        problems.append(
            f"`{key}`: ожидается {'/'.join(t.__name__ for t in expected)}, "
            f"пришло {type(value).__name__}"
        )
    return problems


def _to_object(raw: dict[str, Any]) -> MetadataObject:
    obj = MetadataObject(
        full_name=raw.get("full_name", ""),
        kind=raw.get("type", ""),
        name=raw.get("name", ""),
        synonym=raw.get("synonym", "") or "",
        comment=raw.get("comment", "") or "",
    )

    for section in _FIELD_SECTIONS:
        setattr(obj, section, [_to_field(f) for f in raw.get(section) or []])

    for part in raw.get("tabular_parts") or []:
        obj.tabular_parts.append(
            TabularPart(
                name=part.get("name", ""),
                synonym=part.get("synonym", "") or "",
                attributes=[_to_field(f) for f in part.get("attributes") or []],
            )
        )

    for section in _REF_SECTIONS:
        setattr(obj, section, [str(v) for v in raw.get(section) or []])

    obj.predefined = [p.get("name", "") for p in raw.get("predefined") or []]
    obj.enum_values = [
        (v.get("name", ""), v.get("synonym", "") or "")
        for v in raw.get("enum_values") or []
    ]

    value_type = raw.get("value_type")
    if isinstance(value_type, dict):
        obj.value_type = _to_field({"name": "ТипЗначения", **value_type})

    # Всё, что не разобрано явно, — свойства вида: posting, hierarchical, handler…
    for key, value in raw.items():
        if key in _KNOWN_SECTIONS or key in _OBJECT_HEAD:
            continue
        obj.props[key] = _coerce(key, value)

    return obj


# ---------------------------------------------------------------- публичное API


@dataclass(slots=True)
class ExportInfo:
    """Что за выгрузка перед нами — без чтения объектов."""

    path: Path
    fmt: str
    name: str
    version: str
    platform: str
    exported_at: str
    objects_total: int
    truncated: bool
    predefined_available: bool
    warnings: list[str]


def _read_manifest(source: _Source) -> tuple[str, dict[str, Any]]:
    if source.exists(MANIFEST_JSON):
        with source.open(MANIFEST_JSON) as stream:
            return "json", json.loads(stream.read().decode("utf-8-sig"))
    if source.exists(MANIFEST_XML):
        with source.open(MANIFEST_XML) as stream:
            return "xml", _read_manifest_xml(stream)
    if source.exists(LEGACY_MARKER):
        raise ExportError(
            "Это выгрузка старого формата (objects.csv + markdown). "
            "Её чтение пока не реализовано — перевыгрузите обработкой из exporter-1c/."
        )
    raise ExportError(
        f"В {source.path} нет ни {MANIFEST_XML}, ни {MANIFEST_JSON} — "
        "это не выгрузка структуры конфигурации."
    )


def _manifest_warnings(manifest: dict[str, Any]) -> list[str]:
    result = []
    for item in manifest.get("warnings") or []:
        if isinstance(item, dict):
            text = item.get("text", "")
            details = item.get("details")
            result.append(f"{text} [{details}]" if details else text)
        else:
            result.append(str(item))
    return result


def inspect(path: str | Path) -> ExportInfo:
    """Прочитать только манифест. Дёшево — объекты не трогаются."""
    source = _Source(Path(path))
    try:
        fmt, manifest = _read_manifest(source)
        return ExportInfo(
            path=Path(path),
            fmt=fmt,
            name=manifest.get("name", ""),
            version=manifest.get("version", ""),
            platform=manifest.get("platform", ""),
            exported_at=manifest.get("exported_at", ""),
            objects_total=_as_int(manifest.get("objects_total")) or 0,
            truncated=_as_bool(manifest.get("truncated")),
            predefined_available=_as_bool(manifest.get("predefined_available"), True),
            warnings=_manifest_warnings(manifest),
        )
    finally:
        source.close()


def load(path: str | Path) -> Configuration:
    """Прочитать выгрузку целиком в модель."""
    source = _Source(Path(path))
    try:
        fmt, manifest = _read_manifest(source)

        schema_version = str(manifest.get("schema_version", ""))
        if schema_version and schema_version.split(".")[0] != "1":
            raise ExportError(
                f"Неизвестная версия схемы выгрузки: {schema_version}. "
                "Загрузчик умеет только 1.x — обновите его или перевыгрузите."
            )

        config = Configuration(
            name=manifest.get("name", ""),
            synonym=manifest.get("synonym", "") or "",
            version=manifest.get("version", ""),
            vendor=manifest.get("vendor", "") or "",
            platform=manifest.get("platform", ""),
            exported_at=manifest.get("exported_at", ""),
            exporter_version=manifest.get("exporter_version", ""),
            schema_version=schema_version or "1",
            source_format=fmt,
            truncated=_as_bool(manifest.get("truncated")),
            predefined_available=_as_bool(manifest.get("predefined_available"), True),
            warnings=_manifest_warnings(manifest),
        )

        files = manifest.get("files") or []
        if not files:
            raise ExportError("Манифест не содержит списка файлов.")

        нарушения: list[str] = []
        нарушений_всего = 0

        for entry in files:
            rel = entry.get("path", "")
            if not rel or not source.exists(rel):
                raise ExportError(f"Файл из манифеста отсутствует в выгрузке: {rel}")

            with source.open(rel) as stream:
                if fmt == "json":
                    chunk = json.loads(stream.read().decode("utf-8-sig"))
                    raw_objects: Iterator[dict[str, Any]] = iter(chunk.get("objects", []))
                else:
                    raw_objects = _read_objects_xml(stream)

                for raw in raw_objects:
                    # Контракт проверяется до разбора и по всей выгрузке сразу:
                    # чинить такие расхождения по одному значит перевыгружать
                    # конфигурацию на каждую ошибку.
                    issues = contract_problems(raw)
                    if issues:
                        if len(нарушения) < _MAX_CONTRACT_PROBLEMS:
                            нарушения.append(
                                f"{raw.get('full_name') or '?'} — {'; '.join(issues)}"
                            )
                        нарушений_всего += 1
                        continue

                    obj = _to_object(raw)
                    if not obj.full_name:
                        continue
                    config.objects[obj.full_name] = obj

        if нарушения:
            хвост = (
                f"\n… и ещё {нарушений_всего - len(нарушения)} таких же объектов"
                if нарушений_всего > len(нарушения)
                else ""
            )
            raise ExportError(
                "Выгрузка не соответствует schema v1 — обработка пишет в поля "
                f"схемы что-то своё ({нарушений_всего} объектов):\n"
                + "\n".join(f"  {строка}" for строка in нарушения)
                + хвост
                + "\n\nНужна свежая версия обработки из `exporter-1c/dist/`."
            )

        declared = _as_int(manifest.get("objects_total"))
        if declared is not None and declared != len(config.objects):
            config.warnings.append(
                f"Манифест обещает {declared} объектов, прочитано {len(config.objects)}."
            )

        return config
    finally:
        source.close()
