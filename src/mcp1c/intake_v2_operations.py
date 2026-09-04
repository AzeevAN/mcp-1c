"""Durable backend построения preview единого intake.

Модуль связывает уже независимые probe, collector, converter, materializer и
planner. Он не знает HTTP/UI и принципиально не публикует Registry: готовый
preview остаётся проверяемым кандидатом до отдельного подтверждения.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import tempfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Mapping

from .intake_v2 import (
    CandidateJob,
    CandidateJobStage,
    CandidateJobState,
    DurableCandidateStore,
    ExportCandidate,
    ExportIdentity,
    GenerationManifest,
    LayerKind,
    LayerState,
    SourceKind,
    VirtualExportTree,
)
from .intake_v2_collector import collect_source_b
from .intake_v2_composition import compose_generation, compose_manifest
from .intake_v2_converter import convert_collection
from .intake_v2_generation import MaterializedGeneration, materialize_generation
from .intake_v2_planner import IntakeAction, IntakePlan, PlannerError, plan_intake
from .intake_v2_probe import CandidateProbe, probe_export
from .intake_v2_registry import (
    GenerationConflictError,
    GenerationLayerView,
    GenerationOrigin,
    GenerationPointer,
    GenerationView,
    LayerMemberSource,
    LayerPayloadSource,
    hash_layer_payload,
    hash_layer_semantic,
    load_layer_payload,
    native_generation_view,
)


_PREVIEW_FORMAT_VERSION = 1
_MAX_PREVIEW_RECORD_BYTES = 64 << 20
_MAX_REQUEST_RECORD_BYTES = 4 << 20
_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9._-]+\Z")


class OperationError(RuntimeError):
    """Операция intake не может продолжиться без недоказанного состояния."""


class OperationConflict(OperationError):
    """Active generation изменился после построения подтверждаемого preview."""


class OperationStalePreview(OperationError):
    """Durable preview создан прежним, более слабым контрактом planner."""


@dataclass(frozen=True, slots=True)
class _OperationRequest:
    job_id: str
    candidate_id: str
    action: IntakeAction
    active: GenerationView | None
    generation_id: str

    def __post_init__(self) -> None:
        for value, label in (
            (self.job_id, "job_id"),
            (self.candidate_id, "candidate_id"),
            (self.generation_id, "generation_id"),
        ):
            if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value):
                raise OperationError(f"{label} операции имеет недопустимый формат")
        if not isinstance(self.action, IntakeAction):
            raise OperationError("action операции должен быть IntakeAction")
        if self.active is not None and not isinstance(self.active, GenerationView):
            raise OperationError("active операции должен быть GenerationView или None")

    def to_dict(self) -> dict[str, object]:
        return {
            "job_id": self.job_id,
            "candidate_id": self.candidate_id,
            "action": self.action.value,
            "active": _active_to_dict(self.active),
            "generation_id": self.generation_id,
        }

    @classmethod
    def from_dict(cls, raw: object) -> _OperationRequest:
        if not isinstance(raw, dict):
            raise OperationError("request операции должен быть объектом")
        try:
            return cls(
                job_id=raw["job_id"],
                candidate_id=raw["candidate_id"],
                action=IntakeAction(raw["action"]),
                active=_active_from_dict(raw["active"]),
                generation_id=raw["generation_id"],
            )
        except (KeyError, TypeError, ValueError) as error:
            if isinstance(error, OperationError):
                raise
            raise OperationError("request операции содержит неверные поля") from error


@dataclass(frozen=True, slots=True)
class IntakeCommitResult:
    """Durable-результат no-op либо атомарной публикации preview."""

    job_id: str
    candidate_id: str
    no_op: bool
    pointer: GenerationPointer
    applied_layers: frozenset[LayerKind]

    def __post_init__(self) -> None:
        for value, label in (
            (self.job_id, "job_id"),
            (self.candidate_id, "candidate_id"),
        ):
            if not isinstance(value, str) or not _IDENTIFIER_RE.fullmatch(value):
                raise OperationError(f"{label} commit result имеет недопустимый формат")
        if not isinstance(self.no_op, bool):
            raise OperationError("no_op commit result должен быть bool")
        if not isinstance(self.pointer, GenerationPointer):
            raise OperationError("pointer commit result имеет неверный тип")
        if not isinstance(self.applied_layers, frozenset) or not all(
            isinstance(kind, LayerKind) for kind in self.applied_layers
        ):
            raise OperationError("applied_layers commit result имеет неверный тип")
        if self.no_op != (not self.applied_layers):
            raise OperationError("no_op commit result не совпадает с applied_layers")

    def to_dict(self) -> dict[str, object]:
        return {
            "job_id": self.job_id,
            "candidate_id": self.candidate_id,
            "no_op": self.no_op,
            "pointer": self.pointer.to_dict(),
            "applied_layers": sorted(kind.value for kind in self.applied_layers),
        }

    @classmethod
    def from_dict(cls, raw: object) -> IntakeCommitResult:
        if not isinstance(raw, dict):
            raise OperationError("commit result должен быть объектом")
        try:
            if set(raw) != {
                "job_id",
                "candidate_id",
                "no_op",
                "pointer",
                "applied_layers",
            }:
                raise OperationError("commit result содержит неизвестные поля")
            layers = raw["applied_layers"]
            if not isinstance(layers, list) or len(layers) != len(set(layers)):
                raise OperationError("applied_layers commit result должен быть массивом")
            return cls(
                job_id=raw["job_id"],
                candidate_id=raw["candidate_id"],
                no_op=raw["no_op"],
                pointer=GenerationPointer.from_dict(raw["pointer"]),
                applied_layers=frozenset(LayerKind(kind) for kind in layers),
            )
        except (KeyError, TypeError, ValueError) as error:
            if isinstance(error, OperationError):
                raise
            raise OperationError("commit result содержит неверные поля") from error


def _sync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _ensure_directory(path: Path, label: str) -> None:
    try:
        if path.is_symlink():
            raise OperationError(f"{label} не может быть символической ссылкой")
        path.mkdir(parents=True, exist_ok=True)
        if path.is_symlink() or not path.is_dir():
            raise OperationError(f"{label} должен быть обычным каталогом")
    except OperationError:
        raise
    except OSError as error:
        raise OperationError(f"не удалось открыть {label}") from error


def _safe_relative(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise OperationError(f"{label} должен быть относительным POSIX-путём")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.as_posix() != value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise OperationError(f"{label} должен быть безопасным относительным путём")
    return value


def _relative(root: Path, path: Path, label: str) -> str:
    try:
        value = path.relative_to(root).as_posix()
    except ValueError as error:
        raise OperationError(f"{label} вышел за пределы managed operation") from error
    return _safe_relative(value, label)


def _managed_path(root: Path, relative: object, label: str) -> Path:
    value = _safe_relative(relative, label)
    current = root
    for part in PurePosixPath(value).parts:
        current = current / part
        try:
            if current.is_symlink():
                raise OperationError(f"{label} проходит через символическую ссылку")
        except OSError as error:
            raise OperationError(f"{label} недоступен") from error
    return current


def _manifest_from_dict(raw: object) -> GenerationManifest:
    if not isinstance(raw, dict):
        raise OperationError("preview manifest должен быть объектом")
    try:
        encoded = (
            json.dumps(
                raw,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
        return GenerationManifest.from_json_bytes(encoded)
    except (TypeError, ValueError) as error:
        raise OperationError("preview manifest содержит неверные поля") from error


def _layer_view_to_dict(layer: GenerationLayerView) -> dict[str, object]:
    return {
        "state": layer.state.value,
        "content_sha256": layer.content_sha256,
        "payload_sha256": layer.payload_sha256,
        "source_sha256": layer.source_sha256,
        "items_total": layer.items_total,
        "error": layer.error,
    }


def _active_to_dict(active: GenerationView | None) -> object:
    if active is None:
        return None
    if active.origin is GenerationOrigin.NATIVE:
        if active.manifest is None:
            raise OperationError("native active не содержит manifest")
        return {
            "origin": active.origin.value,
            "manifest": active.manifest.to_dict(),
        }
    if active.manifest is not None:
        raise OperationError("legacy active не должен содержать manifest")
    return {
        "origin": active.origin.value,
        "identity": active.identity.to_dict(),
        "layers": {
            kind.value: _layer_view_to_dict(layer)
            for kind, layer in sorted(active.layers.items(), key=lambda item: item[0].value)
        },
    }


def _active_from_dict(raw: object) -> GenerationView | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise OperationError("active preview должен быть объектом или null")
    try:
        origin = GenerationOrigin(raw["origin"])
        if origin is GenerationOrigin.NATIVE:
            return native_generation_view(_manifest_from_dict(raw["manifest"]))
        layers_raw = raw["layers"]
        if not isinstance(layers_raw, dict):
            raise OperationError("legacy active.layers должен быть объектом")
        layers = {
            LayerKind(kind): GenerationLayerView(
                state=LayerState(value["state"]),
                content_sha256=value.get("content_sha256", ""),
                payload_sha256=value.get("payload_sha256", ""),
                source_sha256=value.get("source_sha256", ""),
                items_total=value.get("items_total", 0),
                error=value.get("error", ""),
            )
            for kind, value in layers_raw.items()
            if isinstance(value, dict)
        }
        if len(layers) != len(layers_raw):
            raise OperationError("legacy active.layers содержит неверный слой")
        return GenerationView(
            origin=origin,
            identity=ExportIdentity.from_dict(raw["identity"]),
            manifest=None,
            layers=layers,
        )
    except (KeyError, TypeError, ValueError) as error:
        if isinstance(error, OperationError):
            raise
        raise OperationError("active preview содержит неверные поля") from error


def _payloads_to_dict(
    operation_root: Path,
    materialized: MaterializedGeneration,
) -> dict[str, object]:
    result: dict[str, object] = {}
    for kind, source in sorted(
        materialized.payloads.items(), key=lambda item: item[0].value
    ):
        result[kind.value] = {
            "manifest_path": _relative(
                operation_root,
                source.manifest_path,
                f"{kind.value}.manifest_path",
            ),
            "members": [
                {
                    "key": member.member.key,
                    "source_path": _relative(
                        operation_root,
                        member.source_path,
                        f"{kind.value}.source_path",
                    ),
                }
                for member in source.members
            ],
        }
    return result


def _payloads_from_dict(
    operation_root: Path,
    manifest: GenerationManifest,
    raw: object,
) -> Mapping[LayerKind, LayerPayloadSource]:
    if not isinstance(raw, dict):
        raise OperationError("preview payloads должен быть объектом")
    ready = {
        layer.kind: layer
        for layer in manifest.layers
        if layer.state is LayerState.READY
    }
    try:
        records = {LayerKind(kind): value for kind, value in raw.items()}
    except ValueError as error:
        raise OperationError("preview payloads содержит неизвестный слой") from error
    if set(records) != set(ready):
        raise OperationError("preview payloads не совпадает с ready-слоями")

    result: dict[LayerKind, LayerPayloadSource] = {}
    for kind, layer in sorted(ready.items(), key=lambda item: item[0].value):
        record = records[kind]
        if not isinstance(record, dict) or not isinstance(record.get("members"), list):
            raise OperationError(f"{kind.value}: запись payload повреждена")
        manifest_path = _managed_path(
            operation_root,
            record.get("manifest_path"),
            f"{kind.value}.manifest_path",
        )
        try:
            info = manifest_path.lstat()
        except OSError as error:
            raise OperationError(f"{kind.value}: layer manifest недоступен") from error
        if not stat.S_ISREG(info.st_mode):
            raise OperationError(f"{kind.value}: layer manifest не является файлом")
        payload = load_layer_payload(manifest_path)
        if (
            payload.kind is not kind
            or hash_layer_payload(kind, manifest_path) != layer.payload_sha256
            or hash_layer_semantic(kind, payload.semantic) != layer.content_sha256
        ):
            raise OperationError(f"{kind.value}: layer manifest не совпадает с preview")
        expected = {member.key: member for member in payload.members}
        member_sources: dict[str, LayerMemberSource] = {}
        for item in record["members"]:
            if not isinstance(item, dict) or not isinstance(item.get("key"), str):
                raise OperationError(f"{kind.value}: member source повреждён")
            key = item["key"]
            if key in member_sources or key not in expected:
                raise OperationError(f"{kind.value}: member source не совпадает с envelope")
            source_path = _managed_path(
                operation_root,
                item.get("source_path"),
                f"{kind.value}.source_path",
            )
            try:
                source_info = source_path.lstat()
            except OSError as error:
                raise OperationError(f"{kind.value}: member source недоступен") from error
            if (
                not stat.S_ISREG(source_info.st_mode)
                or source_info.st_size != expected[key].size
            ):
                raise OperationError(f"{kind.value}: member source повреждён")
            member_sources[key] = LayerMemberSource(expected[key], source_path)
        if set(member_sources) != set(expected):
            raise OperationError(f"{kind.value}: не все member sources сохранены")
        result[kind] = LayerPayloadSource(
            manifest_path,
            tuple(member_sources[key] for key in sorted(member_sources)),
        )
    return MappingProxyType(result)


@dataclass(frozen=True, slots=True)
class IntakePreview:
    """Неизменяемый semantic diff и его candidate payload до commit."""

    job_id: str
    candidate_id: str
    action: IntakeAction
    active: GenerationView | None
    materialized: MaterializedGeneration
    plan: IntakePlan = field(init=False)

    def __post_init__(self) -> None:
        if not isinstance(self.job_id, str) or not self.job_id:
            raise OperationError("job_id preview должен быть непустым")
        if not isinstance(self.candidate_id, str) or not self.candidate_id:
            raise OperationError("candidate_id preview должен быть непустым")
        if not isinstance(self.action, IntakeAction):
            raise OperationError("action preview должен быть IntakeAction")
        if self.active is not None and not isinstance(self.active, GenerationView):
            raise OperationError("active preview должен быть GenerationView или None")
        if not isinstance(self.materialized, MaterializedGeneration):
            raise OperationError("materialized preview имеет неверный тип")
        object.__setattr__(
            self,
            "plan",
            plan_intake(
                self.action,
                self.materialized.manifest,
                active=self.active,
            ),
        )

    @property
    def expected_previous(self) -> GenerationPointer | None:
        if self.active is None or self.active.manifest is None:
            return None
        return GenerationPointer.for_manifest(self.active.manifest)


class IntakeCoordinator:
    """Последовательно строит и восстанавливает preview одной операции."""

    def __init__(self, root: str | Path, records: DurableCandidateStore):
        if not isinstance(records, DurableCandidateStore):
            raise TypeError("records должен быть DurableCandidateStore")
        self.root = Path(root)
        self.work_dir = self.root / "work"
        self.requests_dir = self.root / "requests"
        self.previews_dir = self.root / "previews"
        self.commits_dir = self.root / "commits"
        self.records = records
        _ensure_directory(self.root, "operation store")
        _ensure_directory(self.work_dir, "operation work")
        _ensure_directory(self.requests_dir, "operation requests")
        _ensure_directory(self.previews_dir, "operation previews")
        _ensure_directory(self.commits_dir, "operation commits")
        self._cleanup_temporary_records()

    def _cleanup_temporary_records(self) -> None:
        try:
            for directory in (
                self.requests_dir,
                self.previews_dir,
                self.commits_dir,
            ):
                changed = False
                for path in directory.iterdir():
                    if path.is_symlink():
                        raise OperationError(
                            "operation store содержит символическую ссылку"
                        )
                    if (
                        path.is_file()
                        and path.name.startswith(".")
                        and path.name.endswith(".tmp")
                    ):
                        path.unlink()
                        changed = True
                if changed:
                    _sync_directory(directory)
        except OperationError:
            raise
        except OSError as error:
            raise OperationError("не удалось восстановить operation previews") from error

    def _work_root(self, job_id: str) -> Path:
        self.records.load_job(job_id)
        return self.work_dir / job_id

    def _save_job(self, job: CandidateJob) -> CandidateJob:
        self.records.save_job(job)
        return job

    @staticmethod
    def _encoded_record(kind: str, payload: dict[str, object]) -> bytes:
        return (
            json.dumps(
                {
                    "format_version": _PREVIEW_FORMAT_VERSION,
                    "kind": kind,
                    "payload": payload,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")

    def _write_record(
        self,
        directory: Path,
        record_id: str,
        kind: str,
        payload: dict[str, object],
        *,
        limit: int,
        label: str,
    ) -> None:
        encoded = self._encoded_record(kind, payload)
        if len(encoded) > limit:
            raise OperationError(f"{label} превышает допустимый размер")
        target = directory / f"{record_id}.json"
        temporary: Path | None = None
        try:
            descriptor, raw_path = tempfile.mkstemp(
                dir=directory,
                prefix=f".{record_id}.",
                suffix=".tmp",
            )
            temporary = Path(raw_path)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            temporary = None
            _sync_directory(directory)
        except OSError as error:
            raise OperationError(f"не удалось атомарно сохранить {label}") from error
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass

    @staticmethod
    def _read_record(
        directory: Path,
        record_id: str,
        kind: str,
        *,
        limit: int,
        label: str,
    ) -> dict[str, object]:
        path = directory / f"{record_id}.json"
        try:
            info = path.lstat()
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise OperationError(f"{label} должен быть обычным файлом")
            if info.st_size > limit:
                raise OperationError(f"{label} превышает допустимый размер")
            raw = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            raise KeyError(record_id) from None
        except OperationError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise OperationError(f"{label} повреждён или недоступен") from error
        if (
            not isinstance(raw, dict)
            or raw.get("format_version") != _PREVIEW_FORMAT_VERSION
            or raw.get("kind") != kind
            or not isinstance(raw.get("payload"), dict)
        ):
            raise OperationError(f"{label} имеет несовместимый формат")
        return raw["payload"]

    def _save_request(self, request: _OperationRequest) -> None:
        self._write_record(
            self.requests_dir,
            request.job_id,
            "intake-request",
            request.to_dict(),
            limit=_MAX_REQUEST_RECORD_BYTES,
            label="request операции",
        )

    def _load_request(self, job_id: str) -> _OperationRequest:
        payload = self._read_record(
            self.requests_dir,
            job_id,
            "intake-request",
            limit=_MAX_REQUEST_RECORD_BYTES,
            label="request операции",
        )
        request = _OperationRequest.from_dict(payload)
        if request.job_id != job_id:
            raise OperationError("request операции относится к другой job")
        return request

    def _bind_request(
        self,
        job: CandidateJob,
        action: IntakeAction,
        active: GenerationView | None,
        generation_id: str,
    ) -> _OperationRequest:
        supplied = _OperationRequest(
            job.job_id,
            job.candidate_id,
            action,
            active,
            generation_id,
        )
        try:
            stored = self._load_request(job.job_id)
        except KeyError:
            if job.state is not CandidateJobState.READY:
                raise OperationError("для parsing job отсутствует durable request")
            self._save_request(supplied)
            return supplied
        if stored != supplied:
            raise OperationError("параметры операции изменились после её начала")
        return stored

    def _save_commit(self, result: IntakeCommitResult) -> None:
        self._write_record(
            self.commits_dir,
            result.job_id,
            "intake-commit",
            result.to_dict(),
            limit=_MAX_REQUEST_RECORD_BYTES,
            label="commit result",
        )

    def load_commit(self, job_id: str) -> IntakeCommitResult:
        job = self.records.load_job(job_id)
        if job.state is not CandidateJobState.DONE or job.result != job_id:
            raise OperationError("job не готова к commit result")
        payload = self._read_record(
            self.commits_dir,
            job_id,
            "intake-commit",
            limit=_MAX_REQUEST_RECORD_BYTES,
            label="commit result",
        )
        result = IntakeCommitResult.from_dict(payload)
        if result.job_id != job.job_id or result.candidate_id != job.candidate_id:
            raise OperationError("commit result относится к другой job")
        candidate = self.records.load_candidate(job.candidate_id)
        if result.pointer.identity != candidate.identity:
            raise OperationError("commit result относится к другой identity")
        return result

    def create_job(self, job_id: str, candidate_id: str) -> CandidateJob:
        try:
            self.records.load_job(job_id)
        except KeyError:
            pass
        else:
            raise OperationError("job_id уже существует")
        job = CandidateJob(job_id, candidate_id, CandidateJobState.ACCEPTED)
        return self._save_job(job)

    @staticmethod
    def _candidate(probe: CandidateProbe, candidate_id: str, parent: str) -> ExportCandidate:
        bound = probe.bind(candidate_id, parent_configuration=parent)
        return ExportCandidate(
            candidate_id=bound.candidate_id,
            transport=probe.transport,
            origin_name=probe.origin_name,
            raw_sha256=probe.raw_sha256,
            snapshot_fingerprint=probe.snapshot_fingerprint,
            identity=bound.identity,
        )

    @staticmethod
    def _parent(candidate: ExportCandidate) -> str:
        return (
            candidate.identity.parent_configuration
            if candidate.identity.source_kind is SourceKind.EXTENSION
            else ""
        )

    @staticmethod
    def _error_text(error: Exception) -> str:
        text = str(error).strip() or error.__class__.__name__
        return text[:2048]

    def probe(
        self,
        job_id: str,
        tree: VirtualExportTree,
        *,
        parent_configuration: str = "",
        expected_probe: CandidateProbe | None = None,
    ) -> ExportCandidate:
        job = self.records.load_job(job_id)
        if job.state is CandidateJobState.ACCEPTED:
            job = self._save_job(job.transition(CandidateJobState.PROBING))
        elif job.state is not CandidateJobState.PROBING:
            raise OperationError("probe допустим только для accepted/probing job")
        try:
            current_probe = probe_export(tree)
            if expected_probe is not None and current_probe != expected_probe:
                raise OperationError("candidate изменился после on-demand refresh")
            candidate = self._candidate(
                current_probe,
                job.candidate_id,
                parent_configuration,
            )
            try:
                previous = self.records.load_candidate(job.candidate_id)
            except KeyError:
                previous = None
            if previous is not None and previous != candidate:
                raise OperationError("candidate изменился после сохранённого probe")
            self.records.save_candidate(candidate)
            self._save_job(job.transition(CandidateJobState.READY))
            return candidate
        except Exception as error:
            message = self._error_text(error)
            self._save_job(job.transition(CandidateJobState.FAILED, error=message))
            if isinstance(error, OperationError):
                raise
            raise OperationError(message) from error

    def _discard_preview(self, job_id: str) -> None:
        path = self.previews_dir / f"{job_id}.json"
        if path.is_symlink():
            raise OperationError("preview record не может быть символической ссылкой")
        try:
            if path.exists():
                path.unlink()
                _sync_directory(self.previews_dir)
        except OSError as error:
            raise OperationError("не удалось удалить partial preview") from error

    @staticmethod
    def _discard_record(directory: Path, job_id: str, label: str) -> None:
        if not _IDENTIFIER_RE.fullmatch(job_id):
            raise OperationError("job_id операции имеет недопустимый формат")
        path = directory / f"{job_id}.json"
        try:
            info = path.lstat()
        except FileNotFoundError:
            return
        except OSError as error:
            raise OperationError(f"не удалось проверить {label}") from error
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            raise OperationError(f"{label} должен быть обычным файлом")
        try:
            path.unlink()
            _sync_directory(directory)
        except OSError as error:
            raise OperationError(f"не удалось удалить {label}") from error

    def _discard_work(self, job_id: str) -> None:
        if not _IDENTIFIER_RE.fullmatch(job_id):
            raise OperationError("job_id операции имеет недопустимый формат")
        root = self.work_dir / job_id
        try:
            info = root.lstat()
        except FileNotFoundError:
            return
        except OSError as error:
            raise OperationError("не удалось проверить operation work") from error
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise OperationError("operation work должен быть обычным каталогом")
        try:
            shutil.rmtree(root)
            _sync_directory(self.work_dir)
        except OSError as error:
            raise OperationError("не удалось удалить operation work") from error

    def _discard_payload(
        self,
        job_id: str,
        *,
        keep_commit: bool,
        keep_job: bool,
    ) -> None:
        """Удалить производные данные job, оставляя durable retry при commit."""
        self.records.load_job(job_id)
        self._discard_work(job_id)
        self._discard_record(
            self.requests_dir, job_id, "request операции"
        )
        self._discard_record(
            self.previews_dir, job_id, "preview record"
        )
        if not keep_commit:
            self._discard_record(
                self.commits_dir, job_id, "commit result"
            )
        if not keep_job:
            self.records.remove_job(job_id)

    def discard(self, job_id: str) -> None:
        """Отменить неопубликованный preview и удалить всю его job."""
        job = self.records.load_job(job_id)
        if job.state is not CandidateJobState.DONE:
            raise OperationConflict(
                "Отменить можно только готовый неопубликованный preview."
            )
        try:
            self.load_commit(job_id)
        except KeyError:
            pass
        else:
            raise OperationConflict("Опубликованную job отменить нельзя.")
        self._discard_payload(job_id, keep_commit=False, keep_job=False)

    def remove_job(self, job_id: str) -> None:
        """Каскадно удалить terminal job при снятии её конфигурации."""
        job = self.records.load_job(job_id)
        if job.state not in {CandidateJobState.DONE, CandidateJobState.FAILED}:
            raise OperationConflict(
                "Конфигурацию нельзя удалить во время операции intake."
            )
        self._discard_payload(job_id, keep_commit=False, keep_job=False)

    def _reset_work(self, job_id: str) -> Path:
        root = self.work_dir / job_id
        if root.is_symlink():
            raise OperationError("operation work не может быть символической ссылкой")
        try:
            if root.exists():
                shutil.rmtree(root)
                _sync_directory(self.work_dir)
            root.mkdir()
            _sync_directory(self.work_dir)
        except OSError as error:
            raise OperationError("не удалось подготовить operation work") from error
        return root

    def _write_preview(self, preview: IntakePreview, operation_root: Path) -> None:
        self._write_record(
            self.previews_dir,
            preview.job_id,
            "intake-preview",
            {
                "job_id": preview.job_id,
                "candidate_id": preview.candidate_id,
                "materialized_root": _relative(
                    operation_root,
                    preview.materialized.root,
                    "materialized_root",
                ),
                "manifest": preview.materialized.manifest.to_dict(),
                "payloads": _payloads_to_dict(operation_root, preview.materialized),
            },
            limit=_MAX_PREVIEW_RECORD_BYTES,
            label="preview record",
        )

    def prepare(
        self,
        job_id: str,
        tree: VirtualExportTree,
        *,
        action: IntakeAction,
        active: GenerationView | None,
        generation_id: str,
    ) -> IntakePreview:
        if not isinstance(action, IntakeAction):
            raise TypeError("action должен быть IntakeAction")
        if active is not None and not isinstance(active, GenerationView):
            raise TypeError("active должен быть GenerationView или None")
        job = self.records.load_job(job_id)
        if job.state not in {CandidateJobState.READY, CandidateJobState.PARSING}:
            raise OperationError("prepare допустим только для ready/parsing job")
        request = self._bind_request(job, action, active, generation_id)
        if job.state is CandidateJobState.READY:
            job = self._save_job(job.transition(CandidateJobState.PARSING))
        else:
            job = self._save_job(job.restart_parsing())
        try:
            self._discard_preview(job_id)
            operation_root = self._reset_work(job_id)
            candidate = self.records.load_candidate(job.candidate_id)
            current_probe = probe_export(tree)
            current = self._candidate(
                current_probe,
                job.candidate_id,
                self._parent(candidate),
            )
            if current != candidate:
                raise OperationError("virtual tree изменился после durable probe")

            collection = collect_source_b(
                tree,
                current_probe,
                operation_root / "collection",
            )
            job = self._save_job(job.checkpoint(CandidateJobStage.CONVERTING))
            conversion = convert_collection(collection)
            job = self._save_job(job.checkpoint(CandidateJobStage.MATERIALIZING))
            materialized = materialize_generation(
                collection,
                conversion,
                operation_root / "generation",
                generation_id=request.generation_id,
                parent_configuration=self._parent(candidate),
            )
            job = self._save_job(job.checkpoint(CandidateJobStage.PLANNING))
            preview = IntakePreview(
                job_id=job.job_id,
                candidate_id=job.candidate_id,
                action=request.action,
                active=request.active,
                materialized=materialized,
            )
            self._write_preview(preview, operation_root)
            self._save_job(
                job.transition(CandidateJobState.DONE, result=preview.job_id)
            )
            return preview
        except Exception as error:
            message = self._error_text(error)
            cleanup_error = ""
            try:
                self._discard_preview(job_id)
                work = self.work_dir / job_id
                if work.is_symlink():
                    raise OperationError(
                        "operation work не может быть символической ссылкой"
                    )
                if work.exists():
                    shutil.rmtree(work)
                    _sync_directory(self.work_dir)
            except Exception as cleanup:
                cleanup_error = self._error_text(cleanup)
            finally:
                if cleanup_error:
                    message = f"{message}; cleanup: {cleanup_error}"[:2048]
                self._save_job(
                    job.transition(CandidateJobState.FAILED, error=message)
                )
            if isinstance(error, OperationError) and not cleanup_error:
                raise
            raise OperationError(message) from error

    def resume(
        self,
        job_id: str,
        tree: VirtualExportTree,
        *,
        expected_action: IntakeAction,
    ) -> IntakePreview:
        """Возобновить parsing только с его durable request."""
        request = self._load_request(job_id)
        if request.action is not expected_action:
            raise OperationError("action не совпадает с durable request")
        return self.prepare(
            job_id,
            tree,
            action=request.action,
            active=request.active,
            generation_id=request.generation_id,
        )

    def fail(self, job_id: str, error: Exception) -> CandidateJob:
        """Зафиксировать отказ до входа в ``prepare`` и убрать partial work."""
        job = self.records.load_job(job_id)
        if job.state is CandidateJobState.FAILED:
            return job
        if job.state is CandidateJobState.DONE:
            raise OperationError("готовую job нельзя перевести в failed")
        message = self._error_text(error)
        self._discard_preview(job_id)
        work = self.work_dir / job_id
        if work.is_symlink():
            raise OperationError("operation work не может быть символической ссылкой")
        try:
            if work.exists():
                shutil.rmtree(work)
                _sync_directory(self.work_dir)
        except OSError as cleanup_error:
            message = (
                f"{message}; cleanup: {self._error_text(cleanup_error)}"
            )[:2048]
        return self._save_job(
            job.transition(CandidateJobState.FAILED, error=message)
        )

    def load_preview(self, job_id: str) -> IntakePreview:
        job = self.records.load_job(job_id)
        if job.state is not CandidateJobState.DONE or job.result != job_id:
            raise OperationError("job не содержит готовый preview")
        request = self._load_request(job_id)
        if request.candidate_id != job.candidate_id:
            raise OperationError("request операции относится к другому candidate")
        payload = self._read_record(
            self.previews_dir,
            job_id,
            "intake-preview",
            limit=_MAX_PREVIEW_RECORD_BYTES,
            label="preview record",
        )
        try:
            if payload["job_id"] != job_id or payload["candidate_id"] != job.candidate_id:
                raise OperationError("preview record относится к другой job")
            operation_root = self._work_root(job_id)
            materialized_root = _managed_path(
                operation_root,
                payload["materialized_root"],
                "materialized_root",
            )
            if materialized_root.is_symlink() or not materialized_root.is_dir():
                raise OperationError("materialized_root preview недоступен")
            manifest = _manifest_from_dict(payload["manifest"])
            materialized = MaterializedGeneration(
                materialized_root,
                manifest,
                _payloads_from_dict(operation_root, manifest, payload["payloads"]),
            )
            preview = IntakePreview(
                job_id=job_id,
                candidate_id=job.candidate_id,
                action=request.action,
                active=request.active,
                materialized=materialized,
            )
            candidate = self.records.load_candidate(job.candidate_id)
            if (
                preview.materialized.manifest.identity != candidate.identity
                or preview.materialized.manifest.raw_sha256 != candidate.raw_sha256
            ):
                raise OperationError("preview не совпадает с durable candidate")
            return preview
        except PlannerError as error:
            raise OperationStalePreview(
                "Готовый preview несовместим с текущим контрактом: "
                f"{error} Постройте новый preview."
            ) from error
        except OperationError:
            raise
        except Exception as error:
            raise OperationError("preview record содержит неверные поля") from error

    @staticmethod
    def _require_publisher(publisher: object) -> None:
        required = (
            "active_generation_pointer",
            "active_generation",
            "generation_payload_sources",
            "stage_generation",
            "publish_generation",
        )
        if any(not callable(getattr(publisher, name, None)) for name in required):
            raise TypeError("publisher не реализует generation Registry contract")

    @staticmethod
    def _result(
        preview: IntakePreview,
        pointer: GenerationPointer,
    ) -> IntakeCommitResult:
        return IntakeCommitResult(
            job_id=preview.job_id,
            candidate_id=preview.candidate_id,
            no_op=preview.plan.no_op,
            pointer=pointer,
            applied_layers=preview.plan.applied_layers,
        )

    def _after_publish(self, _pointer: GenerationPointer) -> None:
        """Точка crash-теста между Registry commit и durable result."""

    def confirm(self, job_id: str, publisher: object) -> IntakeCommitResult:
        """CAS-подтвердить preview либо durable вернуть доказанный no-op."""
        self._require_publisher(publisher)
        try:
            result = self.load_commit(job_id)
        except KeyError:
            pass
        else:
            self._discard_payload(job_id, keep_commit=True, keep_job=True)
            return result

        def finish(result: IntakeCommitResult) -> IntakeCommitResult:
            # Сначала durable result: если очистка оборвётся, повторный confirm
            # безопасно закончит её, не публикуя поколение второй раз.
            self._save_commit(result)
            self._discard_payload(job_id, keep_commit=True, keep_job=True)
            return result

        preview = self.load_preview(job_id)
        active_manifest = (
            preview.active.manifest
            if preview.active is not None
            else None
        )
        target_manifest = compose_manifest(
            preview.plan,
            preview.materialized.manifest,
            active_manifest=active_manifest,
        )
        expected = preview.expected_previous
        current = publisher.active_generation_pointer(preview.plan.identity)

        if target_manifest is None:
            if current != expected:
                raise OperationConflict(
                    "active generation изменился после построения preview"
                )
            if current is None:
                raise OperationError("no-op не имеет active generation")
            result = self._result(preview, current)
            return finish(result)

        target = GenerationPointer.for_manifest(target_manifest)
        if current == target:
            if publisher.active_generation(preview.plan.identity) != target_manifest:
                raise OperationConflict(
                    "active generation pointer не совпадает с target manifest"
                )
            result = self._result(preview, target)
            return finish(result)
        if current != expected:
            raise OperationConflict(
                "active generation изменился после построения preview"
            )

        active_payloads: Mapping[LayerKind, LayerPayloadSource] = {}
        if active_manifest is not None:
            if expected is None:
                raise OperationError("native preview не содержит expected pointer")
            active_payloads = publisher.generation_payload_sources(expected)
        composed = compose_generation(
            preview.plan,
            preview.materialized,
            active_manifest=active_manifest,
            active_payloads=active_payloads,
        )
        if composed is None or composed.manifest != target_manifest:
            raise OperationError("physical composition не совпадает с target manifest")

        staged = None
        try:
            staged = publisher.stage_generation(composed.manifest, composed.payloads)
            pointer = publisher.publish_generation(
                staged,
                expected_previous=expected,
                expected_active=preview.active,
            )
        except Exception as error:
            current = publisher.active_generation_pointer(preview.plan.identity)
            if (
                current == target
                and publisher.active_generation(preview.plan.identity) == target_manifest
            ):
                pointer = target
            else:
                discard = getattr(publisher, "discard_staged_generation", None)
                if staged is not None and callable(discard):
                    try:
                        discard(staged)
                    except Exception:
                        pass
                if current != expected:
                    raise OperationConflict(
                        "active generation изменился после построения preview"
                    ) from error
                if isinstance(error, GenerationConflictError):
                    raise OperationConflict(str(error)) from error
                message = self._error_text(error)
                raise OperationError(message) from error
        if pointer != target:
            raise OperationError("Registry опубликовал неожиданный generation pointer")
        self._after_publish(pointer)
        result = self._result(preview, pointer)
        return finish(result)


__all__ = [
    "IntakeCommitResult",
    "IntakeCoordinator",
    "IntakePreview",
    "OperationConflict",
    "OperationError",
    "OperationStalePreview",
]
