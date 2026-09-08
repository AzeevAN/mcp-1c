"""Проверяемое хранение множества immutable members двумя файлами.

Последовательный pack убирает десятки тысяч metadata-операций на bind mount.
Индекс остаётся отдельным каноническим JSON: читатель проверяет его и открывает
ровно нужный диапазон pack, не распаковывая и не загружая остальные members.
"""

from __future__ import annotations

import errno
import hashlib
import io
import json
import os
import stat
from contextlib import contextmanager
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import BinaryIO, Iterator, Mapping


PACK_NAME = "members.pack"
INDEX_NAME = "members.index.json"
_INDEX_LIMIT = 64 << 20
_MAX_ENTRIES = 200_000


class MemberPackError(OSError):
    """Pack либо его индекс нельзя безопасно прочитать или построить."""


def _relative_path(value: object) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise MemberPackError("member path имеет недопустимый формат")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise MemberPackError("member path должен быть безопасным относительным путём")
    return value


def _identity(value: os.stat_result) -> tuple[int, int, int, int]:
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")


@dataclass(frozen=True, slots=True)
class MemberPackEntry:
    relative_path: str
    offset: int
    size: int

    def __post_init__(self) -> None:
        _relative_path(self.relative_path)
        if (
            isinstance(self.offset, bool)
            or not isinstance(self.offset, int)
            or self.offset < 0
            or isinstance(self.size, bool)
            or not isinstance(self.size, int)
            or self.size < 0
        ):
            raise MemberPackError("offset и size member должны быть неотрицательными")

    def to_dict(self) -> dict[str, object]:
        return {
            "relative_path": self.relative_path,
            "offset": self.offset,
            "size": self.size,
        }


class _EntryWriter(io.RawIOBase):
    def __init__(self, output: BinaryIO):
        self._output = output
        self.size = 0
        self.digest = hashlib.sha256()

    def writable(self) -> bool:
        return True

    def write(self, value: bytes | bytearray) -> int:
        payload = bytes(value)
        written = self._output.write(payload)
        if written != len(payload):
            raise MemberPackError("member записан в pack не полностью")
        self.size += written
        self.digest.update(payload)
        return written

    def flush(self) -> None:
        self._output.flush()


