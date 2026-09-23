"""Контракты чистого planner для создания и полного обновления."""

from __future__ import annotations

import importlib
from dataclasses import replace

import pytest

from mcp1c.intake_v2 import (
    CandidateTransport,
    ExportIdentity,
    GenerationManifest,
    LayerKind,
    LayerManifest,
    LayerProvenance,
    LayerSourceProfile,
    LayerState,
)
from mcp1c.intake_v2_registry import (
    native_generation_view,
)
from mcp1c.source_modes import ActivationComponent, ActivationManifest, ActivationMode


SUBJECT = "mcp1c.intake_v2_planner"


def _symbol(name: str):
    try:
        module = importlib.import_module(SUBJECT)
    except ModuleNotFoundError as error:
        if error.name != SUBJECT:
            raise
        pytest.fail(f"RED: отсутствует модуль {SUBJECT} для контракта {name}")
    if not hasattr(module, name):
        pytest.fail(f"RED: в {SUBJECT} отсутствует контракт {name}")
    return getattr(module, name)


def _manifest(
    generation_id: str,
    *,
    identity: ExportIdentity | None = None,
    raw: str = "a",
    parser_version: int = 1,
    selection_version: int = 1,
    changed: frozenset[LayerKind] = frozenset(),
    role_state: LayerState = LayerState.READY,
    physical: str = "c",
    transport: CandidateTransport = CandidateTransport.INCOMING,
) -> GenerationManifest:
    identity = identity or ExportIdentity.configuration("DemoConfiguration")
    provenance = LayerProvenance(
        profile=LayerSourceProfile.SOURCE_B,
        transport=transport,
        origin_name=f"{generation_id}.zip",
        raw_sha256=raw * 64,
        parser_version=parser_version,
        selection_version=selection_version,
    )
    layers = []
    for number, kind in enumerate(LayerKind, 1):
        state = role_state if kind is LayerKind.ROLES else LayerState.READY
        if state is LayerState.ERROR:
            layers.append(
                LayerManifest(
                    kind=kind,
                    state=state,
                    error="непрочитан синтетический Rights.xml",
                    provenance=provenance,
                )
            )
            continue
        content = number + (10 if kind in changed else 0)
        layers.append(
            LayerManifest(
                kind=kind,
                state=state,
                content_sha256=f"{content:064x}",
                payload_sha256=(physical * 64),
                relative_path=f"layers/{kind.value}.json",
                items_total=number,
                provenance=provenance,
            )
        )
    return GenerationManifest(
        format_version=1,
        generation_id=generation_id,
        identity=identity,
        parser_version=parser_version,
        selection_version=selection_version,
        source_transport=transport,
        origin_name=f"{generation_id}.zip",
        raw_sha256=raw * 64,
        layers=tuple(layers),
    )


def _planned(plan, kind: LayerKind):
    return next(layer for layer in plan.layers if layer.kind is kind)


def _active_view(manifest: GenerationManifest):
    activation = ActivationManifest(
        mode=ActivationMode.B_FULL,
        identity_incarnation="inc-1",
        physical_generation_root_id="root-1",
        configuration_version="1.0",
        main=ActivationComponent(
            source="source-b",
            origin=manifest.origin_name,
            raw_sha256=manifest.raw_sha256,
            payload_sha256="a" * 64,
        ),
        extensions=(),
        expected_previous_activation=None,
        transaction_id="tx-1",
        recovery_id="recovery-1",
    )
    return native_generation_view(manifest, activation=activation)


def test_create_применяет_все_пять_слоёв_основной_конфигурации():
    IntakeAction = _symbol("IntakeAction")
    LayerDecision = _symbol("LayerDecision")
    plan_intake = _symbol("plan_intake")
    candidate = _manifest("generation-new")

    plan = plan_intake(IntakeAction.CREATE, candidate, active=None)

    assert plan.identity == candidate.identity
    assert plan.base_generation_id is None
    assert plan.candidate_generation_id == "generation-new"
    assert plan.applied_layers == frozenset(LayerKind)
    assert all(layer.decision is LayerDecision.APPLY for layer in plan.layers)
    assert not plan.no_op


