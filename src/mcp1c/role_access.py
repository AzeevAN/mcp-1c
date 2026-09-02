"""Расходный SQLite-индекс объявленных прав ролей generation V2.

Канонический слой ролей остаётся единственным доказательством. Этот модуль
потоково строит из него компактный прямой и обратный индекс, который можно
безопасно удалить и восстановить без исходного ZIP. В память процесса не
поднимается полный нормализованный корпус прав.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import sqlite3
import stat
import tempfile
import threading
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Iterable, Mapping
from urllib.parse import quote

from .intake_v2 import GenerationManifest, LayerKind, LayerManifest, LayerState
from .intake_v2_registry import (
    BundleStoreError,
    LayerMember,
    LayerPayload,
    hash_layer_payload,
    hash_layer_semantic,
    load_layer_payload,
)


_CACHE_FORMAT = 2
_MAX_CACHE_BYTES = 512 * 1024 * 1024
_MAX_ROLE_XML_BYTES = 256 * 1024 * 1024
_MAX_ROLE_NAME = 512
_MAX_TEXT = 16 * 1024 * 1024
_DESCRIPTOR_NAMESPACE = "http://v8.1c.ru/8.3/MDClasses"
_CORE_NAMESPACE = "http://v8.1c.ru/8.1/data/core"
_RIGHTS_NAMESPACE = "http://v8.1c.ru/8.2/roles"

# Операции означают только проверку одноимённого базового права платформы.
# Интерактивные права не подмешиваются эвристически: потребитель видит точное
# сопоставление и не получает обещание эффективного доступа пользователя.
OPERATION_RIGHTS: Mapping[str, str] = {
    "read": "Read",
    "update": "Update",
    "insert": "Insert",
    "delete": "Delete",
    "posting": "Posting",
    "use": "Use",
}

_PUBLIC_TO_SOURCE_KIND = {
    "РегистрНакопления": "AccumulationRegister",
    "Справочник": "Catalog",
    "ПланВидовХарактеристик": "ChartOfCharacteristicTypes",
    "ПланСчетов": "ChartOfAccounts",
    "ПланВидовРасчета": "ChartOfCalculationTypes",
    "ОбщаяКоманда": "CommonCommand",
    "ОбщаяФорма": "CommonForm",
    "ОбщийМодуль": "CommonModule",
    "Конфигурация": "Configuration",
    "Константа": "Constant",
    "Обработка": "DataProcessor",
    "Документ": "Document",
    "ЖурналДокументов": "DocumentJournal",
    "Перечисление": "Enum",
    "ПланОбмена": "ExchangePlan",
    "КритерийОтбора": "FilterCriterion",
    "HTTPСервис": "HTTPService",
    "РегистрСведений": "InformationRegister",
    "Отчет": "Report",
    "WebСервис": "WebService",
    "РегистрБухгалтерии": "AccountingRegister",
    "РегистрРасчета": "CalculationRegister",
    "БизнесПроцесс": "BusinessProcess",
    "Задача": "Task",
    "ОпределяемыйТип": "DefinedType",
    "ПодпискаНаСобытие": "EventSubscription",
    "РегламентноеЗадание": "ScheduledJob",
    "ПараметрСеанса": "SessionParameter",
    "ОбщийРеквизит": "CommonAttribute",
    "Подсистема": "Subsystem",
}


class RoleAccessError(RuntimeError):
    """Канонический снимок или расходный индекс ролей недоступен."""


@dataclass(frozen=True, slots=True)
class RoleDescriptor:
    uuid: str
    name: str
    synonyms: tuple[tuple[str, str], ...]
    comment: str
    xml_version: str
    set_for_new_objects: bool
    set_for_attributes_by_default: bool
    independent_rights_of_child_objects: bool


@dataclass(frozen=True, slots=True)
class RoleRestriction:
    condition: str
    field: str = ""


@dataclass(frozen=True, slots=True)
class RoleRestrictionReference:
    id: int
    field: str
    chars: int
    bytes: int


@dataclass(frozen=True, slots=True)
class RoleTemplateReference:
    id: int
    name: str
    chars: int
    bytes: int


@dataclass(frozen=True, slots=True)
class RoleTemplatePage:
    templates: tuple[RoleTemplateReference, ...]
    total: int
    offset: int
    next_offset: int | None


@dataclass(frozen=True, slots=True)
class RoleTextPage:
    kind: str
    id: int
    role: str
    content: str
    total_chars: int
    total_bytes: int
    offset: int
    next_offset: int | None
    field: str = ""
    template: str = ""
    target: str = ""
    right: str = ""


@dataclass(frozen=True, slots=True)
class DeclaredRight:
    target: str
    name: str
    value: bool
    has_restrictions: bool = False
    restrictions: tuple[RoleRestriction, ...] = ()
    restriction_refs: tuple[RoleRestrictionReference, ...] = ()

    @property
    def conditional(self) -> bool:
        return self.has_restrictions


@dataclass(frozen=True, slots=True)
class RoleAccessPage:
    role: RoleDescriptor
    rights: tuple[DeclaredRight, ...]
    templates: tuple[tuple[str, str], ...]
    total: int
    offset: int
    next_offset: int | None
    template_refs: tuple[RoleTemplateReference, ...] = ()


@dataclass(frozen=True, slots=True)
class RoleIndexSummary:
    roles: int
    targets: int
    rights: int
    restrictions: int
    templates: int
    conditions: int


@dataclass(frozen=True, slots=True)
class RoleCandidate:
    role: RoleDescriptor
    matched_operations: tuple[str, ...]
    missing_operations: tuple[str, ...]
    conditional_operations: tuple[str, ...]
    denied_operations: tuple[str, ...]
    matched_rights: tuple[DeclaredRight, ...]

    @property
    def complete(self) -> bool:
        return not self.missing_operations


@dataclass(frozen=True, slots=True)
class RoleAccessResolution:
    source_target: str
    checked_rights: tuple[tuple[str, str], ...]
    candidates: tuple[RoleCandidate, ...]
    minimal_role_set: tuple[str, ...]
    minimum_proof: str
    warnings: tuple[str, ...]
    candidates_total: int = 0
    conditional_candidates_excluded: int = 0
    offset: int = 0
    next_offset: int | None = None


@dataclass(frozen=True, slots=True)
class LoadedRoleAccess:
    """Состояние ролевого provider-а в одном поколении Registry."""

    state: str
    generation_id: str
    source_sha256: str
    content_sha256: str
    items_total: int
    index: "RoleAccessIndex | None" = None
    error: str = ""

    def __post_init__(self) -> None:
        if self.state not in {"ready", "error"}:
            raise ValueError("state ролевого provider должен быть ready|error")
        if self.state == "ready" and (self.index is None or self.error):
            raise ValueError("ready ролевой provider обязан содержать index")
        if self.state == "error" and (self.index is not None or not self.error):
            raise ValueError("error ролевой provider обязан содержать причину")

    @property
    def ready(self) -> bool:
        return self.state == "ready"


@dataclass(frozen=True, slots=True)
class _ParsedDescriptor:
    uuid: str
    name: str
    synonyms: tuple[tuple[str, str], ...]
    comment: str
    xml_version: str


class _HashingReader:
    def __init__(self, stream: BinaryIO, limit: int, label: str):
        self.stream = stream
        self.limit = limit
        self.label = label
        self.size = 0
        self.digest = hashlib.sha256()

    def read(self, size: int = -1) -> bytes:
        block = self.stream.read(size)
        self.size += len(block)
        if self.size > self.limit:
            raise RoleAccessError(f"{self.label} превышает предел размера")
        self.digest.update(block)
        return block

    def readable(self) -> bool:
        return True


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _namespace(tag: str) -> str:
    return tag[1:].split("}", 1)[0] if tag.startswith("{") else ""


def _children(element: ET.Element, name: str) -> list[ET.Element]:
    return [child for child in element if _local(child.tag) == name]


def _single(element: ET.Element, name: str, label: str) -> ET.Element:
    found = _children(element, name)
    if len(found) != 1:
        raise RoleAccessError(f"{label}: ожидается ровно один {name}")
    return found[0]


def _text(element: ET.Element, label: str, *, required: bool = True) -> str:
    value = "".join(element.itertext()).strip()
    if required and not value:
        raise RoleAccessError(f"{label}: требуется непустой текст")
    if len(value.encode("utf-8")) > _MAX_TEXT:
        raise RoleAccessError(f"{label}: текст превышает предел")
    return value


def _bool(element: ET.Element, label: str) -> bool:
    value = _text(element, label).casefold()
    if value == "true":
        return True
    if value == "false":
        return False
    raise RoleAccessError(f"{label}: ожидается true или false")


def _parse_descriptor(stream: BinaryIO, expected_name: str) -> _ParsedDescriptor:
    try:
        root = ET.parse(stream).getroot()
    except ET.ParseError as error:
        raise RoleAccessError("descriptor роли содержит неверный XML") from error
    if _local(root.tag) != "MetaDataObject":
        raise RoleAccessError("descriptor роли имеет неверный корень")
    descriptor_tags = {"MetaDataObject", "Role", "Properties", "Name", "Synonym", "Comment"}
    core_tags = {"item", "lang", "content"}
    for element in root.iter():
        local_name = _local(element.tag)
        namespace = _namespace(element.tag)
        if (
            local_name in descriptor_tags
            and namespace != _DESCRIPTOR_NAMESPACE
        ) or (local_name in core_tags and namespace != _CORE_NAMESPACE):
            raise RoleAccessError("descriptor роли содержит неверное пространство имён")
        if local_name not in descriptor_tags | core_tags:
            raise RoleAccessError("descriptor роли содержит неизвестный элемент")
    role = _single(root, "Role", "descriptor роли")
    properties = _single(role, "Properties", "descriptor роли")
    name = _text(_single(properties, "Name", "descriptor роли"), "имя роли")
    if name != expected_name:
        raise RoleAccessError("имя роли не совпадает с путём canonical snapshot")
    if len(name) > _MAX_ROLE_NAME:
        raise RoleAccessError("имя роли превышает предел")
    uuid = role.attrib.get("uuid", "").strip()
    if not uuid:
        raise RoleAccessError("descriptor роли не содержит UUID")
    synonyms: list[tuple[str, str]] = []
    synonym_nodes = _children(properties, "Synonym")
    if len(synonym_nodes) > 1:
        raise RoleAccessError("descriptor роли дублирует Synonym")
    if synonym_nodes:
        for item in _children(synonym_nodes[0], "item"):
            language = _text(_single(item, "lang", "синоним роли"), "язык")
            content = _text(
                _single(item, "content", "синоним роли"), "текст синонима"
            )
            synonyms.append((language, content))
    comment_nodes = _children(properties, "Comment")
    if len(comment_nodes) > 1:
        raise RoleAccessError("descriptor роли дублирует Comment")
    comment = _text(comment_nodes[0], "комментарий", required=False) if comment_nodes else ""
    return _ParsedDescriptor(
        uuid=uuid,
        name=name,
        synonyms=tuple(sorted(set(synonyms))),
        comment=comment,
        xml_version=root.attrib.get("version", ""),
    )


def _safe_member(root: Path, relative_path: str) -> tuple[BinaryIO, os.stat_result]:
    path = PurePosixPath(relative_path)
    if (
        not relative_path
        or "\\" in relative_path
        or path.is_absolute()
        or path.as_posix() != relative_path
        or any(part in ("", ".", "..") for part in path.parts)
    ):
        raise RoleAccessError("role member имеет небезопасный путь")
    current = root
    try:
        for part in path.parts[:-1]:
            current = current / part
            value = current.lstat()
            if not stat.S_ISDIR(value.st_mode) or stat.S_ISLNK(value.st_mode):
                raise RoleAccessError("role member проходит через недоверенный каталог")
        target = current / path.parts[-1]
        before = target.lstat()
        if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode):
            raise RoleAccessError("role member должен быть обычным файлом")
        descriptor = os.open(target, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode):
            os.close(descriptor)
            raise RoleAccessError("role member должен быть обычным файлом")
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
        ):
            os.close(descriptor)
            raise RoleAccessError("role member изменился во время открытия")
        return os.fdopen(descriptor, "rb"), opened
    except RoleAccessError:
        raise
    except OSError as error:
        raise RoleAccessError("role member недоступен") from error


def _verified_xml_stream(
    root: Path,
    member: LayerMember,
    raw_size: int,
    raw_sha256: str,
):
    class _Context:
        def __enter__(self):
            source, opened = _safe_member(root, member.relative_path)
            self.source = source
            self.opened = opened
            self.compressed = _HashingReader(
                source, member.size + 1, f"member {member.key}"
            )
            self.gzip = gzip.GzipFile(fileobj=self.compressed, mode="rb")
            self.raw = _HashingReader(
                self.gzip,
                min(raw_size, _MAX_ROLE_XML_BYTES) + 1,
                f"XML {member.key}",
            )
            return self.raw

        def __exit__(self, exc_type, exc, traceback):
            try:
                if exc is None:
                    if self.raw.read(1):
                        raise RoleAccessError(
                            f"XML {member.key} превышает заявленный размер"
                        )
                    if (
                        self.raw.size != raw_size
                        or self.raw.digest.hexdigest() != raw_sha256
                    ):
                        raise RoleAccessError(
                            f"XML {member.key} не совпал по размеру или хешу"
                        )
                    if (
                        self.compressed.size != member.size
                        or self.compressed.digest.hexdigest() != member.sha256
                    ):
                        raise RoleAccessError(
                            f"member {member.key} не совпал по размеру или хешу"
                        )
            except (OSError, EOFError, gzip.BadGzipFile) as error:
                raise RoleAccessError(f"member {member.key} содержит неверный gzip") from error
            finally:
                try:
                    self.gzip.close()
                finally:
                    self.source.close()
            return False

    if raw_size > _MAX_ROLE_XML_BYTES:
        raise RoleAccessError(f"XML {member.key} превышает предел размера")
    return _Context()


def _source_target(full_name: str, child_path: str) -> str:
    if not isinstance(full_name, str) or full_name.strip() != full_name:
        raise ValueError("full_name должен быть точным именем объекта")
    root = full_name.split(".")
    if len(root) != 2 or not all(root):
        raise ValueError("full_name должен иметь вид Вид.Имя")
    root[0] = _PUBLIC_TO_SOURCE_KIND.get(root[0], root[0])
    if child_path:
        if not isinstance(child_path, str) or child_path.strip() != child_path:
            raise ValueError("child_path должен быть точным путём")
        child = child_path.split(".")
        if len(child) % 2 or not all(child):
            raise ValueError("child_path должен состоять из пар Вид.Имя")
        root.extend(child)
    return ".".join(root)


def source_target(full_name: str, child_path: str = "") -> str:
    """Публичное точное сопоставление адреса объекта и source-B пути."""
    return _source_target(full_name, child_path)


@lru_cache(maxsize=1)
def _code_digest() -> str:
    digest = hashlib.sha256()
    for path in sorted(Path(__file__).parent.glob("*.py")):
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _stamp(layer: LayerManifest) -> str:
    return json.dumps(
        {
            "format": _CACHE_FORMAT,
            "code": _code_digest(),
            "content": layer.content_sha256,
            "payload": layer.payload_sha256,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


_SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;
CREATE TABLE roles (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    name_key TEXT NOT NULL UNIQUE,
    uuid TEXT NOT NULL UNIQUE,
    comment TEXT NOT NULL,
    xml_version TEXT NOT NULL,
    set_for_new INTEGER NOT NULL CHECK(set_for_new IN (0, 1)),
    set_for_attributes INTEGER NOT NULL CHECK(set_for_attributes IN (0, 1)),
    independent_children INTEGER NOT NULL CHECK(independent_children IN (0, 1))
);
CREATE TABLE role_synonyms (
    role_id INTEGER NOT NULL REFERENCES roles(id),
    language TEXT NOT NULL,
    content TEXT NOT NULL,
    PRIMARY KEY(role_id, language, content)
) WITHOUT ROWID;
CREATE TABLE targets (
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL,
    path_key TEXT NOT NULL UNIQUE
);
CREATE TABLE right_names (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    name_key TEXT NOT NULL UNIQUE
);
CREATE TABLE conditions (
    id INTEGER PRIMARY KEY,
    content TEXT NOT NULL UNIQUE
);
CREATE TABLE rights (
    id INTEGER PRIMARY KEY,
    role_id INTEGER NOT NULL REFERENCES roles(id),
    target_id INTEGER NOT NULL REFERENCES targets(id),
    right_name_id INTEGER NOT NULL REFERENCES right_names(id),
    value INTEGER NOT NULL CHECK(value IN (0, 1)),
    conditional INTEGER NOT NULL CHECK(conditional IN (0, 1)),
    UNIQUE(role_id, target_id, right_name_id)
);
CREATE TABLE restrictions (
    id INTEGER PRIMARY KEY,
    right_id INTEGER NOT NULL REFERENCES rights(id),
    condition_id INTEGER NOT NULL REFERENCES conditions(id),
    field_target_id INTEGER REFERENCES targets(id)
);
CREATE TABLE templates (
    id INTEGER PRIMARY KEY,
    role_id INTEGER NOT NULL REFERENCES roles(id),
    name TEXT NOT NULL,
    name_key TEXT NOT NULL,
    condition_id INTEGER NOT NULL REFERENCES conditions(id),
    UNIQUE(role_id, name_key)
);
CREATE INDEX rights_by_target ON rights(target_id, right_name_id, value, conditional, role_id);
CREATE INDEX rights_by_role ON rights(role_id, target_id, right_name_id);
CREATE INDEX restrictions_by_right ON restrictions(right_id);
CREATE UNIQUE INDEX restrictions_unique
    ON restrictions(right_id, condition_id, COALESCE(field_target_id, 0));
"""


