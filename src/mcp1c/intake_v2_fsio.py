"""Общие файловые примитивы durable-записей intake V2.

Раньше каждый модуль intake держал свою копию regexp идентификатора, fsync
каталога и чтения малой записи. Копии разошлись: одни читатели открывали файл
с ``O_NOFOLLOW`` и сверяли ``fstat``, другие читали по пути после ``lstat``, а
regexp пропускал ``..`` и ``.``, и ``job_id`` становился путём к родителю.
Здесь живёт единственная версия; модули лишь переводят исключения в свои типы.
"""

from __future__ import annotations

import errno
import os
import re
import stat
import tempfile
from pathlib import Path


# Запрещена только точка первым символом: этого достаточно, чтобы отсечь
# ``.``, ``..`` и скрытые имена (временные файлы хранилищ начинаются с точки).
# ``_`` и ``-`` первыми допустимы: так уже называются каталоги в
# /config-sources и их сохранённые привязки, а id нигде не попадает в argv
# внешних команд, поэтому «похожесть на ключ» опасности не несёт. Предел
# длины держит ``<id>.json`` и ``.<id>.json.*.tmp`` внутри NAME_MAX любой ФС.
IDENTIFIER_RE = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._-]{0,127}\Z")


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


# umask читается один раз при импорте: os.umask() меняет его на время вызова
# для всего процесса, и чтение на каждой записи давало бы гонку с потоками,
# создающими файлы в тот же момент.
_UMASK = os.umask(0)
os.umask(_UMASK)
_NEW_RECORD_MODE = 0o644 & ~_UMASK


def _record_mode(path: Path) -> int:
    """Права новой версии записи: как у прежней, иначе как у обычного open."""
    try:
        info = path.lstat()
    except OSError:
        return _NEW_RECORD_MODE
    if stat.S_ISREG(info.st_mode):
        return stat.S_IMODE(info.st_mode)
    return _NEW_RECORD_MODE


def write_record_atomic(path: Path, payload: bytes, *, suffix: str = ".tmp") -> None:
    """Атомарно заменить малую запись: temp + fsync + rename + fsync каталога.

    Ошибки ввода-вывода пробрасываются как ``OSError``: сообщение для человека
    у каждого хранилища своё. Временный файл скрытый (``.<имя>.*``), чтобы
    уборка при старте отличала его от настоящей записи. ``mkstemp`` создаёт
    файл с правами 0600; без ``fchmod`` замена молча сужала бы права
    записи, которую раньше писали обычным ``open`` и читает не только сервер
    (резервная копия тома, оператор).
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
        os.fchmod(descriptor, _record_mode(path))
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


def read_record(path: Path, *, limit: int) -> bytes:
    """Прочитать малую запись без окна подмены на символическую ссылку.

    Все проверки делаются по уже открытому дескриптору, а не по пути:
    ``O_NOFOLLOW`` не открывает ссылку, ``fstat`` проверяет тип и размер
    именно того файла, который будет прочитан. Прежняя сверка ``lstat`` с
    ``fstat`` давала ложный отказ на штатной атомарной замене записи (job
    переписывается во время prepare, а интерфейс опрашивает её каждую
    секунду). ``O_NONBLOCK`` не даёт зависнуть на FIFO, подложенном вместо
    записи: открытие проходит сразу, а ``fstat`` его отвергает.
    Отсутствие файла — ``FileNotFoundError``: для хранилищ это штатное
    «записи нет», а не повреждение.
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor: int | None = os.open(path, flags)
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise RecordTypeError(path.name) from error
        raise
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode):
            raise RecordTypeError(path.name)
        if info.st_size > limit:
            raise RecordTooLargeError(path.name)
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            payload = stream.read(limit + 1)
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if len(payload) > limit:
        raise RecordTooLargeError(path.name)
    return payload


def remove_temporary_records(path: Path, *, suffix: str = ".tmp") -> None:
    """Убрать временные файлы ``write_record_atomic`` для одной записи.

    Они остаются, только если процесс погиб между ``mkstemp`` и ``rename``.
    Вызывается при открытии хранилища; сбой уборки не мешает работе — мусор
    просто доживёт до следующего старта.
    """
    prefix = f".{path.name}."
    try:
        candidates = [
            entry
            for entry in path.parent.iterdir()
            if entry.name.startswith(prefix) and entry.name.endswith(suffix)
        ]
    except OSError:
        return
    for entry in candidates:
        try:
            if stat.S_ISREG(entry.lstat().st_mode):
                entry.unlink()
        except OSError:
            continue


__all__ = [
    "IDENTIFIER_RE",
    "RecordReadError",
    "RecordTooLargeError",
    "RecordTypeError",
    "is_safe_identifier",
    "read_record",
    "remove_temporary_records",
    "sync_directory",
    "write_record_atomic",
]