def test_create_не_создаёт_расширение_и_не_перезаписывает_existing():
    IntakeAction = _symbol("IntakeAction")
    PlannerError = _symbol("PlannerError")
    plan_intake = _symbol("plan_intake")
    extension = _manifest(
        "generation-extension",
        identity=ExportIdentity.extension(
            "DemoExtension",
            parent_configuration="DemoConfiguration",
        ),
    )

    with pytest.raises(PlannerError, match="расширен"):
        plan_intake(IntakeAction.CREATE, extension, active=None)

    candidate = _manifest("generation-new")
    with pytest.raises(PlannerError, match="существ"):
        plan_intake(
            IntakeAction.CREATE,
            candidate,
            active=_active_view(_manifest("generation-old")),
        )


def test_intake_содержит_только_create_и_полное_обновление():
    IntakeAction = _symbol("IntakeAction")

    assert {action.value for action in IntakeAction} == {"create", "update_full"}


def test_full_update_применяет_изменённые_структуру_и_содержимое_вместе():
    IntakeAction = _symbol("IntakeAction")
    LayerDecision = _symbol("LayerDecision")
    plan_intake = _symbol("plan_intake")
    active = _manifest("generation-old")
    candidate = _manifest(
        "generation-new",
        raw="b",
        changed=frozenset(
            {LayerKind.BASE_STRUCTURE, LayerKind.EXTENDED_STRUCTURE, LayerKind.CODE}
        ),
    )

    plan = plan_intake(
        IntakeAction.UPDATE_FULL,
        candidate,
        active=_active_view(active),
    )

    assert plan.changed_layers == frozenset(
        {LayerKind.BASE_STRUCTURE, LayerKind.EXTENDED_STRUCTURE, LayerKind.CODE}
    )
    assert plan.applied_layers == frozenset(LayerKind)
    assert _planned(plan, LayerKind.BASE_STRUCTURE).decision is LayerDecision.APPLY
    assert _planned(plan, LayerKind.CODE).decision is LayerDecision.APPLY
    assert not plan.no_op


def test_full_update_пересобирает_ранее_смешанное_b_даже_без_semantic_diff():
    IntakeAction = _symbol("IntakeAction")
    plan_intake = _symbol("plan_intake")
    active = _manifest("generation-old")
    mixed = replace(
        active,
        layers=tuple(
            replace(
                layer,
                provenance=replace(layer.provenance, raw_sha256="b" * 64),
            ) if layer.kind is LayerKind.BASE_STRUCTURE else layer
            for layer in active.layers
        ),
    )
    candidate = _manifest("generation-new")

    plan = plan_intake(
        IntakeAction.UPDATE_FULL,
        candidate,
        active=_active_view(mixed),
    )

    assert plan.changed_layers == frozenset()
    assert plan.applied_layers == frozenset(LayerKind)
    assert not plan.no_op


def test_repack_и_physical_hash_не_создают_ложный_full_update():
    IntakeAction = _symbol("IntakeAction")
    plan_intake = _symbol("plan_intake")
    active = _manifest("generation-old", raw="a", physical="c")
    repacked = _manifest("generation-new", raw="b", physical="d")

    plan = plan_intake(
        IntakeAction.UPDATE_FULL,
        repacked,
        active=_active_view(active),
    )

    assert plan.changed_layers == frozenset()
    assert plan.applied_layers == frozenset()
    assert plan.no_op


def test_legacy_barrier_content_identical_всё_равно_активирует_b_full():
    IntakeAction = _symbol("IntakeAction")
    plan_intake = _symbol("plan_intake")
    active = _manifest("generation-old")
    candidate = _manifest("generation-new")

    plan = plan_intake(
        IntakeAction.UPDATE_FULL,
        candidate,
        active=native_generation_view(active, activation=None, legacy_barrier=True),
    )

    assert plan.applied_layers == frozenset(LayerKind)
    assert not plan.no_op


