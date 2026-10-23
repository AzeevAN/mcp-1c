"""Минимальный внутренний startup-контракт capability-модулей.

Каталог содержит только заранее известные модули mcp-1c. Это не система
сторонних плагинов: произвольные import path из окружения не принимаются.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
import logging
import os
from pathlib import Path
import stat
import threading
import uuid

from . import fsio


SERVER_SETTINGS_NAME = "server-settings.json"
SERVER_SETTINGS_VERSION = 2
MAX_SERVER_SETTINGS_BYTES = 64 * 1024
logger = logging.getLogger(__name__)
_RETIRED_CAPABILITIES = frozenset({"forms", "metadata_authoring"})


class CapabilityConfigurationError(ValueError):
    """Оператор задал неоднозначный или неизвестный набор модулей."""


class CapabilityContractError(RuntimeError):
    """Startup-набор capability противоречит сборке сервера."""


@dataclass(frozen=True)
class CapabilityDefinition:
    """Имя настройки и её описание для панели «Дополнительные модули».

    Инструменты всех трёх capability регистрирует `server.py`: им нужны
    Registry, подписанный пакет справки или HTTP-маршрут, то есть то, что
    есть только у сборки сервера. Отдельного загрузчика модулей нет.
    """

    name: str
    display_name: str = ""
    description: str = ""
    tool_count: int | None = None
    approx_tokens: int | None = None
    tokenizer: str | None = None
    measured_at: str | None = None
    measurement_command: str | None = None
    default_enabled: bool = False


CAPABILITY_DEFINITIONS: Mapping[str, CapabilityDefinition] = {
    "module_source_download": CapabilityDefinition(
        "module_source_download",
        "Скачивание полного модуля",
        "Добавляет инструмент для получения полного исходника точного модуля BSL через краткоживущий билет и HTTP-ссылку.",
        1, 514, "o200k_base", "2026-10-02",
        "tools/measure_capability_context.py module_source_download --check",
        default_enabled=True,
    ),
    "reference": CapabilityDefinition(
        "reference",
        "Общая справка",
        "Добавляет 2 инструмента для поиска и чтения встроенного подписанного пакета общей справки по BSL, языку запросов, СКД и Конфигуратору.",
        2, 1070, "o200k_base", "2026-09-21",
        "tools/measure_capability_context.py reference --check",
    ),
    "role_access": CapabilityDefinition(
        "role_access",
        "Доступ ролей",
        "Поиск ролей и чтение объявленных прав с ограниченной детализацией.",
        2, 1245, "o200k_base", "2026-09-22",
        "tools/measure_capability_context.py role_access --check",
    ),
}


def _normalize_names(
    requested: object,
    *,
    source: str,
    definitions: Mapping[str, CapabilityDefinition],
) -> tuple[str, ...]:
    if not isinstance(requested, list) or any(
        not isinstance(name, str) for name in requested
    ):
        raise CapabilityConfigurationError(
            f"{source}: `capabilities.enabled` должен быть массивом имён."
        )
    if len(set(requested)) != len(requested):
        raise CapabilityConfigurationError(
            f"{source}: имя capability-модуля нельзя повторять."
        )
    unknown = [name for name in requested if name not in definitions]
    if unknown:
        joined = ", ".join(f"`{name}`" for name in unknown)
        raise CapabilityConfigurationError(
            f"{source}: неизвестные capability-модули: {joined}."
        )
    return tuple(name for name in definitions if name in requested)


def _strict_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise CapabilityConfigurationError(
                f"server settings содержит повторяющийся ключ `{key}`."
            )
        result[key] = value
    return result


def _reject_json_constant(value: str) -> object:
    raise CapabilityConfigurationError(
        f"server settings содержит недопустимое JSON-значение `{value}`."
    )


def _validate_json_unicode(payload: object) -> None:
    pending = [payload]
    while pending:
        value = pending.pop()
        if isinstance(value, str):
            value.encode("utf-8")
        elif isinstance(value, dict):
            pending.extend(value.keys())
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)


class CapabilitySettingsStore:
    """Версионированная секция capability в общем server settings-файле."""

    def __init__(
        self,
        data_dir: str | Path,
        *,
        fallback: tuple[str, ...] | None = None,
        definitions: Mapping[str, CapabilityDefinition] = CAPABILITY_DEFINITIONS,
    ) -> None:
        self.path = Path(data_dir) / SERVER_SETTINGS_NAME
        self.definitions = definitions
        self.fallback = _normalize_names(
            list(fallback if fallback is not None else (
                name for name, definition in definitions.items()
                if definition.default_enabled
            )),
            source="capability fallback",
            definitions=definitions,
        )
        self._lock = threading.RLock()

    def normalize(self, enabled: object) -> tuple[str, ...]:
        """Проверить полный desired-набор до любой записи."""
        return _normalize_names(
            enabled,
            source=str(self.path),
            definitions=self.definitions,
        )

    def _read_payload(self) -> dict | None:
        try:
            descriptor = os.open(
                self.path,
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0),
            )
        except FileNotFoundError:
            return None
        except OSError as error:
            raise CapabilityConfigurationError(
                f"{self.path}: не удалось открыть server settings."
            ) from error
        try:
            with os.fdopen(descriptor, "rb") as stream:
                metadata = os.fstat(stream.fileno())
                if not stat.S_ISREG(metadata.st_mode):
                    raise CapabilityConfigurationError(
                        f"{self.path}: server settings должен быть обычным файлом."
                    )
                if metadata.st_size > MAX_SERVER_SETTINGS_BYTES:
                    raise CapabilityConfigurationError(
                        f"{self.path}: server settings превышает допустимый размер."
                    )
                encoded = stream.read(MAX_SERVER_SETTINGS_BYTES + 1)
            if len(encoded) > MAX_SERVER_SETTINGS_BYTES:
                raise CapabilityConfigurationError(
                    f"{self.path}: server settings превышает допустимый размер."
                )
            payload = json.loads(
                encoded.decode("utf-8"),
                object_pairs_hook=_strict_json_object,
                parse_constant=_reject_json_constant,
            )
            _validate_json_unicode(payload)
        except CapabilityConfigurationError:
            raise
        except (OSError, RecursionError, UnicodeError, ValueError) as error:
            raise CapabilityConfigurationError(
                f"{self.path}: не удалось прочитать server settings."
            ) from error
        if (
            not isinstance(payload, dict)
            or type(payload.get("version")) is not int
            or payload["version"] not in (1, SERVER_SETTINGS_VERSION)
        ):
            raise CapabilityConfigurationError(
                f"{self.path}: неподдерживаемый формат server settings."
            )
        capabilities = payload.get("capabilities")
        if not isinstance(capabilities, dict) or set(capabilities) != {"enabled"}:
            raise CapabilityConfigurationError(
                f"{self.path}: повреждена секция `capabilities`."
            )
        enabled = capabilities.get("enabled")
        if not isinstance(enabled, list) or any(not isinstance(name, str) for name in enabled):
            raise CapabilityConfigurationError(
                f"{self.path}: `capabilities.enabled` должен быть массивом имён."
            )
        if len(set(enabled)) != len(enabled):
            raise CapabilityConfigurationError(
                f"{self.path}: имя capability-модуля нельзя повторять."
            )
        _normalize_names(
            [name for name in enabled if name not in _RETIRED_CAPABILITIES],
            source=str(self.path),
            definitions=self.definitions,
        )
        return payload

    def load_optional(self) -> tuple[str, ...] | None:
        """Вернуть сохранённый выбор или ``None``, если файла ещё нет."""
        with self._lock:
            payload = self._read_payload()
            if payload is None:
                return None
            enabled = [name for name in payload["capabilities"]["enabled"]
                       if name not in _RETIRED_CAPABILITIES]
            if payload["version"] == 1:
                # В старом формате переключателя скачивания не было: оно
                # было включено всегда, поэтому сохраняем это поведение.
                enabled = [*enabled, "module_source_download"] if (
                    "module_source_download" in self.definitions
                    and "module_source_download" not in enabled
                ) else enabled
            return _normalize_names(
                enabled,
                source=str(self.path),
                definitions=self.definitions,
            )

    def load(self) -> tuple[str, ...]:
        """Прочитать desired-набор; defaults действуют только без файла."""
        stored = self.load_optional()
        if stored is not None:
            return stored
        return self.fallback

    def save(self, enabled: tuple[str, ...]) -> tuple[str, ...]:
        """Атомарно заменить секцию; успешный replace считается commit-точкой."""
        normalized = self.normalize(list(enabled))
        with self._lock:
            payload = self._read_payload()
            if payload is None:
                payload = {"version": SERVER_SETTINGS_VERSION}
            payload["version"] = SERVER_SETTINGS_VERSION
            payload["capabilities"] = {"enabled": list(normalized)}
            encoded = (
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n"
            ).encode("utf-8")
            if len(encoded) > MAX_SERVER_SETTINGS_BYTES:
                raise CapabilityConfigurationError(
                    f"{self.path}: server settings превышает допустимый размер."
                )

            def sync_parent(directory: int) -> None:
                try:
                    os.fsync(directory)
                except OSError:
                    # После replace ошибка барьера не отменяет видимую запись.
                    logger.warning(
                        "%s: server settings заменён, но каталог не синхронизирован.",
                        self.path,
                        exc_info=True,
                    )

            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                directory = os.open(
                    self.path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
                )
                try:
                    with fsio.atomic_writer(
                        directory, self.path.name, mode=0o600,
                        temporary_name=f".{self.path.name}.tmp-{uuid.uuid4().hex}",
                        sync_parent=sync_parent,
                    ) as stream:
                        stream.write(encoded)
                finally:
                    os.close(directory)
            except OSError as error:
                raise CapabilityConfigurationError(
                    f"{self.path}: не удалось сохранить server settings."
                ) from error
        return normalized


@dataclass(frozen=True)
class CapabilityRuntime:
    """Активный startup-набор и сохранённый desired-набор для dashboard."""

    store: CapabilitySettingsStore
    active: tuple[str, ...]

    def payload(self) -> dict[str, object]:
        desired = self.store.load()
        return self._payload(desired)

    def _payload(self, desired: tuple[str, ...]) -> dict[str, object]:
        return {
            "available": list(self.store.definitions),
            "modules": [
                {
                    "id": definition.name,
                    "display_name": definition.display_name or definition.name,
                    "description": definition.description,
                    "tool_count": definition.tool_count,
                    "approx_tokens": definition.approx_tokens,
                    "tokenizer": definition.tokenizer,
                    "measured_at": definition.measured_at,
                    "measurement_command": definition.measurement_command,
                    "active": definition.name in self.active,
                    "desired": definition.name in desired,
                    "pending_restart": (
                        (definition.name in self.active)
                        != (definition.name in desired)
                    ),
                }
                for definition in self.store.definitions.values()
            ],
            "active": list(self.active),
            "desired": list(desired),
            "pending_restart": desired != self.active,
        }

    def save_desired(self, desired: tuple[str, ...]) -> dict[str, object]:
        """Сохранить полный выбор и вернуть статус именно этой записи."""
        saved = self.store.save(desired)
        return self._payload(saved)

    def pending_restart(self) -> bool:
        return self.store.load() != self.active


def resolve_capability_settings(
    data_dir: str | Path,
    *,
    definitions: Mapping[str, CapabilityDefinition] = CAPABILITY_DEFINITIONS,
) -> tuple[CapabilitySettingsStore, tuple[str, ...]]:
    """Выбрать сохранённый набор или встроенные значения по умолчанию."""
    stored = CapabilitySettingsStore(
        data_dir,
        definitions=definitions,
    )
    return stored, stored.load()
