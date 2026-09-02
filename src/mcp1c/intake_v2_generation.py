"""Каноническая materialization пяти слоёв одного source-B collection."""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Mapping

from .intake_v2 import (
    ExportIdentity,
    GenerationManifest,
    LayerKind,
    LayerManifest,
    LayerState,
    SourceKind,
)
from .intake_v2_collector import (
    CollectionArtifact,
    CollectionError,
    CollectionResult,
    open_collection_member,
)
from .intake_v2_converter import (
    CommonFormPayload,
    ConversionError,
    ExtendedStructure,
    FormStructureState,
    StructureConversion,
    base_layer_data,
    extended_layer_data,
)
from .intake_v2_registry import (
    BundleStoreError,
    LayerMember,
    LayerMemberSource,
    LayerPayload,
    LayerPayloadSource,
    hash_layer_payload,
    hash_layer_semantic,
    load_layer_payload,
)
from .v8container import V8Container, V8ContainerError, V8ResourceLimitError


GENERATION_FORMAT_VERSION = 1
GENERATION_PARSER_VERSION = 1
_READ_CHUNK = 1 << 20
_MAX_FORM_CONTAINER_SIZE = 64 << 20


class GenerationMaterializationError(RuntimeError):
    """Collection и conversion нельзя превратить в доказуемое поколение."""


@dataclass(frozen=True, slots=True)
class MaterializedGeneration:
    root: Path
    manifest: GenerationManifest
    payloads: Mapping[LayerKind, LayerPayloadSource]

    def __post_init__(self) -> None:
        object.__setattr__(self, "root", Path(self.root))
        object.__setattr__(self, "payloads", MappingProxyType(dict(self.payloads)))


@dataclass(frozen=True, slots=True)
class _SourceSpec:
    member: LayerMember
    source_path: Path
    local_relative: str = ""


@dataclass(frozen=True, slots=True)
class _LayerBuild:
    payload: LayerPayload
    sources: tuple[_SourceSpec, ...]
    items_total: int


@dataclass(frozen=True, slots=True)
class _ModuleBody:
    address: str
    size: int
    sha256: str
    source_path: Path
    local_relative: str = ""


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())


def _read_artifact(
    collection: CollectionResult,
    artifact: CollectionArtifact,
    *,
    limit: int,
) -> bytes:
    try:
        with open_collection_member(collection.root, artifact.relative_path) as stream:
            payload = stream.read(limit + 1)
    except CollectionError as error:
        raise GenerationMaterializationError(
            f"{artifact.address or artifact.source_path}: payload недоступен"
        ) from error
    if len(payload) > limit:
        raise GenerationMaterializationError(
            f"{artifact.address or artifact.source_path}: payload превышает предел"
        )
    if len(payload) != artifact.size or hashlib.sha256(payload).hexdigest() != artifact.sha256:
        raise GenerationMaterializationError(
            f"{artifact.address or artifact.source_path}: payload collection изменён"
        )
    return payload


def _stream_raw_member(root: Path, relative_path: str) -> tuple[int, str]:
    digest = hashlib.sha256()
    total = 0
    try:
        with open_collection_member(root, relative_path) as stream:
            for block in iter(lambda: stream.read(_READ_CHUNK), b""):
                total += len(block)
                digest.update(block)
    except CollectionError as error:
        raise GenerationMaterializationError(
            f"{relative_path}: payload collection недоступен"
        ) from error
    return total, digest.hexdigest()


def _module_bodies(
    collection: CollectionResult,
    temporary: Path,
) -> tuple[_ModuleBody, ...]:
    modules: dict[str, _ModuleBody] = {}

    def add(body: _ModuleBody) -> None:
        key = body.address.casefold()
        previous = modules.get(key)
        if previous is None:
            modules[key] = body
            return
        if previous.address != body.address:
            raise GenerationMaterializationError(
                "адреса модулей различаются только регистром"
            )
        if previous.sha256 != body.sha256 or previous.size != body.size:
            raise GenerationMaterializationError(
                f"{body.address}: конфликтуют тела одного модуля"
            )

    for artifact in collection.code:
        add(
            _ModuleBody(
                artifact.address,
                artifact.size,
                artifact.sha256,
                collection.root / artifact.relative_path,
            )
        )

    containers = tuple(
        artifact
        for artifact in collection.forms
        if artifact.source_path.endswith(("/Ext/Form.bin", ".Form"))
    )
    for ordinal, artifact in enumerate(containers):
        payload = _read_artifact(
            collection,
            artifact,
            limit=_MAX_FORM_CONTAINER_SIZE,
        )
        try:
            with V8Container(payload) as container:
                if "module" not in container:
                    continue
                module = container.read("module")
        except (V8ContainerError, V8ResourceLimitError):
            # Нечитаемая форма остаётся честно представлена в forms-слое;
            # недоказанное тело модуля не публикуется как код.
            continue
        digest = hashlib.sha256(module).hexdigest()
        relative = f"derived/code/{ordinal:08d}.bsl"
        _write(temporary / relative, module)
        add(
            _ModuleBody(
                artifact.address,
                len(module),
                digest,
                temporary / relative,
                local_relative=relative,
            )
        )
    return tuple(sorted(modules.values(), key=lambda item: (item.address.casefold(), item.address)))