@pytest.mark.parametrize("transport", list(CandidateTransport))
def test_native_без_activation_не_становится_noop_при_одинаковом_b(transport):
    IntakeAction = _symbol("IntakeAction")
    plan_intake = _symbol("plan_intake")
    active = _manifest("generation-old", transport=transport)
    candidate = _manifest("generation-new", transport=transport)
    plan = plan_intake(
        IntakeAction.UPDATE_FULL,
        candidate,
        active=native_generation_view(active, activation=None),
    )
    assert plan.applied_layers == frozenset(LayerKind)
    assert not plan.no_op


def test_parser_upgrade_требует_reparse_а_downgrade_отклоняется():
    IntakeAction = _symbol("IntakeAction")
    LayerChangeReason = _symbol("LayerChangeReason")
    PlannerError = _symbol("PlannerError")
    plan_intake = _symbol("plan_intake")
    active = _manifest("generation-old", parser_version=2, selection_version=3)
    upgraded = _manifest("generation-new", parser_version=3, selection_version=4)

    plan = plan_intake(
        IntakeAction.UPDATE_FULL,
        upgraded,
        active=_active_view(active),
    )

    assert plan.applied_layers == frozenset(LayerKind)
    assert all(
        layer.reason is LayerChangeReason.REPARSE for layer in plan.layers
    )
    with pytest.raises(PlannerError, match="старее"):
        plan_intake(
            IntakeAction.UPDATE_FULL,
            _manifest(
                "generation-stale",
                parser_version=1,
                selection_version=2,
                changed=frozenset({LayerKind.CODE}),
            ),
            active=_active_view(active),
        )


def test_b_full_отклоняет_неготовые_роли_вместо_частичной_публикации():
    IntakeAction = _symbol("IntakeAction")
    plan_intake = _symbol("plan_intake")
    PlannerError = _symbol("PlannerError")
    active = _manifest("generation-old")
    candidate = _manifest("generation-new", role_state=LayerState.ERROR)

    with pytest.raises(PlannerError, match="roles"):
        plan_intake(
            IntakeAction.UPDATE_FULL,
            candidate,
            active=_active_view(active),
        )


def test_a_only_разрешает_schema_v1_без_ролей_и_не_содержит_extensions():
    IntakeAction = _symbol("IntakeAction")
    plan_intake = _symbol("plan_intake")
    candidate = _manifest("a-only")
    provenance = candidate.layers[0].provenance
    assert provenance is not None
    a_provenance = replace(provenance, profile=LayerSourceProfile.SCHEMA_V1)
    layers = tuple(
        replace(
            layer,
            provenance=a_provenance,
            state=(LayerState.UNAVAILABLE if layer.kind is LayerKind.ROLES else layer.state),
            content_sha256=("" if layer.kind is LayerKind.ROLES else layer.content_sha256),
            payload_sha256=("" if layer.kind is LayerKind.ROLES else layer.payload_sha256),
            relative_path=("" if layer.kind is LayerKind.ROLES else layer.relative_path),
            items_total=(0 if layer.kind is LayerKind.ROLES else layer.items_total),
        )
        for layer in candidate.layers
    )
    a_candidate = replace(candidate, layers=layers)
    plan = plan_intake(IntakeAction.CREATE, a_candidate, active=None)
    assert plan.mode is ActivationMode.A_ONLY


def test_b_full_не_разрешает_full_план_с_неготовыми_ролями():
    IntakeAction = _symbol("IntakeAction")
    plan_intake = _symbol("plan_intake")
    PlannerError = _symbol("PlannerError")
    candidate = _manifest("b-incomplete", role_state=LayerState.ERROR)
    with pytest.raises(PlannerError, match="roles|ролей"):
        plan_intake(IntakeAction.CREATE, candidate, active=None)
