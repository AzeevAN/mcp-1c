"""Неизменяемые generation bundle для единого приёма конфигураций.

Модуль отвечает только за проверяемое дисковое поколение. Тяжёлые runtime-
индексы строятся до публикации и будут подключены planner-ом; здесь нет
второго Registry, фонового scanner или доступа к пользовательскому ``data``.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import tempfile
import uuid
from dataclasses import dataclass
from enum import Enum
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import BinaryIO, Mapping

from .intake_v2 import (
    CandidateTransport,
    ExportIdentity,
    GenerationManifest,
    LayerKind,
    LayerManifest,
    LayerState,
    RecoveryAction,
    RecoveryPhase,
    RecoveryRecord,
    decide_recovery,
)


_HASH_BLOCK_SIZE = 1 << 20
_MAX_MANIFEST_SIZE = 1 << 20


class BundleStoreError(RuntimeError):
    """Generation bundle повреждён либо нарушает границу публикации."""


class RecoveryBlocked(BundleStoreError):
    """WAL и active pointer не позволяют доказать старое или новое состояние."""


class GenerationOrigin(str, Enum):
    NATIVE = "native"
    LEGACY = "legacy"


def _identity_digest(identity: ExportIdentity) -> str:
    raw = json.dumps(
        identity.to_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(b"mcp1c-generation-identity-v1\0" + raw).hexdigest()


def _safe_relative(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value:
        raise BundleStoreError(f"{label} должен быть относительным POSIX-путём")
    path = PurePosixPath(value)
    if (
        value == "."
        or path.is_absolute()
        or not path.parts
        or any(part in ("", ".", "..") for part in path.parts)
        or path.as_posix() != value
    ):
        raise BundleStoreError(f"{label} должен быть безопасным относительным путём")
    return value


def _layer_prefix(kind: LayerKind) -> bytes:
    if not isinstance(kind, LayerKind):
        raise TypeError("kind должен быть LayerKind")
    return b"mcp1c-layer-v1\0" + kind.value.encode("ascii") + b"\0"


def _hash_stream(kind: LayerKind, stream: BinaryIO) -> str:
    digest = hashlib.sha256(_layer_prefix(kind))
    for block in iter(lambda: stream.read(_HASH_BLOCK_SIZE), b""):
        digest.update(block)
    return digest.hexdigest()


def _stat_identity(value: os.stat_result) -> tuple[int, int, int, int]:
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns


def hash_layer_payload(kind: LayerKind, path: str | Path) -> str:
    """Потоковый semantic hash канонического payload без materialization."""
    path = Path(path)
    try:
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode):
            raise BundleStoreError("payload слоя должен быть обычным файлом")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode):
                raise BundleStoreError("payload слоя должен быть обычным файлом")
            with os.fdopen(descriptor, "rb", closefd=False) as stream:
                digest = _hash_stream(kind, stream)
            after = os.fstat(descriptor)
        finally:
            os.close(descriptor)
        current = path.lstat()
    except BundleStoreError:
        raise
    except OSError as error:
        raise BundleStoreError("payload слоя недоступен для чтения") from error
    if (
        _stat_identity(before) != _stat_identity(opened)
        or _stat_identity(opened) != _stat_identity(after)
    ):
        raise BundleStoreError("payload слоя изменился во время чтения")
    if _stat_identity(after) != _stat_identity(current):
        raise BundleStoreError("payload слоя заменён во время чтения")
    return digest


@dataclass(frozen=True, slots=True)
class GenerationPointer:
    """Единственное содержимое Registry о нативном активном поколении."""

    identity: ExportIdentity
    generation_id: str
    root_path: str
    manifest_path: str
    manifest_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.identity, ExportIdentity):
            raise BundleStoreError("identity pointer должен быть ExportIdentity")
        if not isinstance(self.generation_id, str) or not self.generation_id:
            raise BundleStoreError("generation_id pointer должен быть строкой")
        root = _safe_relative(self.root_path, "root_path")
        manifest = _safe_relative(self.manifest_path, "manifest_path")
        expected_root = (
            f"generations/{_identity_digest(self.identity)}/{self.generation_id}"
        )
        if root != expected_root or manifest != f"{expected_root}/manifest.json":
            raise BundleStoreError("pointer не соответствует identity/generation")
        if (
            not isinstance(self.manifest_sha256, str)
            or len(self.manifest_sha256) != 64
            or any(char not in "0123456789abcdef" for char in self.manifest_sha256)
        ):
            raise BundleStoreError("manifest_sha256 pointer должен быть sha256")

    @classmethod
    def for_manifest(cls, manifest: GenerationManifest) -> GenerationPointer:
        digest = _identity_digest(manifest.identity)
        root = f"generations/{digest}/{manifest.generation_id}"
        return cls(
            identity=manifest.identity,
            generation_id=manifest.generation_id,
            root_path=root,
            manifest_path=f"{root}/manifest.json",
            manifest_sha256=manifest.sha256,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "identity": self.identity.to_dict(),
            "generation_id": self.generation_id,
            "manifest_path": self.manifest_path,
            "manifest_sha256": self.manifest_sha256,
            "root_path": self.root_path,
        }

    @classmethod
    def from_dict(cls, raw: object) -> GenerationPointer:
        if not isinstance(raw, dict):
            raise BundleStoreError("generation pointer должен быть объектом")
        try:
            return cls(
                identity=ExportIdentity.from_dict(raw["identity"]),
                generation_id=raw["generation_id"],
                root_path=raw["root_path"],
                manifest_path=raw["manifest_path"],
                manifest_sha256=raw["manifest_sha256"],
            )
        except (KeyError, TypeError, ValueError) as error:
            if isinstance(error, BundleStoreError):
                raise
            raise BundleStoreError("generation pointer содержит неверные поля") from error


@dataclass(frozen=True, slots=True)
class StagedGeneration:
    manifest: GenerationManifest
    root: Path
    pointer: GenerationPointer


@dataclass(frozen=True, slots=True)
class GenerationLayerView:
    state: LayerState
    content_sha256: str = ""
    source_sha256: str = ""
    items_total: int = 0
    error: str = ""


@dataclass(frozen=True, slots=True)
class GenerationView:
    origin: GenerationOrigin
    identity: ExportIdentity
    manifest: GenerationManifest | None
    layers: Mapping[LayerKind, GenerationLayerView]

    def __post_init__(self) -> None:
        if not isinstance(self.origin, GenerationOrigin):
            raise BundleStoreError("origin generation view должен быть GenerationOrigin")
        if not isinstance(self.identity, ExportIdentity):
            raise BundleStoreError("identity generation view должен быть ExportIdentity")
        object.__setattr__(self, "layers", MappingProxyType(dict(self.layers)))


@dataclass(frozen=True, slots=True)
class GenerationRecovery:
    previous: GenerationPointer | None
    staged: GenerationPointer
    phase: RecoveryPhase
    staging_path: str = ""

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "previous": self.previous.to_dict() if self.previous else None,
            "staged": self.staged.to_dict(),
            "phase": self.phase.value,
        }
        if self.staging_path:
            payload["staging_path"] = self.staging_path
        return payload

    @classmethod
    def from_dict(cls, raw: object) -> GenerationRecovery:
        if not isinstance(raw, dict):
            raise RecoveryBlocked("generation WAL должен быть объектом")
        try:
            previous = raw.get("previous")
            return cls(
                previous=(
                    GenerationPointer.from_dict(previous)
                    if previous is not None
                    else None
                ),
                staged=GenerationPointer.from_dict(raw["staged"]),
                phase=RecoveryPhase(raw["phase"]),
                staging_path=(
                    _safe_relative(raw["staging_path"], "staging_path")
                    if raw.get("staging_path")
                    else ""
                ),
            )
        except (KeyError, TypeError, ValueError, BundleStoreError) as error:
            if isinstance(error, RecoveryBlocked):
                raise
            raise RecoveryBlocked("generation WAL содержит неверные поля") from error


def native_generation_view(manifest: GenerationManifest) -> GenerationView:
    layers = {
        layer.kind: GenerationLayerView(
            state=layer.state,
            content_sha256=layer.content_sha256,
            items_total=layer.items_total,
            error=layer.error,
        )
        for layer in manifest.layers
    }
    return GenerationView(
        origin=GenerationOrigin.NATIVE,
        identity=manifest.identity,
        manifest=manifest,
        layers=layers,
    )


def legacy_generation_view(
    identity: ExportIdentity,
    *,
    base_sha256: str,
    base_items_total: int,
    code_sha256: str = "",
    code_items_total: int = 0,
) -> GenerationView:
    unavailable = GenerationLayerView(LayerState.UNAVAILABLE)
    layers = {
        LayerKind.BASE_STRUCTURE: GenerationLayerView(
            LayerState.READY,
            source_sha256=base_sha256,
            items_total=base_items_total,
        ),
        LayerKind.EXTENDED_STRUCTURE: unavailable,
        LayerKind.CODE: (
            GenerationLayerView(
                LayerState.READY,
                source_sha256=code_sha256,
                items_total=code_items_total,
            )
            if code_sha256
            else unavailable
        ),
        LayerKind.FORMS: (
            GenerationLayerView(
                LayerState.READY,
                source_sha256=code_sha256,
                items_total=code_items_total,
            )
            if code_sha256
            else unavailable
        ),
        LayerKind.ROLES: unavailable,
    }
    return GenerationView(
        origin=GenerationOrigin.LEGACY,
        identity=identity,
        manifest=None,
        layers=layers,
    )


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{uuid.uuid4().hex}")
    try:
        with temporary.open("xb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


class GenerationBundleStore:
    """Потоково строит и проверяет неизменяемые поколения."""

    def __init__(self, data_dir: str | Path):
        self.data_dir = Path(data_dir)
        self.root = self.data_dir / "generations"
        self.recovery_path = self.data_dir / ".generation-publish.json"

    def _absolute(self, relative: str) -> Path:
        return self.data_dir / _safe_relative(relative, "generation path")

    def _validate_path_chain(
        self, path: Path, *, allow_missing_leaf: bool = False
    ) -> None:
        try:
            relative = path.relative_to(self.data_dir)
        except ValueError as error:
            raise BundleStoreError("generation path находится вне data_dir") from error
        current = self.data_dir
        try:
            root_stat = current.lstat()
            if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
                raise BundleStoreError("data_dir должен быть обычным каталогом")
            for position, part in enumerate(relative.parts):
                current /= part
                try:
                    value = current.lstat()
                except FileNotFoundError:
                    if allow_missing_leaf and position == len(relative.parts) - 1:
                        return
                    raise
                if stat.S_ISLNK(value.st_mode):
                    raise BundleStoreError("generation path содержит symlink")
                if position < len(relative.parts) - 1 and not stat.S_ISDIR(
                    value.st_mode
                ):
                    raise BundleStoreError("generation path содержит не каталог")
        except BundleStoreError:
            raise
        except OSError as error:
            raise BundleStoreError("generation path недоступен") from error

    @staticmethod
    def _staging_relative(root: Path, data_dir: Path) -> str:
        try:
            relative = root.relative_to(data_dir).as_posix()
        except ValueError as error:
            raise BundleStoreError("staging находится вне data_dir") from error
        return _safe_relative(relative, "staging_path")

    def _copy_layer(self, kind: LayerKind, source: Path, target: Path) -> str:
        try:
            before = source.lstat()
            if not stat.S_ISREG(before.st_mode):
                raise BundleStoreError("payload слоя должен быть обычным файлом")
            descriptor = os.open(
                source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256(_layer_prefix(kind))
            try:
                opened = os.fstat(descriptor)
                with os.fdopen(descriptor, "rb", closefd=False) as input_stream:
                    with target.open("xb") as output_stream:
                        for block in iter(
                            lambda: input_stream.read(_HASH_BLOCK_SIZE), b""
                        ):
                            digest.update(block)
                            output_stream.write(block)
                        output_stream.flush()
                        os.fsync(output_stream.fileno())
                after = os.fstat(descriptor)
            finally:
                os.close(descriptor)
            current = source.lstat()
        except BundleStoreError:
            raise
        except OSError as error:
            raise BundleStoreError("не удалось сохранить payload слоя") from error
        if not (
            _stat_identity(before)
            == _stat_identity(opened)
            == _stat_identity(after)
            == _stat_identity(current)
        ):
            raise BundleStoreError("payload слоя изменился во время staging")
        _sync_directory(target.parent)
        return digest.hexdigest()

    def stage(
        self,
        manifest: GenerationManifest,
        payloads: Mapping[LayerKind, str | Path],
    ) -> StagedGeneration:
        if not isinstance(manifest, GenerationManifest):
            raise TypeError("manifest должен быть GenerationManifest")
        ready = {
            layer.kind: layer
            for layer in manifest.layers
            if layer.state is LayerState.READY
        }
        ready_paths = [layer.relative_path for layer in ready.values()]
        if (
            len(set(ready_paths)) != len(ready_paths)
            or any(
                not PurePosixPath(relative_path).parts
                or PurePosixPath(relative_path).parts[0] != "layers"
                for relative_path in ready_paths
            )
        ):
            raise BundleStoreError(
                "ready payload должен иметь уникальный путь внутри layers/"
            )
        supplied = set(payloads)
        if supplied != set(ready):
            raise BundleStoreError(
                "payloads должны точно совпадать с ready-слоями без лишних"
            )
        pointer = GenerationPointer.for_manifest(manifest)
        final_root = self._absolute(pointer.root_path)
        if final_root.exists():
            raise BundleStoreError("generation_id уже существует для identity")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._validate_path_chain(self.data_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self._validate_path_chain(self.root)
        temporary = Path(
            tempfile.mkdtemp(
                prefix=f".staging-{manifest.generation_id}-",
                dir=self.root,
            )
        )
        try:
            for kind, layer in sorted(ready.items(), key=lambda item: item[0].value):
                target = temporary / _safe_relative(
                    layer.relative_path, "relative_path слоя"
                )
                actual = self._copy_layer(kind, Path(payloads[kind]), target)
                if actual != layer.content_sha256:
                    raise BundleStoreError(
                        f"{kind.value}: контрольная сумма payload не совпала"
                    )
            manifest_path = temporary / "manifest.json"
            with manifest_path.open("xb") as stream:
                stream.write(manifest.to_json_bytes())
                stream.flush()
                os.fsync(stream.fileno())
            _sync_directory(temporary)
            staged = StagedGeneration(manifest, temporary, pointer)
            self.verify(staged)
            return staged
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise

    def _open_member(self, root: Path, relative_path: str) -> BinaryIO:
        relative_path = _safe_relative(relative_path, "member path")
        self._validate_path_chain(root)
        try:
            root_stat = root.lstat()
            if not stat.S_ISDIR(root_stat.st_mode):
                raise BundleStoreError("generation root не является каталогом")
            descriptors: list[int] = [
                os.open(
                    root,
                    os.O_RDONLY
                    | getattr(os, "O_DIRECTORY", 0)
                    | getattr(os, "O_NOFOLLOW", 0),
                )
            ]
            for part in PurePosixPath(relative_path).parts[:-1]:
                descriptors.append(
                    os.open(
                        part,
                        os.O_RDONLY
                        | getattr(os, "O_DIRECTORY", 0)
                        | getattr(os, "O_NOFOLLOW", 0),
                        dir_fd=descriptors[-1],
                    )
                )
            file_descriptor = os.open(
                PurePosixPath(relative_path).parts[-1],
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=descriptors[-1],
            )
            opened = os.fstat(file_descriptor)
            if not stat.S_ISREG(opened.st_mode):
                os.close(file_descriptor)
                raise BundleStoreError("generation member не является файлом")
            stream = os.fdopen(file_descriptor, "rb")
        except BundleStoreError:
            raise
        except OSError as error:
            raise BundleStoreError("generation member недоступен") from error
        finally:
            for descriptor in reversed(locals().get("descriptors", [])):
                os.close(descriptor)
        return stream

    def verify(
        self, target: GenerationPointer | StagedGeneration
    ) -> GenerationManifest:
        if isinstance(target, StagedGeneration):
            root = target.root
            pointer = target.pointer
        elif isinstance(target, GenerationPointer):
            root = self._absolute(target.root_path)
            pointer = target
        else:
            raise TypeError("target должен быть GenerationPointer или StagedGeneration")
        with self._open_member(root, "manifest.json") as stream:
            raw = stream.read(_MAX_MANIFEST_SIZE + 1)
        if len(raw) > _MAX_MANIFEST_SIZE:
            raise BundleStoreError("generation manifest превышает предел")
        if hashlib.sha256(raw).hexdigest() != pointer.manifest_sha256:
            raise BundleStoreError("generation manifest: контрольная сумма не совпала")
        try:
            manifest = GenerationManifest.from_json_bytes(raw)
        except ValueError as error:
            raise BundleStoreError("generation manifest повреждён") from error
        if (
            manifest.identity != pointer.identity
            or manifest.generation_id != pointer.generation_id
        ):
            raise BundleStoreError("generation manifest не совпадает с pointer")
        for layer in manifest.layers:
            if layer.state is not LayerState.READY:
                continue
            with self._open_member(root, layer.relative_path) as stream:
                actual = _hash_stream(layer.kind, stream)
            if actual != layer.content_sha256:
                raise BundleStoreError(
                    f"{layer.kind.value}: контрольная сумма payload не совпала"
                )
        return manifest

    def promote(self, staged: StagedGeneration) -> None:
        self.verify(staged)
        target = self._absolute(staged.pointer.root_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            raise BundleStoreError("generation target уже существует")
        try:
            os.rename(staged.root, target)
            _sync_directory(target.parent)
        except OSError as error:
            raise BundleStoreError("generation staging не опубликован") from error

    def write_recovery(self, recovery: GenerationRecovery) -> None:
        _write_atomic(
            self.recovery_path,
            (
                json.dumps(
                    recovery.to_dict(),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                + "\n"
            ).encode("utf-8"),
        )

    def read_recovery(self) -> GenerationRecovery | None:
        try:
            self.recovery_path.lstat()
        except FileNotFoundError:
            return None
        except OSError as error:
            raise RecoveryBlocked("generation WAL недоступен") from error
        try:
            self._validate_path_chain(self.recovery_path)
            with self.recovery_path.open("rb") as stream:
                raw = stream.read(_MAX_MANIFEST_SIZE + 1)
            if len(raw) > _MAX_MANIFEST_SIZE:
                raise RecoveryBlocked("generation WAL превышает предел")
            return GenerationRecovery.from_dict(json.loads(raw.decode("utf-8")))
        except RecoveryBlocked:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise RecoveryBlocked("generation WAL повреждён") from error

    def clear_recovery(self) -> None:
        self.recovery_path.unlink(missing_ok=True)
        if self.data_dir.is_dir():
            _sync_directory(self.data_dir)

    def remove_pointer_root(self, pointer: GenerationPointer | None) -> None:
        if pointer is None:
            return
        root = self._absolute(pointer.root_path)
        try:
            self._validate_path_chain(root, allow_missing_leaf=True)
        except BundleStoreError as error:
            raise RecoveryBlocked(str(error)) from error
        if root.exists():
            shutil.rmtree(root)
            _sync_directory(root.parent)

    def remove_staging(self, relative_path: str) -> None:
        if not relative_path:
            return
        path = self._absolute(relative_path)
        if path.parent != self.root or not path.name.startswith(".staging-"):
            raise RecoveryBlocked("generation WAL содержит чужой staging")
        try:
            self._validate_path_chain(path, allow_missing_leaf=True)
        except BundleStoreError as error:
            raise RecoveryBlocked(str(error)) from error
        if path.exists():
            shutil.rmtree(path)
            _sync_directory(path.parent)

    def recovery_for(
        self,
        previous: GenerationPointer | None,
        staged: StagedGeneration,
        phase: RecoveryPhase,
    ) -> GenerationRecovery:
        return GenerationRecovery(
            previous=previous,
            staged=staged.pointer,
            phase=phase,
            staging_path=self._staging_relative(staged.root, self.data_dir),
        )


__all__ = [
    "BundleStoreError",
    "CandidateTransport",
    "ExportIdentity",
    "GenerationBundleStore",
    "GenerationLayerView",
    "GenerationManifest",
    "GenerationOrigin",
    "GenerationPointer",
    "GenerationRecovery",
    "GenerationView",
    "LayerKind",
    "LayerManifest",
    "LayerState",
    "RecoveryBlocked",
    "StagedGeneration",
    "decide_recovery",
    "hash_layer_payload",
    "legacy_generation_view",
    "native_generation_view",
]
