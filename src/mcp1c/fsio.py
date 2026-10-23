"""Общие файловые примитивы durable-записей.

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
import secrets
import stat
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO


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


def record_mode(path: Path) -> int:
    """Права новой версии записи: как у прежней, иначе как у обычного open."""
    try:
        info = path.lstat()
    except OSError:
        return _NEW_RECORD_MODE
    if stat.S_ISREG(info.st_mode):
        return stat.S_IMODE(info.st_mode)
    return _NEW_RECORD_MODE


def _validate_component(name: str) -> None:
    if (
        not isinstance(name, str)
        or not name
        or name in {".", ".."}
        or "/" in name
        or "\0" in name
    ):
        raise ValueError("Имя файла должно быть одним непустым компонентом пути")


@contextmanager
def atomic_writer(
    dir_fd: int,
    name: str,
    *,
    mode: int = 0o600,
    temporary_name: str | None = None,
    suffix: str = ".tmp",
    sync_parent: Callable[[int], None] | None = None,
    ignore_cleanup_errors: bool = False,
) -> Iterator[BinaryIO]:
    """Записать поток во временный файл и durable заменить запись в каталоге.

    Дескриптор каталога принадлежит вызывающему коду. ``mode`` применяется
    через обычный umask; нужный ``fchmod`` вызывающий делает сам. Если имя
    временного файла задано, оно должно быть свободно: чужой файл при отказе
    ``O_EXCL`` не удаляется. Callback заменяет только fsync каталога после
    успешной замены и позволяет вызывающему сохранить свою политику ошибок.
    """
    _validate_component(name)
    if temporary_name is not None:
        _validate_component(temporary_name)
        if not temporary_name.startswith(".") or temporary_name == name:
            raise ValueError("Временное имя должно быть скрытым и отличаться от имени записи")

    descriptor: int | None = None
    temporary: str | None = None
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        if temporary_name is not None:
            descriptor = os.open(temporary_name, flags, mode, dir_fd=dir_fd)
            temporary = temporary_name
        else:
            for _ in range(100):
                candidate = f".{name}.{secrets.token_urlsafe(6)}{suffix}"
                _validate_component(candidate)
                if candidate == name:
                    raise ValueError("Временное имя должно отличаться от имени записи")
                try:
                    descriptor = os.open(candidate, flags, mode, dir_fd=dir_fd)
                except FileExistsError:
                    continue
                temporary = candidate
                break
            else:
                raise FileExistsError(errno.EEXIST, "Не удалось выбрать свободное временное имя", name)

        stream = os.fdopen(descriptor, "wb")
        descriptor = None
        with stream:
            yield stream
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        temporary = None
        if sync_parent is None:
            os.fsync(dir_fd)
        else:
            sync_parent(dir_fd)
    finally:
        try:
            # fdopen может отказать до передачи владения потоку.
            if descriptor is not None:
                os.close(descriptor)
        finally:
            if temporary is not None:
                try:
                    os.unlink(temporary, dir_fd=dir_fd)
                except FileNotFoundError:
                    pass
                except OSError:
                    if not ignore_cleanup_errors:
                        raise


def _record_mode_in_dir(dir_fd: int, name: str) -> int:
    try:
        info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except OSError:
        return _NEW_RECORD_MODE
    if stat.S_ISREG(info.st_mode):
        return stat.S_IMODE(info.st_mode)
    return _NEW_RECORD_MODE


def _preserve_record_mode(stream: BinaryIO, dir_fd: int, name: str) -> None:
    # На ФС без chmod (exFAT, часть SMB/FUSE) запись всё равно должна пройти.
    try:
        os.fchmod(stream.fileno(), _record_mode_in_dir(dir_fd, name))
    except OSError:
        pass


def write_in_dir(
    dir_fd: int, name: str, payload: bytes, *, mode: int | None = None,
) -> None:
    """Атомарно заменить запись относительно уже открытого каталога.

    Без ``mode`` сохраняются права обычного файла, без следования ссылке;
    для новой записи используются права ``write_record_atomic``. Явный ``mode``
    передаётся только в ``os.open``, с обычным применением umask.
    """
    with atomic_writer(dir_fd, name, mode=0o600 if mode is None else mode) as stream:
        if mode is None:
            _preserve_record_mode(stream, dir_fd, name)
        stream.write(payload)


def write_record_atomic(path: Path, payload: bytes, *, suffix: str = ".tmp") -> None:
    """Атомарно заменить малую запись: temp + fsync + rename + fsync каталога.

    Ошибки ввода-вывода пробрасываются как ``OSError``: сообщение для человека
    у каждого хранилища своё. Временный файл скрытый (``.<имя>.*``), чтобы
    уборка при старте отличала его от настоящей записи. Он создаётся
    с правами 0600; без ``fchmod`` замена молча сужала бы права
    записи, которую раньше писали обычным ``open`` и читает не только сервер
    (резервная копия тома, оператор).
    """
    descriptor = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        with atomic_writer(
            descriptor,
            path.name,
            suffix=suffix,
            ignore_cleanup_errors=True,
        ) as stream:
            _preserve_record_mode(stream, descriptor, path.name)
            stream.write(payload)
    finally:
        os.close(descriptor)


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
        # O_NONBLOCK нужен только для открытия; читать обычный файл надо
        # блокирующе, иначе экзотическая ФС вправе вернуть EAGAIN.
        os.set_blocking(descriptor, True)
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

    Они остаются, только если процесс погиб между созданием temp и ``rename``.
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
    "atomic_writer",
    "is_safe_identifier",
    "read_record",
    "record_mode",
    "remove_temporary_records",
    "sync_directory",
    "write_in_dir",
    "write_record_atomic",
]