def _id_for(connection: sqlite3.Connection, table: str, value: str) -> int:
    if table == "targets":
        name_column, key_column = "path", "path_key"
    elif table == "right_names":
        name_column, key_column = "name", "name_key"
    else:
        raise AssertionError(table)
    key = value.casefold()
    connection.execute(
        f"INSERT OR IGNORE INTO {table}({name_column}, {key_column}) VALUES (?, ?)",
        (value, key),
    )
    row = connection.execute(
        f"SELECT id, {name_column} FROM {table} WHERE {key_column} = ?", (key,)
    ).fetchone()
    assert row is not None
    if row[1] != value:
        raise RoleAccessError(f"{table} содержит имена, различающиеся только регистром")
    return int(row[0])


def _condition_id(connection: sqlite3.Connection, value: str) -> int:
    connection.execute(
        "INSERT OR IGNORE INTO conditions(content) VALUES (?)", (value,)
    )
    row = connection.execute(
        "SELECT id FROM conditions WHERE content = ?", (value,)
    ).fetchone()
    assert row is not None
    return int(row[0])


def _insert_right(
    connection: sqlite3.Connection,
    role_id: int,
    target: str,
    element: ET.Element,
) -> None:
    name = _text(_single(element, "name", "право роли"), "имя права")
    value = _bool(_single(element, "value", "право роли"), "значение права")
    restriction_nodes = _children(element, "restrictionByCondition")
    target_id = _id_for(connection, "targets", target)
    right_name_id = _id_for(connection, "right_names", name)
    row = connection.execute(
        "SELECT id, value FROM rights WHERE role_id=? AND target_id=? AND right_name_id=?",
        (role_id, target_id, right_name_id),
    ).fetchone()
    if row is None:
        cursor = connection.execute(
            "INSERT INTO rights(role_id,target_id,right_name_id,value,conditional) "
            "VALUES (?,?,?,?,?)",
            (role_id, target_id, right_name_id, int(value), int(bool(restriction_nodes))),
        )
        right_id = int(cursor.lastrowid)
    else:
        right_id = int(row[0])
        if bool(row[1]) != value:
            raise RoleAccessError("роль содержит противоречащие значения одного права")
        if restriction_nodes:
            connection.execute(
                "UPDATE rights SET conditional=1 WHERE id=?", (right_id,)
            )
    for restriction in restriction_nodes:
        condition = _text(
            _single(restriction, "condition", "ограничение права"),
            "условие ограничения",
        )
        fields = _children(restriction, "field")
        if len(fields) > 1:
            raise RoleAccessError("ограничение права дублирует field")
        field_id = (
            _id_for(connection, "targets", _text(fields[0], "field ограничения"))
            if fields
            else None
        )
        connection.execute(
            "INSERT OR IGNORE INTO restrictions(right_id,condition_id,field_target_id) "
            "VALUES (?,?,?)",
            (right_id, _condition_id(connection, condition), field_id),
        )


