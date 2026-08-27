"""Чтение выгрузки структуры конфигурации: ZIP или распакованный каталог.

Оба формата (XML и JSON) разбираются в один и тот же промежуточный словарь,
дальше единственная нормализация в модель. Конвенция «структура → XML»
описана в docs/schema-v1.md.

XML читается потоково (iterparse + clear), поэтому память не зависит от
размера чанка.
"""

from __future__ import annotations

import json
import re
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, Iterator

from .model import (
    Configuration,
    Field,
    MetadataObject,
    TabularPart,
    normalize_common_module_binding,
)
from .resource_limits import (
    ARCHIVE_LIMITS,
    LimitedReader,
    LimitedZipFile,
    ResourceBudget,
    ResourceLimitError,
)

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
        "period_adjustment_length",
        "max_ext_dimension_count",
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
        "correspondence",
        "action_period",
        "base_period",
        "action_period_use",
        "distributed_infobase",
        "global",
        "server",
        "client_managed",
        "server_call",
        "privileged",
        "external_connection",
        "use",
        "is_predefined",
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
_CHUNK_PATH = re.compile(r"^objects/[^/]+\.(\d{3})\.(json|xml)$")


class ExportError(Exception):
    """Выгрузка не может быть прочитана."""


# ---------------------------------------------------------------- источник


class _Source:
    """Доступ к файлам выгрузки — одинаковый для ZIP и каталога."""

    def __init__(self, path: Path):
        self.path = path
        self._zip: LimitedZipFile | None = None
        self._budget: ResourceBudget | None = None
        self._root = ""

        if path.is_dir():
            files = [p for p in path.rglob("*") if p.is_file()]
            self._names = [str(p.relative_to(path)).replace("\\", "/") for p in files]
            self._budget = ResourceBudget(ARCHIVE_LIMITS, "каталог выгрузки")
            self._budget.validate_members(
                (name, file.stat().st_size, file.stat().st_size)
                for name, file in zip(self._names, files)
            )
        elif zipfile.is_zipfile(path):
            self._zip = LimitedZipFile(path, label="ZIP выгрузки schema v1")
            self._names = self._zip.namelist()
            self._root = _common_root(self._names)
        else:
            raise ExportError(f"Не ZIP и не каталог: {path}")

    def exists(self, name: str) -> bool:
        return (self._root + name) in self._names

    def open(self, name: str) -> IO[bytes]:
        full = self._root + name
        if self._zip is not None:
            return self._zip.open(full)
        assert self._budget is not None
        return LimitedReader(
            (self.path / full).open("rb"),
            self._budget,
            full,
        )

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


def _read_objects_xml(
    stream: IO[bytes], *, schema_version: str, spec: "_ChunkSpec"
) -> Iterator[dict[str, Any]]:
    """Проверить XML-envelope и потоково читать по одному объекту."""
    root: ET.Element | None = None
    for event, element in ET.iterparse(stream, events=("start", "end")):
        if root is None:
            root = element
            if event != "start" or root.tag != "objects":
                raise ExportError(f"{spec.path}: корень чанка должен быть <objects>.")
            _validate_chunk_envelope(
                dict(root.attrib), "xml", schema_version=schema_version, spec=spec
            )
            continue
        if event != "end" or element.tag != "object":
            continue
        yield _xml_dict(element)
        element.clear()


def _read_manifest_xml(stream: IO[bytes]) -> dict[str, Any]:
    root = ET.parse(stream).getroot()
    if root.tag != "manifest":
        raise ExportError("Корень manifest.xml должен быть <manifest>.")
    manifest = _xml_dict(root)
    files = root.find("files")
    if files is not None and len(files) == 0:
        manifest["files"] = []
    return manifest


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
        normalized = _coerce(key, value)
        if key == "handler" and isinstance(normalized, str):
            normalized = normalize_common_module_binding(normalized)
        obj.props[key] = normalized

    return obj


# --------------------------------------------------------- контракт schema v1


@dataclass(frozen=True, slots=True)
class _ChunkSpec:
    path: str
    kind: str
    chunk: int
    count: int


@dataclass(frozen=True, slots=True)
class _ManifestContract:
    schema_version: str
    objects_total: int
    truncated: bool
    files: tuple[_ChunkSpec, ...]


