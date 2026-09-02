"""RED-контракты чистого planner create/content-only/full."""

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
    legacy_generation_view,
    native_generation_view,
)


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
) -> GenerationManifest:
    identity = identity or ExportIdentity.configuration("DemoConfiguration")
    provenance = LayerProvenance(
        profile=LayerSourceProfile.SOURCE_B,
        transport=CandidateTransport.INCOMING,
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
        source_transport=CandidateTransport.INCOMING,
        origin_name=f"{generation_id}.zip",
        raw_sha256=raw * 64,
        layers=tuple(layers),
    )


def _planned(plan, kind: LayerKind):
    return next(layer for layer in plan.layers if layer.kind is kind)


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
            active=native_generation_view(_manifest("generation-old")),
        )


def test_content_only_показывает_structural_diff_но_применяет_только_content():
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
        IntakeAction.UPDATE_CONTENT,
        candidate,
        active=native_generation_view(active),
    )

    assert plan.changed_layers == frozenset(
        {LayerKind.BASE_STRUCTURE, LayerKind.EXTENDED_STRUCTURE, LayerKind.CODE}
    )
    assert plan.applied_layers == frozenset({LayerKind.CODE})
    assert plan.preserved_layers >= {
        LayerKind.BASE_STRUCTURE,
        LayerKind.EXTENDED_STRUCTURE,
    }
    assert _planned(plan, LayerKind.BASE_STRUCTURE).decision is LayerDecision.PRESERVE
    assert _planned(plan, LayerKind.CODE).decision is LayerDecision.APPLY
    assert not plan.no_op


def test_repack_и_physical_hash_не_создают_ложный_full_update():
    IntakeAction = _symbol("IntakeAction")
    plan_intake = _symbol("plan_intake")
    active = _manifest("generation-old", raw="a", physical="c")
    repacked = _manifest("generation-new", raw="b", physical="d")

    plan = plan_intake(
        IntakeAction.UPDATE_FULL,
        repacked,
        active=native_generation_view(active),
    )

    assert plan.changed_layers == frozenset()
    assert plan.applied_layers == frozenset()
    assert plan.no_op


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
        active=native_generation_view(active),
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
            active=native_generation_view(active),
        )


def test_roles_error_заменяет_ready_без_отката_остальных_слоёв():
    IntakeAction = _symbol("IntakeAction")
    plan_intake = _symbol("plan_intake")
    active = _manifest("generation-old")
    candidate = _manifest("generation-new", role_state=LayerState.ERROR)

    plan = plan_intake(
        IntakeAction.UPDATE_CONTENT,
        candidate,
        active=native_generation_view(active),
    )

    assert plan.applied_layers == frozenset({LayerKind.ROLES})
    assert _planned(plan, LayerKind.ROLES).candidate.state is LayerState.ERROR
    assert _planned(plan, LayerKind.CODE).candidate.state is LayerState.READY


def test_legacy_content_update_fail_closed_считает_content_нуждающимся_в_reparse():
    IntakeAction = _symbol("IntakeAction")
    LayerChangeReason = _symbol("LayerChangeReason")
    plan_intake = _symbol("plan_intake")
    identity = ExportIdentity.configuration("DemoConfiguration")
    active = legacy_generation_view(
        identity,
        base_sha256="a" * 64,
        base_items_total=10,
        code_sha256="b" * 64,
        code_items_total=20,
    )

    plan = plan_intake(
        IntakeAction.UPDATE_CONTENT,
        _manifest("generation-new"),
        active=active,
    )

    assert plan.applied_layers == frozenset(
        {LayerKind.CODE, LayerKind.FORMS, LayerKind.ROLES}
    )
    assert {
        _planned(plan, kind).reason
        for kind in (LayerKind.CODE, LayerKind.FORMS, LayerKind.ROLES)
    } == {LayerChangeReason.REPARSE}
    assert _planned(plan, LayerKind.BASE_STRUCTURE).decision.value == "preserve"