def _parse_rights(
    stream: BinaryIO,
    connection: sqlite3.Connection,
    role_id: int,
) -> tuple[bool, bool, bool]:
    flags: dict[str, bool] = {}
    root: ET.Element | None = None
    try:
        for event, element in ET.iterparse(stream, events=("start", "end")):
            name = _local(element.tag)
            if event == "start":
                if root is None:
                    root = element
                    if name != "Rights" or _namespace(element.tag) != _RIGHTS_NAMESPACE:
                        raise RoleAccessError("Rights.xml имеет неверный корень")
                elif _namespace(element.tag) != _RIGHTS_NAMESPACE:
                    raise RoleAccessError("Rights.xml содержит неверное пространство имён")
                continue
            if name in {
                "setForNewObjects",
                "setForAttributesByDefault",
                "independentRightsOfChildObjects",
            }:
                if name in flags:
                    raise RoleAccessError(f"Rights.xml дублирует {name}")
                flags[name] = _bool(element, name)
            elif name == "object":
                target = _text(
                    _single(element, "name", "объект права"), "путь объекта права"
                )
                for right in _children(element, "right"):
                    _insert_right(connection, role_id, target, right)
                if root is not None:
                    root.clear()
            elif name == "restrictionTemplate":
                template_name = _text(
                    _single(element, "name", "шаблон ограничения"),
                    "имя шаблона ограничения",
                )
                condition = _text(
                    _single(element, "condition", "шаблон ограничения"),
                    "условие шаблона ограничения",
                )
                condition_id = _condition_id(connection, condition)
                existing = connection.execute(
                    "SELECT name, condition_id FROM templates "
                    "WHERE role_id=? AND name_key=?",
                    (role_id, template_name.casefold()),
                ).fetchone()
                if existing is not None and (
                    existing[0] != template_name or int(existing[1]) != condition_id
                ):
                    raise RoleAccessError("роль содержит противоречащий шаблон")
                connection.execute(
                    "INSERT OR IGNORE INTO templates(role_id,name,name_key,condition_id) "
                    "VALUES (?,?,?,?)",
                    (role_id, template_name, template_name.casefold(), condition_id),
                )
                if root is not None:
                    root.clear()
    except ET.ParseError as error:
        raise RoleAccessError("Rights.xml содержит неверный XML") from error
    required = {
        "setForNewObjects",
        "setForAttributesByDefault",
        "independentRightsOfChildObjects",
    }
    if set(flags) != required:
        raise RoleAccessError("Rights.xml не содержит полный набор default-флагов")
    return (
        flags["setForNewObjects"],
        flags["setForAttributesByDefault"],
        flags["independentRightsOfChildObjects"],
    )