def _required_string(raw: dict[str, Any], key: str, where: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ExportError(f"{where}: обязательное поле `{key}` должно быть строкой.")
    return value.strip()


def _contract_int(value: Any, *, key: str, where: str, fmt: str) -> int:
    if isinstance(value, bool):
        raise ExportError(f"{where}: `{key}` должно быть неотрицательным целым.")
    if fmt == "json":
        if type(value) is not int:
            raise ExportError(f"{where}: `{key}` должно быть неотрицательным целым.")
        result = value
    else:
        text = value if isinstance(value, str) else ""
        if not text.isdecimal():
            raise ExportError(f"{where}: `{key}` должно быть неотрицательным целым.")
        result = int(text)
    if result < 0:
        raise ExportError(f"{where}: `{key}` должно быть неотрицательным целым.")
    return result


def _contract_bool(value: Any, *, key: str, where: str, fmt: str) -> bool:
    if fmt == "json":
        if type(value) is not bool:
            raise ExportError(f"{where}: `{key}` должно быть true или false.")
        return value
    if value not in ("true", "false"):
        raise ExportError(f"{where}: `{key}` должно быть true или false.")
    return value == "true"


def _schema_version(raw: dict[str, Any], where: str) -> str:
    version = _required_string(raw, "schema_version", where)
    if not re.fullmatch(r"1(?:\.\d+)*", version):
        raise ExportError(
            f"{where}: неизвестная schema_version `{version}`; "
            "загрузчик умеет только 1.x."
        )
    return version


def _validate_manifest(
    source: _Source, fmt: str, manifest: Any
) -> _ManifestContract:
    """Единая fail-closed проверка манифеста до создания модели."""
    where = f"manifest.{fmt}"
    if not isinstance(manifest, dict):
        raise ExportError(f"{where}: корень должен быть объектом.")

    schema_version = _schema_version(manifest, where)
    declared_format = _required_string(manifest, "format", where)
    if declared_format != fmt:
        raise ExportError(
            f"{where}: format=`{declared_format}` не совпадает с расширением `{fmt}`."
        )
    _required_string(manifest, "name", where)
    objects_total = _contract_int(
        manifest.get("objects_total"), key="objects_total", where=where, fmt=fmt
    )
    truncated = _contract_bool(
        manifest.get("truncated"), key="truncated", where=where, fmt=fmt
    )

    raw_files = manifest.get("files")
    if not isinstance(raw_files, list):
        raise ExportError(f"{where}: `files` должен быть массивом.")

    files: list[_ChunkSpec] = []
    paths: set[str] = set()
    for number, entry in enumerate(raw_files, 1):
        entry_where = f"{where}, files[{number}]"
        if not isinstance(entry, dict):
            raise ExportError(f"{entry_where}: ожидается объект.")
        path = _required_string(entry, "path", entry_where)
        match = _CHUNK_PATH.fullmatch(path)
        if match is None or match.group(2) != fmt:
            raise ExportError(
                f"{entry_where}: path должен иметь вид `objects/name.001.{fmt}`."
            )
        if path in paths:
            raise ExportError(f"{entry_where}: path `{path}` повторяется.")
        paths.add(path)
        if not source.exists(path):
            raise ExportError(f"Файл из манифеста отсутствует в выгрузке: {path}")
        kind = _required_string(entry, "type", entry_where)
        count = _contract_int(
            entry.get("count"), key="count", where=entry_where, fmt=fmt
        )
        files.append(
            _ChunkSpec(path=path, kind=kind, chunk=int(match.group(1)), count=count)
        )

    declared_by_files = sum(file.count for file in files)
    if declared_by_files != objects_total:
        raise ExportError(
            f"{where}: objects_total={objects_total}, но сумма files[].count="
            f"{declared_by_files}."
        )
    return _ManifestContract(
        schema_version=schema_version,
        objects_total=objects_total,
        truncated=truncated,
        files=tuple(files),
    )


def _validate_chunk_envelope(
    chunk: Any,
    fmt: str,
    *,
    schema_version: str,
    spec: _ChunkSpec,
) -> None:
    where = spec.path
    if not isinstance(chunk, dict):
        raise ExportError(f"{where}: корень чанка должен быть объектом.")
    actual_schema = _schema_version(chunk, where)
    if actual_schema != schema_version:
        raise ExportError(
            f"{where}: schema_version `{actual_schema}` не совпадает с "
            f"манифестом `{schema_version}`."
        )
    kind = _required_string(chunk, "type", where)
    if kind != spec.kind:
        raise ExportError(
            f"{where}: type `{kind}` не совпадает с files[].type `{spec.kind}`."
        )
    chunk_number = _contract_int(
        chunk.get("chunk"), key="chunk", where=where, fmt=fmt
    )
    if chunk_number != spec.chunk:
        raise ExportError(
            f"{where}: chunk={chunk_number}, но имя файла задаёт {spec.chunk}."
        )
    count = _contract_int(chunk.get("count"), key="count", where=where, fmt=fmt)
    if count != spec.count:
        raise ExportError(
            f"{where}: count={count} не совпадает с files[].count={spec.count}."
        )


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
    try:
        source = _Source(Path(path))
    except ResourceLimitError as error:
        raise ExportError(str(error)) from error
    try:
        fmt, manifest = _read_manifest(source)
        contract = _validate_manifest(source, fmt, manifest)
        return ExportInfo(
            path=Path(path),
            fmt=fmt,
            name=manifest.get("name", ""),
            version=manifest.get("version", ""),
            platform=manifest.get("platform", ""),
            exported_at=manifest.get("exported_at", ""),
            objects_total=contract.objects_total,
            truncated=contract.truncated,
            predefined_available=_as_bool(manifest.get("predefined_available"), True),
            warnings=_manifest_warnings(manifest),
        )
    except ResourceLimitError as error:
        raise ExportError(str(error)) from error
    finally:
        source.close()


def load(path: str | Path, *, allow_truncated: bool = False) -> Configuration:
    """Прочитать schema v1; усечённую выгрузку — только по явному opt-in."""
    try:
        source = _Source(Path(path))
    except ResourceLimitError as error:
        raise ExportError(str(error)) from error
    try:
        fmt, manifest = _read_manifest(source)
        contract = _validate_manifest(source, fmt, manifest)
        if contract.truncated and not allow_truncated:
            raise ExportError(
                "Выгрузка помечена truncated=true и не является полной. "
                "Рабочая загрузка запрещена; нужен явный режим allow_truncated."
            )

        config = Configuration(
            name=manifest.get("name", ""),
            synonym=manifest.get("synonym", "") or "",
            version=manifest.get("version", ""),
            vendor=manifest.get("vendor", "") or "",
            platform=manifest.get("platform", ""),
            exported_at=manifest.get("exported_at", ""),
            exporter_version=manifest.get("exporter_version", ""),
            schema_version=contract.schema_version,
            source_format=fmt,
            truncated=contract.truncated,
            predefined_available=_as_bool(manifest.get("predefined_available"), True),
            warnings=_manifest_warnings(manifest),
        )

        нарушения: list[str] = []
        нарушений_всего = 0
        full_names: set[str] = set()

        for spec in contract.files:
            with source.open(spec.path) as stream:
                if fmt == "json":
                    chunk = json.loads(stream.read().decode("utf-8-sig"))
                    _validate_chunk_envelope(
                        chunk,
                        fmt,
                        schema_version=contract.schema_version,
                        spec=spec,
                    )
                    objects = chunk.get("objects")
                    if not isinstance(objects, list):
                        raise ExportError(f"{spec.path}: `objects` должен быть массивом.")
                    raw_objects: Iterator[dict[str, Any]] = iter(objects)
                else:
                    raw_objects = _read_objects_xml(
                        stream,
                        schema_version=contract.schema_version,
                        spec=spec,
                    )

                actual_count = 0
                for raw in raw_objects:
                    actual_count += 1
                    if not isinstance(raw, dict):
                        raise ExportError(
                            f"{spec.path}, объект {actual_count}: ожидается объект."
                        )
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
                        raise ExportError(
                            f"{spec.path}, объект {actual_count}: `full_name` обязателен."
                        )
                    if obj.kind != spec.kind:
                        raise ExportError(
                            f"{spec.path}, `{obj.full_name}`: type `{obj.kind}` "
                            f"не совпадает с типом чанка `{spec.kind}`."
                        )
                    if obj.full_name in full_names:
                        raise ExportError(
                            f"Полный идентификатор `{obj.full_name}` повторяется в выгрузке."
                        )
                    full_names.add(obj.full_name)
                    config.objects[obj.full_name] = obj

                if actual_count != spec.count:
                    raise ExportError(
                        f"{spec.path}: count={spec.count}, фактически объектов "
                        f"{actual_count}."
                    )

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

        if contract.objects_total != len(config.objects):
            raise ExportError(
                f"Манифест обещает {contract.objects_total} объектов, прочитано "
                f"{len(config.objects)}."
            )

        return config
    except ResourceLimitError as error:
        raise ExportError(str(error)) from error
    finally:
        source.close()
