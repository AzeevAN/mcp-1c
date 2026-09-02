"""Чистый planner действий единого intake без диска, Registry и UI."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .intake_v2 import (
    ExportIdentity,
    GenerationManifest,
    LayerKind,
    LayerManifest,
    LayerProvenance,
    LayerState,
    SourceKind,
)
from .intake_v2_registry import GenerationOrigin, GenerationView


class PlannerError(ValueError):
    """Кандидат и выбранное действие не образуют безопасный план."""


class IntakeAction(str, Enum):
    CREATE = "create"
    UPDATE_CONTENT = "update"
    UPDATE_FULL = "update_full"


class LayerDecision(str, Enum):
    APPLY = "apply"
    PRESERVE = "preserve"


class LayerChangeReason(str, Enum):
    NONE = "none"
    ADDED = "added"
    CONTENT = "content"
    STATE = "state"
    REPARSE = "reparse"
    PROVENANCE = "provenance"


@dataclass(frozen=True, slots=True)
class LayerVersion:
    state: LayerState
    content_sha256: str = ""
    payload_sha256: str = ""
    source_sha256: str = ""
    items_total: int = 0
    error: str = ""
    provenance: LayerProvenance | None = None

    @classmethod
    def from_manifest(cls, layer: LayerManifest) -> LayerVersion:
        return cls(
            state=layer.state,
            content_sha256=layer.content_sha256,
            payload_sha256=layer.payload_sha256,
            items_total=layer.items_total,
            error=layer.error,
            provenance=layer.provenance,
        )


@dataclass(frozen=True, slots=True)
class PlannedLayer:
    kind: LayerKind
    current: LayerVersion | None
    candidate: LayerManifest
    reason: LayerChangeReason
    decision: LayerDecision

    @property
    def changed(self) -> bool:
        return self.reason is not LayerChangeReason.NONE


@dataclass(frozen=True, slots=True)
class IntakePlan:
    action: IntakeAction
    identity: ExportIdentity
    base_generation_id: str | None
    candidate_generation_id: str
    layers: tuple[PlannedLayer, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.identity, ExportIdentity):
            raise PlannerError("identity плана должен быть ExportIdentity")
        kinds = tuple(layer.kind for layer in self.layers)
        if len(kinds) != len(set(kinds)) or set(kinds) != set(LayerKind):
            raise PlannerError("план обязан описывать ровно пять слоёв")
        object.__setattr__(
            self,
            "layers",
            tuple(sorted(self.layers, key=lambda item: item.kind.value)),
        )

    @property
    def changed_layers(self) -> frozenset[LayerKind]:
        return frozenset(layer.kind for layer in self.layers if layer.changed)

    @property
    def applied_layers(self) -> frozenset[LayerKind]:
        return frozenset(
            layer.kind
            for layer in self.layers
            if layer.decision is LayerDecision.APPLY
        )

    @property
    def preserved_layers(self) -> frozenset[LayerKind]:
        return frozenset(
            layer.kind
            for layer in self.layers
            if layer.decision is LayerDecision.PRESERVE
        )

    @property
    def no_op(self) -> bool:
        return not self.applied_layers


_CONTENT_LAYERS = frozenset(
    {LayerKind.CODE, LayerKind.FORMS, LayerKind.ROLES}
)
_REQUIRED_READY = frozenset(
    {
        LayerKind.BASE_STRUCTURE,
        LayerKind.EXTENDED_STRUCTURE,
        LayerKind.CODE,
        LayerKind.FORMS,
    }
)


def _candidate_layers(
    candidate: GenerationManifest,
) -> dict[LayerKind, LayerManifest]:
    layers = {layer.kind: layer for layer in candidate.layers}
    if set(layers) != set(LayerKind):
        raise PlannerError("кандидат обязан описывать ровно пять слоёв")
    if any(layer.provenance is None for layer in layers.values()):
        raise PlannerError("каждый слой кандидата обязан иметь provenance")
    for layer in layers.values():
        provenance = layer.provenance
        assert provenance is not None
        if (
            provenance.transport is not candidate.source_transport
            or provenance.origin_name != candidate.origin_name
            or provenance.raw_sha256 != candidate.raw_sha256
            or provenance.parser_version != candidate.parser_version
            or provenance.selection_version != candidate.selection_version
        ):
            raise PlannerError(
                f"{layer.kind.value}: provenance не совпадает с кандидатом"
            )
    if any(
        layers[kind].state is not LayerState.READY
        for kind in _REQUIRED_READY
    ):
        raise PlannerError("структура, код и формы кандидата должны быть ready")
    if layers[LayerKind.ROLES].state is LayerState.UNAVAILABLE:
        raise PlannerError("source-B кандидат не должен скрывать состояние ролей")
    return layers


def _active_manifest_layers(
    active: GenerationView,
) -> dict[LayerKind, LayerManifest]:
    if active.manifest is None:
        return {}
    return {layer.kind: layer for layer in active.manifest.layers}


def _current_version(
    active: GenerationView,
    native: dict[LayerKind, LayerManifest],
    kind: LayerKind,
) -> LayerVersion | None:
    layer = native.get(kind)
    if layer is not None:
        return LayerVersion.from_manifest(layer)
    view = active.layers.get(kind)
    if view is None:
        return None
    return LayerVersion(
        state=view.state,
        content_sha256=view.content_sha256,
        source_sha256=view.source_sha256,
        items_total=view.items_total,
        error=view.error,
    )


def _reason(
    current: LayerVersion | None,
    candidate: LayerManifest,
    *,
    legacy: bool,
) -> LayerChangeReason:
    if current is None:
        return LayerChangeReason.ADDED
    if legacy:
        # У legacy нет semantic hash и версий parser по слоям: совпадение
        # сырого SHA не доказывает готовность нового канонического формата.
        return LayerChangeReason.REPARSE
    before = current.provenance
    after = candidate.provenance
    if before is not None and after is not None and before.profile is after.profile:
        if (
            after.parser_version < before.parser_version
            or after.selection_version < before.selection_version
        ):
            raise PlannerError(
                f"{candidate.kind.value}: parser кандидата старее active"
            )
    if current.state is not candidate.state:
        return LayerChangeReason.STATE
    if (
        current.content_sha256 != candidate.content_sha256
        or current.items_total != candidate.items_total
        or current.error != candidate.error
    ):
        return LayerChangeReason.CONTENT
    if before is None or after is None:
        return LayerChangeReason.REPARSE
    if before.profile is not after.profile:
        return LayerChangeReason.PROVENANCE
    if (
        after.parser_version > before.parser_version
        or after.selection_version > before.selection_version
    ):
        return LayerChangeReason.REPARSE
    # raw SHA, transport, имя и физический hash не являются semantic diff.
    return LayerChangeReason.NONE


def plan_intake(
    action: IntakeAction,
    candidate: GenerationManifest,
    *,
    active: GenerationView | None,
) -> IntakePlan:
    """Построить diff и разрешённые слои, не выполняя публикацию."""
    if not isinstance(action, IntakeAction):
        raise TypeError("action должен быть IntakeAction")
    if not isinstance(candidate, GenerationManifest):
        raise TypeError("candidate должен быть GenerationManifest")
    if active is not None and not isinstance(active, GenerationView):
        raise TypeError("active должен быть GenerationView или None")
    candidate_layers = _candidate_layers(candidate)

    if action is IntakeAction.CREATE:
        if candidate.identity.source_kind is not SourceKind.CONFIGURATION:
            raise PlannerError("create не создаёт конфигурацию из расширения")
        if active is not None:
            raise PlannerError("конфигурация уже существует")
        allowed = frozenset(LayerKind)
    else:
        if active is None:
            raise PlannerError("для обновления нужна существующая конфигурация")
        if active.identity != candidate.identity:
            raise PlannerError("личность кандидата не совпадает с целью")
        allowed = (
            _CONTENT_LAYERS
            if action is IntakeAction.UPDATE_CONTENT
            else frozenset(LayerKind)
        )

    native = _active_manifest_layers(active) if active is not None else {}
    legacy = active is not None and active.origin is GenerationOrigin.LEGACY
    planned: list[PlannedLayer] = []
    for kind in LayerKind:
        current = (
            _current_version(active, native, kind)
            if active is not None
            else None
        )
        layer = candidate_layers[kind]
        reason = _reason(current, layer, legacy=legacy) if active else LayerChangeReason.ADDED
        decision = (
            LayerDecision.APPLY
            if kind in allowed and reason is not LayerChangeReason.NONE
            else LayerDecision.PRESERVE
        )
        planned.append(PlannedLayer(kind, current, layer, reason, decision))

    return IntakePlan(
        action=action,
        identity=candidate.identity,
        base_generation_id=(
            active.manifest.generation_id
            if active is not None and active.manifest is not None
            else None
        ),
        candidate_generation_id=candidate.generation_id,
        layers=tuple(planned),
    )


__all__ = [
    "IntakeAction",
    "IntakePlan",
    "LayerChangeReason",
    "LayerDecision",
    "LayerVersion",
    "PlannedLayer",
    "PlannerError",
    "plan_intake",
]