def _code_layer(
    collection: CollectionResult,
    temporary: Path,
) -> _LayerBuild:
    bodies = _module_bodies(collection, temporary)
    members: list[LayerMember] = []
    sources: list[_SourceSpec] = []
    modules: list[dict[str, object]] = []
    for ordinal, body in enumerate(bodies):
        member = LayerMember(
            key=body.address,
            relative_path=f"payload/code/{ordinal:08d}.bsl",
            size=body.size,
            sha256=body.sha256,
        )
        members.append(member)
        sources.append(
            _SourceSpec(
                member,
                body.source_path,
                local_relative=body.local_relative,
            )
        )
        modules.append(
            {
                "address": body.address,
                "size": body.size,
                "sha256": body.sha256,
            }
        )
    return _LayerBuild(
        LayerPayload(LayerKind.CODE, {"modules": modules}, tuple(members)),
        tuple(sources),
        len(modules),
    )


def _common_form_semantic(
    address: str,
    payload: CommonFormPayload,
    artifacts: tuple[CollectionArtifact, ...],
) -> dict[str, object]:
    value: dict[str, object] = {
        "address": address,
        "structure_state": payload.structure_state.value,
        "container_marker": payload.container_marker,
        "attributes": list(payload.attributes),
        "elements": list(payload.elements),
        "events": [
            {
                "element": event.element,
                "event": event.event,
                "handler": event.handler,
            }
            for event in payload.events
        ],
    }
    if payload.structure_state is not FormStructureState.READY:
        value["unparsed_payload_sha256"] = sorted(
            artifact.sha256
            for artifact in artifacts
            if artifact.source_path.endswith(("/Ext/Form.xml", "/Ext/Form.bin", ".Form"))
        )
    return value


def _forms_layer(
    collection: CollectionResult,
    extended: ExtendedStructure,
) -> _LayerBuild:
    grouped: dict[str, tuple[str, list[CollectionArtifact]]] = {}
    for artifact in collection.forms:
        key = artifact.address.casefold()
        current = grouped.get(key)
        if current is None:
            grouped[key] = artifact.address, [artifact]
        else:
            if current[0] != artifact.address:
                raise GenerationMaterializationError(
                    "адреса форм различаются только регистром"
                )
            current[1].append(artifact)

    semantic_forms: list[dict[str, object]] = []
    ordered_artifacts: list[CollectionArtifact] = []
    for _key, (address, artifacts_list) in sorted(grouped.items()):
        artifacts = tuple(sorted(artifacts_list, key=lambda item: item.source_path))
        obj = extended.get(address)
        if obj is not None and isinstance(obj.payload, CommonFormPayload):
            semantic_forms.append(_common_form_semantic(address, obj.payload, artifacts))
        else:
            # Пока структура остальных форм не доказана, raw hash входит в
            # семантику: неизвестное изменение нельзя объявить no-op.
            semantic_forms.append(
                {
                    "address": address,
                    "structure_state": "opaque",
                    "payload_sha256": sorted(item.sha256 for item in artifacts),
                }
            )
        ordered_artifacts.extend(artifacts)

    members: list[LayerMember] = []
    sources: list[_SourceSpec] = []
    for ordinal, artifact in enumerate(ordered_artifacts):
        suffix = PurePosixPath(artifact.source_path).suffix.lower() or ".bin"
        member = LayerMember(
            key=f"{artifact.address}|{artifact.source_path}",
            relative_path=f"payload/forms/{ordinal:08d}{suffix}",
            size=artifact.size,
            sha256=artifact.sha256,
        )
        members.append(member)
        sources.append(
            _SourceSpec(member, collection.root / artifact.relative_path)
        )
    return _LayerBuild(
        LayerPayload(
            LayerKind.FORMS,
            {"forms": semantic_forms},
            tuple(members),
        ),
        tuple(sources),
        len(semantic_forms),
    )


def _roles_layer(collection: CollectionResult) -> _LayerBuild | None:
    roles = collection.roles
    if roles.state is LayerState.ERROR:
        return None
    semantic = {
        "roles_total": roles.roles_total,
        "artifacts": [
            {
                "source_path": artifact.source_path,
                "size": artifact.size,
                "sha256": artifact.sha256,
            }
            for artifact in roles.artifacts
        ],
    }
    members: list[LayerMember] = []
    sources: list[_SourceSpec] = []
    for ordinal, artifact in enumerate(roles.artifacts):
        size, digest = _stream_raw_member(collection.root, artifact.relative_path)
        member = LayerMember(
            key=artifact.source_path,
            relative_path=f"payload/roles/{ordinal:08d}.xml.gz",
            size=size,
            sha256=digest,
        )
        members.append(member)
        sources.append(
            _SourceSpec(member, collection.root / artifact.relative_path)
        )
    return _LayerBuild(
        LayerPayload(LayerKind.ROLES, semantic, tuple(members)),
        tuple(sources),
        roles.roles_total,
    )


