"""Типизированные контракты единого приёма конфигураций.

Модуль намеренно не читает ZIP, не разбирает XML и не публикует Registry.
Он фиксирует общий язык следующих этапов: личность кандидата, потоковое дерево,
состояния слоёв, manifest поколения, durable candidate/job и recovery.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass, field, replace
from enum import Enum
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import BinaryIO, Protocol, runtime_checkable


_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9._-]+\Z")
_STORE_FORMAT_VERSION = 1


class IntakeV2ContractError(ValueError):
    """Нарушен типизированный контракт единого приёма."""


class CandidateStoreError(RuntimeError):
    """Durable candidate/job нельзя достоверно прочитать или записать."""


class SourceKind(str, Enum):
    CONFIGURATION = "configuration"
    EXTENSION = "extension"


class CandidateTransport(str, Enum):
    BROWSER = "browser"
    INCOMING = "incoming"
    LOCAL_FILE = "local-file"
    LOCAL_DIRECTORY = "local-directory"


class CandidateJobState(str, Enum):
    ACCEPTED = "accepted"
    PROBING = "probing"
    READY = "ready"
    PARSING = "parsing"
    DONE = "done"
    FAILED = "failed"


class MetadataKindPolicy(str, Enum):
    SUPPORTED = "supported"
    DEFERRED = "deferred"
    IGNORED = "ignored"


class LayerKind(str, Enum):
    BASE_STRUCTURE = "base_structure"
    EXTENDED_STRUCTURE = "extended_structure"
    FORMS = "forms"
    CODE = "code"
    ROLES = "roles"


class LayerState(str, Enum):
    READY = "ready"
    ERROR = "error"
    UNAVAILABLE = "unavailable"


class RecoveryPhase(str, Enum):
    PREPARED = "prepared"
    POINTER_SWITCHED = "pointer_switched"


class RecoveryAction(str, Enum):
    ROLLBACK_STAGING = "rollback_staging"
    FINALIZE_NEW = "finalize_new"
    BLOCK = "block"


def _required_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise IntakeV2ContractError(f"{label} должен быть непустой строкой")
    if "\x00" in value:
        raise IntakeV2ContractError(f"{label} содержит недопустимый символ")
    return value


def _safe_identifier(value: object, label: str) -> str:
    value = _required_text(value, label)
    if not _IDENTIFIER_RE.fullmatch(value):
        raise IntakeV2ContractError(
            f"{label} должен содержать только ASCII-буквы, цифры, '.', '_' или '-'"
        )
    return value


def _sha256(value: object, label: str, *, allow_empty: bool = False) -> str:
    if allow_empty and value == "":
        return ""
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise IntakeV2ContractError(f"{label} должен быть sha256 в нижнем регистре")
    return value


def _nonnegative_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise IntakeV2ContractError(f"{label} должен быть целым числом не меньше нуля")
    return value


def _positive_int(value: object, label: str) -> int:
    value = _nonnegative_int(value, label)
    if value == 0:
        raise IntakeV2ContractError(f"{label} должен быть больше нуля")
    return value


def _relative_manifest_path(value: object, *, allow_empty: bool = False) -> str:
    if allow_empty and value == "":
        return ""
    value = _required_text(value, "relative_path")
    if "\\" in value:
        raise IntakeV2ContractError("relative_path должен быть относительным POSIX-путём")
    path = PurePosixPath(value)
    if (
        value == "."
        or not path.parts
        or path.is_absolute()
        or any(part in ("", ".", "..") for part in path.parts)
    ):
        raise IntakeV2ContractError("relative_path должен быть относительным безопасным путём")
    normalized = path.as_posix()
    if normalized != value:
        raise IntakeV2ContractError("relative_path должен быть нормализован")
    return normalized


@dataclass(frozen=True, slots=True)
class ExportIdentity:
    """Внутренняя личность source B, независимая от имени и упаковки файла."""

    source_kind: SourceKind
    configuration_name: str = ""
    extension_name: str = ""
    parent_configuration: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.source_kind, SourceKind):
            raise IntakeV2ContractError("source_kind должен быть SourceKind")
        if self.source_kind is SourceKind.CONFIGURATION:
            _required_text(self.configuration_name, "имя конфигурации")
            if self.extension_name or self.parent_configuration:
                raise IntakeV2ContractError(
                    "у основной конфигурации не бывает расширения или родителя"
                )
            return
        _required_text(self.extension_name, "имя расширения")
        _required_text(self.parent_configuration, "родитель расширения")
        if self.configuration_name:
            raise IntakeV2ContractError(
                "личность расширения хранит базу только в parent_configuration"
            )

    @classmethod
    def configuration(cls, name: str) -> ExportIdentity:
        return cls(SourceKind.CONFIGURATION, configuration_name=name)

    @classmethod
    def extension(
        cls, name: str, *, parent_configuration: str
    ) -> ExportIdentity:
        return cls(
            SourceKind.EXTENSION,
            extension_name=name,
            parent_configuration=parent_configuration,
        )

    @property
    def grouping_key(self) -> tuple[str, ...]:
        if self.source_kind is SourceKind.CONFIGURATION:
            return self.source_kind.value, self.configuration_name
        return (
            self.source_kind.value,
            self.parent_configuration,
            self.extension_name,
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "source_kind": self.source_kind.value,
            "configuration_name": self.configuration_name,
            "extension_name": self.extension_name,
            "parent_configuration": self.parent_configuration,
        }

    @classmethod
    def from_dict(cls, raw: object) -> ExportIdentity:
        if not isinstance(raw, dict):
            raise IntakeV2ContractError("identity должен быть объектом")
        try:
            return cls(
                source_kind=SourceKind(raw["source_kind"]),
                configuration_name=raw.get("configuration_name", ""),
                extension_name=raw.get("extension_name", ""),
                parent_configuration=raw.get("parent_configuration", ""),
            )
        except (KeyError, TypeError, ValueError) as error:
            if isinstance(error, IntakeV2ContractError):
                raise
            raise IntakeV2ContractError("identity содержит неверные поля") from error


@dataclass(frozen=True, slots=True)
class ExportCandidate:
    """Доказанный устойчивый снимок входа до тяжёлого разбора."""

    candidate_id: str
    transport: CandidateTransport
    origin_name: str
    raw_sha256: str
    snapshot_fingerprint: str
    identity: ExportIdentity

    def __post_init__(self) -> None:
        _safe_identifier(self.candidate_id, "candidate_id")
        if not isinstance(self.transport, CandidateTransport):
            raise IntakeV2ContractError("transport должен быть CandidateTransport")
        _required_text(self.origin_name, "origin_name")
        _sha256(self.raw_sha256, "raw_sha256")
        _sha256(self.snapshot_fingerprint, "snapshot_fingerprint")
        if not isinstance(self.identity, ExportIdentity):
            raise IntakeV2ContractError("identity должен быть ExportIdentity")

    def to_dict(self) -> dict[str, object]:
        return {
            "candidate_id": self.candidate_id,
            "transport": self.transport.value,
            "origin_name": self.origin_name,
            "raw_sha256": self.raw_sha256,
            "snapshot_fingerprint": self.snapshot_fingerprint,
            "identity": self.identity.to_dict(),
        }

    @classmethod
    def from_dict(cls, raw: object) -> ExportCandidate:
        if not isinstance(raw, dict):
            raise IntakeV2ContractError("candidate должен быть объектом")
        try:
            return cls(
                candidate_id=raw["candidate_id"],
                transport=CandidateTransport(raw["transport"]),
                origin_name=raw["origin_name"],
                raw_sha256=raw["raw_sha256"],
                snapshot_fingerprint=raw["snapshot_fingerprint"],
                identity=ExportIdentity.from_dict(raw["identity"]),
            )
        except (KeyError, TypeError, ValueError) as error:
            if isinstance(error, IntakeV2ContractError):
                raise
            raise IntakeV2ContractError("candidate содержит неверные поля") from error


@runtime_checkable
class VirtualExportTree(Protocol):
    """Потоковый read-only снимок, общий для всех транспортов."""

    transport: CandidateTransport
    origin_name: str

    def paths(self) -> tuple[str, ...]: ...

    def open(self, path: str) -> BinaryIO: ...

    def size(self, path: str) -> int: ...

    def fingerprint(self) -> str: ...

    def source_sha256(self) -> str: ...

    def verify_stable(self, expected: str) -> bool: ...


_JOB_TRANSITIONS = MappingProxyType(
    {
        CandidateJobState.ACCEPTED: frozenset(
            {
                CandidateJobState.PROBING,
                CandidateJobState.FAILED,
            }
        ),
        CandidateJobState.PROBING: frozenset(
            {CandidateJobState.READY, CandidateJobState.FAILED}
        ),
        CandidateJobState.READY: frozenset(
            {CandidateJobState.PARSING, CandidateJobState.FAILED}
        ),
        CandidateJobState.PARSING: frozenset(
            {CandidateJobState.DONE, CandidateJobState.FAILED}
        ),
        CandidateJobState.DONE: frozenset(),
        CandidateJobState.FAILED: frozenset(),
    }
)


@dataclass(frozen=True, slots=True)
class CandidateJob:
    """Переживающее рестарт состояние server-side работы над кандидатом."""

    job_id: str
    candidate_id: str
    state: CandidateJobState
    error: str = ""
    result: str = ""

    def __post_init__(self) -> None:
        _safe_identifier(self.job_id, "job_id")
        _safe_identifier(self.candidate_id, "candidate_id")
        if not isinstance(self.state, CandidateJobState):
            raise IntakeV2ContractError("state должен быть CandidateJobState")
        if not isinstance(self.error, str) or not isinstance(self.result, str):
            raise IntakeV2ContractError("error и result должны быть строками")
        if self.state is CandidateJobState.FAILED and not self.error:
            raise IntakeV2ContractError("failed job обязан содержать error")
        if self.state is not CandidateJobState.FAILED and self.error:
            raise IntakeV2ContractError("error допустим только для failed job")

    def transition(
        self,
        state: CandidateJobState,
        *,
        error: str = "",
        result: str = "",
    ) -> CandidateJob:
        if not isinstance(state, CandidateJobState) or state not in _JOB_TRANSITIONS[
            self.state
        ]:
            target = state.value if isinstance(state, CandidateJobState) else repr(state)
            raise IntakeV2ContractError(
                f"недопустимый переход job: {self.state.value} -> {target}"
            )
        return replace(self, state=state, error=error, result=result)

    def to_dict(self) -> dict[str, str]:
        return {
            "job_id": self.job_id,
            "candidate_id": self.candidate_id,
            "state": self.state.value,
            "error": self.error,
            "result": self.result,
        }

    @classmethod
    def from_dict(cls, raw: object) -> CandidateJob:
        if not isinstance(raw, dict):
            raise IntakeV2ContractError("job должен быть объектом")
        try:
            return cls(
                job_id=raw["job_id"],
                candidate_id=raw["candidate_id"],
                state=CandidateJobState(raw["state"]),
                error=raw.get("error", ""),
                result=raw.get("result", ""),
            )
        except (KeyError, TypeError, ValueError) as error:
            if isinstance(error, IntakeV2ContractError):
                raise
            raise IntakeV2ContractError("job содержит неверные поля") from error


class DurableCandidateStore:
    """Атомарное малое хранилище metadata кандидатов и заданий.

    Байты browser-upload живут в отдельном managed staging. Здесь сохраняются
    только проверяемые typed records, поэтому store не дублирует большой ZIP.
    """

    def __init__(self, root: Path):
        self.root = Path(root)
        self.candidates_dir = self.root / "candidates"
        self.jobs_dir = self.root / "jobs"
        self._ensure_directories()

    def _ensure_directories(self) -> None:
        try:
            if self.root.is_symlink():
                raise CandidateStoreError(
                    "корень candidate store не может быть символической ссылкой"
                )
            self.root.mkdir(parents=True, exist_ok=True)
            if self.root.is_symlink() or not self.root.is_dir():
                raise CandidateStoreError(
                    "корень candidate store должен быть обычным каталогом"
                )
            for directory in (self.candidates_dir, self.jobs_dir):
                if directory.is_symlink():
                    raise CandidateStoreError(
                        "каталог candidate store не может быть символической ссылкой"
                    )
                directory.mkdir(exist_ok=True)
                if directory.is_symlink() or not directory.is_dir():
                    raise CandidateStoreError(
                        "каталог candidate store не может быть символической ссылкой"
                    )
        except CandidateStoreError:
            raise
        except OSError as error:
            raise CandidateStoreError("не удалось открыть candidate store") from error

    @staticmethod
    def _path(directory: Path, record_id: str) -> Path:
        _safe_identifier(record_id, "record_id")
        return directory / f"{record_id}.json"

    @staticmethod
    def _encoded(kind: str, payload: dict[str, object]) -> bytes:
        envelope = {
            "format_version": _STORE_FORMAT_VERSION,
            "kind": kind,
            "payload": payload,
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

    @staticmethod
    def _sync_directory(directory: Path) -> None:
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _write(self, path: Path, kind: str, payload: dict[str, object]) -> None:
        temporary: Path | None = None
        try:
            descriptor, raw_path = tempfile.mkstemp(
                dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
            )
            temporary = Path(raw_path)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(self._encoded(kind, payload))
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            temporary = None
            self._sync_directory(path.parent)
        except (OSError, UnicodeError, TypeError, ValueError) as error:
            raise CandidateStoreError("не удалось атомарно записать candidate store") from error
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    def _read(self, path: Path, expected_kind: str) -> object:
        if path.is_symlink():
            raise CandidateStoreError(
                "запись candidate store не может быть символической ссылкой"
            )
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise KeyError(path.stem) from None
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise CandidateStoreError("candidate store повреждён или недоступен") from error
        if (
            not isinstance(raw, dict)
            or raw.get("format_version") != _STORE_FORMAT_VERSION
            or raw.get("kind") != expected_kind
            or not isinstance(raw.get("payload"), dict)
        ):
            raise CandidateStoreError("candidate store содержит несовместимую запись")
        return raw["payload"]

    def save_candidate(self, candidate: ExportCandidate) -> None:
        if not isinstance(candidate, ExportCandidate):
            raise IntakeV2ContractError("candidate должен быть ExportCandidate")
        self._write(
            self._path(self.candidates_dir, candidate.candidate_id),
            "candidate",
            candidate.to_dict(),
        )

    def load_candidate(self, candidate_id: str) -> ExportCandidate:
        payload = self._read(
            self._path(self.candidates_dir, candidate_id), "candidate"
        )
        try:
            return ExportCandidate.from_dict(payload)
        except IntakeV2ContractError as error:
            raise CandidateStoreError("candidate store содержит неверный candidate") from error

    def save_job(self, job: CandidateJob) -> None:
        if not isinstance(job, CandidateJob):
            raise IntakeV2ContractError("job должен быть CandidateJob")
        self._write(
            self._path(self.jobs_dir, job.job_id),
            "job",
            job.to_dict(),
        )

    def load_job(self, job_id: str) -> CandidateJob:
        payload = self._read(self._path(self.jobs_dir, job_id), "job")
        try:
            return CandidateJob.from_dict(payload)
        except IntakeV2ContractError as error:
            raise CandidateStoreError("candidate store содержит неверный job") from error


@dataclass(frozen=True, slots=True)
class MetadataKindSpec:
    """Одна декларация coverage вместо веток по виду во всех потребителях."""

    source_name: str
    canonical_kind: str
    policy: MetadataKindPolicy
    layers: frozenset[LayerKind] = field(default_factory=frozenset)
    layouts: frozenset[str] = field(default_factory=frozenset)
    aliases: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        _required_text(self.source_name, "source_name")
        _required_text(self.canonical_kind, "canonical_kind")
        if not isinstance(self.policy, MetadataKindPolicy):
            raise IntakeV2ContractError("policy должен быть MetadataKindPolicy")
        if not isinstance(self.layers, frozenset) or not all(
            isinstance(layer, LayerKind) for layer in self.layers
        ):
            raise IntakeV2ContractError("layers должен быть frozenset[LayerKind]")
        if not isinstance(self.layouts, frozenset) or not all(
            isinstance(layout, str) and layout for layout in self.layouts
        ):
            raise IntakeV2ContractError("layouts должен быть frozenset[str]")
        if not isinstance(self.aliases, frozenset) or not all(
            isinstance(alias, str) and alias and alias.strip() == alias
            for alias in self.aliases
        ):
            raise IntakeV2ContractError("aliases должен быть frozenset[str]")
        if self.source_name in self.aliases:
            raise IntakeV2ContractError("aliases не должен повторять source_name")
        if self.policy is not MetadataKindPolicy.SUPPORTED and (
            self.layers or self.layouts
        ):
            raise IntakeV2ContractError(
                "deferred/ignored вид не объявляет поддержанные слои или layouts"
            )
        if self.policy is MetadataKindPolicy.SUPPORTED and not self.layers:
            raise IntakeV2ContractError("supported вид обязан объявить слои")

    def supports(self, layer: LayerKind) -> bool:
        return self.policy is MetadataKindPolicy.SUPPORTED and layer in self.layers


@dataclass(frozen=True, slots=True)
class LayerManifest:
    """Проверяемая ссылка manifest на один канонический слой."""

    kind: LayerKind
    state: LayerState
    content_sha256: str = ""
    relative_path: str = ""
    items_total: int = 0
    error: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.kind, LayerKind):
            raise IntakeV2ContractError("kind должен быть LayerKind")
        if not isinstance(self.state, LayerState):
            raise IntakeV2ContractError("state должен быть LayerState")
        _nonnegative_int(self.items_total, "items_total")
        if not isinstance(self.error, str):
            raise IntakeV2ContractError("error должен быть строкой")
        if self.state is LayerState.READY:
            _sha256(self.content_sha256, "content_sha256")
            _relative_manifest_path(self.relative_path)
            if self.error:
                raise IntakeV2ContractError("ready layer не должен содержать error")
            return
        _sha256(self.content_sha256, "content_sha256", allow_empty=True)
        _relative_manifest_path(self.relative_path, allow_empty=True)
        if self.content_sha256 or self.relative_path or self.items_total:
            raise IntakeV2ContractError(
                "error/unavailable layer не должен ссылаться на старый payload"
            )
        if self.state is LayerState.ERROR and not self.error:
            raise IntakeV2ContractError("error layer обязан содержать error")
        if self.state is LayerState.UNAVAILABLE and self.error:
            raise IntakeV2ContractError("unavailable layer не должен содержать error")

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind.value,
            "state": self.state.value,
            "content_sha256": self.content_sha256,
            "relative_path": self.relative_path,
            "items_total": self.items_total,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, raw: object) -> LayerManifest:
        if not isinstance(raw, dict):
            raise IntakeV2ContractError("layer должен быть объектом")
        try:
            return cls(
                kind=LayerKind(raw["kind"]),
                state=LayerState(raw["state"]),
                content_sha256=raw.get("content_sha256", ""),
                relative_path=raw.get("relative_path", ""),
                items_total=raw.get("items_total", 0),
                error=raw.get("error", ""),
            )
        except (KeyError, TypeError, ValueError) as error:
            if isinstance(error, IntakeV2ContractError):
                raise
            raise IntakeV2ContractError("layer содержит неверные поля") from error


@dataclass(frozen=True, slots=True)
class GenerationManifest:
    """Канонический manifest активного поколения без дублирования payload."""

    format_version: int
    generation_id: str
    identity: ExportIdentity
    parser_version: int
    selection_version: int
    source_transport: CandidateTransport
    origin_name: str
    raw_sha256: str
    layers: tuple[LayerManifest, ...]

    def __post_init__(self) -> None:
        _positive_int(self.format_version, "format_version")
        _safe_identifier(self.generation_id, "generation_id")
        if not isinstance(self.identity, ExportIdentity):
            raise IntakeV2ContractError("identity должен быть ExportIdentity")
        _positive_int(self.parser_version, "parser_version")
        _positive_int(self.selection_version, "selection_version")
        if not isinstance(self.source_transport, CandidateTransport):
            raise IntakeV2ContractError(
                "source_transport должен быть CandidateTransport"
            )
        _required_text(self.origin_name, "origin_name")
        _sha256(self.raw_sha256, "raw_sha256")
        if not isinstance(self.layers, tuple) or not all(
            isinstance(layer, LayerManifest) for layer in self.layers
        ):
            raise IntakeV2ContractError("layers должен быть tuple[LayerManifest, ...]")
        kinds = tuple(layer.kind for layer in self.layers)
        if len(kinds) != len(set(kinds)):
            raise IntakeV2ContractError("generation manifest дублирует слой")
        object.__setattr__(
            self,
            "layers",
            tuple(sorted(self.layers, key=lambda layer: layer.kind.value)),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "format_version": self.format_version,
            "generation_id": self.generation_id,
            "identity": self.identity.to_dict(),
            "parser_version": self.parser_version,
            "selection_version": self.selection_version,
            "source_transport": self.source_transport.value,
            "origin_name": self.origin_name,
            "raw_sha256": self.raw_sha256,
            "layers": [layer.to_dict() for layer in self.layers],
        }

    def to_json_bytes(self) -> bytes:
        return (
            json.dumps(
                self.to_dict(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.to_json_bytes()).hexdigest()

    @classmethod
    def from_json_bytes(cls, raw: bytes) -> GenerationManifest:
        if not isinstance(raw, bytes):
            raise IntakeV2ContractError("manifest должен быть bytes")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as error:
            raise IntakeV2ContractError("manifest не является корректным JSON") from error
        if not isinstance(payload, dict):
            raise IntakeV2ContractError("manifest должен быть JSON-объектом")
        try:
            layers = payload["layers"]
            if not isinstance(layers, list):
                raise IntakeV2ContractError("manifest.layers должен быть массивом")
            return cls(
                format_version=payload["format_version"],
                generation_id=payload["generation_id"],
                identity=ExportIdentity.from_dict(payload["identity"]),
                parser_version=payload["parser_version"],
                selection_version=payload["selection_version"],
                source_transport=CandidateTransport(payload["source_transport"]),
                origin_name=payload["origin_name"],
                raw_sha256=payload["raw_sha256"],
                layers=tuple(LayerManifest.from_dict(layer) for layer in layers),
            )
        except (KeyError, TypeError, ValueError) as error:
            if isinstance(error, IntakeV2ContractError):
                raise
            raise IntakeV2ContractError("manifest содержит неверные поля") from error


@dataclass(frozen=True, slots=True)
class RecoveryRecord:
    """Малый journal между готовым staging и завершённой публикацией."""

    previous_generation: str | None
    staged_generation: str
    phase: RecoveryPhase

    def __post_init__(self) -> None:
        if self.previous_generation is not None:
            _safe_identifier(self.previous_generation, "previous_generation")
        _safe_identifier(self.staged_generation, "staged_generation")
        if self.previous_generation == self.staged_generation:
            raise IntakeV2ContractError("recovery требует два разных поколения")
        if not isinstance(self.phase, RecoveryPhase):
            raise IntakeV2ContractError("phase должен быть RecoveryPhase")


def decide_recovery(
    active_generation: str | None, record: RecoveryRecord
) -> RecoveryAction:
    """Выбрать только доказуемое старое/новое состояние, иначе fail-closed."""
    if active_generation is not None:
        _safe_identifier(active_generation, "active_generation")
    if not isinstance(record, RecoveryRecord):
        raise IntakeV2ContractError("record должен быть RecoveryRecord")
    if active_generation == record.staged_generation:
        return RecoveryAction.FINALIZE_NEW
    if (
        active_generation == record.previous_generation
        and record.phase is RecoveryPhase.PREPARED
    ):
        return RecoveryAction.ROLLBACK_STAGING
    return RecoveryAction.BLOCK


__all__ = [
    "CandidateJob",
    "CandidateJobState",
    "CandidateStoreError",
    "CandidateTransport",
    "DurableCandidateStore",
    "ExportCandidate",
    "ExportIdentity",
    "GenerationManifest",
    "IntakeV2ContractError",
    "LayerKind",
    "LayerManifest",
    "LayerState",
    "MetadataKindPolicy",
    "MetadataKindSpec",
    "RecoveryAction",
    "RecoveryPhase",
    "RecoveryRecord",
    "SourceKind",
    "VirtualExportTree",
    "decide_recovery",
]
