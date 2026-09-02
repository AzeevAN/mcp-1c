"""Безопасные транспортные адаптеры единого приёма конфигураций.

Модуль не распознаёт конфигурацию и не знает о Registry. Он превращает
managed browser-upload, incoming/read-only ZIP и read-only каталог в один
потоковый ``VirtualExportTree`` с общими лимитами и проверкой стабильности.
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import shutil
import stat
import tempfile
import threading
import time
import zlib
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Iterable

from .intake_v2 import CandidateTransport
from .resource_limits import (
    ARCHIVE_LIMITS,
    MIB,
    LimitedReader,
    ResourceBudget,
    ResourceLimitError,
    ResourceLimits,
)


MAX_BROWSER_UPLOAD_BYTES = 500 * MIB
DIRECTORY_SETTLE_SECONDS = 5.0
_READ_CHUNK = 1 << 20
_STAGING_FORMAT_VERSION = 1
_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9._-]+\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_MAX_PATH_BYTES = 4096
_MAX_PATH_DEPTH = 64
_MAX_STAGING_RECORD_BYTES = 64 * 1024


class TransportError(RuntimeError):
    """Вход нельзя достоверно прочитать через транспортный контракт."""


class TransportSecurityError(TransportError):
    """Вход нарушает границу пути или типа файловой системы."""


class TransportLimitError(TransportError):
    """Вход или managed staging превышает разрешённый бюджет."""


class TransportUnstableError(TransportError):
    """Вход изменился между фиксацией снимка и чтением."""


def _candidate_id(value: object) -> str:
    if (
        not isinstance(value, str)
        or not _IDENTIFIER_RE.fullmatch(value)
        or len(value) > 128
    ):
        raise TransportSecurityError("candidate_id имеет недопустимый формат")
    return value


def _display_name(value: object) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise TransportSecurityError("origin_name имеет недопустимый формат")
    name = value.replace("\\", "/").rsplit("/", 1)[-1].strip()
    if (
        not name
        or name in {".", ".."}
        or len(name.encode("utf-8")) > 1024
        or any(ord(char) < 32 for char in name)
    ):
        raise TransportSecurityError("origin_name имеет недопустимый формат")
    return name


def _normalized_member_path(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or any(ord(char) < 32 for char in value)
    ):
        raise TransportSecurityError("вход содержит небезопасный путь")
    raw = PurePosixPath(value)
    if raw.is_absolute() or ".." in raw.parts or not raw.parts:
        raise TransportSecurityError("вход содержит небезопасный путь")
    if raw.parts[0].endswith(":"):
        raise TransportSecurityError("вход содержит небезопасный путь")
    normalized = raw.as_posix()
    if normalized in {"", "."}:
        raise TransportSecurityError("вход содержит небезопасный путь")
    if len(normalized.encode("utf-8")) > _MAX_PATH_BYTES:
        raise TransportLimitError("путь входа превышает предел длины")
    if len(raw.parts) > _MAX_PATH_DEPTH:
        raise TransportLimitError("глубина пути входа превышает предел")
    return normalized


def _requested_path(value: object) -> str:
    normalized = _normalized_member_path(value)
    if value != normalized:
        raise TransportSecurityError("запрошен ненормализованный путь")
    return normalized


def _identity(info: os.stat_result) -> tuple[int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _fingerprint(parts: Iterable[object]) -> str:
    digest = hashlib.sha256()
    for part in parts:
        encoded = str(part).encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def _sync_directory(directory: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(directory, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _ensure_regular_directory(path: Path, label: str) -> None:
    try:
        if path.is_symlink():
            raise TransportSecurityError(
                f"{label} не может быть символической ссылкой"
            )
        path.mkdir(parents=True, exist_ok=True)
        if path.is_symlink() or not path.is_dir():
            raise TransportSecurityError(f"{label} должен быть обычным каталогом")
    except TransportError:
        raise
    except OSError as error:
        raise TransportError(f"не удалось открыть {label}") from error


def _read_small_regular_file(path: Path, limit: int) -> bytes:
    """Прочитать малую metadata-запись без окна перехода по symlink."""
    try:
        before = path.lstat()
    except FileNotFoundError:
        raise
    except OSError as error:
        raise TransportError("запись browser staging недоступна") from error
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise TransportSecurityError(
            "запись browser staging должна быть обычным файлом"
        )
    if before.st_size > limit:
        raise TransportLimitError("запись browser staging превышает предел")
    descriptor: int | None = None
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
        )
        after = os.fstat(descriptor)
        if _identity(before) != _identity(after):
            raise TransportUnstableError(
                "запись browser staging изменилась во время чтения"
            )
        with os.fdopen(descriptor, "rb") as stream:
            descriptor = None
            payload = stream.read(limit + 1)
        if len(payload) > limit:
            raise TransportLimitError("запись browser staging превышает предел")
        return payload
    except TransportError:
        raise
    except OSError as error:
        raise TransportError("запись browser staging недоступна") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _resource_budget(
    limits: ResourceLimits,
    label: str,
    members: Iterable[tuple[str, int, int]],
) -> ResourceBudget:
    if not isinstance(limits, ResourceLimits):
        raise TypeError("limits должен быть ResourceLimits")
    budget = ResourceBudget(limits, label)
    try:
        budget.validate_members(members)
    except ResourceLimitError as error:
        raise TransportLimitError(str(error)) from error
    return budget


@dataclass(frozen=True, slots=True)
class StagedUpload:
    """Durable metadata одного полностью принятого browser-upload."""

    candidate_id: str
    origin_name: str
    size: int
    sha256: str

    def __post_init__(self) -> None:
        _candidate_id(self.candidate_id)
        if _display_name(self.origin_name) != self.origin_name:
            raise TransportSecurityError("origin_name должен быть безопасным именем")
        if (
            isinstance(self.size, bool)
            or not isinstance(self.size, int)
            or self.size < 0
        ):
            raise TransportSecurityError("size должен быть целым числом не меньше нуля")
        if not isinstance(self.sha256, str) or not _SHA256_RE.fullmatch(self.sha256):
            raise TransportSecurityError("sha256 имеет недопустимый формат")

    def to_dict(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "origin_name": self.origin_name,
            "size": self.size,
            "sha256": self.sha256,
        }

    @classmethod
    def from_dict(cls, raw: object) -> StagedUpload:
        if not isinstance(raw, dict):
            raise TransportError("запись browser staging повреждена")
        try:
            return cls(
                candidate_id=raw["candidate_id"],
                origin_name=raw["origin_name"],
                size=raw["size"],
                sha256=raw["sha256"],
            )
        except (KeyError, TypeError, ValueError, TransportError) as error:
            raise TransportError("запись browser staging повреждена") from error


class BrowserStagingStore:
    """Управляемое durable-хранилище принятых browser-upload.

    Полностью записанный payload и его metadata переживают рестарт. Временные
    и односторонние файлы не считаются принятыми и удаляются при новом
    экземпляре store. Пользовательский ``incoming`` этим хранилищем не служит.
    """

    def __init__(
        self,
        root: Path,
        *,
        max_upload_bytes: int = MAX_BROWSER_UPLOAD_BYTES,
        free_space_reserve: int = 0,
    ):
        if (
            isinstance(max_upload_bytes, bool)
            or not isinstance(max_upload_bytes, int)
            or max_upload_bytes <= 0
        ):
            raise ValueError("max_upload_bytes должен быть положительным целым")
        if (
            isinstance(free_space_reserve, bool)
            or not isinstance(free_space_reserve, int)
            or free_space_reserve < 0
        ):
            raise ValueError("free_space_reserve должен быть неотрицательным целым")
        self.root = Path(root)
        self.payloads_dir = self.root / "payloads"
        self.records_dir = self.root / "records"
        self.max_upload_bytes = max_upload_bytes
        self.free_space_reserve = free_space_reserve
        _ensure_regular_directory(self.root, "browser staging")
        _ensure_regular_directory(self.payloads_dir, "каталог payload browser staging")
        _ensure_regular_directory(self.records_dir, "каталог metadata browser staging")
        self._cleanup_after_restart()

    def _payload_path(self, candidate_id: str) -> Path:
        return self.payloads_dir / f"{_candidate_id(candidate_id)}.upload"

    def _record_path(self, candidate_id: str) -> Path:
        return self.records_dir / f"{_candidate_id(candidate_id)}.json"

    @staticmethod
    def _reject_symlinks(directory: Path) -> None:
        try:
            for path in directory.iterdir():
                if path.is_symlink():
                    raise TransportSecurityError(
                        "browser staging содержит символическую ссылку"
                    )
        except TransportError:
            raise
        except OSError as error:
            raise TransportError("browser staging недоступен") from error

    def _cleanup_after_restart(self) -> None:
        self._reject_symlinks(self.payloads_dir)
        self._reject_symlinks(self.records_dir)
        changed_payloads = False
        changed_records = False
        try:
            for directory, is_payload in (
                (self.payloads_dir, True),
                (self.records_dir, False),
            ):
                for path in directory.iterdir():
                    if (
                        path.is_file()
                        and path.name.startswith(".")
                        and path.name.endswith(".part")
                    ):
                        path.unlink()
                        if is_payload:
                            changed_payloads = True
                        else:
                            changed_records = True
            payload_ids = {
                path.name[: -len(".upload")]
                for path in self.payloads_dir.glob("*.upload")
                if path.is_file()
            }
            record_ids = {
                path.stem for path in self.records_dir.glob("*.json") if path.is_file()
            }
            for candidate_id in payload_ids - record_ids:
                self._payload_path(candidate_id).unlink()
                changed_payloads = True
            for candidate_id in record_ids - payload_ids:
                self._record_path(candidate_id).unlink()
                changed_records = True
        except (OSError, TransportError) as error:
            if isinstance(error, TransportError):
                raise
            raise TransportError("не удалось восстановить browser staging") from error
        if changed_payloads:
            _sync_directory(self.payloads_dir)
        if changed_records:
            _sync_directory(self.records_dir)

    def _check_free_space(self, expected_size: int | None) -> None:
        planned = self.max_upload_bytes if expected_size is None else expected_size
        if isinstance(planned, bool) or not isinstance(planned, int) or planned < 0:
            raise TransportLimitError("ожидаемый размер upload некорректен")
        if planned > self.max_upload_bytes:
            raise TransportLimitError("browser-upload превышает предел размера")
        try:
            free = shutil.disk_usage(self.root).free
        except OSError as error:
            raise TransportError("не удалось проверить свободное место") from error
        required = planned + self.free_space_reserve
        if free < required:
            raise TransportLimitError(
                "для browser staging недостаточно свободного места"
            )

    @staticmethod
    def _record_bytes(record: StagedUpload) -> bytes:
        envelope = {
            "format_version": _STAGING_FORMAT_VERSION,
            "kind": "browser-upload",
            "payload": record.to_dict(),
        }
        return (
            json.dumps(
                envelope,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")

    def _write_record(self, record: StagedUpload) -> None:
        target = self._record_path(record.candidate_id)
        temporary: Path | None = None
        target_linked = False
        try:
            descriptor, raw_path = tempfile.mkstemp(
                dir=self.records_dir,
                prefix=f".{record.candidate_id}.",
                suffix=".part",
            )
            temporary = Path(raw_path)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(self._record_bytes(record))
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temporary, target, follow_symlinks=False)
            target_linked = True
            temporary.unlink()
            temporary = None
            _sync_directory(self.records_dir)
        except Exception:
            if target_linked:
                try:
                    target.unlink(missing_ok=True)
                    _sync_directory(self.records_dir)
                except OSError:
                    pass
            raise
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def accept(
        self,
        candidate_id: str,
        origin_name: str,
        stream: BinaryIO,
        *,
        expected_size: int | None = None,
    ) -> StagedUpload:
        candidate_id = _candidate_id(candidate_id)
        origin_name = _display_name(origin_name)
        self._check_free_space(expected_size)
        payload = self._payload_path(candidate_id)
        record_path = self._record_path(candidate_id)
        if payload.exists() or record_path.exists():
            raise TransportError("candidate_id уже существует в browser staging")

        temporary: Path | None = None
        payload_committed = False
        record_committed = False
        try:
            descriptor, raw_path = tempfile.mkstemp(
                dir=self.payloads_dir,
                prefix=f".{candidate_id}.",
                suffix=".part",
            )
            temporary = Path(raw_path)
            digest = hashlib.sha256()
            total = 0
            with os.fdopen(descriptor, "wb") as output:
                while True:
                    block = stream.read(_READ_CHUNK)
                    if not block:
                        break
                    if not isinstance(block, bytes):
                        raise TransportError("upload stream должен возвращать bytes")
                    total += len(block)
                    if total > self.max_upload_bytes:
                        raise TransportLimitError(
                            "browser-upload превышает предел размера"
                        )
                    output.write(block)
                    digest.update(block)
                output.flush()
                os.fsync(output.fileno())
            if expected_size is not None and total != expected_size:
                raise TransportUnstableError(
                    "browser-upload завершился с неожиданным размером"
                )
            os.link(temporary, payload, follow_symlinks=False)
            payload_committed = True
            temporary.unlink()
            temporary = None
            _sync_directory(self.payloads_dir)
            record = StagedUpload(
                candidate_id=candidate_id,
                origin_name=origin_name,
                size=total,
                sha256=digest.hexdigest(),
            )
            self._write_record(record)
            record_committed = True
            return record
        except OSError as error:
            if error.errno in {errno.ENOSPC, getattr(errno, "EDQUOT", -1)}:
                raise TransportLimitError(
                    "для browser staging недостаточно свободного места"
                ) from error
            raise TransportError("не удалось сохранить browser-upload") from error
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
            if payload_committed and not record_committed:
                try:
                    payload.unlink(missing_ok=True)
                    _sync_directory(self.payloads_dir)
                except OSError:
                    pass

    def load(self, candidate_id: str) -> StagedUpload:
        candidate_id = _candidate_id(candidate_id)
        record_path = self._record_path(candidate_id)
        payload_path = self._payload_path(candidate_id)
        try:
            encoded = _read_small_regular_file(
                record_path,
                _MAX_STAGING_RECORD_BYTES,
            )
            raw = json.loads(encoded.decode("utf-8"))
        except FileNotFoundError:
            raise KeyError(candidate_id) from None
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise TransportError("запись browser staging повреждена") from error
        if (
            not isinstance(raw, dict)
            or raw.get("format_version") != _STAGING_FORMAT_VERSION
            or raw.get("kind") != "browser-upload"
        ):
            raise TransportError("запись browser staging несовместима")
        record = StagedUpload.from_dict(raw.get("payload"))
        if record.candidate_id != candidate_id:
            raise TransportError("запись browser staging имеет чужой candidate_id")
        try:
            info = payload_path.lstat()
        except FileNotFoundError:
            raise TransportError("payload browser staging отсутствует") from None
        except OSError as error:
            raise TransportError("payload browser staging недоступен") from error
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISREG(info.st_mode)
            or info.st_size != record.size
        ):
            raise TransportError("payload browser staging повреждён")
        return record

    def candidate_ids(self) -> tuple[str, ...]:
        """Перечислить ids полностью принятых uploads без чтения payload."""
        self._reject_symlinks(self.records_dir)
        try:
            return tuple(
                sorted(
                    path.stem
                    for path in self.records_dir.iterdir()
                    if path.is_file() and path.suffix == ".json"
                )
            )
        except OSError as error:
            raise TransportError("browser staging недоступен") from error

    def list_uploads(self) -> tuple[StagedUpload, ...]:
        """Вернуть только полностью принятые uploads в стабильном порядке."""
        return tuple(self.load(candidate_id) for candidate_id in self.candidate_ids())

    def open_tree(
        self,
        candidate_id: str,
        *,
        limits: ResourceLimits = ARCHIVE_LIMITS,
    ) -> ZipExportTree:
        record = self.load(candidate_id)
        tree = ZipExportTree(
            self._payload_path(record.candidate_id),
            transport=CandidateTransport.BROWSER,
            origin_name=record.origin_name,
            limits=limits,
        )
        try:
            if tree.source_sha256() != record.sha256:
                raise TransportError("payload browser staging не совпадает с SHA-256")
            return tree
        except Exception:
            tree.close()
            raise

    def discard(self, candidate_id: str) -> None:
        candidate_id = _candidate_id(candidate_id)
        record = self._record_path(candidate_id)
        payload = self._payload_path(candidate_id)
        if record.is_symlink() or payload.is_symlink():
            raise TransportSecurityError(
                "browser staging содержит символическую ссылку"
            )
        try:
            if not record.exists() and not payload.exists():
                return
            record.unlink(missing_ok=True)
            _sync_directory(self.records_dir)
            payload.unlink(missing_ok=True)
            _sync_directory(self.payloads_dir)
        except OSError as error:
            raise TransportError("не удалось удалить browser candidate") from error


@dataclass(frozen=True, slots=True)
class _ZipMember:
    raw_name: str
    size: int
    compressed_size: int
    crc: int
    header_offset: int


def _open_regular_file(path: Path) -> tuple[BinaryIO, tuple[int, int, int, int]]:
    try:
        before = path.lstat()
    except OSError as error:
        raise TransportError("ZIP недоступен") from error
    if stat.S_ISLNK(before.st_mode):
        raise TransportSecurityError("ZIP не может быть символической ссылкой")
    if not stat.S_ISREG(before.st_mode):
        raise TransportSecurityError("ZIP должен быть обычным файлом")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(path, flags)
        after = os.fstat(descriptor)
        if _identity(before) != _identity(after):
            os.close(descriptor)
            descriptor = None
            raise TransportUnstableError("ZIP изменился во время открытия")
        stream = os.fdopen(descriptor, "rb")
        descriptor = None
        return stream, _identity(after)
    except TransportError:
        raise
    except OSError as error:
        raise TransportError("ZIP недоступен") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _zip_member_type(info: zipfile.ZipInfo) -> int:
    if info.create_system != 3:
        return 0
    return stat.S_IFMT((info.external_attr >> 16) & 0xFFFF)


class _TransportReader:
    """LimitedReader с единым отказом и владельцами ZIP/file descriptor."""

    def __init__(
        self,
        stream: BinaryIO,
        budget: ResourceBudget,
        name: str,
        *,
        owners: tuple[object, ...] = (),
        lock: threading.RLock | None = None,
        failure_message: str = "ZIP повреждён или не прошёл CRC",
        failure_type: type[TransportError] = TransportError,
    ):
        self._limited = LimitedReader(stream, budget, name)
        self._owners = owners
        self._lock = lock or threading.RLock()
        self._failure_message = failure_message
        self._failure_type = failure_type
        self._closed = False

    def read(self, size: int = -1) -> bytes:
        try:
            with self._lock:
                return self._limited.read(size)
        except ResourceLimitError as error:
            raise TransportLimitError(str(error)) from error
        except (zipfile.BadZipFile, zlib.error, EOFError, OSError) as error:
            raise self._failure_type(self._failure_message) from error

    def readable(self) -> bool:
        return True

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with self._lock:
            try:
                self._limited.close()
            finally:
                for owner in self._owners:
                    close = getattr(owner, "close", None)
                    if close is not None:
                        close()

    def __enter__(self) -> _TransportReader:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def __getattr__(self, name: str):
        return getattr(self._limited, name)


class ZipExportTree:
    """Безопасный read-only virtual tree поверх одного устойчивого ZIP."""

    _ALLOWED_TRANSPORTS = frozenset(
        {
            CandidateTransport.BROWSER,
            CandidateTransport.INCOMING,
            CandidateTransport.LOCAL_FILE,
        }
    )

    def __init__(
        self,
        path: Path,
        *,
        transport: CandidateTransport,
        origin_name: str | None = None,
        limits: ResourceLimits = ARCHIVE_LIMITS,
    ):
        if transport not in self._ALLOWED_TRANSPORTS:
            raise ValueError("для ZIP выбран неподдержанный transport")
        self.path = Path(path)
        self.transport = transport
        self.origin_name = _display_name(origin_name or self.path.name)
        self._lock = threading.RLock()
        source, identity = _open_regular_file(self.path)
        archive: zipfile.ZipFile | None = None
        try:
            archive = zipfile.ZipFile(source)
            members = self._inspect(archive, limits)
            budget = _resource_budget(
                limits,
                "ZIP файловой выгрузки",
                (
                    (path, member.size, member.compressed_size)
                    for path, member in members.items()
                ),
            )
        except (zipfile.BadZipFile, OSError, EOFError) as error:
            if archive is not None:
                archive.close()
            source.close()
            raise TransportError("ZIP повреждён или недоступен") from error
        except Exception:
            if archive is not None:
                archive.close()
            source.close()
            raise
        self._identity = identity
        self._members = members
        self._paths = tuple(sorted(members))
        self._fingerprint = _fingerprint(["zip", *identity])
        self._budget = budget
        self._source = source
        self._archive = archive
        self._closed = False
        self._source_sha256: str | None = None

    @staticmethod
    def _inspect(
        archive: zipfile.ZipFile,
        limits: ResourceLimits,
    ) -> dict[str, _ZipMember]:
        members: dict[str, _ZipMember] = {}
        seen: set[str] = set()
        infos = archive.infolist()
        if len(infos) > limits.max_entries:
            raise TransportLimitError(
                "ZIP файловой выгрузки: число записей превышает предел"
            )
        for info in infos:
            normalized = _normalized_member_path(info.filename)
            if normalized in seen:
                raise TransportSecurityError(
                    "ZIP содержит дубликат или неоднозначный путь"
                )
            seen.add(normalized)
            file_type = _zip_member_type(info)
            if file_type == stat.S_IFLNK:
                raise TransportSecurityError(
                    "ZIP содержит символическую ссылку"
                )
            if info.is_dir():
                continue
            if file_type not in (0, stat.S_IFREG):
                raise TransportSecurityError(
                    "ZIP содержит неподдержанный специальный файл"
                )
            if info.flag_bits & 0x1:
                raise TransportSecurityError("зашифрованный ZIP не поддерживается")
            member = _ZipMember(
                raw_name=info.filename,
                size=info.file_size,
                compressed_size=info.compress_size,
                crc=info.CRC,
                header_offset=info.header_offset,
            )
            members[normalized] = member
        return members

    def paths(self) -> tuple[str, ...]:
        return self._paths

    def size(self, path: str) -> int:
        path = _requested_path(path)
        try:
            return self._members[path].size
        except KeyError:
            raise KeyError(path) from None

    def fingerprint(self) -> str:
        return self._fingerprint

    def source_sha256(self) -> str:
        with self._lock:
            if self._closed:
                raise TransportError("ZIP tree уже закрыт")
            if self._source_sha256 is not None:
                return self._source_sha256
            source, identity = _open_regular_file(self.path)
            if identity != self._identity:
                source.close()
                raise TransportUnstableError(
                    "ZIP изменился после фиксации снимка"
                )
            digest = hashlib.sha256()
            try:
                with source:
                    for block in iter(lambda: source.read(_READ_CHUNK), b""):
                        digest.update(block)
            except OSError as error:
                raise TransportError("ZIP недоступен для расчёта SHA-256") from error
            if self._current_identity() != self._identity:
                raise TransportUnstableError(
                    "ZIP изменился во время расчёта SHA-256"
                )
            self._source_sha256 = digest.hexdigest()
            return self._source_sha256

    def _current_identity(self) -> tuple[int, int, int, int] | None:
        try:
            info = self.path.lstat()
        except OSError:
            return None
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            return None
        return _identity(info)

    def verify_stable(self, expected: str) -> bool:
        return (
            not self._closed
            and isinstance(expected, str)
            and expected == self._fingerprint
            and self._current_identity() == self._identity
        )

    def open(self, path: str) -> BinaryIO:
        path = _requested_path(path)
        try:
            expected_member = self._members[path]
        except KeyError:
            raise KeyError(path) from None
        with self._lock:
            if self._closed:
                raise TransportError("ZIP tree уже закрыт")
            if self._current_identity() != self._identity:
                raise TransportUnstableError("ZIP изменился после фиксации снимка")
            try:
                info = self._archive.getinfo(expected_member.raw_name)
                actual = _ZipMember(
                    raw_name=info.filename,
                    size=info.file_size,
                    compressed_size=info.compress_size,
                    crc=info.CRC,
                    header_offset=info.header_offset,
                )
                if actual != expected_member:
                    raise TransportUnstableError(
                        "ZIP изменился после фиксации снимка"
                    )
                member_stream = self._archive.open(info)
                return _TransportReader(
                    member_stream,
                    self._budget,
                    path,
                    lock=self._lock,
                )
            except TransportError:
                raise
            except (KeyError, zipfile.BadZipFile, OSError, EOFError) as error:
                raise TransportError("ZIP повреждён или недоступен") from error

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._archive.close()
            self._source.close()

    def __enter__(self) -> ZipExportTree:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass


@dataclass(frozen=True, slots=True)
class _DirectoryMember:
    size: int
    identity: tuple[int, int, int, int]


@dataclass(frozen=True, slots=True)
class _DirectorySnapshot:
    root_identity: tuple[int, int, int, int]
    members: dict[str, _DirectoryMember]
    fingerprint: str


def _open_directory_root(root: Path) -> tuple[int, tuple[int, int, int, int]]:
    try:
        before = root.lstat()
    except OSError as error:
        raise TransportError("read-only каталог недоступен") from error
    if stat.S_ISLNK(before.st_mode):
        raise TransportSecurityError(
            "read-only каталог не может быть символической ссылкой"
        )
    if not stat.S_ISDIR(before.st_mode):
        raise TransportSecurityError("ожидался read-only каталог")
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(
        os, "O_NOFOLLOW", 0
    )
    descriptor: int | None = None
    try:
        descriptor = os.open(root, flags)
        after = os.fstat(descriptor)
        if _identity(before) != _identity(after):
            os.close(descriptor)
            descriptor = None
            raise TransportUnstableError(
                "read-only каталог изменился во время открытия"
            )
        result = descriptor
        descriptor = None
        return result, _identity(after)
    except TransportError:
        raise
    except OSError as error:
        raise TransportError("read-only каталог недоступен") from error
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _scan_directory(
    root: Path,
    limits: ResourceLimits,
    settle_seconds: float,
) -> tuple[_DirectorySnapshot, ResourceBudget]:
    if not isinstance(limits, ResourceLimits):
        raise TypeError("limits должен быть ResourceLimits")
    root_fd, root_identity = _open_directory_root(root)
    members: dict[str, _DirectoryMember] = {}
    entries_total = 0
    bytes_total = 0
    latest_mtime_ns = root_identity[3]

    def visit(directory_fd: int, prefix: tuple[str, ...]) -> None:
        nonlocal entries_total, bytes_total, latest_mtime_ns
        try:
            with os.scandir(directory_fd) as iterator:
                entries = sorted(iterator, key=lambda item: item.name)
        except OSError as error:
            raise TransportError("read-only каталог недоступен") from error
        for entry in entries:
            entries_total += 1
            if entries_total > limits.max_entries:
                raise TransportLimitError(
                    "каталог файловой выгрузки: число записей превышает предел"
                )
            raw_path = PurePosixPath(*prefix, entry.name).as_posix()
            normalized = _normalized_member_path(raw_path)
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError as error:
                raise TransportUnstableError(
                    "read-only каталог изменился во время перечисления"
                ) from error
            latest_mtime_ns = max(latest_mtime_ns, info.st_mtime_ns)
            if stat.S_ISLNK(info.st_mode):
                raise TransportSecurityError(
                    "read-only каталог содержит символическую ссылку"
                )
            if stat.S_ISDIR(info.st_mode):
                flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(
                    os, "O_NOFOLLOW", 0
                )
                child_fd: int | None = None
                try:
                    child_fd = os.open(entry.name, flags, dir_fd=directory_fd)
                    opened = os.fstat(child_fd)
                except OSError as error:
                    if child_fd is not None:
                        os.close(child_fd)
                    raise TransportUnstableError(
                        "read-only каталог изменился во время перечисления"
                    ) from error
                if _identity(opened) != _identity(info):
                    os.close(child_fd)
                    raise TransportUnstableError(
                        "read-only каталог изменился во время перечисления"
                    )
                try:
                    visit(child_fd, (*prefix, entry.name))
                finally:
                    os.close(child_fd)
                continue
            if not stat.S_ISREG(info.st_mode):
                raise TransportSecurityError(
                    "read-only каталог содержит специальный файл"
                )
            member = _DirectoryMember(info.st_size, _identity(info))
            if member.size > limits.max_entry_bytes:
                raise TransportLimitError(
                    "файл read-only каталога превышает предел размера"
                )
            bytes_total += member.size
            if bytes_total > limits.max_total_bytes:
                raise TransportLimitError(
                    "суммарный размер read-only каталога превышает предел"
                )
            members[normalized] = member

    try:
        visit(root_fd, ())
    finally:
        os.close(root_fd)
    age_seconds = (time.time_ns() - latest_mtime_ns) / 1_000_000_000
    if 0 <= age_seconds < settle_seconds:
        raise TransportUnstableError(
            "read-only каталог ещё изменяется; повторите проверку позже"
        )
    budget = _resource_budget(
        limits,
        "каталог файловой выгрузки",
        ((path, member.size, member.size) for path, member in members.items()),
    )

    def fingerprint_parts() -> Iterable[object]:
        yield "directory"
        yield from root_identity
        for path in sorted(members):
            yield path
            yield from members[path].identity

    snapshot = _DirectorySnapshot(
        root_identity=root_identity,
        members=members,
        fingerprint=_fingerprint(fingerprint_parts()),
    )
    return snapshot, budget


def _open_directory_member(
    root: Path,
    path: str,
) -> tuple[BinaryIO, tuple[int, int, int, int]]:
    root_fd, _root_identity = _open_directory_root(root)
    opened_directories = [root_fd]
    parts = PurePosixPath(path).parts
    flags_directory = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(
        os, "O_NOFOLLOW", 0
    )
    flags_file = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    file_fd: int | None = None
    try:
        current = root_fd
        for part in parts[:-1]:
            current = os.open(part, flags_directory, dir_fd=current)
            opened_directories.append(current)
        file_fd = os.open(parts[-1], flags_file, dir_fd=current)
        info = os.fstat(file_fd)
        if not stat.S_ISREG(info.st_mode):
            os.close(file_fd)
            file_fd = None
            raise TransportSecurityError(
                "read-only каталог содержит специальный файл"
            )
        stream = os.fdopen(file_fd, "rb")
        file_fd = None
        return stream, _identity(info)
    except TransportError:
        raise
    except OSError as error:
        raise TransportUnstableError(
            "read-only каталог изменился после фиксации снимка"
        ) from error
    finally:
        if file_fd is not None:
            os.close(file_fd)
        for descriptor in reversed(opened_directories):
            os.close(descriptor)


class DirectoryExportTree:
    """Безопасный virtual tree поверх read-only каталога выгрузки."""

    transport = CandidateTransport.LOCAL_DIRECTORY

    def __init__(
        self,
        root: Path,
        *,
        origin_name: str | None = None,
        limits: ResourceLimits = ARCHIVE_LIMITS,
        settle_seconds: float = DIRECTORY_SETTLE_SECONDS,
    ):
        if (
            isinstance(settle_seconds, bool)
            or not isinstance(settle_seconds, (int, float))
            or settle_seconds < 0
        ):
            raise ValueError("settle_seconds должен быть неотрицательным числом")
        self.root = Path(root)
        self.origin_name = _display_name(origin_name or self.root.name)
        self._limits = limits
        self._settle_seconds = float(settle_seconds)
        self._snapshot, self._budget = _scan_directory(
            self.root,
            limits,
            self._settle_seconds,
        )
        self._paths = tuple(sorted(self._snapshot.members))
        self._lock = threading.RLock()
        self._source_sha256: str | None = None

    def paths(self) -> tuple[str, ...]:
        return self._paths

    def size(self, path: str) -> int:
        path = _requested_path(path)
        try:
            return self._snapshot.members[path].size
        except KeyError:
            raise KeyError(path) from None

    def fingerprint(self) -> str:
        return self._snapshot.fingerprint

    def source_sha256(self) -> str:
        with self._lock:
            if self._source_sha256 is not None:
                return self._source_sha256
            # Состав уже проверен при создании snapshot. Новый счётчик нужен
            # лишь для фактически прочитанных байтов; повторный список всех
            # членов здесь дал бы лишний пик памяти на больших выгрузках.
            budget = ResourceBudget(
                self._limits,
                "канонический снимок каталога",
            )
            digest = hashlib.sha256(b"mcp1c-directory-snapshot-v1\0")
            for path in self._paths:
                encoded_path = path.encode("utf-8")
                digest.update(len(encoded_path).to_bytes(8, "big"))
                digest.update(encoded_path)
                member = self._snapshot.members[path]
                digest.update(member.size.to_bytes(8, "big"))
                stream, identity = _open_directory_member(self.root, path)
                if identity != member.identity:
                    stream.close()
                    raise TransportUnstableError(
                        "read-only каталог изменился при расчёте SHA-256"
                    )
                with _TransportReader(
                    stream,
                    budget,
                    path,
                    lock=self._lock,
                    failure_message=(
                        "файл read-only каталога изменился при расчёте SHA-256"
                    ),
                    failure_type=TransportUnstableError,
                ) as reader:
                    for block in iter(lambda: reader.read(_READ_CHUNK), b""):
                        digest.update(block)
            if not self.verify_stable(self._snapshot.fingerprint):
                raise TransportUnstableError(
                    "read-only каталог изменился при расчёте SHA-256"
                )
            self._source_sha256 = digest.hexdigest()
            return self._source_sha256

    def verify_stable(self, expected: str) -> bool:
        if not isinstance(expected, str) or expected != self._snapshot.fingerprint:
            return False
        try:
            current, _budget = _scan_directory(
                self.root,
                self._limits,
                self._settle_seconds,
            )
        except TransportError:
            return False
        return current.fingerprint == expected

    def open(self, path: str) -> BinaryIO:
        path = _requested_path(path)
        try:
            expected = self._snapshot.members[path]
        except KeyError:
            raise KeyError(path) from None
        stream, identity = _open_directory_member(self.root, path)
        if identity != expected.identity:
            stream.close()
            raise TransportUnstableError(
                "read-only каталог изменился после фиксации снимка"
            )
        return _TransportReader(
            stream,
            self._budget,
            path,
            lock=self._lock,
            failure_message="файл read-only каталога изменился во время чтения",
            failure_type=TransportUnstableError,
        )


def open_export_tree(
    source: Path,
    transport: CandidateTransport,
    *,
    origin_name: str | None = None,
    limits: ResourceLimits = ARCHIVE_LIMITS,
    directory_settle_seconds: float = DIRECTORY_SETTLE_SECONDS,
) -> ZipExportTree | DirectoryExportTree:
    """Открыть настроенный вход без выбора parser или публикации Registry."""
    if transport is CandidateTransport.LOCAL_DIRECTORY:
        return DirectoryExportTree(
            source,
            origin_name=origin_name,
            limits=limits,
            settle_seconds=directory_settle_seconds,
        )
    if transport in {CandidateTransport.INCOMING, CandidateTransport.LOCAL_FILE}:
        return ZipExportTree(
            source,
            transport=transport,
            origin_name=origin_name,
            limits=limits,
        )
    raise ValueError(
        "browser tree открывается только через BrowserStagingStore; "
        "transport для настроенного пути неизвестен"
    )


__all__ = [
    "BrowserStagingStore",
    "DIRECTORY_SETTLE_SECONDS",
    "DirectoryExportTree",
    "MAX_BROWSER_UPLOAD_BYTES",
    "StagedUpload",
    "TransportError",
    "TransportLimitError",
    "TransportSecurityError",
    "TransportUnstableError",
    "ZipExportTree",
    "open_export_tree",
]