class MemberPackWriter:
    """Однопроходный writer: одновременно может записываться один member."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.pack_path = self.root / PACK_NAME
        self.index_path = self.root / INDEX_NAME
        self._output: BinaryIO | None = None
        self._entries: list[MemberPackEntry] = []
        self._paths: set[str] = set()
        self._casefold_paths: set[str] = set()
        self._active = False
        self._finished = False

    def __enter__(self) -> MemberPackWriter:
        return self.open()

    def open(self) -> MemberPackWriter:
        """Создать новый pack; для явного lifecycle вне блока ``with``."""
        try:
            if self._output is not None or self._finished:
                raise MemberPackError("member pack writer уже был открыт")
            root_info = self.root.lstat()
            if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
                raise MemberPackError("корень member pack должен быть обычным каталогом")
            if self.pack_path.exists() or self.pack_path.is_symlink():
                raise MemberPackError("member pack уже существует")
            if self.index_path.exists() or self.index_path.is_symlink():
                raise MemberPackError("индекс member pack уже существует")
            self._output = self.pack_path.open("xb")
            return self
        except MemberPackError:
            raise
        except OSError as error:
            raise MemberPackError("не удалось создать member pack") from error

    @contextmanager
    def entry(self, relative_path: str) -> Iterator[_EntryWriter]:
        relative_path = _relative_path(relative_path)
        if self._output is None or self._finished:
            raise MemberPackError("member pack writer не открыт")
        if self._active:
            raise MemberPackError("нельзя одновременно записывать два member")
        if relative_path in self._paths:
            raise MemberPackError("member pack дублирует путь")
        if relative_path.casefold() in self._casefold_paths:
            raise MemberPackError("пути member различаются только регистром")
        self._active = True
        offset = self._output.tell()
        writer = _EntryWriter(self._output)
        try:
            yield writer
        except Exception:
            # Ошибка одного member не должна оставлять неиндексированный хвост:
            # collector умеет честно пропустить повреждённый role XML.
            self._output.seek(offset)
            self._output.truncate()
            raise
        else:
            self._paths.add(relative_path)
            self._casefold_paths.add(relative_path.casefold())
            self._entries.append(MemberPackEntry(relative_path, offset, writer.size))
        finally:
            self._active = False

    def copy(
        self,
        relative_path: str,
        source: BinaryIO,
        *,
        expected_size: int,
        expected_sha256: str,
        block_size: int = 1 << 20,
    ) -> None:
        with self.entry(relative_path) as target:
            for block in iter(lambda: source.read(block_size), b""):
                target.write(block)
            if (
                target.size != expected_size
                or target.digest.hexdigest() != expected_sha256
            ):
                raise MemberPackError("исходный member не совпал по размеру или хешу")

    def finish(self) -> None:
        if self._output is None or self._finished or self._active:
            raise MemberPackError("member pack writer находится в неверном состоянии")
        try:
            self._output.flush()
            os.fsync(self._output.fileno())
            self._output.close()
            self._output = None
            pack_size = self.pack_path.stat().st_size
            by_offset = sorted(self._entries, key=lambda item: item.offset)
            cursor = 0
            for entry in by_offset:
                if entry.offset != cursor:
                    raise MemberPackError("member pack содержит разрыв или пересечение")
                cursor += entry.size
            if cursor != pack_size:
                raise MemberPackError("размер member pack не совпадает с индексом")
            payload = _canonical_json(
                {
                    "format_version": 1,
                    "pack_size": pack_size,
                    "entries": [
                        entry.to_dict()
                        for entry in sorted(
                            self._entries, key=lambda item: item.relative_path
                        )
                    ],
                }
            )
            with self.index_path.open("xb") as output:
                output.write(payload)
                output.flush()
                os.fsync(output.fileno())
            descriptor = os.open(
                self.root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
            )
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            self._finished = True
        except MemberPackError:
            raise
        except OSError as error:
            raise MemberPackError("не удалось завершить member pack") from error

    def __exit__(self, exc_type, exc, traceback) -> None:
        if exc is None and not self._finished:
            self.finish()
        elif self._output is not None:
            self._output.close()
            self._output = None

    def close(self) -> None:
        """Закрыть незавершённый writer; временный каталог удаляет владелец."""
        if self._output is not None:
            self._output.close()
            self._output = None


def _open_regular(path: Path, label: str) -> tuple[BinaryIO, os.stat_result]:
    descriptor: int | None = None
    try:
        before = path.lstat()
        if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
            raise MemberPackError(f"{label} должен быть обычным файлом")
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or _identity(opened) != _identity(before):
            raise MemberPackError(f"{label} изменился во время открытия")
        stream = os.fdopen(descriptor, "rb")
        descriptor = None
        return stream, opened
    except MemberPackError:
        raise
    except OSError as error:
        raise MemberPackError(f"{label} недоступен") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _stat_regular(path: Path, label: str) -> os.stat_result:
    try:
        value = path.lstat()
    except OSError as error:
        raise MemberPackError(f"{label} недоступен") from error
    if stat.S_ISLNK(value.st_mode) or not stat.S_ISREG(value.st_mode):
        raise MemberPackError(f"{label} должен быть обычным файлом")
    return value


@lru_cache(maxsize=32)
def _cached_index(
    index_path: str,
    index_identity: tuple[int, int, int, int],
    pack_size: int,
) -> Mapping[str, MemberPackEntry]:
    path = Path(index_path)
    with _open_regular(path, "индекс member pack")[0] as source:
        raw = source.read(_INDEX_LIMIT + 1)
    if len(raw) > _INDEX_LIMIT:
        raise MemberPackError("индекс member pack превышает предел")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise MemberPackError("индекс member pack повреждён") from error
    if (
        not isinstance(value, dict)
        or set(value) != {"format_version", "pack_size", "entries"}
        or value.get("format_version") != 1
        or value.get("pack_size") != pack_size
        or not isinstance(value.get("entries"), list)
        or len(value["entries"]) > _MAX_ENTRIES
    ):
        raise MemberPackError("индекс member pack имеет неверный формат")
    entries: list[MemberPackEntry] = []
    try:
        for raw_entry in value["entries"]:
            if not isinstance(raw_entry, dict) or set(raw_entry) != {
                "relative_path",
                "offset",
                "size",
            }:
                raise MemberPackError("запись индекса member pack повреждена")
            entries.append(
                MemberPackEntry(
                    raw_entry["relative_path"], raw_entry["offset"], raw_entry["size"]
                )
            )
    except (KeyError, TypeError, ValueError) as error:
        if isinstance(error, MemberPackError):
            raise
        raise MemberPackError("запись индекса member pack повреждена") from error
    if [entry.relative_path for entry in entries] != sorted(
        entry.relative_path for entry in entries
    ) or len({entry.relative_path for entry in entries}) != len(entries):
        raise MemberPackError("индекс member pack не каноничен или дублирует путь")
    cursor = 0
    for entry in sorted(entries, key=lambda item: item.offset):
        if entry.offset != cursor or entry.offset + entry.size > pack_size:
            raise MemberPackError("индекс member pack содержит разрыв или пересечение")
        cursor += entry.size
    if cursor != pack_size or _canonical_json(value) != raw:
        raise MemberPackError("индекс member pack не совпадает с pack")
    return MappingProxyType({entry.relative_path: entry for entry in entries})


class _PackedReader(io.RawIOBase):
    def __init__(self, source: BinaryIO, offset: int, size: int):
        self._source = source
        self._remaining = size
        self._source.seek(offset)

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return False

    def read(self, size: int = -1) -> bytes:
        if self._remaining <= 0:
            return b""
        requested = self._remaining if size < 0 else min(size, self._remaining)
        payload = self._source.read(requested)
        if len(payload) != requested:
            raise MemberPackError("member pack прочитан не полностью")
        self._remaining -= len(payload)
        return payload

    def close(self) -> None:
        if not self.closed:
            self._source.close()
        super().close()


def _open_loose(root: Path, relative_path: str) -> BinaryIO | None:
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(
        os, "O_NOFOLLOW", 0
    )
    file_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptors: list[int] = []
    file_descriptor: int | None = None
    try:
        root_info = root.lstat()
        if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
            raise MemberPackError("корень members должен быть обычным каталогом")
        current = os.open(root, directory_flags)
        descriptors.append(current)
        parts = PurePosixPath(relative_path).parts
        for part in parts[:-1]:
            current = os.open(part, directory_flags, dir_fd=current)
            descriptors.append(current)
        file_descriptor = os.open(parts[-1], file_flags, dir_fd=current)
        opened = os.fstat(file_descriptor)
        if not stat.S_ISREG(opened.st_mode):
            raise MemberPackError("loose member должен быть обычным файлом")
        stream = os.fdopen(file_descriptor, "rb")
        file_descriptor = None
        return stream
    except FileNotFoundError:
        return None
    except MemberPackError:
        raise
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise MemberPackError("loose member содержит символическую ссылку") from error
        raise MemberPackError("loose member недоступен") from error
    finally:
        if file_descriptor is not None:
            os.close(file_descriptor)
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def open_stored_member(root: str | Path, relative_path: str) -> BinaryIO:
    """Открыть новый packed либо прежний loose member без следования symlink."""
    root = Path(root)
    relative_path = _relative_path(relative_path)
    try:
        root_info = root.lstat()
    except OSError as error:
        raise MemberPackError("корень members недоступен") from error
    if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
        raise MemberPackError("корень members должен быть обычным каталогом")
    pack_path = root / PACK_NAME
    index_path = root / INDEX_NAME
    pack_exists = pack_path.exists() or pack_path.is_symlink()
    index_exists = index_path.exists() or index_path.is_symlink()
    if pack_exists != index_exists:
        raise MemberPackError("member pack и индекс должны существовать парой")
    if pack_exists:
        pack_info = _stat_regular(pack_path, "member pack")
        index_info = _stat_regular(index_path, "индекс member pack")
        entries = _cached_index(
            str(index_path), _identity(index_info), pack_info.st_size
        )
        entry = entries.get(relative_path)
        if entry is not None:
            source, opened = _open_regular(pack_path, "member pack")
            if _identity(opened) != _identity(pack_info):
                source.close()
                raise MemberPackError("member pack изменился во время открытия")
            return _PackedReader(source, entry.offset, entry.size)
    loose = _open_loose(root, relative_path)
    if loose is not None:
        return loose
    if pack_exists:
        raise MemberPackError("member отсутствует в pack")
    raise MemberPackError("loose member отсутствует")


def has_member_pack(root: str | Path) -> bool:
    root = Path(root)
    return (root / PACK_NAME).is_file() and (root / INDEX_NAME).is_file()


__all__ = [
    "INDEX_NAME",
    "PACK_NAME",
    "MemberPackError",
    "MemberPackWriter",
    "has_member_pack",
    "open_stored_member",
]
