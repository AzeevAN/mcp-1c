"""Сборка независимого generation bundle по решению чистого planner."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from .intake_v2 import GenerationManifest, LayerKind, LayerManifest, LayerState
from .intake_v2_generation import MaterializedGeneration
from .intake_v2_planner import IntakeAction, IntakePlan, LayerDecision
from .intake_v2_registry import LayerPayloadSource


class CompositionError(ValueError):
    """План нельзя доказуемо собрать из candidate и active bundle."""


@dataclass(frozen=True, slots=True)
class ComposedGeneration:
    manifest: GenerationManifest
    payloads: Mapping[LayerKind, LayerPayloadSource]

    def __post_init__(self) -> None:
        object.__setattr__(self, "payloads", MappingProxyType(dict(self.payloads)))


def _layer_map(manifest: GenerationManifest) -> dict[LayerKind, LayerManifest]:
    return {layer.kind: layer for layer in manifest.layers}


def compose_generation(
    plan: IntakePlan,
    candidate: MaterializedGeneration,
    *,
    active_manifest: GenerationManifest | None,
    active_payloads: Mapping[LayerKind, LayerPayloadSource],
) -> ComposedGeneration | None:
    """Выбрать candidate/preserved layers; сами bytes копирует bundle store."""
    if not isinstance(plan, IntakePlan):
        raise TypeError("plan должен быть IntakePlan")
    if not isinstance(candidate, MaterializedGeneration):
        raise TypeError("candidate должен быть MaterializedGeneration")
    if candidate.manifest.identity != plan.identity or (
        candidate.manifest.generation_id != plan.candidate_generation_id
    ):
        raise CompositionError("candidate не совпадает с планом")
    candidate_layers = _layer_map(candidate.manifest)
    if any(
        planned.candidate != candidate_layers.get(planned.kind)
        for planned in plan.layers
    ):
        raise CompositionError("слои candidate изменились после preview")
    if plan.action is IntakeAction.CREATE:
        if active_manifest is not None or plan.base_generation_id is not None:
            raise CompositionError("create не принимает active generation")
        active_layers: dict[LayerKind, LayerManifest] = {}
    else:
        if active_manifest is None:
            raise CompositionError(
                "legacy update требует legacy publisher до полной миграции"
            )
        if (
            active_manifest.identity != plan.identity
            or active_manifest.generation_id != plan.base_generation_id
        ):
            raise CompositionError("active generation не совпадает с preview")
        active_layers = _layer_map(active_manifest)

    if plan.no_op:
        return None

    layers: list[LayerManifest] = []
    payloads: dict[LayerKind, LayerPayloadSource] = {}
    for planned in plan.layers:
        if planned.decision is LayerDecision.APPLY:
            layer = candidate_layers[planned.kind]
            source = candidate.payloads.get(planned.kind)
        else:
            layer = active_layers.get(planned.kind)
            if layer is None:
                raise CompositionError(
                    f"{planned.kind.value}: отсутствует preserved active слой"
                )
            source = active_payloads.get(planned.kind)
        layers.append(layer)
        if layer.state is LayerState.READY:
            if source is None:
                raise CompositionError(
                    f"{planned.kind.value}: ready слой не имеет payload source"
                )
            payloads[planned.kind] = source
        elif source is not None:
            raise CompositionError(
                f"{planned.kind.value}: error/unavailable не принимает payload"
            )

    manifest = GenerationManifest(
        format_version=candidate.manifest.format_version,
        generation_id=candidate.manifest.generation_id,
        identity=candidate.manifest.identity,
        parser_version=candidate.manifest.parser_version,
        selection_version=candidate.manifest.selection_version,
        source_transport=candidate.manifest.source_transport,
        origin_name=candidate.manifest.origin_name,
        raw_sha256=candidate.manifest.raw_sha256,
        layers=tuple(layers),
    )
    return ComposedGeneration(manifest, payloads)


__all__ = [
    "ComposedGeneration",
    "CompositionError",
    "compose_generation",
]
