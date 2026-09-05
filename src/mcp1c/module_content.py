"""Безопасные локаторы и единое чтение содержимого модулей.

Публичный адрес модуля не обязан быть обратим в физический путь: код формы
может лежать записью ``module`` внутри ``Form.bin`` или плоского ``.Form``.
Локатор хранит только путь относительно канонического корня поколения и имя
записи. Текст существует лишь на время чтения и в сериализуемое состояние не
попадает.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Mapping

from .bsl_lex import нормализовать
from .readers.modules import read_module_body
from .v8container import V8Container, V8ContainerError, V8ResourceLimitError


_KINDS = frozenset(("file", "container", "compiled"))


def _safe_relative(value: str) -> str:
    try:
        encoded = os.fsencode(value)
    except UnicodeEncodeError as error:
        raise ValueError("Локатор содержит недопустимые символы пути.") from error
    path = PurePosixPath(value)
    if (
        not value
        or b"\x00" in encoded
        or value.startswith("/")
        or "\\" in value
        or path.is_absolute()
        or any(part in ("", ".", "..") for part in path.parts)
    ):
        raise ValueError("Локатор обязан содержать безопасный относительный путь.")
    return path.as_posix()


def _safe_entry(value: str) -> str:
    try:
        encoded = os.fsencode(value)
    except UnicodeEncodeError as error:
        raise ValueError("Имя записи содержит недопустимые символы.") from error
    if (
        not value
        or b"\x00" in encoded
        or value in (".", "..")
        or "/" in value
        or "\\" in value
    ):
        raise ValueError("Имя записи контейнера небезопасно.")
    return value


@dataclass(frozen=True, slots=True)
class ModuleLocator:
    """Компактное положение тела без абсолютного корня и самого текста."""

    kind: str
    relative_path: str
    entry: str = ""

    def __post_init__(self) -> None:
        if self.kind not in _KINDS:
            raise ValueError("Неизвестный вид локатора модуля.")
        object.__setattr__(self, "relative_path", _safe_relative(self.relative_path))
        if self.kind == "container":
            object.__setattr__(self, "entry", _safe_entry(self.entry))
        elif self.entry:
            raise ValueError("Имя записи допустимо только для контейнера.")

    @classmethod
    def file(cls, relative_path: str) -> "ModuleLocator":
        return cls("file", relative_path)

    @classmethod
    def container(cls, relative_path: str, entry: str) -> "ModuleLocator":
        return cls("container", relative_path, entry)

    @classmethod
    def compiled(cls, relative_path: str) -> "ModuleLocator":
        return cls("compiled", relative_path)

    def to_state(self) -> tuple[str, str, str]:
        return self.kind, self.relative_path, self.entry

    @classmethod
    def from_state(cls, state: object) -> "ModuleLocator":
        if not isinstance(state, tuple) or len(state) != 3:
            raise ValueError("Повреждено состояние локатора.")
        kind, relative_path, entry = state
        if not all(isinstance(item, str) for item in state):
            raise ValueError("Повреждено состояние локатора.")
        return cls(kind, relative_path, entry)


@dataclass(frozen=True, slots=True)
class LocatorIdentity:
    """Поколение, которому принадлежат локаторы и четыре индекса."""

    source_id: str
    source_sha256: str
    generation: int

    def to_state(self) -> tuple[str, str, int]:
        return self.source_id, self.source_sha256, self.generation

    @classmethod
    def from_state(cls, state: object) -> "LocatorIdentity":
        if (
            not isinstance(state, tuple)
            or len(state) != 3
            or not isinstance(state[0], str)
            or not isinstance(state[1], str)
            or type(state[2]) is not int
        ):
            raise ValueError("Повреждена идентичность локаторов.")
        return cls(state[0], state[1], state[2])


@dataclass(frozen=True, slots=True)
class LocatorEnvelope:
    """Сериализуемый снимок локаторов ровно одного поколения источника."""

    identity: LocatorIdentity
    locators: Mapping[str, ModuleLocator]

    def __post_init__(self) -> None:
        ordered = dict(sorted(self.locators.items()))
        if not all(isinstance(address, str) and address for address in ordered):
            raise ValueError("Адрес локатора обязан быть непустой строкой.")
        if not all(isinstance(locator, ModuleLocator) for locator in ordered.values()):
            raise ValueError("Каталог содержит не локатор.")
        object.__setattr__(self, "locators", MappingProxyType(ordered))

    def to_state(self) -> dict:
        return {
            "identity": self.identity.to_state(),
            "locators": [
                (address, locator.to_state())
                for address, locator in self.locators.items()
            ],
        }

    @classmethod
    def from_state(
        cls, state: object, expected: LocatorIdentity
    ) -> "LocatorEnvelope | None":
        try:
            if not isinstance(state, dict):
                return None
            identity = LocatorIdentity.from_state(state["identity"])
            if identity != expected:
                return None
            raw_locators = state["locators"]
            if not isinstance(raw_locators, list):
                return None
            locators = {
                address: ModuleLocator.from_state(locator)
                for address, locator in raw_locators
                if isinstance(address, str)
            }
            if len(locators) != len(raw_locators):
                return None
            return cls(identity, locators)
        except (KeyError, TypeError, ValueError):
            return None


class ContentReadError(OSError):
    """Обезличенный отказ чтения одного канонического адреса."""

    def __init__(self, category: str, address: str, reason: str):
        self.category = category
        self.address = address
        self.reason = reason
        super().__init__(f"{address}: {reason}")


def _read_relative_bytes(root: Path, relative_path: str) -> bytes:
    """Открыть каждый сегмент без перехода по вложенным symlink.

    Одной проверки ``resolve()`` недостаточно: между проверкой и чтением цель
    можно подменить. ``dir_fd`` и ``O_NOFOLLOW`` удерживают цепочку каталогов,
    а финальный файл читается через уже открытый дескриптор.
    """

    parts = PurePosixPath(relative_path).parts
    flags_directory = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    flags_no_follow = getattr(os, "O_NOFOLLOW", 0)
    opened: list[int] = []
    try:
        current = os.open(root, flags_directory)
        opened.append(current)
        for part in parts[:-1]:
            current = os.open(
                part,
                flags_directory | flags_no_follow,
                dir_fd=current,
            )
            opened.append(current)
        file_fd = os.open(parts[-1], os.O_RDONLY | flags_no_follow, dir_fd=current)
        with os.fdopen(file_fd, "rb") as stream:
            return stream.read()
    finally:
        for descriptor in reversed(opened):
            os.close(descriptor)


def _read_bytes(root: Path, address: str, locator: ModuleLocator) -> bytes:
    if locator.kind == "compiled":
        raise ContentReadError(
            "compiled_without_source",
            address,
            "исходный текст модуля поставлен скомпилированным",
        )
    if locator.kind == "file":
        try:
            return _read_relative_bytes(root, locator.relative_path)
        except OSError as error:
            raise ContentReadError(
                "file_unreadable", address, "файл модуля недоступен"
            ) from error

    try:
        raw_container = _read_relative_bytes(root, locator.relative_path)
        with V8Container(raw_container) as container:
            return container.read(locator.entry)
    except KeyError as error:
        raise ContentReadError(
            "container_entry_missing",
            address,
            "в контейнере нет ожидаемой записи модуля",
        ) from error
    except V8ResourceLimitError as error:
        raise ContentReadError(
            "budget_exceeded",
            address,
            "контейнер модуля превысил вычислительный предел",
        ) from error
    except (OSError, V8ContainerError) as error:
        raise ContentReadError(
            "container_unreadable", address, "контейнер модуля не прочитан"
        ) from error


def read_bsl(
    root: Path,
    address: str,
    locator: ModuleLocator,
) -> str:
    """Прочитать обычный файл или запись контейнера одним нормализатором."""

    raw = read_content_bytes(root, address, locator)
    result = read_module_body(raw)
    if result.state == "unreadable":
        raise ContentReadError(
            "module_invalid_utf8", address, result.reason
        )
    assert result.text is not None
    return нормализовать(result.text)


def read_content_bytes(
    root: Path,
    address: str,
    locator: ModuleLocator,
) -> bytes:
    """Прочитать файл или запись контейнера без текстовой интерпретации."""
    return _read_bytes(root, address, locator)
