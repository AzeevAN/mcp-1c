"""On-demand обнаружение и безопасное повторное открытие кандидатов intake.

Модуль не публикует поколения и не принимает пути от клиента. Он объединяет
managed browser staging с заранее известными серверу входами, сохраняет малый
probe отдельно от привязанного ``ExportCandidate`` и передаёт готовый снимок
существующему backend операций только после выбора родителя.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Iterator, Mapping

from .intake_v2 import (
    CandidateJob,
    CandidateJobState,
    CandidateTransport,
    ExportCandidate,
    SourceKind,
)
from .intake_v2_operations import IntakeCoordinator, IntakePreview, OperationError
from .intake_v2_planner import IntakeAction
from .intake_v2_registry import GenerationView
from .intake_v2_probe import CandidateProbe, ProbeError, probe_export
from .intake_v2_transport import (
    BrowserStagingStore,
    TransportError,
    TransportSecurityError,
    open_export_tree,
)
from .resource_limits import ARCHIVE_LIMITS, ResourceLimits


_FORMAT_VERSION = 1
_MAX_RECORD_BYTES = 64 * 1024
_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9._-]+\Z")


class LifecycleError(RuntimeError):
    """Lifecycle кандидатов не может достоверно выполнить запрос."""


class LifecycleConflict(LifecycleError):
    """Сохранённый candidate или выбранное действие уже не совпадают."""


def _identifier(value: object, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 128
        or not _IDENTIFIER_RE.fullmatch(value)
    ):
        raise LifecycleError(f"{label} имеет недопустимый формат")
    return value


def _entry_name(value: object, *, allow_empty: bool = False) -> str:
    if allow_empty and value == "":
        return ""
    if not isinstance(value, str) or not value or "\x00" in value:
        raise LifecycleError("entry_name имеет недопустимый формат")
    if (
        value in {".", ".."}
        or "/" in value
        or "\\" in value
        or len(value.encode("utf-8")) > 1024
        or any(ord(char) < 32 for char in value)
    ):
        raise LifecycleError("entry_name имеет недопустимый формат")
    return value


def _identity(info: os.stat_result) -> tuple[int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns


def _sync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _ensure_directory(path: Path, label: str) -> None:
    try:
        if path.is_symlink():
            raise LifecycleError(f"{label} не может быть символической ссылкой")
        path.mkdir(parents=True, exist_ok=True)
        if path.is_symlink() or not path.is_dir():
            raise LifecycleError(f"{label} должен быть обычным каталогом")
    except LifecycleError:
        raise
    except OSError as error:
        raise LifecycleError(f"не удалось открыть {label}") from error


@dataclass(frozen=True, slots=True)
class CandidateLocator:
    """Серверная ссылка на настроенный вход без абсолютного пути."""

    transport: CandidateTransport
    source_id: str
    entry_name: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.transport, CandidateTransport):
            raise LifecycleError("transport locator должен быть CandidateTransport")
        _identifier(self.source_id, "source_id")
        _entry_name(self.entry_name, allow_empty=True)
        if self.transport in {CandidateTransport.BROWSER, CandidateTransport.INCOMING}:
            if not self.entry_name:
                raise LifecycleError("browser/incoming locator требует entry_name")
        elif self.transport is CandidateTransport.LOCAL_DIRECTORY:
            if self.entry_name:
                raise LifecycleError("local-directory locator не принимает entry_name")

    def to_dict(self) -> dict[str, str]:
        return {
            "transport": self.transport.value,
            "source_id": self.source_id,
            "entry_name": self.entry_name,
        }

    @classmethod
    def from_dict(cls, raw: object) -> CandidateLocator:
        if not isinstance(raw, dict):
            raise LifecycleError("locator должен быть объектом")
        try:
            return cls(
                transport=CandidateTransport(raw["transport"]),
                source_id=raw["source_id"],
                entry_name=raw.get("entry_name", ""),
            )
        except (KeyError, TypeError, ValueError) as error:
            if isinstance(error, LifecycleError):
                raise
            raise LifecycleError("locator содержит неверные поля") from error


@dataclass(frozen=True, slots=True)
class DiscoveredCandidate:
    """Durable probe, ещё не привязанный к родителю и операции."""

    candidate_id: str
    locator: CandidateLocator
    probe: CandidateProbe

    def __post_init__(self) -> None:
        _identifier(self.candidate_id, "candidate_id")
        if not isinstance(self.locator, CandidateLocator):
            raise LifecycleError("locator имеет неверный тип")
        if not isinstance(self.probe, CandidateProbe):
            raise LifecycleError("probe имеет неверный тип")
        if self.probe.transport is not self.locator.transport:
            raise LifecycleError("probe и locator относятся к разным transport")
        if (
            self.locator.transport is CandidateTransport.BROWSER
            and self.candidate_id != self.locator.entry_name
        ):
            raise LifecycleError("browser candidate_id не совпадает с staging")

    @property
    def grouping_key(self) -> tuple[str, str]:
        return self.probe.source_kind.value, self.probe.internal_name

    def to_dict(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "locator": self.locator.to_dict(),
            "probe": self.probe.to_dict(),
        }

    @classmethod
    def from_dict(cls, raw: object) -> DiscoveredCandidate:
        if not isinstance(raw, dict):
            raise LifecycleError("discovered candidate должен быть объектом")
        try:
            return cls(
                candidate_id=raw["candidate_id"],
                locator=CandidateLocator.from_dict(raw["locator"]),
                probe=CandidateProbe.from_dict(raw["probe"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            if isinstance(error, LifecycleError):
                raise
            raise LifecycleError("discovered candidate содержит неверные поля") from error


@dataclass(frozen=True, slots=True)
class DiscoveryIssue:
    source_id: str
    origin_name: str
    message: str

    def __post_init__(self) -> None:
        _identifier(self.source_id, "source_id")
        _entry_name(self.origin_name)
        if not isinstance(self.message, str) or not self.message:
            raise LifecycleError("message issue должен быть непустой строкой")
        if len(self.message) > 2048:
            raise LifecycleError("message issue превышает предел")


@dataclass(frozen=True, slots=True)
class CandidateRefresh:
    candidates: tuple[DiscoveredCandidate, ...]
    groups: Mapping[tuple[str, str], tuple[str, ...]]
    issues: tuple[DiscoveryIssue, ...]
    selected_candidate_id: None = None

    def __post_init__(self) -> None:
        if not isinstance(self.candidates, tuple) or not all(
            isinstance(item, DiscoveredCandidate) for item in self.candidates
        ):
            raise LifecycleError("candidates должны быть tuple")
        if not isinstance(self.issues, tuple) or not all(
            isinstance(item, DiscoveryIssue) for item in self.issues
        ):
            raise LifecycleError("issues должны быть tuple")
        normalized = {key: tuple(value) for key, value in sorted(self.groups.items())}
        object.__setattr__(self, "groups", MappingProxyType(normalized))


class CandidateCatalog:
    """Малое атомарное хранилище unbound probe и серверных locator."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.records_dir = self.root / "records"
        _ensure_directory(self.root, "candidate catalog")
        _ensure_directory(self.records_dir, "records candidate catalog")
        self._cleanup_temporary()

    def _cleanup_temporary(self) -> None:
        try:
            changed = False
            for path in self.records_dir.iterdir():
                if path.is_symlink():
                    raise LifecycleError(
                        "candidate catalog содержит символическую ссылку"
                    )
                if (
                    path.is_file()
                    and path.name.startswith(".")
                    and path.suffix == ".tmp"
                ):
                    path.unlink()
                    changed = True
            if changed:
                _sync_directory(self.records_dir)
        except LifecycleError:
            raise
        except OSError as error:
            raise LifecycleError("не удалось восстановить candidate catalog") from error

    def _path(self, candidate_id: str) -> Path:
        return self.records_dir / f"{_identifier(candidate_id, 'candidate_id')}.json"

    @staticmethod
    def _encoded(candidate: DiscoveredCandidate) -> bytes:
        return (
            json.dumps(
                {
                    "format_version": _FORMAT_VERSION,
                    "kind": "discovered-candidate",
                    "payload": candidate.to_dict(),
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")

    def save(self, candidate: DiscoveredCandidate) -> None:
        if not isinstance(candidate, DiscoveredCandidate):
            raise TypeError("candidate должен быть DiscoveredCandidate")
        target = self._path(candidate.candidate_id)
        try:
            previous = self.load(candidate.candidate_id)
        except KeyError:
            previous = None
        if previous is not None and previous.locator != candidate.locator:
            raise LifecycleConflict("candidate_id уже относится к другому locator")
        encoded = self._encoded(candidate)
        if len(encoded) > _MAX_RECORD_BYTES:
            raise LifecycleError("запись candidate catalog превышает предел")
        temporary: Path | None = None
        try:
            descriptor, raw_path = tempfile.mkstemp(
                dir=self.records_dir,
                prefix=f".{candidate.candidate_id}.",
                suffix=".tmp",
            )
            temporary = Path(raw_path)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            temporary = None
            _sync_directory(self.records_dir)
        except OSError as error:
            raise LifecycleError("не удалось сохранить candidate catalog") from error
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def load(self, candidate_id: str) -> DiscoveredCandidate:
        path = self._path(candidate_id)
        descriptor: int | None = None
        try:
            before = path.lstat()
            if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
                raise LifecycleError(
                    "запись candidate catalog должна быть обычным файлом"
                )
            if before.st_size > _MAX_RECORD_BYTES:
                raise LifecycleError("запись candidate catalog превышает предел")
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            after = os.fstat(descriptor)
            if _identity(before) != _identity(after):
                raise LifecycleError("запись candidate catalog изменилась при чтении")
            with os.fdopen(descriptor, "rb") as stream:
                descriptor = None
                encoded = stream.read(_MAX_RECORD_BYTES + 1)
            if len(encoded) > _MAX_RECORD_BYTES:
                raise LifecycleError("запись candidate catalog превышает предел")
            raw = json.loads(encoded.decode("utf-8"))
        except FileNotFoundError:
            raise KeyError(candidate_id) from None
        except LifecycleError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise LifecycleError("запись candidate catalog повреждена") from error
        finally:
            if descriptor is not None:
                os.close(descriptor)
        if (
            not isinstance(raw, dict)
            or raw.get("format_version") != _FORMAT_VERSION
            or raw.get("kind") != "discovered-candidate"
        ):
            raise LifecycleError("запись candidate catalog несовместима")
        try:
            candidate = DiscoveredCandidate.from_dict(raw.get("payload"))
        except (LifecycleError, ProbeError) as error:
            raise LifecycleError(
                "запись candidate catalog содержит неверный candidate"
            ) from error
        if candidate.candidate_id != candidate_id:
            raise LifecycleError(
                "запись candidate catalog относится к другому candidate"
            )
        return candidate

    def remove(self, candidate_id: str) -> None:
        path = self._path(candidate_id)
        try:
            info = path.lstat()
        except FileNotFoundError:
            return
        except OSError as error:
            raise LifecycleError("candidate catalog недоступен") from error
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise LifecycleError(
                "запись candidate catalog должна быть обычным файлом"
            )
        try:
            path.unlink()
            _sync_directory(self.records_dir)
        except OSError as error:
            raise LifecycleError(
                "не удалось удалить запись candidate catalog"
            ) from error


class IntakeLifecycle:
    """Обнаруживает входы только по ``refresh`` и начинает durable job."""

    def __init__(
        self,
        catalog: CandidateCatalog,
        browser: BrowserStagingStore,
        operations: IntakeCoordinator,
        *,
        incoming_root: Path | None = None,
        local_sources: Mapping[str, Path] | None = None,
        limits: ResourceLimits = ARCHIVE_LIMITS,
        directory_settle_seconds: float = 5.0,
    ):
        if not isinstance(catalog, CandidateCatalog):
            raise TypeError("catalog должен быть CandidateCatalog")
        if not isinstance(browser, BrowserStagingStore):
            raise TypeError("browser должен быть BrowserStagingStore")
        if not isinstance(operations, IntakeCoordinator):
            raise TypeError("operations должен быть IntakeCoordinator")
        if not isinstance(limits, ResourceLimits):
            raise TypeError("limits должен быть ResourceLimits")
        if (
            isinstance(directory_settle_seconds, bool)
            or not isinstance(directory_settle_seconds, (int, float))
            or directory_settle_seconds < 0
        ):
            raise ValueError("directory_settle_seconds должен быть неотрицательным")
        normalized_sources: dict[str, Path] = {}
        for source_id, source in (local_sources or {}).items():
            normalized_sources[_identifier(source_id, "local source_id")] = Path(source)
        self.catalog = catalog
        self.browser = browser
        self.operations = operations
        self.incoming_root = (
            Path(incoming_root) if incoming_root is not None else None
        )
        self.local_sources = MappingProxyType(
            dict(sorted(normalized_sources.items()))
        )
        self.limits = limits
        self.directory_settle_seconds = float(directory_settle_seconds)
        self._lock = threading.RLock()

    @staticmethod
    def _issue(source_id: str, origin_name: str, error: Exception) -> DiscoveryIssue:
        try:
            _entry_name(origin_name)
        except LifecycleError:
            origin_name = source_id
        message = (
            "источник недоступен"
            if isinstance(error, OSError)
            else str(error).strip() or error.__class__.__name__
        )
        return DiscoveryIssue(
            source_id,
            origin_name,
            message[:2048],
        )

    @staticmethod
    def _candidate_id(locator: CandidateLocator, probe: CandidateProbe) -> str:
        if locator.transport is CandidateTransport.BROWSER:
            return locator.entry_name
        payload = json.dumps(
            {
                "locator": locator.to_dict(),
                "raw_sha256": probe.raw_sha256,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return f"source-{hashlib.sha256(payload).hexdigest()[:40]}"

    def _discover(self, locator: CandidateLocator) -> DiscoveredCandidate:
        with self._open(locator) as tree:
            probe = probe_export(tree)
        candidate = DiscoveredCandidate(
            self._candidate_id(locator, probe), locator, probe
        )
        self.catalog.save(candidate)
        return candidate

    def discover_browser(self, candidate_id: str) -> DiscoveredCandidate:
        """Проверить ровно один уже принятый browser-upload."""
        with self._lock:
            upload = self.browser.load(candidate_id)
            locator = CandidateLocator(
                CandidateTransport.BROWSER,
                "browser",
                upload.candidate_id,
            )
            try:
                return self._discover(locator)
            except (ProbeError, TransportError) as error:
                raise LifecycleError(str(error)) from error

    def _cached_browser(
        self, locator: CandidateLocator
    ) -> DiscoveredCandidate | None:
        try:
            candidate = self.catalog.load(locator.entry_name)
        except KeyError:
            return None
        upload = self.browser.load(locator.entry_name)
        if (
            candidate.locator != locator
            or candidate.probe.origin_name != upload.origin_name
            or candidate.probe.raw_sha256 != upload.sha256
        ):
            return None
        return candidate

    @staticmethod
    def _safe_directory_entries(
        path: Path, label: str
    ) -> tuple[os.DirEntry[str], ...]:
        try:
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise TransportSecurityError(f"{label} должен быть обычным каталогом")
            with os.scandir(path) as entries:
                return tuple(
                    sorted(
                        entries,
                        key=lambda entry: (entry.name.casefold(), entry.name),
                    )
                )
        except FileNotFoundError:
            raise
        except TransportError:
            raise
        except OSError as error:
            raise TransportError(f"{label} недоступен") from error

    @staticmethod
    def _archive_entries(
        entries: tuple[os.DirEntry[str], ...],
        *,
        source_id: str,
        transport: CandidateTransport,
    ) -> tuple[list[CandidateLocator], list[DiscoveryIssue]]:
        locators: list[CandidateLocator] = []
        issues: list[DiscoveryIssue] = []
        for entry in entries:
            if not entry.name.casefold().endswith(".zip"):
                continue
            try:
                if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                    raise TransportSecurityError(
                        "ZIP candidate должен быть обычным файлом"
                    )
                locators.append(CandidateLocator(transport, source_id, entry.name))
            except (OSError, LifecycleError, TransportError) as error:
                issues.append(IntakeLifecycle._issue(source_id, entry.name, error))
        return locators, issues

    def _browser_locators(self) -> tuple[list[CandidateLocator], list[DiscoveryIssue]]:
        locators: list[CandidateLocator] = []
        issues: list[DiscoveryIssue] = []
        try:
            candidate_ids = self.browser.candidate_ids()
        except TransportError as error:
            return [], [self._issue("browser", "browser", error)]
        for candidate_id in candidate_ids:
            try:
                upload = self.browser.load(candidate_id)
                locators.append(
                    CandidateLocator(
                        CandidateTransport.BROWSER,
                        "browser",
                        upload.candidate_id,
                    )
                )
            except (LifecycleError, TransportError) as error:
                issues.append(self._issue("browser", candidate_id, error))
        return locators, issues

    def _incoming_locators(self) -> tuple[list[CandidateLocator], list[DiscoveryIssue]]:
        if self.incoming_root is None:
            return [], []
        try:
            entries = self._safe_directory_entries(self.incoming_root, "incoming")
        except FileNotFoundError:
            return [], []
        except TransportError as error:
            return [], [self._issue("incoming", "incoming", error)]
        return self._archive_entries(
            entries,
            source_id="incoming",
            transport=CandidateTransport.INCOMING,
        )

    def _local_locators(
        self, source_id: str, source: Path
    ) -> tuple[list[CandidateLocator], list[DiscoveryIssue]]:
        try:
            info = source.lstat()
        except FileNotFoundError:
            return [], [
                self._issue(
                    source_id,
                    source.name or source_id,
                    LifecycleError("local source не найден"),
                )
            ]
        except OSError:
            return [], [
                self._issue(
                    source_id,
                    source.name or source_id,
                    LifecycleError("local source недоступен"),
                )
            ]
        if stat.S_ISLNK(info.st_mode):
            return [], [
                self._issue(
                    source_id,
                    source.name or source_id,
                    TransportSecurityError(
                        "local source не может быть символической ссылкой"
                    ),
                )
            ]
        if stat.S_ISREG(info.st_mode):
            return [CandidateLocator(CandidateTransport.LOCAL_FILE, source_id)], []
        if not stat.S_ISDIR(info.st_mode):
            return [], [
                self._issue(
                    source_id,
                    source.name or source_id,
                    TransportSecurityError("local source имеет неподдержанный тип"),
                )
            ]
        try:
            entries = self._safe_directory_entries(source, "local source")
        except (FileNotFoundError, TransportError) as error:
            return [], [self._issue(source_id, source.name or source_id, error)]
        archive_locators, issues = self._archive_entries(
            entries,
            source_id=source_id,
            transport=CandidateTransport.LOCAL_FILE,
        )
        root_descriptor = any(
            entry.name == "Configuration.xml"
            and not entry.is_symlink()
            and entry.is_file(follow_symlinks=False)
            for entry in entries
        )
        wrapper_descriptor = False
        for entry in entries:
            if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                continue
            descriptor = source / entry.name / "Configuration.xml"
            try:
                child_info = descriptor.lstat()
            except FileNotFoundError:
                continue
            except OSError:
                continue
            if stat.S_ISREG(child_info.st_mode) and not stat.S_ISLNK(
                child_info.st_mode
            ):
                wrapper_descriptor = True
                break
        if (root_descriptor or wrapper_descriptor) and archive_locators:
            return [], issues + [
                self._issue(
                    source_id,
                    source.name or source_id,
                    LifecycleError("local source неоднозначен: найдены дерево и ZIP"),
                )
            ]
        if root_descriptor or wrapper_descriptor or not archive_locators:
            return [CandidateLocator(CandidateTransport.LOCAL_DIRECTORY, source_id)], issues
        return archive_locators, issues

    @contextmanager
    def _open(self, locator: CandidateLocator) -> Iterator[object]:
        if locator.transport is CandidateTransport.BROWSER:
            tree = self.browser.open_tree(locator.entry_name, limits=self.limits)
        elif locator.transport is CandidateTransport.INCOMING:
            if self.incoming_root is None:
                raise LifecycleConflict("incoming больше не настроен")
            tree = open_export_tree(
                self.incoming_root / locator.entry_name,
                CandidateTransport.INCOMING,
                origin_name=locator.entry_name,
                limits=self.limits,
            )
        else:
            try:
                source = self.local_sources[locator.source_id]
            except KeyError:
                raise LifecycleConflict("local source больше не настроен") from None
            if locator.entry_name:
                source = source / locator.entry_name
            tree = open_export_tree(
                source,
                locator.transport,
                origin_name=locator.entry_name or source.name,
                limits=self.limits,
                directory_settle_seconds=self.directory_settle_seconds,
            )
        try:
            yield tree
        finally:
            close = getattr(tree, "close", None)
            if close is not None:
                close()

    def refresh(self) -> CandidateRefresh:
        """Сканировать входы ровно по явному запросу вызывающего."""
        with self._lock:
            locators: list[CandidateLocator] = []
            issues: list[DiscoveryIssue] = []
            found, failed = self._browser_locators()
            locators.extend(found)
            issues.extend(failed)
            found, failed = self._incoming_locators()
            locators.extend(found)
            issues.extend(failed)
            for source_id, source in self.local_sources.items():
                found, failed = self._local_locators(source_id, source)
                locators.extend(found)
                issues.extend(failed)

            candidates: list[DiscoveredCandidate] = []
            seen: set[str] = set()
            for locator in sorted(
                locators,
                key=lambda item: (
                    item.transport.value,
                    item.source_id,
                    item.entry_name.casefold(),
                    item.entry_name,
                ),
            ):
                try:
                    candidate = (
                        self._cached_browser(locator)
                        if locator.transport is CandidateTransport.BROWSER
                        else None
                    )
                    if candidate is None:
                        candidate = self._discover(locator)
                    if candidate.candidate_id in seen:
                        raise LifecycleConflict("candidate_id продублирован при refresh")
                    seen.add(candidate.candidate_id)
                    candidates.append(candidate)
                except (LifecycleError, ProbeError, TransportError) as error:
                    issues.append(
                        self._issue(
                            locator.source_id,
                            locator.entry_name or locator.source_id,
                            error,
                        )
                    )

            candidates.sort(
                key=lambda item: (
                    item.grouping_key,
                    item.probe.origin_name.casefold(),
                    item.probe.origin_name,
                    item.candidate_id,
                )
            )
            groups: dict[tuple[str, str], list[str]] = {}
            for candidate in candidates:
                groups.setdefault(candidate.grouping_key, []).append(
                    candidate.candidate_id
                )
            issues.sort(
                key=lambda item: (
                    item.source_id,
                    item.origin_name.casefold(),
                    item.origin_name,
                    item.message,
                )
            )
            return CandidateRefresh(
                tuple(candidates),
                {key: tuple(value) for key, value in groups.items()},
                tuple(issues),
            )

    @staticmethod
    def _expected_candidate(
        discovered: DiscoveredCandidate, parent_configuration: str
    ) -> ExportCandidate:
        bound = discovered.probe.bind(
            discovered.candidate_id,
            parent_configuration=parent_configuration,
        )
        return ExportCandidate(
            candidate_id=discovered.candidate_id,
            transport=discovered.probe.transport,
            origin_name=discovered.probe.origin_name,
            raw_sha256=discovered.probe.raw_sha256,
            snapshot_fingerprint=discovered.probe.snapshot_fingerprint,
            identity=bound.identity,
        )

    def start(
        self,
        job_id: str,
        candidate_id: str,
        *,
        parent_configuration: str = "",
    ) -> ExportCandidate:
        """Привязать candidate и создать либо продолжить его durable job."""
        with self._lock:
            _identifier(job_id, "job_id")
            discovered = self.catalog.load(candidate_id)
            try:
                expected = self._expected_candidate(discovered, parent_configuration)
            except ProbeError as error:
                raise LifecycleConflict(str(error)) from error
            try:
                job = self.operations.records.load_job(job_id)
            except KeyError:
                job = None
            if job is not None:
                if job.candidate_id != candidate_id:
                    raise LifecycleConflict("job уже относится к другому candidate")
                if job.state is CandidateJobState.FAILED:
                    raise LifecycleConflict("failed job нельзя возобновить")
                if job.state in {
                    CandidateJobState.READY,
                    CandidateJobState.PARSING,
                    CandidateJobState.DONE,
                }:
                    candidate = self.operations.records.load_candidate(candidate_id)
                    if candidate != expected:
                        raise LifecycleConflict(
                            "родитель или candidate изменились после start"
                        )
                    return candidate
            try:
                with self._open(discovered.locator) as tree:
                    if job is None:
                        self.operations.create_job(job_id, candidate_id)
                    return self.operations.probe(
                        job_id,
                        tree,
                        parent_configuration=parent_configuration,
                        expected_probe=discovered.probe,
                    )
            except (OperationError, ProbeError, TransportError) as error:
                raise LifecycleConflict(str(error)) from error

    def prepare(
        self,
        job_id: str,
        *,
        action: IntakeAction,
        active: GenerationView | None,
        generation_id: str,
    ) -> IntakePreview:
        """Построить preview из server-side locator, не принимая путь."""
        job = self.operations.records.load_job(job_id)
        discovered = self.catalog.load(job.candidate_id)
        with self._open(discovered.locator) as tree:
            return self.operations.prepare(
                job_id,
                tree,
                action=action,
                active=active,
                generation_id=generation_id,
            )

    def resume(
        self, job_id: str, *, expected_action: IntakeAction
    ) -> IntakePreview:
        """Возобновить оборванный parse с ранее сохранёнными параметрами."""
        job = self.operations.records.load_job(job_id)
        discovered = self.catalog.load(job.candidate_id)
        with self._open(discovered.locator) as tree:
            return self.operations.resume(
                job_id,
                tree,
                expected_action=expected_action,
            )

    def discard(self, job_id: str) -> None:
        """Отменить готовый preview, сохранив сам входной candidate."""
        with self._lock:
            self.operations.discard(job_id)

    def release_committed_candidate(self, job_id: str) -> None:
        """Убрать managed ZIP после commit; server-side источники не менять."""
        with self._lock:
            job = self.operations.records.load_job(job_id)
            try:
                candidate = self.catalog.load(job.candidate_id)
            except KeyError:
                return
            if candidate.locator.transport is not CandidateTransport.BROWSER:
                return
            try:
                self.browser.discard(job.candidate_id)
            except TransportError as error:
                raise LifecycleError(str(error)) from error
            self.catalog.remove(job.candidate_id)

    @staticmethod
    def _belongs_to_configuration(
        candidate: ExportCandidate, configuration: str
    ) -> bool:
        identity = candidate.identity
        return (
            identity.source_kind is SourceKind.CONFIGURATION
            and identity.configuration_name == configuration
        ) or (
            identity.source_kind is SourceKind.EXTENSION
            and identity.parent_configuration == configuration
        )

    def configuration_jobs(self, configuration: str) -> tuple[CandidateJob, ...]:
        """Найти durable jobs основной конфигурации и её расширений."""
        with self._lock:
            matched: list[CandidateJob] = []
            for job in self.operations.records.list_jobs():
                try:
                    candidate = self.operations.records.load_candidate(
                        job.candidate_id
                    )
                except KeyError:
                    # До успешного probe identity ещё не доказана, а готового
                    # preview или тяжёлого work у такой job быть не может.
                    continue
                if self._belongs_to_configuration(candidate, configuration):
                    matched.append(job)
            return tuple(matched)

    def ensure_configuration_purgeable(self, configuration: str) -> None:
        for job in self.configuration_jobs(configuration):
            if job.state not in {CandidateJobState.DONE, CandidateJobState.FAILED}:
                raise LifecycleConflict(
                    "Конфигурацию нельзя удалить во время операции intake."
                )

    def purge_configuration(self, configuration: str) -> None:
        """Снести derived jobs конфигурации, не меняя входные источники."""
        with self._lock:
            jobs = self.configuration_jobs(configuration)
            active = [
                job
                for job in jobs
                if job.state not in {
                    CandidateJobState.DONE,
                    CandidateJobState.FAILED,
                }
            ]
            if active:
                raise LifecycleConflict(
                    "Конфигурацию нельзя удалить во время операции intake."
                )
            candidate_ids = {job.candidate_id for job in jobs}
            for job in jobs:
                self.operations.remove_job(job.job_id)
            referenced = {
                job.candidate_id
                for job in self.operations.records.list_jobs()
            }
            for candidate_id in sorted(candidate_ids - referenced):
                try:
                    discovered = self.catalog.load(candidate_id)
                except KeyError:
                    discovered = None
                if (
                    discovered is not None
                    and discovered.locator.transport is CandidateTransport.BROWSER
                ):
                    try:
                        self.browser.discard(candidate_id)
                    except TransportError as error:
                        raise LifecycleError(str(error)) from error
                self.catalog.remove(candidate_id)
                self.operations.records.remove_candidate(candidate_id)


__all__ = [
    "CandidateCatalog",
    "CandidateLocator",
    "CandidateRefresh",
    "DiscoveredCandidate",
    "DiscoveryIssue",
    "IntakeLifecycle",
    "LifecycleConflict",
    "LifecycleError",
]
