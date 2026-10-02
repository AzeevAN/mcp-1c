"""Единая проверка пролога XML перед серверным разбором.

Выгрузки 1С, справка и schema v1 DTD не содержат. Внутренний DTD опасен не
внешними сущностями (expat их не грузит), а раскрытием внутренних: лимит
входа в 8–128 МиБ превращается в сотни мегабайт текста, прежде чем expat
сработает на коэффициенте усиления. Поэтому до любого разбора пролог
читается отдельным expat-парсером, который останавливается на первом же
элементе: отказ на ``<!DOCTYPE``, а сущность без DOCTYPE expat объявить не
даст. Читается только пролог, не весь файл; прочитанные байты возвращаются
разборщику, так что поток не нужно перематывать.

Кодировку определяет тот же expat, что и у ElementTree: BOM, UTF-16 и
однобайтовые кодировки из XML-декларации распознаются одинаково, и обойти
проверку сменой кодировки нельзя.
"""

from __future__ import annotations

import io
import os
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from pyexpat import ExpatError, ParserCreate
from typing import BinaryIO


# Пролог реальной выгрузки — BOM и XML-декларация, десятки байт. Длинный
# пролог из комментариев лишь удлиняет проверку, поэтому он ограничен.
PROLOG_LIMIT = 64 * 1024
_CHUNK = 4096


class XmlDeclarationError(ET.ParseError):
    """В прологе XML найден DOCTYPE/ENTITY либо пролог слишком длинный.

    Наследует ``ET.ParseError``: все места, где битый XML уже обрабатывается,
    отвергают такой вход по тому же пути, без новой ветки.
    """


class _RootReached(Exception):
    pass


def _reject_doctype(*_args: object) -> None:
    raise XmlDeclarationError(
        "XML содержит DOCTYPE/ENTITY: объявления DTD в выгрузках 1С не "
        "встречаются и не поддерживаются"
    )


def _stop_at_root(*_args: object) -> None:
    raise _RootReached


class _PrologChecker:
    def __init__(self) -> None:
        self._parser = ParserCreate()
        self._parser.StartDoctypeDeclHandler = _reject_doctype
        self._parser.EntityDeclHandler = _reject_doctype
        self._parser.StartElementHandler = _stop_at_root
        self._seen = 0
        self.done = False

    def feed(self, chunk: bytes | str, final: bool = False) -> None:
        """Скормить очередной кусок; ``done`` — пролог пройден или не наш."""
        if self.done:
            return
        self._seen += len(chunk)
        try:
            self._parser.Parse(chunk, final)
        except _RootReached:
            self.done = True
            return
        except ExpatError:
            # Синтаксис пролога сломан: решать будет сам разборщик, тот же
            # expat остановится на той же позиции, до любого DOCTYPE дальше.
            self.done = True
            return
        if final:
            self.done = True
        elif self._seen > PROLOG_LIMIT:
            raise XmlDeclarationError(
                f"пролог XML длиннее {PROLOG_LIMIT} байт: корневой элемент "
                "не найден"
            )


def check_bytes(data: bytes | bytearray | memoryview | str) -> None:
    """Проверить пролог XML, уже лежащего в памяти."""
    checker = _PrologChecker()
    view = data if isinstance(data, str) else memoryview(data)
    for offset in range(0, len(view), _CHUNK):
        checker.feed(
            view[offset : offset + _CHUNK]
            if isinstance(view, str)
            else bytes(view[offset : offset + _CHUNK])
        )
        if checker.done:
            return
    checker.feed(b"" if not isinstance(view, str) else "", final=True)


class _ReplayStream(io.RawIOBase):
    """Сначала отдаёт уже прочитанный пролог, затем остаток исходного потока."""

    def __init__(self, prefix: bytes, stream: BinaryIO):
        super().__init__()
        self._prefix = prefix
        self._offset = 0
        self._stream = stream

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        if self._offset < len(self._prefix):
            if size is None or size < 0:
                head = self._prefix[self._offset :]
                self._offset = len(self._prefix)
                return head + self._stream.read()
            head = self._prefix[self._offset : self._offset + size]
            self._offset += len(head)
            return head
        return self._stream.read(-1 if size is None else size)

    def readinto(self, buffer) -> int:
        chunk = self.read(len(buffer))
        buffer[: len(chunk)] = chunk
        return len(chunk)

    def close(self) -> None:
        # Владелец исходного потока закрывает его сам (обычно через with).
        super().close()


def checked_stream(stream: BinaryIO) -> BinaryIO:
    """Прочитать и проверить пролог потока, вернуть поток с теми же байтами."""
    checker = _PrologChecker()
    prefix = bytearray()
    while not checker.done:
        chunk = stream.read(_CHUNK)
        if not chunk:
            checker.feed(b"", final=True)
            break
        prefix += chunk
        checker.feed(chunk)
    return _ReplayStream(bytes(prefix), stream)  # type: ignore[return-value]


def _is_path(source: object) -> bool:
    return isinstance(source, (str, bytes, os.PathLike))


def fromstring(data: bytes | str) -> ET.Element:
    check_bytes(data)
    return ET.fromstring(data)


def parse(source: str | os.PathLike[str] | BinaryIO) -> ET.ElementTree:
    if _is_path(source):
        with open(source, "rb") as stream:
            return ET.parse(checked_stream(stream))
    return ET.parse(checked_stream(source))


def iterparse(
    source: str | os.PathLike[str] | BinaryIO,
    events: tuple[str, ...] | None = None,
) -> Iterator[tuple[str, object]]:
    """Ленивый как ``ET.iterparse``: проверка и разбор — при первом ``next``."""
    if _is_path(source):
        with open(source, "rb") as stream:
            yield from ET.iterparse(checked_stream(stream), events=events)
        return
    yield from ET.iterparse(checked_stream(source), events=events)


def canonicalize(*, from_file: BinaryIO, **options: object) -> str | None:
    return ET.canonicalize(from_file=checked_stream(from_file), **options)


__all__ = [
    "PROLOG_LIMIT",
    "XmlDeclarationError",
    "canonicalize",
    "check_bytes",
    "checked_stream",
    "fromstring",
    "iterparse",
    "parse",
]