def _artifact_rows(payload: LayerPayload) -> dict[str, tuple[int, str]]:
    semantic = payload.semantic
    if set(semantic) != {"roles_total", "artifacts"}:
        raise RoleAccessError("roles semantic содержит неверные поля")
    total = semantic["roles_total"]
    artifacts = semantic["artifacts"]
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise RoleAccessError("roles_total имеет неверное значение")
    if not isinstance(artifacts, list):
        raise RoleAccessError("roles artifacts должен быть массивом")
    result: dict[str, tuple[int, str]] = {}
    for raw in artifacts:
        if not isinstance(raw, dict) or set(raw) != {"source_path", "size", "sha256"}:
            raise RoleAccessError("roles artifact содержит неверные поля")
        source_path, size, digest = raw["source_path"], raw["size"], raw["sha256"]
        if (
            not isinstance(source_path, str)
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
            or not isinstance(digest, str)
            or len(digest) != 64
        ):
            raise RoleAccessError("roles artifact содержит неверные значения")
        if source_path in result:
            raise RoleAccessError("roles semantic дублирует source_path")
        result[source_path] = size, digest
    if len(result) != total * 2:
        raise RoleAccessError("roles semantic не содержит точные пары XML")
    return result


def _role_pairs(paths: Iterable[str], roles_total: int) -> tuple[tuple[str, str, str], ...]:
    descriptors: dict[str, str] = {}
    rights: dict[str, str] = {}
    for source_path in paths:
        parts = PurePosixPath(source_path).parts
        if len(parts) == 2 and parts[0] == "Roles" and parts[1].endswith(".xml"):
            name = parts[1][:-4]
            descriptors[name] = source_path
        elif len(parts) == 4 and parts[0] == "Roles" and parts[2:] == ("Ext", "Rights.xml"):
            rights[parts[1]] = source_path
        else:
            raise RoleAccessError("roles layer содержит посторонний member")
    if set(descriptors) != set(rights) or len(descriptors) != roles_total:
        raise RoleAccessError("roles layer не содержит точные пары descriptor/Rights")
    return tuple(
        (name, descriptors[name], rights[name])
        for name in sorted(descriptors, key=lambda value: (value.casefold(), value))
    )


