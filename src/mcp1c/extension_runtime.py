"""Снимок расширений, которые платформа видит в конкретном сеансе.

Это отдельный источник, а не часть schema v1: структура конфигурации живёт
долго, а активность расширений меняется при подключении, отключении и новом
сеансе. Порядок списков сохраняется дословно, но сам по себе не объявляется
порядком исполнения модулей — справка метода ``Получить`` этого не обещает.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Mapping


FORMAT = "mcp1c-extension-runtime"
SCHEMA_VERSION = 1
SCOPE = "current_session_current_data_area"
MAX_BYTES = 2 * 1024 * 1024

_TIMESTAMP = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$"
)
_TOP_KEYS = {
    "format",
    "schema_version",
    "snapshot_id",
    "captured_at",
    "scope",
    "configuration",
    "database_changed_since_session_start",
    "database",
    "session_active",
    "session_disabled",
}
_CONFIGURATION_KEYS = {"name", "version", "platform"}
_EXTENSION_KEYS = {
    "uuid",
    "name",
    "synonym",
    "version",
    "purpose",
    "scope",
    "enabled",
}


class ExtensionRuntimeError(ValueError):
    """Снимок не соответствует контракту и не должен публиковаться."""


@dataclass(frozen=True, slots=True)
class RuntimeConfiguration:
    name: str
    version: str
    platform: str


@dataclass(frozen=True, slots=True)
class RuntimeExtension:
    uuid: str
    name: str
    synonym: str
    version: str
    purpose: str
    scope: str
    enabled: bool
    database_position: int | None = None
    session_position: int | None = None
    active_in_session: bool | None = None


@dataclass(frozen=True, slots=True)
class ExtensionState:
    uuid: str
    name: str
    active_in_session: bool | None
    registered_in_database: bool
    enabled_in_database: bool | None


@dataclass(frozen=True, slots=True)
class ExtensionRuntimeSnapshot:
    snapshot_id: str
    captured_at: str
    scope: str
    configuration: RuntimeConfiguration
    database_changed_since_session_start: bool | None
    database: tuple[RuntimeExtension, ...]
    session_active: tuple[RuntimeExtension, ...]
    session_disabled: tuple[RuntimeExtension, ...]
    by_uuid: Mapping[str, ExtensionState]

    def state_for_name(self, name: str) -> ExtensionState | None:
        """Однозначное точное имя; коллизия возвращает неизвестность."""
        matches = [
            state
            for state in self.by_uuid.values()
            if state.name.casefold() == name.casefold()
        ]
        return matches[0] if len(matches) == 1 else None


def _object(value: object, where: str) -> dict:
    if not isinstance(value, dict):
        raise ExtensionRuntimeError(f"{where}: ожидался объект JSON")
    return value


def _exact_keys(value: dict, expected: set[str], where: str) -> None:
    missing = sorted(expected - set(value))
    extra = sorted(set(value) - expected)
    if missing:
        raise ExtensionRuntimeError(
            f"{where}: отсутствуют обязательные поля: {', '.join(missing)}"
        )
    if extra:
        raise ExtensionRuntimeError(
            f"{where}: неизвестные поля: {', '.join(extra)}"
        )


def _text(
    value: object, where: str, *, required: bool = False, limit: int = 500
) -> str:
    if not isinstance(value, str):
        raise ExtensionRuntimeError(f"{where}: ожидалась строка")
    if required and not value.strip():
        raise ExtensionRuntimeError(f"{where}: пустое значение недопустимо")
    if len(value) > limit:
        raise ExtensionRuntimeError(f"{where}: строка длиннее {limit} символов")
    return value


def _uuid(value: object, where: str) -> str:
    raw = _text(value, where, required=True, limit=64)
    try:
        return str(uuid.UUID(raw))
    except ValueError as error:
        raise ExtensionRuntimeError(f"{where}: некорректный UUID") from error


def _extension(raw: object, where: str, *, position: int, source: str) -> RuntimeExtension:
    value = _object(raw, where)
    _exact_keys(value, _EXTENSION_KEYS, where)
    enabled = value["enabled"]
    if type(enabled) is not bool:
        raise ExtensionRuntimeError(f"{where}.enabled: ожидалось true или false")
    return RuntimeExtension(
        uuid=_uuid(value["uuid"], f"{where}.uuid"),
        name=_text(value["name"], f"{where}.name", required=True, limit=200),
        synonym=_text(value["synonym"], f"{where}.synonym"),
        version=_text(value["version"], f"{where}.version", limit=100),
        purpose=_text(value["purpose"], f"{where}.purpose", limit=100),
        scope=_text(value["scope"], f"{where}.scope", limit=100),
        enabled=enabled,
        database_position=position if source == "database" else None,
        session_position=position if source == "session_active" else None,
        active_in_session=(
            True
            if source == "session_active"
            else False if source == "session_disabled" else None
        ),
    )


def _extension_list(value: object, where: str, source: str) -> tuple[RuntimeExtension, ...]:
    if not isinstance(value, list):
        raise ExtensionRuntimeError(f"{where}: ожидался массив")
    result = tuple(
        _extension(raw, f"{where}[{position - 1}]", position=position, source=source)
        for position, raw in enumerate(value, 1)
    )
    ids = [item.uuid for item in result]
    if len(ids) != len(set(ids)):
        raise ExtensionRuntimeError(f"{where}: UUID расширения повторяется")
    return result


def _parse(payload: object) -> ExtensionRuntimeSnapshot:
    value = _object(payload, "снимок")
    _exact_keys(value, _TOP_KEYS, "снимок")
    if value["format"] != FORMAT:
        raise ExtensionRuntimeError(
            f"снимок.format: ожидалось {FORMAT!r}"
        )
    if type(value["schema_version"]) is not int or value["schema_version"] != SCHEMA_VERSION:
        raise ExtensionRuntimeError(
            f"снимок.schema_version: поддерживается только {SCHEMA_VERSION}"
        )
    snapshot_id = _uuid(value["snapshot_id"], "снимок.snapshot_id")
    captured_at = _text(
        value["captured_at"], "снимок.captured_at", required=True, limit=40
    )
    if not _TIMESTAMP.fullmatch(captured_at):
        raise ExtensionRuntimeError(
            "снимок.captured_at: нужна дата RFC 3339 с часовым поясом"
        )
    if value["scope"] != SCOPE:
        raise ExtensionRuntimeError(
            f"снимок.scope: поддерживается только {SCOPE!r}"
        )

    config_raw = _object(value["configuration"], "снимок.configuration")
    _exact_keys(config_raw, _CONFIGURATION_KEYS, "снимок.configuration")
    configuration = RuntimeConfiguration(
        name=_text(
            config_raw["name"], "снимок.configuration.name", required=True, limit=200
        ),
        version=_text(
            config_raw["version"], "снимок.configuration.version", limit=100
        ),
        platform=_text(
            config_raw["platform"], "снимок.configuration.platform", limit=100
        ),
    )
    changed = value["database_changed_since_session_start"]
    if changed is not None and type(changed) is not bool:
        raise ExtensionRuntimeError(
            "снимок.database_changed_since_session_start: ожидалось true, "
            "false или null для платформ до 8.3.22"
        )

    database = _extension_list(value["database"], "снимок.database", "database")
    active = _extension_list(
        value["session_active"], "снимок.session_active", "session_active"
    )
    disabled = _extension_list(
        value["session_disabled"], "снимок.session_disabled", "session_disabled"
    )
    active_ids = {item.uuid for item in active}
    overlap = active_ids & {item.uuid for item in disabled}
    if overlap:
        raise ExtensionRuntimeError(
            "расширение не может быть одновременно действующим и не применённым "
            "в одном снимке"
        )

    by_database = {item.uuid: item for item in database}
    by_active = {item.uuid: item for item in active}
    by_disabled = {item.uuid: item for item in disabled}
    all_ids = tuple(dict.fromkeys((*by_database, *by_active, *by_disabled)))
    states: dict[str, ExtensionState] = {}
    for extension_id in all_ids:
        database_item = by_database.get(extension_id)
        session_item = by_active.get(extension_id) or by_disabled.get(extension_id)
        item = session_item or database_item
        assert item is not None
        states[extension_id] = ExtensionState(
            uuid=extension_id,
            name=item.name,
            active_in_session=(
                True
                if extension_id in by_active
                else False if extension_id in by_disabled else None
            ),
            registered_in_database=database_item is not None,
            enabled_in_database=(
                database_item.enabled if database_item is not None else None
            ),
        )

    return ExtensionRuntimeSnapshot(
        snapshot_id=snapshot_id,
        captured_at=captured_at,
        scope=SCOPE,
        configuration=configuration,
        database_changed_since_session_start=changed,
        database=database,
        session_active=active,
        session_disabled=disabled,
        by_uuid=MappingProxyType(states),
    )


def load_extension_runtime(path: str | Path) -> ExtensionRuntimeSnapshot:
    """Прочитать малый JSON-снимок с явным пределом и строгой схемой."""
    source = Path(path)
    try:
        size = source.stat().st_size
    except OSError as error:
        raise ExtensionRuntimeError("файл снимка недоступен") from error
    if size > MAX_BYTES:
        raise ExtensionRuntimeError(
            f"снимок больше допустимых {MAX_BYTES} байт"
        )
    try:
        raw = json.loads(source.read_text(encoding="utf-8"))
    except UnicodeError as error:
        raise ExtensionRuntimeError("снимок должен быть в UTF-8") from error
    except json.JSONDecodeError as error:
        raise ExtensionRuntimeError("снимок не является корректным JSON") from error
    return _parse(raw)