def _identity(collection: CollectionResult, parent_configuration: str) -> ExportIdentity:
    probe = collection.probe
    if probe.source_kind is SourceKind.CONFIGURATION:
        if parent_configuration:
            raise GenerationMaterializationError(
                "основная конфигурация не принимает parent_configuration"
            )
        return ExportIdentity.configuration(probe.internal_name)
    if not parent_configuration:
        raise GenerationMaterializationError(
            "для расширения обязателен parent_configuration"
        )
    return ExportIdentity.extension(
        probe.internal_name,
        parent_configuration=parent_configuration,
    )


def materialize_generation(
    collection: CollectionResult,
    conversion: StructureConversion,
    target: str | Path,
    *,
    generation_id: str,
    parent_configuration: str = "",
) -> MaterializedGeneration:
    """Атомарно собрать manifests и ссылки на тела, не меняя Registry."""
    if not isinstance(collection, CollectionResult):
        raise TypeError("collection должен быть CollectionResult")
    if not isinstance(conversion, StructureConversion):
        raise TypeError("conversion должен быть StructureConversion")
    target = Path(target)
    if target.exists() or target.is_symlink():
        raise GenerationMaterializationError("target materialization уже существует")
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{target.name}-", dir=target.parent))
    try:
        base_semantic = base_layer_data(conversion.base)
        extended_semantic = extended_layer_data(conversion.extended)
        if (
            hash_layer_semantic(LayerKind.BASE_STRUCTURE, base_semantic)
            != conversion.base_content_sha256
            or hash_layer_semantic(LayerKind.EXTENDED_STRUCTURE, extended_semantic)
            != conversion.extended_content_sha256
        ):
            raise GenerationMaterializationError(
                "conversion не совпадает с каноническими структурными слоями"
            )

        builds: dict[LayerKind, _LayerBuild | None] = {
            LayerKind.BASE_STRUCTURE: _LayerBuild(
                LayerPayload(LayerKind.BASE_STRUCTURE, base_semantic),
                (),
                len(conversion.base.objects),
            ),
            LayerKind.EXTENDED_STRUCTURE: _LayerBuild(
                LayerPayload(LayerKind.EXTENDED_STRUCTURE, extended_semantic),
                (),
                len(conversion.extended),
            ),
            LayerKind.CODE: _code_layer(collection, temporary),
            LayerKind.FORMS: _forms_layer(collection, conversion.extended),
            LayerKind.ROLES: _roles_layer(collection),
        }
        layers: list[LayerManifest] = []
        source_specs: dict[LayerKind, tuple[_SourceSpec, ...]] = {}
        for kind in LayerKind:
            build = builds[kind]
            if build is None:
                layers.append(
                    LayerManifest(
                        kind=kind,
                        state=LayerState.ERROR,
                        error=collection.roles.error,
                    )
                )
                continue
            manifest_relative = f"layers/{kind.value}.json"
            manifest_path = temporary / manifest_relative
            _write(manifest_path, build.payload.to_json_bytes())
            semantic_hash = hash_layer_semantic(kind, build.payload.semantic)
            layers.append(
                LayerManifest(
                    kind=kind,
                    state=LayerState.READY,
                    content_sha256=semantic_hash,
                    payload_sha256=hash_layer_payload(kind, manifest_path),
                    relative_path=manifest_relative,
                    items_total=build.items_total,
                )
            )
            source_specs[kind] = build.sources

        manifest = GenerationManifest(
            format_version=GENERATION_FORMAT_VERSION,
            generation_id=generation_id,
            identity=_identity(collection, parent_configuration),
            parser_version=GENERATION_PARSER_VERSION,
            selection_version=collection.selection_version,
            source_transport=collection.probe.transport,
            origin_name=collection.probe.origin_name,
            raw_sha256=collection.probe.raw_sha256,
            layers=tuple(layers),
        )
        _sync_directory(temporary)
        if target.exists() or target.is_symlink():
            raise GenerationMaterializationError(
                "target materialization появился во время сборки"
            )
        os.rename(temporary, target)
        _sync_directory(target.parent)
        temporary = Path()

        payloads: dict[LayerKind, LayerPayloadSource] = {}
        for layer in manifest.layers:
            if layer.state is not LayerState.READY:
                continue
            members = tuple(
                LayerMemberSource(
                    spec.member,
                    target / spec.local_relative
                    if spec.local_relative
                    else spec.source_path,
                )
                for spec in source_specs[layer.kind]
            )
            payloads[layer.kind] = LayerPayloadSource(
                target / layer.relative_path,
                members,
            )
        return MaterializedGeneration(target, manifest, payloads)
    except (BundleStoreError, ConversionError) as error:
        raise GenerationMaterializationError(str(error)) from error
    finally:
        if temporary != Path() and temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)


__all__ = [
    "BundleStoreError",
    "GenerationMaterializationError",
    "MaterializedGeneration",
    "load_layer_payload",
    "materialize_generation",
]
