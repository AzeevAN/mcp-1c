"""Минимальный внутренний startup-контракт capability-модулей.

Каталог содержит только заранее известные модули mcp-1c. Это не система
сторонних плагинов: произвольные import path из окружения не принимаются.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from importlib import import_module
import json
import logging
import os
from pathlib import Path
import re
import stat
import threading
import uuid
from typing import Final


CAPABILITIES_ENV = "MCP1C_CAPABILITIES"
SERVER_SETTINGS_NAME = "server-settings.json"
SERVER_SETTINGS_VERSION = 1
MAX_SERVER_SETTINGS_BYTES = 64 * 1024
_NAME_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_NO_ENV_FALLBACK: Final = object()
logger = logging.getLogger(__name__)


class CapabilityConfigurationError(ValueError):
    """Оператор задал неоднозначный или неизвестный набор модулей."""


class CapabilityContractError(RuntimeError):
    """Внутренний модуль нарушил контракт загрузки или имён инструментов."""


@dataclass(frozen=True)
class CapabilityDefinition:
    """Имя настройки и закрытый import path фабрики внутреннего модуля."""

    name: str
    loader: str


@dataclass(frozen=True)
class CapabilityTool:
    """Один инструмент, который модуль предлагает основному MCP-серверу."""

    name: str
    function: Callable
    description: str


@dataclass(frozen=True)
class CapabilityModule:
    """Полностью загруженный набор инструментов одного внутреннего модуля."""

    name: str
    tools: tuple[CapabilityTool, ...]


CAPABILITY_DEFINITIONS: Mapping[str, CapabilityDefinition] = {
    "forms": CapabilityDefinition(
        "forms",
        "mcp1c.capability_modules.forms:load",
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
        fallback: tuple[str, ...] = (),
        fallback_environment: str | None | object = _NO_ENV_FALLBACK,
        definitions: Mapping[str, CapabilityDefinition] = CAPABILITY_DEFINITIONS,
    ) -> None:
        self.path = Path(data_dir) / SERVER_SETTINGS_NAME
        self.definitions = definitions
        self.fallback = _normalize_names(
            list(fallback),
            source="capability fallback",
            definitions=definitions,
        )
        self.fallback_environment = fallback_environment
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
            or payload["version"] != SERVER_SETTINGS_VERSION
        ):
            raise CapabilityConfigurationError(
                f"{self.path}: неподдерживаемый формат server settings."
            )
        capabilities = payload.get("capabilities")
        if not isinstance(capabilities, dict) or set(capabilities) != {"enabled"}:
            raise CapabilityConfigurationError(
                f"{self.path}: повреждена секция `capabilities`."
            )
        _normalize_names(
            capabilities.get("enabled"),
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
            return _normalize_names(
                payload["capabilities"]["enabled"],
                source=str(self.path),
                definitions=self.definitions,
            )

    def load(self) -> tuple[str, ...]:
        """Прочитать desired-набор; fallback действует только без файла."""
        stored = self.load_optional()
        if stored is not None:
            return stored
        if self.fallback_environment is not _NO_ENV_FALLBACK:
            return parse_capability_config(
                self.fallback_environment,
                definitions=self.definitions,
            )
        return self.fallback

    def save(self, enabled: tuple[str, ...]) -> tuple[str, ...]:
        """Атомарно заменить секцию; успешный replace считается commit-точкой."""
        normalized = self.normalize(list(enabled))
        with self._lock:
            payload = self._read_payload()
            if payload is None:
                payload = {"version": SERVER_SETTINGS_VERSION}
            payload["capabilities"] = {"enabled": list(normalized)}
            encoded = (
                json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n"
            ).encode("utf-8")
            if len(encoded) > MAX_SERVER_SETTINGS_BYTES:
                raise CapabilityConfigurationError(
                    f"{self.path}: server settings превышает допустимый размер."
                )
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                temporary = self.path.with_name(
                    f".{self.path.name}.tmp-{uuid.uuid4().hex}"
                )
                descriptor = os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                    0o600,
                )
                try:
                    with os.fdopen(descriptor, "wb") as stream:
                        stream.write(encoded)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.replace(temporary, self.path)
                    try:
                        directory = os.open(
                            self.path.parent,
                            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
                        )
                        try:
                            os.fsync(directory)
                        finally:
                            os.close(directory)
                    except OSError:
                        # После replace новые bytes уже видимы текущему процессу.
                        # Ошибка durability-барьера не должна превращать
                        # применённую запись в ложный HTTP-отказ.
                        logger.warning(
                            "%s: server settings заменён, но каталог не синхронизирован.",
                            self.path,
                            exc_info=True,
                        )
                finally:
                    temporary.unlink(missing_ok=True)
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
    environment: str | None,
    definitions: Mapping[str, CapabilityDefinition] = CAPABILITY_DEFINITIONS,
) -> tuple[CapabilitySettingsStore, tuple[str, ...]]:
    """Выбрать file-first startup config, сохранив env лишь как bootstrap."""
    stored = CapabilitySettingsStore(
        data_dir,
        fallback_environment=environment,
        definitions=definitions,
    )
    selected = stored.load_optional()
    if selected is not None:
        return stored, selected
    fallback = stored.load()
    return stored, fallback


def parse_capability_config(
    value: str | None,
    *,
    definitions: Mapping[str, CapabilityDefinition] = CAPABILITY_DEFINITIONS,
) -> tuple[str, ...]:
    """Разобрать точные имена без импорта реализаций.

    Отсутствующая переменная и буквальное ``off`` означают пустой набор.
    Пустая строка, пробелы, повторы, смесь ``off`` с именем и неизвестные
    значения отклоняются: неоднозначная настройка не должна частично включать
    сервер.
    """
    if value is None or value == "off":
        return ()
    if not value or len(value) > 1024:
        raise CapabilityConfigurationError(
            f"{CAPABILITIES_ENV}: укажите `off` или точные имена модулей."
        )

    requested = value.split(",")
    if any(not item or item != item.strip() for item in requested):
        raise CapabilityConfigurationError(
            f"{CAPABILITIES_ENV}: пустые элементы и пробелы недопустимы."
        )
    if "off" in requested:
        raise CapabilityConfigurationError(
            f"{CAPABILITIES_ENV}: `off` нельзя сочетать с именами модулей."
        )
    if len(set(requested)) != len(requested):
        raise CapabilityConfigurationError(
            f"{CAPABILITIES_ENV}: имя модуля нельзя повторять."
        )

    unknown = [name for name in requested if name not in definitions]
    if unknown:
        joined = ", ".join(f"`{name}`" for name in unknown)
        raise CapabilityConfigurationError(
            f"{CAPABILITIES_ENV}: неизвестные capability-модули: {joined}."
        )

    # Порядок tools/list задаёт код, а не порядок имён в окружении.
    return tuple(name for name in definitions if name in requested)


def _load_factory(definition: CapabilityDefinition) -> Callable[[], object]:
    module_name, separator, attribute = definition.loader.partition(":")
    if not separator or not module_name or not attribute:
        raise CapabilityContractError(
            f"Capability-модуль `{definition.name}` имеет некорректный loader."
        )
    try:
        module = import_module(module_name)
        factory = getattr(module, attribute)
    except Exception as error:
        raise CapabilityContractError(
            f"Capability-модуль `{definition.name}` не удалось загрузить."
        ) from error
    if not callable(factory):
        raise CapabilityContractError(
            f"Loader capability-модуля `{definition.name}` не является функцией."
        )
    return factory


def load_capability_modules(
    names: tuple[str, ...],
    *,
    definitions: Mapping[str, CapabilityDefinition] = CAPABILITY_DEFINITIONS,
    dependencies: Mapping[str, object] | None = None,
) -> tuple[CapabilityModule, ...]:
    """Лениво импортировать и инициализировать только выбранные модули."""
    dependencies = dependencies or {}
    loaded: list[CapabilityModule] = []
    for name in names:
        definition = definitions.get(name)
        if definition is None:
            raise CapabilityConfigurationError(
                f"{CAPABILITIES_ENV}: неизвестный capability-модуль `{name}`."
            )
        factory = _load_factory(definition)
        try:
            raw_tools = (
                factory(dependencies[name])
                if name in dependencies
                else factory()
            )
            tools = tuple(raw_tools)
        except Exception as error:
            raise CapabilityContractError(
                f"Capability-модуль `{name}` не удалось инициализировать."
            ) from error
        if any(not isinstance(tool, CapabilityTool) for tool in tools):
            raise CapabilityContractError(
                f"Capability-модуль `{name}` вернул объект вне контракта."
            )
        loaded.append(CapabilityModule(name, tools))
    return tuple(loaded)


def validate_capability_tool(module: str, tool: CapabilityTool) -> None:
    """Проверить локальную часть контракта до изменения каталога SDK."""
    if not isinstance(module, str) or not _NAME_PATTERN.fullmatch(module):
        raise CapabilityContractError(
            f"Некорректное имя capability-модуля `{module}`."
        )
    if not isinstance(tool.name, str) or not _NAME_PATTERN.fullmatch(tool.name):
        raise CapabilityContractError(
            f"Capability-модуль `{module}` объявил некорректное имя `{tool.name}`."
        )
    if (
        not callable(tool.function)
        or not isinstance(tool.description, str)
        or not tool.description.strip()
    ):
        raise CapabilityContractError(
            f"Capability-инструмент `{tool.name}` не имеет функции или описания."
        )