def _build_database(
    path: Path,
    root: Path,
    layer: LayerManifest,
    payload: LayerPayload,
) -> None:
    artifacts = _artifact_rows(payload)
    members = {member.key: member for member in payload.members}
    if set(members) != set(artifacts):
        raise RoleAccessError("roles semantic и members расходятся")
    roles_total = int(payload.semantic["roles_total"])
    pairs = _role_pairs(members, roles_total)
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA journal_mode=OFF")
        connection.execute("PRAGMA synchronous=OFF")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript(_SCHEMA)
        with connection:
            for role_name, descriptor_path, rights_path in pairs:
                descriptor_member = members[descriptor_path]
                descriptor_size, descriptor_sha = artifacts[descriptor_path]
                try:
                    with _verified_xml_stream(
                        root,
                        descriptor_member,
                        descriptor_size,
                        descriptor_sha,
                    ) as stream:
                        descriptor = _parse_descriptor(stream, role_name)
                except (OSError, EOFError, gzip.BadGzipFile) as error:
                    raise RoleAccessError(
                        f"descriptor member {descriptor_path} повреждён"
                    ) from error
                cursor = connection.execute(
                    "INSERT INTO roles(name,name_key,uuid,comment,xml_version,"
                    "set_for_new,set_for_attributes,independent_children) "
                    "VALUES (?,?,?,?,?,0,0,0)",
                    (
                        descriptor.name,
                        descriptor.name.casefold(),
                        descriptor.uuid,
                        descriptor.comment,
                        descriptor.xml_version,
                    ),
                )
                role_id = int(cursor.lastrowid)
                connection.executemany(
                    "INSERT INTO role_synonyms(role_id,language,content) VALUES (?,?,?)",
                    ((role_id, language, content) for language, content in descriptor.synonyms),
                )
                rights_member = members[rights_path]
                rights_size, rights_sha = artifacts[rights_path]
                try:
                    with _verified_xml_stream(
                        root, rights_member, rights_size, rights_sha
                    ) as stream:
                        flags = _parse_rights(stream, connection, role_id)
                except (OSError, EOFError, gzip.BadGzipFile) as error:
                    raise RoleAccessError(
                        f"rights member {rights_path} повреждён"
                    ) from error
                connection.execute(
                    "UPDATE roles SET set_for_new=?,set_for_attributes=?,"
                    "independent_children=? WHERE id=?",
                    (*map(int, flags), role_id),
                )
            connection.execute(
                "INSERT INTO meta(key,value) VALUES ('stamp',?)", (_stamp(layer),)
            )
        connection.execute("ANALYZE")
    except (sqlite3.Error, UnicodeError, ValueError) as error:
        if isinstance(error, RoleAccessError):
            raise
        raise RoleAccessError("ролевой индекс не построен") from error
    finally:
        connection.close()


def _open_readonly(path: Path, layer: LayerManifest, *, from_cache: bool, ephemeral: bool):
    connection: sqlite3.Connection | None = None
    try:
        before = path.lstat()
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_ISLNK(before.st_mode)
            or before.st_size > _MAX_CACHE_BYTES
        ):
            raise RoleAccessError("кэш ролей имеет неверный тип или размер")
        uri = f"file:{quote(str(path.resolve()), safe='/')}?mode=ro&immutable=1"
        connection = sqlite3.connect(uri, uri=True, check_same_thread=False)
        connection.row_factory = sqlite3.Row
        if connection.execute("PRAGMA quick_check(1)").fetchone()[0] != "ok":
            raise RoleAccessError("кэш ролей не прошёл quick_check")
        row = connection.execute("SELECT value FROM meta WHERE key='stamp'").fetchone()
        if row is None or row[0] != _stamp(layer):
            raise RoleAccessError("штамп кэша ролей не совпал")
        return RoleAccessIndex(
            connection,
            path,
            from_cache=from_cache,
            ephemeral=ephemeral,
        )
    except RoleAccessError:
        if connection is not None:
            try:
                connection.close()
            except sqlite3.Error:
                pass
        raise
    except (OSError, sqlite3.Error, KeyError, TypeError) as error:
        if connection is not None:
            try:
                connection.close()
            except sqlite3.Error:
                pass
        raise RoleAccessError("кэш ролей повреждён или недоступен") from error


