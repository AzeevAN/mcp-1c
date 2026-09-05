"""Опциональный read-only провайдер канонической общей справки schema v1.

Готовая SQLite является входом продукта, а не частью ``Registry``.  Отсутствие
или отказ этого входа отключает только две справочные операции; основной MCP
продолжает запускаться. Поисковый индекс полностью производный и потому
подчиняется тем же правилам расходного кэша, что остальные индексы проекта.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import sqlite3
import stat
import tempfile
import threading
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from . import index_cache
from .search import Doc, SearchIndex

CANONICAL_SCHEMA_VERSION = "1"
MAX_REFERENCE_DB_BYTES = 32 * 1024 * 1024
MAX_REFERENCE_ARTIFACT_BYTES = MAX_REFERENCE_DB_BYTES + 1024 * 1024
MIN_PAGE_CHARS = 256
MAX_PAGE_CHARS = 20_000
DEFAULT_PAGE_CHARS = 8_000
REFERENCE_PATH_ENV = "MCP1C_REFERENCE_ARTIFACT"
REFERENCE_ARTIFACT_NAME = "reference.mcp1cref"
REFERENCE_ARTIFACT_SUFFIX = ".mcp1cref"
REFERENCE_DATABASE_MEMBER = "reference.sqlite3"
REFERENCE_MANIFEST_MEMBER = "manifest.json"
REFERENCE_SIGNATURE_MEMBER = "manifest.sig"
REFERENCE_FORMAT = "mcp1c-reference"
REFERENCE_ARTIFACT_VERSION = "1"
REFERENCE_SIGNATURE_ALGORITHM = "ed25519"
MAX_REFERENCE_MANIFEST_BYTES = 4096
ED25519_SIGNATURE_BYTES = 64
ED25519_PUBLIC_KEY_BYTES = 32
_QUERY_TABLE_MARKER = re.compile(r"\x00таблица-\d+\x00")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_KEY_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")

# Приватные половины release-ключей никогда не попадают в этот проект.
# SHA-256 raw public key `reference-2026-01`:
# 509d077f669ebf935aa03453bdb1e904f95e0192fdf265b2c45b239678615c2e
TRUSTED_REFERENCE_PUBLIC_KEYS: dict[str, bytes] = {
    "reference-2026-01": bytes.fromhex(
        "02ed13d505d3dea350e991c7ca9e6ef2"
        "e11c4c0879a3859da9b916c30ab70c25"
    ),
}

# Идентификаторы остаются частью машинного контракта, а подписи дают
# дашборду и описаниям MCP один общий человекочитаемый словарь.
REFERENCE_DOMAIN_TITLES: dict[str, str] = {
    "bsl": "Встроенный язык (BSL)",
    "query": "Язык запросов",
    "dcs": "Выражения СКД",
    "dcs_ui": "Интерфейс СКД",
    "configurator": "Конфигуратор",
    "development_tools": "Инструменты разработки",
    "legacy": "Устаревшие инструменты",
}

REFERENCE_DOMAIN_ORDER = (
    "bsl",
    "query",
    "dcs",
    "dcs_ui",
    "configurator",
    "development_tools",
    "legacy",
)

REFERENCE_DOMAIN_DESCRIPTIONS: dict[str, str] = {
    "bsl": "Конструкции языка, правила и практические шаблоны BSL.",
    "query": "Функции, операторы и конструкции языка запросов 1С.",
    "dcs": "Функции и выражения системы компоновки данных.",
    "dcs_ui": "Настройка и использование интерфейса СКД.",
    "configurator": "Команды и возможности Конфигуратора.",
    "development_tools": "Команды вспомогательных инструментов разработки.",
    "legacy": "Архивные материалы по устаревшим инструментам.",
}

REFERENCE_KIND_TITLES: dict[str, str] = {
    "book_index": "Указатель книги",
    "bsl_auxiliary": "Дополнительный материал BSL",
    "bsl_construct": "Конструкция BSL",
    "bsl_guidance": "Рекомендация по BSL",
    "bsl_template": "Шаблон BSL",
    "legacy": "Архивный материал",
    "module_context": "Контекст модуля",
    "source_rule": "Правило исходного кода",
    "config_operator": "Команда Конфигуратора",
    "dcs_article": "Статья о СКД",
    "dcs_function": "Функция СКД",
    "dcs_function_group": "Группа функций СКД",
    "term_dictionary": "Термин словаря",
    "dcs_ui_article": "Статья об интерфейсе СКД",
    "devtool_operator": "Команда инструмента разработки",
    "legacy_ui_designer": "Устаревший редактор интерфейса",
    "query_function": "Функция языка запросов",
    "query_keyword": "Оператор языка запросов",
}

# Schema runtime-слоя публична: адаптер обязан отвергать похожую SQLite с
# недостающими или подменёнными таблицами до выполнения предметных запросов.
EXPECTED_COLUMNS: dict[str, tuple[str, ...]] = {
    "meta": ("key", "value"),
    "sources": (
        "source_key", "label", "kind", "book_id", "language",
        "platform_version", "source_sha256", "source_size", "parser_version",
    ),
    "items": (
        "id", "source_key", "source_path", "source_content_sha256", "domain",
        "kind", "access_scope", "safety", "title_ru", "title_en", "signature",
        "body", "search_text", "accepted", "content_sha256",
    ),
    "sections": (
        "id", "item_id", "parent_id", "ordinal", "heading_level", "anchor",
        "title_ru", "title_en", "signature", "parameters_json", "examples_json",
        "body", "content_sha256",
    ),
    "aliases": ("id", "item_id", "value", "normalized", "language", "alias_kind"),
    "parameters": (
        "item_id", "ordinal", "name", "required", "types_json", "description",
        "default_value",
    ),
    "examples": ("item_id", "ordinal", "label", "content", "locator_json"),
    "item_tables": ("item_id", "ordinal", "header_json", "rows_json", "content_sha256"),
    "templates": (
        "item_id", "article_item_id", "internal_name", "content_ru", "content_en",
        "placeholders_json", "parsed_structure_json", "content_sha256",
    ),
    "terms": (
        "id", "source_key", "domain", "source_path", "ordinal", "name_ru",
        "name_en", "target_href", "target_item_id", "status", "correction_reason",
    ),
    "relations": (
        "id", "source_item_id", "source_path", "ordinal", "relation_kind", "label",
        "original_href", "resolved_href", "target_item_id", "target_section_id",
        "status", "correction_reason",
    ),
    "version_facts": (
        "id", "item_id", "fact_kind", "version", "evidence_kind", "evidence_ref",
        "evidence_note",
    ),
    "observations": (
        "item_id", "source_key", "presence", "platform_version", "evidence_ref",
    ),
    "search_hints": ("item_id", "ordinal", "text", "source_kind"),
    "assets": (
        "id", "source_key", "source_path", "media_type", "width", "height",
        "content_sha256",
    ),
    "asset_relations": ("article_item_id", "asset_id", "ordinal", "src", "alt"),
    "build_issues": ("id", "severity", "code", "entity_id", "message"),
}

JSON_COLUMNS: dict[str, tuple[str, ...]] = {
    "sections": ("parameters_json", "examples_json"),
    "parameters": ("types_json",),
    "examples": ("locator_json",),
    "item_tables": ("header_json", "rows_json"),
    "templates": ("placeholders_json", "parsed_structure_json"),
}


class ReferenceQueryError(ValueError):
    """Ожидаемая ошибка параметров двух справочных операций."""


class ReferenceValidationError(RuntimeError):
    """Безопасно классифицированный отказ необязательной базы."""

    def __init__(
        self,
        state: str,
        message: str,
        *,
        key_id: str | None = None,
        signature: str = "not-checked",
    ):
        super().__init__(message)
        self.state = state
        self.key_id = key_id
        self.signature = signature


@dataclass(frozen=True, slots=True)
class VerifiedArtifact:
    database: Path
    artifact_sha256: str
    file_sha256: str
    logical_sha256: str
    schema_version: str
    key_id: str
    signature: str = REFERENCE_SIGNATURE_ALGORITHM


class ArtifactVerifier(Protocol):
    """Проверить доверие к bundle, не открывая SQLite."""

    def verify(self, artifact: Path, extraction_dir: Path) -> VerifiedArtifact:
        ...


def canonical_manifest_bytes(manifest: Mapping[str, object]) -> bytes:
    """Точные подписываемые байты: ASCII JSON, сортировка ключей и один LF."""
    return (
        json.dumps(
            manifest,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )


def _manifest_object(raw: bytes) -> dict[str, object]:
    if not raw or len(raw) > MAX_REFERENCE_MANIFEST_BYTES:
        raise ReferenceValidationError(
            "untrusted", "Manifest подписанного артефакта отсутствует или слишком велик."
        )

    def unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = value
        return result

    try:
        decoded = raw.decode("utf-8")
        manifest = json.loads(decoded, object_pairs_hook=unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise ReferenceValidationError(
            "untrusted", "Manifest подписанного артефакта повреждён."
        ) from error
    if not isinstance(manifest, dict) or canonical_manifest_bytes(manifest) != raw:
        raise ReferenceValidationError(
            "untrusted", "Manifest не соответствует каноническому формату."
        )
    return manifest


def _validated_manifest(manifest: dict[str, object]) -> dict[str, object]:
    expected = {
        "artifact",
        "artifact_sha256",
        "artifact_size",
        "format",
        "format_version",
        "key_id",
        "logical_sha256",
        "schema_version",
        "signature_algorithm",
    }
    if set(manifest) != expected:
        raise ReferenceValidationError(
            "untrusted", "Manifest содержит неизвестный или неполный набор полей."
        )
    key_id = manifest.get("key_id")
    if not isinstance(key_id, str) or _KEY_ID.fullmatch(key_id) is None:
        raise ReferenceValidationError(
            "untrusted", "Manifest содержит недопустимый key_id."
        )
    if (
        manifest.get("artifact") != REFERENCE_DATABASE_MEMBER
        or manifest.get("format") != REFERENCE_FORMAT
        or manifest.get("format_version") != REFERENCE_ARTIFACT_VERSION
        or manifest.get("signature_algorithm") != REFERENCE_SIGNATURE_ALGORITHM
    ):
        raise ReferenceValidationError(
            "incompatible",
            "Формат подписанного артефакта не поддерживается.",
            key_id=key_id,
        )
    if manifest.get("schema_version") != CANONICAL_SCHEMA_VERSION:
        raise ReferenceValidationError(
            "incompatible",
            "Версия канонической базы несовместима с schema v1.",
            key_id=key_id,
        )
    if (
        not isinstance(manifest.get("artifact_size"), int)
        or isinstance(manifest.get("artifact_size"), bool)
        or not 0 < int(manifest["artifact_size"]) <= MAX_REFERENCE_DB_BYTES
        or not isinstance(manifest.get("artifact_sha256"), str)
        or _SHA256.fullmatch(str(manifest["artifact_sha256"])) is None
        or not isinstance(manifest.get("logical_sha256"), str)
        or _SHA256.fullmatch(str(manifest["logical_sha256"])) is None
    ):
        raise ReferenceValidationError(
            "untrusted", "Manifest содержит недопустимые контрольные значения.",
            key_id=key_id,
        )
    return manifest


def _strict_bundle(artifact: Path) -> tuple[zipfile.ZipFile, dict[str, zipfile.ZipInfo]]:
    try:
        bundle = zipfile.ZipFile(artifact)
    except (OSError, zipfile.BadZipFile) as error:
        try:
            with artifact.open("rb") as stream:
                unsigned_sqlite = stream.read(16) == b"SQLite format 3\x00"
        except OSError:
            unsigned_sqlite = False
        state = "untrusted" if unsigned_sqlite else "corrupt"
        message = (
            "Неподписанная SQLite не принимается."
            if unsigned_sqlite
            else "Файл не является подписанным артефактом общей справки."
        )
        raise ReferenceValidationError(state, message) from error
    infos = bundle.infolist()
    names = [info.filename for info in infos]
    expected = {
        REFERENCE_DATABASE_MEMBER,
        REFERENCE_MANIFEST_MEMBER,
        REFERENCE_SIGNATURE_MEMBER,
    }
    if len(names) != len(expected) or set(names) != expected:
        bundle.close()
        missing_trust = (
            REFERENCE_MANIFEST_MEMBER not in names
            or REFERENCE_SIGNATURE_MEMBER not in names
        )
        raise ReferenceValidationError(
            "untrusted" if missing_trust else "corrupt",
            "Подписанный артефакт должен содержать ровно три обязательных файла.",
        )
    by_name = {info.filename: info for info in infos}
    for info in infos:
        mode = info.external_attr >> 16
        file_type = stat.S_IFMT(mode)
        if (
            info.is_dir()
            or info.flag_bits & 0x1
            or info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
            or file_type not in (0, stat.S_IFREG)
        ):
            bundle.close()
            raise ReferenceValidationError(
                "corrupt", "Подписанный артефакт содержит недопустимую запись ZIP."
            )
    database_info = by_name[REFERENCE_DATABASE_MEMBER]
    manifest_info = by_name[REFERENCE_MANIFEST_MEMBER]
    signature_info = by_name[REFERENCE_SIGNATURE_MEMBER]
    if (
        not 0 < database_info.file_size <= MAX_REFERENCE_DB_BYTES
        or not 0 < manifest_info.file_size <= MAX_REFERENCE_MANIFEST_BYTES
        or signature_info.file_size != ED25519_SIGNATURE_BYTES
    ):
        bundle.close()
        state = "untrusted" if signature_info.file_size != ED25519_SIGNATURE_BYTES else "corrupt"
        raise ReferenceValidationError(
            state, "Размер одного из файлов подписанного артефакта недопустим."
        )
    return bundle, by_name


class SignedArtifactVerifier:
    """Ed25519 detached-подпись manifest и потоковая проверка SQLite."""

    def __init__(self, public_keys: Mapping[str, bytes] | None = None):
        self.public_keys = dict(
            TRUSTED_REFERENCE_PUBLIC_KEYS if public_keys is None else public_keys
        )

    def verify(self, artifact: Path, extraction_dir: Path) -> VerifiedArtifact:
        artifact_size = artifact.stat().st_size
        if not 0 < artifact_size <= MAX_REFERENCE_ARTIFACT_BYTES:
            raise ReferenceValidationError(
                "corrupt", "Размер подписанного артефакта недопустим."
            )
        artifact_sha256 = _file_sha256(artifact)
        bundle, infos = _strict_bundle(artifact)
        temporary: Path | None = None
        try:
            raw_manifest = bundle.read(REFERENCE_MANIFEST_MEMBER)
            manifest = _validated_manifest(_manifest_object(raw_manifest))
            key_id = str(manifest["key_id"])
            public_bytes = self.public_keys.get(key_id)
            if public_bytes is None:
                raise ReferenceValidationError(
                    "untrusted", "Артефакт подписан неизвестным ключом.",
                    key_id=key_id, signature=REFERENCE_SIGNATURE_ALGORITHM,
                )
            if len(public_bytes) != ED25519_PUBLIC_KEY_BYTES:
                raise RuntimeError("invalid embedded Ed25519 public key")
            signature = bundle.read(REFERENCE_SIGNATURE_MEMBER)
            try:
                Ed25519PublicKey.from_public_bytes(public_bytes).verify(
                    signature, raw_manifest
                )
            except (InvalidSignature, ValueError) as error:
                raise ReferenceValidationError(
                    "untrusted", "Подпись артефакта не прошла проверку.",
                    key_id=key_id, signature=REFERENCE_SIGNATURE_ALGORITHM,
                ) from error

            database_info = infos[REFERENCE_DATABASE_MEMBER]
            if database_info.file_size != manifest["artifact_size"]:
                raise ReferenceValidationError(
                    "corrupt", "Размер SQLite не совпал с подписанным manifest.",
                    key_id=key_id, signature=REFERENCE_SIGNATURE_ALGORITHM,
                )
            database_dir = extraction_dir / "databases"
            database_dir.mkdir(parents=True, exist_ok=True)
            descriptor, raw_path = tempfile.mkstemp(
                prefix=".reference-db-", suffix=".sqlite3", dir=database_dir
            )
            os.close(descriptor)
            temporary = Path(raw_path)
            digest = hashlib.sha256()
            size = 0
            with bundle.open(REFERENCE_DATABASE_MEMBER) as source, temporary.open("wb") as target:
                while chunk := source.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_REFERENCE_DB_BYTES:
                        raise ReferenceValidationError(
                            "corrupt", "SQLite внутри артефакта превышает предел."
                        )
                    digest.update(chunk)
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
            file_sha256 = digest.hexdigest()
            if size != manifest["artifact_size"] or file_sha256 != manifest["artifact_sha256"]:
                raise ReferenceValidationError(
                    "corrupt", "SHA-256 SQLite не совпал с подписанным manifest.",
                    key_id=key_id, signature=REFERENCE_SIGNATURE_ALGORITHM,
                )
            destination = database_dir / f"{file_sha256}.sqlite3"
            temporary.chmod(0o600)
            if destination.is_file() and _file_sha256(destination) == file_sha256:
                temporary.unlink()
            else:
                temporary.replace(destination)
            temporary = None
            return VerifiedArtifact(
                database=destination,
                artifact_sha256=artifact_sha256,
                file_sha256=file_sha256,
                logical_sha256=str(manifest["logical_sha256"]),
                schema_version=str(manifest["schema_version"]),
                key_id=key_id,
            )
        finally:
            bundle.close()
            if temporary is not None:
                temporary.unlink(missing_ok=True)


@dataclass(frozen=True, slots=True)
class ReferenceStatus:
    state: str
    message: str
    signature: str
    schema_version: str | None = None
    content_sha256: str | None = None
    file_sha256: str | None = None
    items: int | None = None
    index_cache: str | None = None
    key_id: str | None = None
    action: str | None = None

    @property
    def ready(self) -> bool:
        return self.state == "ready"

    def payload(self, *, detailed: bool = False) -> dict[str, Any]:
        result: dict[str, Any] = {
            "state": self.state,
            "ready": self.ready,
            "message": self.message,
        }
        if detailed:
            result.update(
                {
                    "signature": self.signature,
                    "schema_version": self.schema_version,
                    "content_sha256": self.content_sha256,
                    "file_sha256": self.file_sha256,
                    "items": self.items,
                    "index_cache": self.index_cache,
                    "key_id": self.key_id,
                    "action": self.action,
                }
            )
        return result


@dataclass(frozen=True, slots=True)
class SearchTarget:
    item_id: str
    section_id: str | None
    domain: str
    kind: str
    access_scope: str


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _cleanup_reference_derivatives(data_dir: Path) -> None:
    """Подмести только расходные файлы общей справки, не трогая источники."""
    root = data_dir / "index" / "reference"
    targets = [root / "reference.search"]
    databases = root / "databases"
    if databases.is_dir():
        targets.extend(
            path for path in databases.iterdir()
            if path.is_file() and path.suffix == ".sqlite3"
        )
    for target in targets:
        try:
            target.unlink(missing_ok=True)
        except OSError:
            # Кэш не может быть причиной отказа основного сервера.
            pass


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def calculate_logical_hash(connection: sqlite3.Connection) -> str:
    """Повторить логический SHA-256 schema v1 без зависимости от layout SQLite."""
    connection.row_factory = sqlite3.Row
    digest = hashlib.sha256()
    tables = [
        row["name"]
        for row in connection.execute(
            """SELECT name FROM sqlite_schema
               WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name!='meta'
               ORDER BY name"""
        )
    ]
    for table in tables:
        quoted = _quote_identifier(table)
        columns = [
            row["name"] for row in connection.execute(f"PRAGMA table_info({quoted})")
        ]
        order = ", ".join(_quote_identifier(column) for column in columns)
        digest.update(f"\n[{table}]\n".encode())
        for row in connection.execute(f"SELECT * FROM {quoted} ORDER BY {order}"):
            encoded = json.dumps(
                [row[column] for column in columns],
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            digest.update(encoded.encode("utf-8"))
            digest.update(b"\n")
    return digest.hexdigest()


def _connect(path: Path) -> sqlite3.Connection:
    try:
        connection = sqlite3.connect(
            f"{path.resolve().as_uri()}?mode=ro&immutable=1",
            uri=True,
            check_same_thread=False,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection
    except (OSError, sqlite3.Error) as error:
        raise ReferenceValidationError("corrupt", "SQLite не удалось открыть.") from error


def _validate_json_columns(connection: sqlite3.Connection) -> None:
    """Не откладывать обнаружение битого runtime-поля до первого tools/call."""
    for table, columns in JSON_COLUMNS.items():
        quoted_table = _quote_identifier(table)
        quoted_columns = ", ".join(_quote_identifier(column) for column in columns)
        for row in connection.execute(f"SELECT {quoted_columns} FROM {quoted_table}"):
            for column in columns:
                try:
                    value = json.loads(row[column])
                except (TypeError, json.JSONDecodeError) as error:
                    raise ReferenceValidationError(
                        "corrupt", "JSON-поля канонической базы повреждены."
                    ) from error
                if table == "item_tables" and not isinstance(value, list):
                    raise ReferenceValidationError(
                        "corrupt", "Табличные JSON-поля имеют неверный тип."
                    )


def _validate_schema(connection: sqlite3.Connection) -> tuple[str, str, int]:
    try:
        objects = {
            row["name"]: row["type"]
            for row in connection.execute(
                "SELECT name, type FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'"
            )
            if row["type"] in ("table", "view", "trigger")
        }
        for table, columns in EXPECTED_COLUMNS.items():
            if objects.get(table) != "table":
                raise ReferenceValidationError(
                    "incompatible", "В базе отсутствует обязательная таблица schema v1."
                )
            quoted = _quote_identifier(table)
            actual = tuple(
                row["name"] for row in connection.execute(f"PRAGMA table_info({quoted})")
            )
            if actual != columns:
                raise ReferenceValidationError(
                    "incompatible", "Структура таблиц не соответствует schema v1."
                )
        unexpected = set(objects) - set(EXPECTED_COLUMNS)
        if unexpected:
            raise ReferenceValidationError(
                "incompatible", "В базе есть неподдерживаемые таблицы, views или triggers."
            )
        meta = dict(connection.execute("SELECT key, value FROM meta"))
        schema_version = meta.get("schema_version", "")
        if schema_version != CANONICAL_SCHEMA_VERSION:
            raise ReferenceValidationError(
                "incompatible", "Версия канонической базы несовместима с schema v1."
            )
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok":
            raise ReferenceValidationError("corrupt", "SQLite не прошла integrity_check.")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise ReferenceValidationError("corrupt", "В базе нарушены внешние ключи.")
        _validate_json_columns(connection)
        rejected = connection.execute(
            "SELECT COUNT(*) FROM items WHERE accepted!=1"
        ).fetchone()[0]
        items = connection.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        if rejected or not items:
            raise ReferenceValidationError(
                "incompatible", "Runtime-база должна содержать только принятые элементы."
            )
        logical = calculate_logical_hash(connection)
        if meta.get("content_sha256") != logical:
            raise ReferenceValidationError(
                "corrupt", "Логический SHA-256 содержимого базы не совпал."
            )
        return schema_version, logical, items
    except ReferenceValidationError:
        raise
    except sqlite3.DatabaseError as error:
        raise ReferenceValidationError("corrupt", "SQLite повреждена или не читается.") from error


def _release(value: str) -> tuple[int, ...]:
    parts: list[int] = []
    for part in value.strip().split("."):
        if not part.isdigit():
            break
        parts.append(int(part))
    if len(parts) < 2:
        raise ReferenceQueryError(f"Некорректная версия платформы: {value!r}")
    return tuple(parts[:3])


def _version_text(value: tuple[int, ...]) -> str:
    return ".".join(str(part) for part in value)


class ReferenceProvider:
    """Один неизменяемый снимок базы и восстановленные поисковые индексы."""

    def __init__(self, path: Path, cache_path: Path, file_sha256: str):
        self.path = path.resolve()
        self.file_sha256 = file_sha256
        self._connection = _connect(self.path)
        self._lock = threading.Lock()
        try:
            self._initialize(cache_path)
        except BaseException:
            # Неудачная индексация необязательного входа не должна оставлять
            # descriptor до завершения всего MCP-процесса.
            self._connection.close()
            raise

    def _initialize(self, cache_path: Path) -> None:
        self._items = {
            row["id"]: dict(row)
            for row in self._connection.execute(
                "SELECT * FROM items WHERE accepted=1 ORDER BY id"
            )
        }
        self._facts = self._group_rows(
            "SELECT * FROM version_facts ORDER BY item_id, fact_kind, version, id"
        )
        self._observations = self._group_rows(
            "SELECT * FROM observations ORDER BY item_id, source_key"
        )
        self._catalog = self._build_catalog()
        documents, payloads, domain_payloads = self._documents()
        cached = index_cache.load_blob(
            cache_path,
            source_sha256=self.file_sha256,
            kind="reference-search-v1",
        )
        restored = self._restore_indices(cached, payloads, domain_payloads)
        if restored is None:
            self.index, self.domain_indices = self._build_indices(documents)
            state = {
                "global": self.index.export_state(),
                "domains": {
                    domain: index.export_state()
                    for domain, index in sorted(self.domain_indices.items())
                },
            }
            saved = index_cache.save_blob(
                state, cache_path, source_sha256=self.file_sha256,
                kind="reference-search-v1",
            )
            self.index_cache_state = "rebuilt" if saved is not None else "not-saved"
        else:
            self.index, self.domain_indices = restored
            self.index_cache_state = "hit"

    @staticmethod
    def _catalog_scope(scopes: set[str]) -> str:
        if "default" in scopes:
            return "default"
        if "explicit" in scopes:
            return "explicit"
        return "hidden"

    def _build_catalog(self) -> dict[str, Any]:
        domains: dict[str, dict[str, Any]] = {}
        kinds: dict[tuple[str, str], dict[str, Any]] = {}
        for item in self._items.values():
            domain = item["domain"]
            kind = item["kind"]
            scope = item["access_scope"]
            domain_entry = domains.setdefault(
                domain, {"items": 0, "scopes": set()}
            )
            domain_entry["items"] += 1
            domain_entry["scopes"].add(scope)
            kind_entry = kinds.setdefault(
                (domain, kind), {"items": 0, "scopes": set()}
            )
            kind_entry["items"] += 1
            kind_entry["scopes"].add(scope)

        domain_rows = [
            {
                "id": domain,
                "title": REFERENCE_DOMAIN_TITLES.get(domain, domain),
                "description": REFERENCE_DOMAIN_DESCRIPTIONS.get(domain, ""),
                "access_scope": self._catalog_scope(entry["scopes"]),
                "items": entry["items"],
            }
            for domain, entry in domains.items()
        ]
        order = {domain: index for index, domain in enumerate(REFERENCE_DOMAIN_ORDER)}
        domain_rows.sort(
            key=lambda row: (
                order.get(row["id"], len(order)),
                row["title"].casefold(),
                row["id"],
            )
        )
        kind_rows = [
            {
                "id": kind,
                "domain": domain,
                "title": REFERENCE_KIND_TITLES.get(kind, kind),
                "access_scope": self._catalog_scope(entry["scopes"]),
                "items": entry["items"],
            }
            for (domain, kind), entry in kinds.items()
        ]
        kind_rows.sort(
            key=lambda row: (
                REFERENCE_DOMAIN_TITLES.get(row["domain"], row["domain"]).casefold(),
                row["title"].casefold(),
                row["id"],
            )
        )

        raw_versions = {
            row[0]
            for row in self._connection.execute(
                "SELECT platform_version FROM sources "
                "WHERE platform_version IS NOT NULL AND platform_version != '' "
                "UNION SELECT version FROM version_facts WHERE version != ''"
            )
        }
        versions: list[tuple[tuple[int, ...], str]] = []
        for value in raw_versions:
            try:
                versions.append((_release(value), value))
            except ReferenceQueryError:
                continue
        versions.sort(reverse=True)
        return {
            "domains": domain_rows,
            "kinds": kind_rows,
            "platform_versions": [value for _, value in versions],
        }

    def catalog(self) -> dict[str, Any]:
        """Вернуть доступные фильтры без догадок о составе конкретной базы."""
        return {
            "domains": [dict(row) for row in self._catalog["domains"]],
            "kinds": [dict(row) for row in self._catalog["kinds"]],
            "platform_versions": list(self._catalog["platform_versions"]),
        }

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def _group_rows(self, query: str) -> dict[str, list[dict[str, Any]]]:
        result: dict[str, list[dict[str, Any]]] = {}
        for row in self._connection.execute(query):
            result.setdefault(row["item_id"], []).append(dict(row))
        return result

    def _documents(
        self,
    ) -> tuple[list[tuple[Doc, str]], dict[str, SearchTarget], dict[str, dict[str, SearchTarget]]]:
        aliases: dict[str, list[str]] = {}
        for row in self._connection.execute(
            "SELECT item_id, value FROM aliases ORDER BY item_id, alias_kind, value"
        ):
            aliases.setdefault(row["item_id"], []).append(row["value"])
        migrated: dict[str, list[str]] = {}
        measured: dict[str, list[str]] = {}
        for row in self._connection.execute(
            "SELECT item_id, text, source_kind FROM search_hints ORDER BY item_id, ordinal"
        ):
            target = migrated if row["source_kind"] == "migrated" else measured
            target.setdefault(row["item_id"], []).append(row["text"])

        documents: list[tuple[Doc, str]] = []
        payloads: dict[str, SearchTarget] = {}
        domain_payloads: dict[str, dict[str, SearchTarget]] = {}
        for item_id, row in self._items.items():
            item_aliases = aliases.get(item_id, [])
            if row["domain"] == "query" and row["kind"].startswith("query_"):
                fields = {
                    "name": row["title_ru"], "name_en": row["title_en"],
                    "parent": "", "kind": row["kind"],
                    "keys": "\n".join(migrated.get(item_id, [])),
                    "description": re.sub(
                        r"\n{3,}", "\n\n",
                        _QUERY_TABLE_MARKER.sub("", row["search_text"]),
                    ).strip(),
                }
                exact_keys = [row["title_ru"], row["title_en"], *measured.get(item_id, [])]
            else:
                fields = {
                    "name": row["title_ru"], "name_en": row["title_en"],
                    "synonym": "\n".join(item_aliases), "parent": row["domain"],
                    "kind": f"{row['domain']} {row['kind']}",
                    "keys": "\n".join(migrated.get(item_id, [])),
                    "description": row["body"],
                }
                exact_keys = [
                    item_id, row["title_ru"], row["title_en"], *item_aliases,
                    *measured.get(item_id, []),
                ]
                exact_keys.extend(
                    value for value in (
                        f"{row['domain']}.{row['title_ru']}" if row["title_ru"] else "",
                        f"{row['domain']}.{row['title_en']}" if row["title_en"] else "",
                    ) if value
                )
            target = SearchTarget(
                item_id=item_id, section_id=None, domain=row["domain"],
                kind=row["kind"], access_scope=row["access_scope"],
            )
            doc = Doc(
                id=item_id, fields=fields, kind=row["kind"], payload=target,
                exact_keys=[value for value in exact_keys if value],
            )
            documents.append((doc, row["domain"]))
            payloads[doc.id] = target
            domain_payloads.setdefault(row["domain"], {})[doc.id] = target

        for row in self._connection.execute(
            """SELECT s.*, i.domain, i.kind AS item_kind, i.access_scope,
                      i.title_ru AS item_title_ru, i.title_en AS item_title_en
               FROM sections s JOIN items i ON i.id=s.item_id
               ORDER BY s.item_id, s.ordinal"""
        ):
            if row["domain"] == "query":
                continue
            if (
                row["ordinal"] == 1
                and row["title_ru"] == row["item_title_ru"]
                and row["title_en"] == row["item_title_en"]
            ):
                continue
            target = SearchTarget(
                item_id=row["item_id"], section_id=row["id"], domain=row["domain"],
                kind=row["item_kind"], access_scope=row["access_scope"],
            )
            doc = Doc(
                id=row["id"], kind="reference_section", payload=target, boost=0.85,
                fields={
                    "name": row["title_ru"], "name_en": row["title_en"],
                    "parent": f"{row['item_title_ru']} {row['item_title_en']}",
                    "kind": f"{row['domain']} section {row['item_kind']}",
                    "description": row["body"],
                },
                exact_keys=[row["id"], row["title_ru"], row["title_en"]],
            )
            documents.append((doc, row["domain"]))
            payloads[doc.id] = target
            domain_payloads.setdefault(row["domain"], {})[doc.id] = target
        return documents, payloads, domain_payloads

    @staticmethod
    def _build_indices(documents: list[tuple[Doc, str]]) -> tuple[SearchIndex, dict[str, SearchIndex]]:
        index = SearchIndex()
        domains: dict[str, SearchIndex] = {}
        for doc, domain in documents:
            index.add(doc)
            domains.setdefault(domain, SearchIndex()).add(doc)
        return index, domains

    @staticmethod
    def _restore_indices(
        cached: Any,
        payloads: dict[str, SearchTarget],
        domain_payloads: dict[str, dict[str, SearchTarget]],
    ) -> tuple[SearchIndex, dict[str, SearchIndex]] | None:
        if not isinstance(cached, dict):
            return None
        try:
            global_index = SearchIndex.from_state(cached["global"], payloads)
            raw_domains = cached["domains"]
            if not isinstance(raw_domains, dict) or set(raw_domains) != set(domain_payloads):
                return None
            domains = {
                domain: SearchIndex.from_state(raw_domains[domain], domain_payloads[domain])
                for domain in sorted(domain_payloads)
            }
            return global_index, domains
        except (KeyError, TypeError, ValueError):
            return None

    def availability(self, item_id: str, platform: str | None) -> dict[str, Any]:
        facts = self._facts.get(item_id, [])
        observations = self._observations.get(item_id, [])
        introduced = sorted(
            {_release(row["version"]) for row in facts if row["fact_kind"] == "introduced"}
        )
        removed = sorted(
            {_release(row["version"]) for row in facts if row["fact_kind"] == "removed"}
        )
        evidence = [
            {
                "kind": row["evidence_kind"], "fact": row["fact_kind"],
                "version": row["version"], "ref": row["evidence_ref"],
            }
            for row in facts
        ]
        present = []
        for observation in observations:
            version_value = observation["platform_version"]
            if observation["presence"] != "present" or not version_value:
                continue
            present.append((_release(version_value), observation))
        present.sort(key=lambda value: value[0])

        introduced_text = _version_text(introduced[0]) if introduced else None
        removed_text = _version_text(removed[0]) if removed else None
        known_present_in = _version_text(present[0][0]) if present else None
        summary = {
            "introduced": introduced_text,
            "removed": removed_text,
            "known_present_in": known_present_in,
        }

        if platform is None:
            if introduced_text and removed_text:
                reason = (
                    f"Подтверждена версия появления {introduced_text}; "
                    f"элемент удалён начиная с {removed_text}. "
                    "Целевая версия платформы не указана."
                )
            elif introduced_text:
                reason = (
                    f"Подтверждена версия появления {introduced_text}. "
                    "Целевая версия платформы не указана."
                )
            elif known_present_in:
                reason = (
                    f"Элемент присутствует в полном снимке {known_present_in}. "
                    "Точная версия появления не установлена. "
                    "Целевая версия платформы не указана."
                )
                observation = present[0][1]
                evidence.append({
                    "kind": "observation", "presence": "present",
                    "version": observation["platform_version"],
                    "ref": observation["evidence_ref"],
                })
            elif removed_text:
                reason = (
                    f"Элемент удалён начиная с {removed_text}. "
                    "Точная версия появления не установлена. "
                    "Целевая версия платформы не указана."
                )
            else:
                reason = (
                    "Данных о версии появления нет. "
                    "Целевая версия платформы не указана."
                )
            return {
                "status": "unknown", "platform": None, "reason": reason,
                "evidence": evidence, **summary,
            }

        target = _release(platform)
        if introduced and target < introduced[0]:
            return {
                "status": "unavailable", "platform": platform,
                "reason": f"Элемент появился в {_version_text(introduced[0])}.",
                "evidence": evidence, **summary,
            }
        if removed and target >= removed[0]:
            return {
                "status": "unavailable", "platform": platform,
                "reason": f"Элемент удалён начиная с {_version_text(removed[0])}.",
                "evidence": evidence, **summary,
            }
        if introduced:
            return {
                "status": "available", "platform": platform,
                "reason": f"Подтверждена версия появления {_version_text(introduced[0])}.",
                "evidence": evidence, **summary,
            }
        applicable = [value for value in present if value[0] <= target]
        if applicable and not removed:
            version, observation = applicable[-1]
            return {
                "status": "available", "platform": platform,
                "reason": f"Элемент присутствует в полном снимке {_version_text(version)}.",
                "evidence": [{
                    "kind": "observation", "presence": "present",
                    "version": observation["platform_version"],
                    "ref": observation["evidence_ref"],
                }], **summary,
            }
        return {
            "status": "unknown", "platform": platform,
            "reason": "Для этой версии нет достаточного факта или версионного снимка.",
            "evidence": evidence, **summary,
        }

    @staticmethod
    def _visible(target: SearchTarget, include_explicit: bool, include_hidden: bool) -> bool:
        if target.access_scope == "hidden":
            return include_hidden
        if target.access_scope == "explicit":
            return include_explicit
        return True

    def search(
        self,
        query: str,
        *,
        domain: str | None = None,
        kind: str | None = None,
        platform: str | None = None,
        include_explicit: bool = False,
        include_hidden: bool = False,
        limit: int = 10,
    ) -> dict[str, Any]:
        if not query.strip():
            raise ReferenceQueryError("query не должен быть пустым.")
        if not 1 <= limit <= 50:
            raise ReferenceQueryError("limit должен быть от 1 до 50.")
        domains = {row["id"] for row in self._catalog["domains"]}
        if domain and domain not in domains:
            allowed = ", ".join(sorted(domains))
            raise ReferenceQueryError(
                f"Неизвестный domain {domain!r}. Допустимые значения: {allowed}."
            )
        kinds = {
            row["id"]
            for row in self._catalog["kinds"]
            if not domain or row["domain"] == domain
        }
        if kind and kind not in kinds:
            allowed = ", ".join(sorted(kinds))
            boundary = f" в domain {domain!r}" if domain else ""
            raise ReferenceQueryError(
                f"Неизвестный kind {kind!r}{boundary}. "
                f"Допустимые значения: {allowed}."
            )

        # Сам выбор полностью закрытого домена уже является явным намерением.
        # Для дополнительных карточек внутри смешанного домена по-прежнему
        # требуется include_explicit, чтобы они не загрязняли обычную выдачу.
        selected_domain = next(
            (row for row in self._catalog["domains"] if row["id"] == domain),
            None,
        )
        effective_explicit = include_explicit or (
            selected_domain is not None
            and selected_domain["access_scope"] == "explicit"
        )

        def predicate(doc: Doc) -> bool:
            target = doc.payload
            assert isinstance(target, SearchTarget)
            return (
                (not domain or target.domain == domain)
                and (not kind or target.kind == kind)
                and self._visible(target, effective_explicit, include_hidden)
            )

        search_index = self.domain_indices.get(domain, self.index) if domain else self.index
        raw = search_index.search(query, limit=max(100, limit * 12), predicate=predicate)
        available: list[dict[str, Any]] = []
        unavailable: list[dict[str, Any]] = []
        seen: set[str] = set()
        for hit in raw:
            target = hit.doc.payload
            assert isinstance(target, SearchTarget)
            if target.item_id in seen:
                continue
            seen.add(target.item_id)
            item = self._items[target.item_id]
            version = self.availability(target.item_id, platform)
            result = {
                "id": target.item_id, "matched_section_id": target.section_id,
                "domain": target.domain, "kind": target.kind,
                "title_ru": item["title_ru"], "title_en": item["title_en"],
                "signature": item["signature"], "access_scope": target.access_scope,
                "availability": version, "score": round(hit.score, 6),
                "reason": hit.reason,
            }
            if version["status"] == "unavailable":
                unavailable.append(result)
            elif len(available) <= limit:
                available.append(result)
            if len(available) > limit and len(unavailable) >= min(3, limit):
                break
        return {
            "query": query, "domain": domain, "kind": kind, "platform": platform,
            "results": available[:limit],
            "has_more": len(available) > limit,
            "unavailable_matches": unavailable[: min(3, limit)],
        }

    def _render_item(self, item_id: str) -> str:
        item = self._items[item_id]
        parts: list[str] = []
        if item["signature"]:
            parts.extend(("## Синтаксис", item["signature"]))
        if item["body"]:
            # Таблицы выводятся отдельными блоками ниже; метки их прежних
            # позиций не являются текстом справки для пользователя.
            parts.extend(("## Описание", _QUERY_TABLE_MARKER.sub("", item["body"])))
        rows = self._connection.execute(
            "SELECT * FROM parameters WHERE item_id=? ORDER BY ordinal", (item_id,)
        ).fetchall()
        if rows:
            parts.append("## Параметры")
            for row in rows:
                required = "обязательный" if row["required"] else "необязательный"
                parts.append(f"- `{row['name']}` ({required}): {row['description']}")
        examples = self._connection.execute(
            "SELECT * FROM examples WHERE item_id=? ORDER BY ordinal", (item_id,)
        ).fetchall()
        material = [row for row in examples if row["content"]]
        if material:
            parts.append("## Примеры")
            for row in material:
                parts.extend((row["label"], row["content"]))
        for ordinal, row in enumerate(
            self._connection.execute(
                "SELECT * FROM item_tables WHERE item_id=? ORDER BY ordinal", (item_id,)
            ),
            start=1,
        ):
            parts.append(f"## Таблица {ordinal}")
            header = json.loads(row["header_json"])
            table_rows = json.loads(row["rows_json"])
            if header:
                parts.append(" | ".join(str(value) for value in header))
            parts.extend(" | ".join(str(value) for value in values) for values in table_rows)
        template = self._connection.execute(
            "SELECT * FROM templates WHERE item_id=?", (item_id,)
        ).fetchone()
        if template is not None:
            if template["content_ru"]:
                parts.extend(("## Шаблон RU", template["content_ru"]))
            if template["content_en"]:
                parts.extend(("## Шаблон EN", template["content_en"]))
        return "\n\n".join(part for part in parts if part).strip()

    def _content(self, item_id: str, section_id: str | None) -> tuple[str, dict[str, Any]]:
        item = self._items.get(item_id)
        if item is None:
            raise ReferenceQueryError(f"Элемент {item_id!r} не найден.")
        with self._lock:
            if section_id is None:
                content = self._render_item(item_id)
                title_ru, title_en = item["title_ru"], item["title_en"]
            else:
                section = self._connection.execute(
                    "SELECT * FROM sections WHERE id=? AND item_id=?", (section_id, item_id)
                ).fetchone()
                if section is None:
                    raise ReferenceQueryError(
                        f"Раздел {section_id!r} не принадлежит элементу {item_id!r}."
                    )
                content = "\n\n".join(
                    value for value in (
                        section["signature"], _QUERY_TABLE_MARKER.sub("", section["body"]),
                    ) if value
                )
                title_ru, title_en = section["title_ru"], section["title_en"]
        return content, {
            "id": item_id, "section_id": section_id, "domain": item["domain"],
            "kind": item["kind"], "title_ru": title_ru, "title_en": title_en,
            "source_key": item["source_key"], "source_path": item["source_path"],
        }

    @staticmethod
    def _encode_cursor(item_id: str, section_id: str | None, offset: int, digest: str) -> str:
        raw = json.dumps(
            {"item": item_id, "section": section_id, "offset": offset, "sha256": digest},
            ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    @staticmethod
    def _decode_cursor(cursor: str) -> dict[str, Any]:
        try:
            padded = cursor + "=" * (-len(cursor) % 4)
            value = json.loads(base64.urlsafe_b64decode(padded).decode("utf-8"))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ReferenceQueryError("Некорректный курсор продолжения.") from error
        if (
            not isinstance(value, dict)
            or not isinstance(value.get("item"), str)
            or value.get("section") is not None and not isinstance(value.get("section"), str)
            or type(value.get("offset")) is not int
            or not isinstance(value.get("sha256"), str)
        ):
            raise ReferenceQueryError("Некорректный курсор продолжения.")
        return value

    @staticmethod
    def _page_end(content: str, offset: int, maximum: int) -> int:
        hard = min(len(content), offset + maximum)
        if hard == len(content):
            return hard
        minimum = offset + maximum // 2
        paragraph = content.rfind("\n\n", minimum, hard)
        if paragraph >= minimum:
            return paragraph + 2
        newline = content.rfind("\n", minimum, hard)
        return newline + 1 if newline >= minimum else hard

    def get(
        self,
        item_id: str,
        *,
        section_id: str | None = None,
        cursor: str | None = None,
        max_chars: int = DEFAULT_PAGE_CHARS,
        platform: str | None = None,
    ) -> dict[str, Any]:
        if not MIN_PAGE_CHARS <= max_chars <= MAX_PAGE_CHARS:
            raise ReferenceQueryError(
                f"max_chars должен быть от {MIN_PAGE_CHARS} до {MAX_PAGE_CHARS}."
            )
        content, card = self._content(item_id, section_id)
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        offset = 0
        if cursor:
            state = self._decode_cursor(cursor)
            if state["item"] != item_id or state["section"] != section_id:
                raise ReferenceQueryError("Курсор относится к другой карточке или разделу.")
            if state["sha256"] != digest:
                raise ReferenceQueryError(
                    "Карточка изменилась после выдачи курсора; начните чтение заново."
                )
            offset = state["offset"]
        if offset < 0 or offset > len(content):
            raise ReferenceQueryError("Курсор содержит недопустимое смещение.")
        end = self._page_end(content, offset, max_chars)
        next_cursor = (
            self._encode_cursor(item_id, section_id, end, digest)
            if end < len(content) else None
        )
        return {
            "card": card, "availability": self.availability(item_id, platform),
            "content_format": "markdown", "content": content[offset:end],
            "continuation": {
                "offset": offset, "next_offset": end, "total_chars": len(content),
                "next_cursor": next_cursor,
            },
        }


class ReferenceService:
    """Fail-soft состояние необязательного провайдера на один запуск."""

    def __init__(
        self,
        *,
        artifact_path: Path,
        database_path: Path,
        managed_path: Path,
        status: ReferenceStatus,
        provider: ReferenceProvider | None,
        data_dir: Path,
        verifier: ArtifactVerifier,
    ):
        self.data_dir = data_dir
        self.artifact_path = artifact_path
        self.database_path = database_path
        self.managed_path = managed_path
        self.status = status
        self.provider = provider
        self.verifier = verifier
        self.pending_status: ReferenceStatus | None = None
        self._mutation_lock = threading.Lock()

    @classmethod
    def discover(
        cls,
        data_dir: str | Path,
        *,
        database_path: str | Path | None = None,
        verifier: ArtifactVerifier | None = None,
    ) -> "ReferenceService":
        root = Path(data_dir).resolve()
        managed = root / "reference" / REFERENCE_ARTIFACT_NAME
        configured = database_path
        if configured is None:
            configured = os.environ.get(REFERENCE_PATH_ENV, "").strip()
        selected_verifier = verifier or SignedArtifactVerifier()
        if isinstance(configured, str) and configured.casefold() == "off":
            return cls(
                artifact_path=managed,
                database_path=managed, managed_path=managed,
                status=ReferenceStatus(
                    state="disabled", message="Локальная общая справка выключена.",
                    signature="not-checked",
                ),
                provider=None,
                data_dir=root,
                verifier=selected_verifier,
            )
        path = Path(configured).resolve() if configured else managed
        if not path.is_file():
            if path == managed:
                _cleanup_reference_derivatives(root)
            return cls(
                artifact_path=path,
                database_path=path, managed_path=managed,
                status=ReferenceStatus(
                    state="missing", message="Каноническая база не загружена.",
                    signature="not-checked",
                ),
                provider=None,
                data_dir=root,
                verifier=selected_verifier,
            )
        verified: VerifiedArtifact | None = None
        try:
            try:
                verified = selected_verifier.verify(
                    path, root / "index" / "reference"
                )
            except ReferenceValidationError:
                raise
            except Exception:
                return cls(
                    artifact_path=path,
                    database_path=path, managed_path=managed,
                    status=ReferenceStatus(
                        state="untrusted",
                        message="Проверку подписи не удалось выполнить.",
                        signature="verification-error",
                    ),
                    provider=None,
                    data_dir=root,
                    verifier=selected_verifier,
                )
            connection = _connect(verified.database)
            try:
                schema_version, logical, items = _validate_schema(connection)
            finally:
                connection.close()
            if (
                schema_version != verified.schema_version
                or logical != verified.logical_sha256
            ):
                raise ReferenceValidationError(
                    "corrupt",
                    "Логический SHA-256 не совпал с подписанным manifest.",
                    key_id=verified.key_id,
                    signature=verified.signature,
                )
            cache_path = root / "index" / "reference" / "reference.search"
            provider = ReferenceProvider(
                verified.database, cache_path, verified.file_sha256
            )
            return cls(
                artifact_path=path,
                database_path=verified.database, managed_path=managed,
                status=ReferenceStatus(
                    state="ready", message="Каноническая база подключена.",
                    signature=verified.signature, schema_version=schema_version,
                    content_sha256=logical, file_sha256=verified.file_sha256,
                    items=items, index_cache=provider.index_cache_state,
                    key_id=verified.key_id,
                ),
                provider=provider,
                data_dir=root,
                verifier=selected_verifier,
            )
        except ReferenceValidationError as error:
            return cls(
                artifact_path=path,
                database_path=verified.database if verified is not None else path,
                managed_path=managed,
                status=ReferenceStatus(
                    state=error.state, message=str(error),
                    signature=error.signature,
                    key_id=error.key_id,
                ),
                provider=None,
                data_dir=root,
                verifier=selected_verifier,
            )
        except (OSError, sqlite3.Error) as error:
            del error
            return cls(
                artifact_path=path,
                database_path=verified.database if verified is not None else path,
                managed_path=managed,
                status=ReferenceStatus(
                    state="corrupt", message="Каноническая база не читается.",
                    signature=(
                        verified.signature if verified is not None else "not-checked"
                    ),
                    key_id=verified.key_id if verified is not None else None,
                ),
                provider=None,
                data_dir=root,
                verifier=selected_verifier,
            )
        except Exception:
            return cls(
                artifact_path=path,
                database_path=verified.database if verified is not None else path,
                managed_path=managed,
                status=ReferenceStatus(
                    state="corrupt",
                    message="Каноническую базу не удалось проиндексировать.",
                    signature=(
                        verified.signature if verified is not None else "not-checked"
                    ),
                    key_id=verified.key_id if verified is not None else None,
                ),
                provider=None,
                data_dir=root,
                verifier=selected_verifier,
            )

    @property
    def managed_upload_available(self) -> bool:
        return self.artifact_path == self.managed_path

    def payload(self, *, detailed: bool = False) -> dict[str, Any]:
        return {
            "api_version": "v1",
            "active": self.status.payload(detailed=detailed),
            "catalog": self.provider.catalog() if self.provider is not None else None,
            "pending": (
                self.pending_status.payload(detailed=detailed)
                if self.pending_status is not None else None
            ),
            "managed_upload": self.managed_upload_available,
            "managed_file_present": (
                self.managed_upload_available and self.managed_path.is_file()
            ),
            "limits": {"upload_bytes": MAX_REFERENCE_ARTIFACT_BYTES},
        }

    def install_candidate(self, candidate: Path) -> ReferenceStatus:
        """Скопировать candidate, проверить и атомарно установить bundle."""
        with self._mutation_lock:
            return self._install_candidate(candidate)

    def _install_candidate(self, candidate: Path) -> ReferenceStatus:
        if not self.managed_upload_available:
            raise ReferenceValidationError(
                "incompatible",
                "Dashboard upload недоступен при внешнем MCP1C_REFERENCE_ARTIFACT.",
            )
        self.managed_path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, raw_stage = tempfile.mkstemp(
            dir=self.managed_path.parent,
            prefix=".reference-install-",
            suffix=REFERENCE_ARTIFACT_SUFFIX,
        )
        os.close(descriptor)
        stage = Path(raw_stage)
        inspected: ReferenceService | None = None
        closed = False
        try:
            with candidate.open("rb") as source, stage.open("wb") as target:
                size = 0
                while chunk := source.read(1024 * 1024):
                    size += len(chunk)
                    if size > MAX_REFERENCE_ARTIFACT_BYTES:
                        raise ReferenceValidationError(
                            "corrupt", "Размер подписанного артефакта недопустим."
                        )
                    target.write(chunk)
                target.flush()
                os.fsync(target.fileno())
            stage.chmod(0o600)
            inspected = self.discover(
                self.data_dir,
                database_path=stage,
                verifier=self.verifier,
            )
            if inspected.provider is None:
                raise ReferenceValidationError(
                    inspected.status.state,
                    inspected.status.message,
                    key_id=inspected.status.key_id,
                    signature=inspected.status.signature,
                )
            # Подписанный bundle — единственная точка commit: после полной
            # проверки меняется один inode, неполного комплекта не бывает.
            status = inspected.status
            inspected.close()
            closed = True
            stage.replace(self.managed_path)
            candidate.unlink(missing_ok=True)
            self.pending_status = ReferenceStatus(
                state="pending_restart",
                message="База проверена и будет активна после перезапуска сервера.",
                signature=status.signature,
                schema_version=status.schema_version,
                content_sha256=status.content_sha256,
                file_sha256=status.file_sha256,
                items=status.items,
                index_cache=status.index_cache,
                key_id=status.key_id,
                action="activate",
            )
            return self.pending_status
        finally:
            stage.unlink(missing_ok=True)
            if inspected is not None and not closed:
                inspected.close()

    def remove_managed(self) -> ReferenceStatus | None:
        """Снять управляемый файл, сохранив открытый снимок до рестарта."""
        with self._mutation_lock:
            return self._remove_managed()

    def _remove_managed(self) -> ReferenceStatus | None:
        if not self.managed_upload_available:
            raise ReferenceValidationError(
                "incompatible",
                "Dashboard delete недоступен при внешнем MCP1C_REFERENCE_ARTIFACT.",
            )
        if not self.managed_path.is_file():
            raise ReferenceValidationError(
                "missing", "Каноническая база уже отсутствует."
            )

        self.managed_path.unlink()
        _cleanup_reference_derivatives(self.data_dir)

        if self.provider is None:
            # Удаление ещё не активированной загрузки отменяет pending: в
            # текущем процессе справочных инструментов и так не было.
            self.pending_status = None
            return None

        self.pending_status = ReferenceStatus(
            state="pending_restart",
            message=(
                "База удалена и будет отключена после перезапуска сервера."
            ),
            signature=self.status.signature,
            schema_version=self.status.schema_version,
            content_sha256=self.status.content_sha256,
            file_sha256=self.status.file_sha256,
            items=self.status.items,
            index_cache=None,
            key_id=self.status.key_id,
            action="remove",
        )
        return self.pending_status

    def close(self) -> None:
        if self.provider is not None:
            self.provider.close()
