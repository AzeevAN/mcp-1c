"""Общие файловые примитивы durable-записей intake V2.

Раньше каждый модуль intake держал свою копию regexp идентификатора, fsync
каталога и чтения малой записи. Копии разошлись: одни читатели открывали файл
с ``O_NOFOLLOW`` и сверяли ``fstat``, другие читали по пути после ``lstat``, а
regexp пропускал ``..`` и ``.``, и ``job_id`` становился путём к родителю.
Здесь живёт единственная версия; модули лишь переводят исключения в свои типы.
"""

from __future__ import annotations

import os
import re
import stat
import tempfile
from pathlib import Path


# Первый символ — буква или цифра: так отсекаются ``.``, ``..``, скрытые имена
# и имена, похожие на ключи командной строки. Предел длины держит имя файла
# ``<id>.json`` и временные ``.<id>.*.tmp`` внутри NAME_MAX любой ФС.
IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


def is_safe_identifier(value: object) -> bool:
    """Годится ли значение как один компонент пути managed-хранилища."""
    return isinstance(value, str) and IDENTIFIER_RE.fullmatch(value) is not None


def sync_directory(directory: Path) -> None:
    """Сделать durable создание, замену или удаление записи в каталоге."""
    descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def write_record_atomic(path: Path, payload: bytes, *, suffix: str = ".tmp") -> None:
    """Атомарно заменить малую запись: temp + fsync + rename + fsync каталога.

    Ошибки ввода-вывода пробрасываются как ``OSError``: сообщение для человека
    у каждого хранилища своё. Временный файл скрытый (``.<имя>.*``), чтобы
    уборка при старте отличала его от настоящей записи.
    """
    directory = path.parent
    temporary: Path | None = None
    try:
        descriptor, raw_path = tempfile.mkstemp(
            dir=directory,
            prefix=f".{path.name}.",
            suffix=suffix,
        )
        temporary = Path(raw_path)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
        sync_directory(directory)
    finally:
        if temporary is not None:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


class RecordReadError(Exception):
    """Запись нельзя безопасно прочитать; подклассы называют причину."""


class RecordTypeError(RecordReadError):
    """По пути лежит не обычный файл (ссылка, каталог, устройство)."""


class RecordTooLargeError(RecordReadError):
    """Запись больше предела, который хранилище готово держать в памяти."""


class RecordChangedError(RecordReadError):
    """Файл подменили между проверкой и открытием."""


def _identity(info: os.stat_result) -> tuple[int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def read_record(path: Path, *, limit: int) -> bytes:
    """Прочитать малую запись без окна подмены на символическую ссылку.

    ``lstat`` отвергает ссылку и чужой тип, ``O_NOFOLLOW`` не даёт открыть
    подменённую после проверки ссылку, сверка ``fstat`` с ``lstat`` ловит
    подмену обычным файлом. Отсутствие файла — ``FileNotFoundError``: для
    хранилищ это штатное «записи нет», а не повреждение.
    """
    before = path.lstat()
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise RecordTypeError(path.name)
    if before.st_size > limit:
        raise RecordTooLargeError(path.name)
    descriptor: int | None = os.open(
        path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        after = os.fstat(descriptor)
        if _identity(before) != _identity(after):
            raise RecordChangedError(path.name)
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            payload = stream.read(limit + 1)
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if len(payload) > limit:
        raise RecordTooLargeError(path.name)
    return payload


__all__ = [
    "IDENTIFIER_RE",
    "RecordChangedError",
    "RecordReadError",
    "RecordTooLargeError",
    "RecordTypeError",
    "is_safe_identifier",
    "read_record",
    "sync_directory",
    "write_record_atomic",
]