class RoleAccessIndex:
    """Потокобезопасный read-only facade над расходным SQLite."""

    def __init__(
        self,
        connection: sqlite3.Connection,
        path: Path,
        *,
        from_cache: bool,
        ephemeral: bool,
    ):
        self._connection = connection
        self._path = path
        self._lock = threading.Lock()
        self._ephemeral = ephemeral
        self.from_cache = from_cache
        self.summary = self._read_summary()

    @classmethod
    def open_or_build(
        cls,
        root: str | Path,
        layer: LayerManifest,
        cache_path: str | Path | None,
    ) -> "RoleAccessIndex":
        if not isinstance(layer, LayerManifest) or layer.kind is not LayerKind.ROLES:
            raise TypeError("layer должен быть manifest слоя roles")
        if layer.state is not LayerState.READY:
            raise RoleAccessError("role snapshot не готов")
        root = Path(root)
        cache = Path(cache_path) if cache_path is not None else None
        if cache is not None:
            try:
                return _open_readonly(cache, layer, from_cache=True, ephemeral=False)
            except RoleAccessError:
                pass
        try:
            if hash_layer_payload(LayerKind.ROLES, root / layer.relative_path) != layer.payload_sha256:
                raise RoleAccessError("manifest payload role snapshot не совпал по хешу")
            payload = load_layer_payload(root / layer.relative_path)
        except RoleAccessError:
            raise
        except (OSError, BundleStoreError, ValueError) as error:
            raise RoleAccessError("manifest role snapshot повреждён") from error
        if payload.kind is not LayerKind.ROLES:
            raise RoleAccessError("role snapshot содержит envelope другого слоя")
        if hash_layer_semantic(LayerKind.ROLES, payload.semantic) != layer.content_sha256:
            raise RoleAccessError("semantic hash role snapshot не совпал")
        if payload.semantic.get("roles_total") != layer.items_total:
            raise RoleAccessError("roles_total не совпал с manifest слоя")
        temporary: Path | None = None
        ephemeral = False
        complete = False
        try:
            try:
                if cache is None:
                    raise OSError("запрошен ephemeral index")
                cache.parent.mkdir(parents=True, exist_ok=True)
                descriptor, name = tempfile.mkstemp(
                    dir=cache.parent, prefix=f".{cache.name}.", suffix=".tmp"
                )
            except OSError:
                descriptor, name = tempfile.mkstemp(
                    prefix="mcp1c-roles-", suffix=".sqlite"
                )
                ephemeral = True
            os.close(descriptor)
            temporary = Path(name)
            _build_database(temporary, root, layer, payload)
            with temporary.open("rb") as stream:
                os.fsync(stream.fileno())
            if not ephemeral:
                assert cache is not None
                try:
                    os.replace(temporary, cache)
                    temporary = None
                    try:
                        descriptor = os.open(
                            cache.parent,
                            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
                        )
                        try:
                            os.fsync(descriptor)
                        finally:
                            os.close(descriptor)
                    except OSError:
                        pass
                except OSError:
                    ephemeral = True
            target = temporary if ephemeral else cache
            assert target is not None
            result = _open_readonly(
                target,
                layer,
                from_cache=False,
                ephemeral=ephemeral,
            )
            complete = True
            return result
        except RoleAccessError:
            raise
        except (OSError, sqlite3.Error) as error:
            raise RoleAccessError("ролевой индекс не сохранён") from error
        finally:
            if temporary is not None and (not ephemeral or not complete):
                temporary.unlink(missing_ok=True)

    @property
    def path(self) -> Path:
        return self._path

    def _read_summary(self) -> RoleIndexSummary:
        with self._lock:
            values = [
                int(self._connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in (
                    "roles",
                    "targets",
                    "rights",
                    "restrictions",
                    "templates",
                    "conditions",
                )
            ]
        return RoleIndexSummary(*values)

    def _descriptors(self, names: Iterable[str] | None = None) -> dict[str, RoleDescriptor]:
        parameters: tuple[object, ...] = ()
        where = ""
        selected = tuple(names or ())
        if selected:
            placeholders = ",".join("?" for _ in selected)
            where = f"WHERE name_key IN ({placeholders})"
            parameters = tuple(name.casefold() for name in selected)
        with self._lock:
            rows = self._connection.execute(
                "SELECT id,name,name_key,uuid,comment,xml_version,set_for_new,"
                f"set_for_attributes,independent_children FROM roles {where} "
                "ORDER BY name_key,name",
                parameters,
            ).fetchall()
            role_ids = [int(row["id"]) for row in rows]
            synonyms: dict[int, list[tuple[str, str]]] = {role_id: [] for role_id in role_ids}
            if role_ids:
                placeholders = ",".join("?" for _ in role_ids)
                for row in self._connection.execute(
                    "SELECT role_id,language,content FROM role_synonyms "
                    f"WHERE role_id IN ({placeholders}) ORDER BY language,content",
                    role_ids,
                ):
                    synonyms[int(row["role_id"])].append(
                        (str(row["language"]), str(row["content"]))
                    )
        return {
            str(row["name"]): RoleDescriptor(
                uuid=str(row["uuid"]),
                name=str(row["name"]),
                synonyms=tuple(synonyms[int(row["id"])]),
                comment=str(row["comment"]),
                xml_version=str(row["xml_version"]),
                set_for_new_objects=bool(row["set_for_new"]),
                set_for_attributes_by_default=bool(row["set_for_attributes"]),
                independent_rights_of_child_objects=bool(row["independent_children"]),
            )
            for row in rows
        }

    def list_roles(
        self,
        *,
        offset: int = 0,
        limit: int | None = None,
    ) -> tuple[RoleDescriptor, ...]:
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset должен быть целым числом не меньше нуля")
        if limit is None:
            if offset:
                raise ValueError("offset без limit не поддерживается")
            return tuple(self._descriptors().values())
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit должен быть от 1 до 100")
        with self._lock:
            names = tuple(
                str(row["name"])
                for row in self._connection.execute(
                    "SELECT name FROM roles ORDER BY name_key,name LIMIT ? OFFSET ?",
                    (limit, offset),
                )
            )
        if not names:
            return ()
        return tuple(self._descriptors(names).values())

    def get_role(self, name: str) -> RoleDescriptor:
        if not isinstance(name, str) or not name:
            raise ValueError("role должен быть непустой строкой")
        roles = self._descriptors((name,))
        if not roles:
            raise KeyError(name)
        return next(iter(roles.values()))

    def role_access(
        self,
        role: str,
        *,
        target: str = "",
        include_restrictions: bool = False,
        offset: int = 0,
        limit: int = 100,
    ) -> RoleAccessPage:
        descriptor = self.get_role(role)
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset должен быть целым числом не меньше нуля")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise ValueError("limit должен быть от 1 до 200")
        where = "r.role_id=(SELECT id FROM roles WHERE name_key=?)"
        parameters: list[object] = [descriptor.name.casefold()]
        if target:
            where += " AND t.path_key=?"
            parameters.append(target.casefold())
        query = (
            "SELECT r.id,t.path,n.name,r.value,r.conditional FROM rights r "
            "JOIN targets t ON t.id=r.target_id "
            "JOIN right_names n ON n.id=r.right_name_id "
            f"WHERE {where} ORDER BY t.path_key,t.path,n.name_key,n.name LIMIT ? OFFSET ?"
        )
        with self._lock:
            total = int(
                self._connection.execute(
                    "SELECT COUNT(*) FROM rights r JOIN targets t ON t.id=r.target_id "
                    f"WHERE {where}",
                    parameters,
                ).fetchone()[0]
            )
            rows = self._connection.execute(
                query, (*parameters, limit, offset)
            ).fetchall()
            restrictions: dict[int, list[RoleRestriction]] = {
                int(row["id"]): [] for row in rows
            }
            restriction_refs: dict[int, list[RoleRestrictionReference]] = {
                int(row["id"]): [] for row in rows
            }
            if rows:
                ids = tuple(restrictions)
                placeholders = ",".join("?" for _ in ids)
                # Обычная страница возвращает только размер и ссылку. Даже
                # локальная строка результата не должна содержать огромный RLS.
                content_column = "c.content" if include_restrictions else "''"
                for raw in self._connection.execute(
                    "SELECT x.id,x.right_id,length(c.content) AS chars,"
                    "length(CAST(c.content AS BLOB)) AS bytes,"
                    f"{content_column} AS content,t.path AS field FROM restrictions x "
                    "JOIN conditions c ON c.id=x.condition_id "
                    "LEFT JOIN targets t ON t.id=x.field_target_id "
                    f"WHERE x.right_id IN ({placeholders}) "
                    "ORDER BY x.right_id,x.id",
                    ids,
                ):
                    right_id = int(raw["right_id"])
                    restriction_refs[right_id].append(
                        RoleRestrictionReference(
                            id=int(raw["id"]),
                            field=str(raw["field"] or ""),
                            chars=int(raw["chars"]),
                            bytes=int(raw["bytes"]),
                        )
                    )
                    if include_restrictions:
                        restrictions[right_id].append(
                            RoleRestriction(
                                str(raw["content"]),
                                str(raw["field"] or ""),
                            )
                        )
            templates: tuple[tuple[str, str], ...] = ()
            if include_restrictions:
                templates = tuple(
                    (str(row["name"]), str(row["content"]))
                    for row in self._connection.execute(
                        "SELECT p.name,c.content FROM templates p "
                        "JOIN conditions c ON c.id=p.condition_id "
                        "WHERE p.role_id=(SELECT id FROM roles WHERE name_key=?) "
                        "ORDER BY p.name_key,p.name",
                        (descriptor.name.casefold(),),
                    )
                )
        rights = tuple(
            DeclaredRight(
                target=str(row["path"]),
                name=str(row["name"]),
                value=bool(row["value"]),
                has_restrictions=bool(row["conditional"]),
                restrictions=tuple(restrictions[int(row["id"])]),
                restriction_refs=tuple(restriction_refs[int(row["id"])]),
            )
            for row in rows
        )
        next_offset = offset + len(rights) if offset + len(rights) < total else None
        return RoleAccessPage(
            descriptor,
            rights,
            templates,
            total,
            offset,
            next_offset,
            (),
        )

    def role_templates(
        self,
        role: str,
        *,
        offset: int = 0,
        limit: int = 20,
    ) -> RoleTemplatePage:
        descriptor = self.get_role(role)
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset должен быть целым числом не меньше нуля")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit должен быть от 1 до 100")
        with self._lock:
            total = int(
                self._connection.execute(
                    "SELECT COUNT(*) FROM templates WHERE role_id="
                    "(SELECT id FROM roles WHERE name_key=?)",
                    (descriptor.name.casefold(),),
                ).fetchone()[0]
            )
            rows = self._connection.execute(
                "SELECT p.id,p.name,length(c.content) AS chars,"
                "length(CAST(c.content AS BLOB)) AS bytes FROM templates p "
                "JOIN conditions c ON c.id=p.condition_id "
                "WHERE p.role_id=(SELECT id FROM roles WHERE name_key=?) "
                "ORDER BY p.name_key,p.name LIMIT ? OFFSET ?",
                (descriptor.name.casefold(), limit, offset),
            ).fetchall()
        templates = tuple(
            RoleTemplateReference(
                id=int(row["id"]),
                name=str(row["name"]),
                chars=int(row["chars"]),
                bytes=int(row["bytes"]),
            )
            for row in rows
        )
        next_offset = offset + len(templates)
        if next_offset >= total:
            next_offset = None
        return RoleTemplatePage(templates, total, offset, next_offset)

    def read_text(
        self,
        role: str,
        kind: str,
        reference_id: int,
        *,
        offset: int = 0,
        max_chars: int = 8000,
    ) -> RoleTextPage:
        """Прочитать одно RLS-условие или шаблон ограниченным окном."""
        descriptor = self.get_role(role)
        if kind not in {"restriction", "template"}:
            raise ValueError("kind должен быть restriction|template")
        if (
            isinstance(reference_id, bool)
            or not isinstance(reference_id, int)
            or reference_id < 1
        ):
            raise ValueError("reference_id должен быть положительным целым")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset должен быть целым числом не меньше нуля")
        if (
            isinstance(max_chars, bool)
            or not isinstance(max_chars, int)
            or not 256 <= max_chars <= 8000
        ):
            raise ValueError("max_chars должен быть от 256 до 8000")
        if kind == "restriction":
            query = (
                "SELECT substr(c.content,?,?) AS content,length(c.content) AS chars,"
                "length(CAST(c.content AS BLOB)) AS bytes,"
                "f.path AS field,t.path AS target,n.name AS right_name "
                "FROM restrictions x JOIN conditions c ON c.id=x.condition_id "
                "JOIN rights r ON r.id=x.right_id "
                "JOIN roles p ON p.id=r.role_id "
                "JOIN targets t ON t.id=r.target_id "
                "JOIN right_names n ON n.id=r.right_name_id "
                "LEFT JOIN targets f ON f.id=x.field_target_id "
                "WHERE x.id=? AND p.name_key=?"
            )
        else:
            query = (
                "SELECT substr(c.content,?,?) AS content,length(c.content) AS chars,"
                "length(CAST(c.content AS BLOB)) AS bytes,"
                "'' AS field,'' AS target,'' AS right_name,p.name AS template "
                "FROM templates p JOIN conditions c ON c.id=p.condition_id "
                "JOIN roles r ON r.id=p.role_id "
                "WHERE p.id=? AND r.name_key=?"
            )
        with self._lock:
            row = self._connection.execute(
                query,
                (
                    offset + 1,
                    max_chars,
                    reference_id,
                    descriptor.name.casefold(),
                ),
            ).fetchone()
        if row is None:
            raise KeyError(reference_id)
        content = str(row["content"])
        total_chars = int(row["chars"])
        next_offset = offset + len(content)
        if next_offset >= total_chars:
            next_offset = None
        return RoleTextPage(
            kind=kind,
            id=reference_id,
            role=descriptor.name,
            content=content,
            total_chars=total_chars,
            total_bytes=int(row["bytes"]),
            offset=offset,
            next_offset=next_offset,
            field=str(row["field"] or ""),
            template=str(row["template"] if kind == "template" else ""),
            target=str(row["target"] or ""),
            right=str(row["right_name"] or ""),
        )

    def find_roles_for_access(
        self,
        full_name: str,
        operations: tuple[str, ...],
        *,
        child_path: str = "",
        include_conditional: bool = False,
        offset: int = 0,
        limit: int = 20,
    ) -> RoleAccessResolution:
        if not isinstance(operations, tuple) or not operations:
            raise ValueError("operations должен быть непустым tuple")
        normalized = tuple(operation.casefold() for operation in operations)
        if len(normalized) != len(set(normalized)) or any(
            operation not in OPERATION_RIGHTS for operation in normalized
        ):
            raise ValueError("operation неизвестна или повторяется")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("limit должен быть от 1 до 100")
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0:
            raise ValueError("offset должен быть целым числом не меньше нуля")
        target = _source_target(full_name, child_path)
        right_to_operation = {
            right.casefold(): operation
            for operation, right in OPERATION_RIGHTS.items()
            if operation in normalized
        }
        placeholders = ",".join("?" for _ in right_to_operation)
        with self._lock:
            rows = self._connection.execute(
                "SELECT r.id,p.name AS role_name,t.path,n.name AS right_name,"
                "r.value,r.conditional FROM rights r "
                "JOIN roles p ON p.id=r.role_id "
                "JOIN targets t ON t.id=r.target_id "
                "JOIN right_names n ON n.id=r.right_name_id "
                f"WHERE t.path_key=? AND n.name_key IN ({placeholders}) "
                "ORDER BY p.name_key,p.name,n.name_key,n.name",
                (target.casefold(), *right_to_operation),
            ).fetchall()
        evidence: dict[str, dict[str, object]] = {}
        conditional_excluded_roles: set[str] = set()
        for row in rows:
            role_name = str(row["role_name"])
            operation = right_to_operation[str(row["right_name"]).casefold()]
            item = evidence.setdefault(
                role_name,
                {"matched": set(), "conditional": set(), "denied": set(), "rights": []},
            )
            if not bool(row["value"]):
                item["denied"].add(operation)
                continue
            conditional = bool(row["conditional"])
            if conditional and not include_conditional:
                conditional_excluded_roles.add(role_name)
                continue
            item["matched"].add(operation)
            if conditional:
                item["conditional"].add(operation)
            item["rights"].append(
                DeclaredRight(
                    target=str(row["path"]),
                    name=str(row["right_name"]),
                    value=True,
                    has_restrictions=conditional,
                    restrictions=(),
                )
            )
        useful = {
            name: item for name, item in evidence.items() if item["matched"]
        }
        descriptors = self._descriptors(useful)
        candidates: list[RoleCandidate] = []
        for name, item in useful.items():
            matched = item["matched"]
            assert isinstance(matched, set)
            candidates.append(
                RoleCandidate(
                    role=descriptors[name],
                    matched_operations=tuple(op for op in normalized if op in matched),
                    missing_operations=tuple(op for op in normalized if op not in matched),
                    conditional_operations=tuple(
                        op for op in normalized if op in item["conditional"]
                    ),
                    denied_operations=tuple(
                        op for op in normalized if op in item["denied"]
                    ),
                    matched_rights=tuple(item["rights"]),
                )
            )
        candidates.sort(
            key=lambda item: (
                not item.complete,
                -len(item.matched_operations),
                len(item.conditional_operations),
                item.role.name.casefold(),
                item.role.name,
            )
        )
        operation_bits = {operation: 1 << index for index, operation in enumerate(normalized)}
        best: dict[int, tuple[str, ...]] = {0: ()}
        by_name = {candidate.role.name: candidate for candidate in candidates}
        for candidate in sorted(candidates, key=lambda item: (item.role.name.casefold(), item.role.name)):
            mask = sum(operation_bits[operation] for operation in candidate.matched_operations)
            for previous_mask, previous_roles in tuple(best.items()):
                combined = previous_mask | mask
                role_set = tuple(
                    sorted(
                        (*previous_roles, candidate.role.name),
                        key=lambda value: (value.casefold(), value),
                    )
                )
                current = best.get(combined)
                if current is None or (len(role_set), tuple(value.casefold() for value in role_set), role_set) < (
                    len(current), tuple(value.casefold() for value in current), current
                ):
                    best[combined] = role_set
        full_mask = (1 << len(normalized)) - 1
        minimum = best.get(full_mask, ())
        proof = ""
        if minimum:
            proof = (
                "explicit_with_conditions"
                if any(by_name[name].conditional_operations for name in minimum)
                else "explicit_unconditional"
            )
        warnings = (
            "Показаны объявленные права роли, а не эффективный доступ пользователя.",
            "Default-флаги сохранены, но недоказанное наследование не участвует в подборе.",
            *(
                (
                    "Условные права не учитывались без явного opt-in."
                )
                if conditional_excluded_roles and not include_conditional
                else ()
            ),
            *(
                (
                    "Условные права являются кандидатами: фактический доступ требует "
                    "отдельной проверки RLS.",
                )
                if include_conditional
                else ()
            ),
        )
        total = len(candidates)
        page = tuple(candidates[offset : offset + limit])
        next_offset = offset + len(page)
        if next_offset >= total:
            next_offset = None
        return RoleAccessResolution(
            source_target=target,
            checked_rights=tuple(
                (operation, OPERATION_RIGHTS[operation]) for operation in normalized
            ),
            candidates=page,
            minimal_role_set=minimum,
            minimum_proof=proof,
            warnings=warnings,
            candidates_total=total,
            conditional_candidates_excluded=len(
                conditional_excluded_roles - set(useful)
            ),
            offset=offset,
            next_offset=next_offset,
        )

    def close(self) -> None:
        with self._lock:
            connection = self._connection
            self._connection = None  # type: ignore[assignment]
            if connection is not None:
                connection.close()
        if self._ephemeral:
            try:
                self._path.unlink(missing_ok=True)
            except OSError:
                pass

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


def load_role_access(
    root: str | Path,
    manifest: GenerationManifest,
    cache_path: str | Path | None = None,
) -> LoadedRoleAccess | None:
    """Поднять provider из generation, не перечитывая исходный ZIP."""
    if not isinstance(manifest, GenerationManifest):
        raise TypeError("manifest должен быть GenerationManifest")
    layer = next(
        (item for item in manifest.layers if item.kind is LayerKind.ROLES), None
    )
    if layer is None or layer.state is LayerState.UNAVAILABLE:
        return None
    if layer.state is LayerState.ERROR:
        return LoadedRoleAccess(
            state="error",
            generation_id=manifest.generation_id,
            source_sha256=manifest.raw_sha256,
            content_sha256="",
            items_total=0,
            error=layer.error,
        )
    try:
        index = RoleAccessIndex.open_or_build(root, layer, cache_path)
    except RoleAccessError as error:
        return LoadedRoleAccess(
            state="error",
            generation_id=manifest.generation_id,
            source_sha256=manifest.raw_sha256,
            content_sha256=layer.content_sha256,
            items_total=layer.items_total,
            error=str(error),
        )
    return LoadedRoleAccess(
        state="ready",
        generation_id=manifest.generation_id,
        source_sha256=manifest.raw_sha256,
        content_sha256=layer.content_sha256,
        items_total=layer.items_total,
        index=index,
    )


__all__ = [
    "DeclaredRight",
    "LoadedRoleAccess",
    "OPERATION_RIGHTS",
    "RoleAccessError",
    "RoleAccessIndex",
    "RoleAccessPage",
    "RoleAccessResolution",
    "RoleCandidate",
    "RoleDescriptor",
    "RoleIndexSummary",
    "RoleRestriction",
    "load_role_access",
]
