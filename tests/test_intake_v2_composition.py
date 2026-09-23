"""RED композиции результата planner и CAS-публикации."""

from __future__ import annotations

import importlib
import shutil
from dataclasses import replace

import pytest

from mcp1c.intake_v2 import LayerKind
from mcp1c.intake_v2_converter import convert_collection
from mcp1c.intake_v2_generation import materialize_generation
from mcp1c.intake_v2_planner import IntakeAction, LayerDecision, plan_intake
from mcp1c.intake_v2_registry import legacy_generation_view, native_generation_view
from mcp1c.registry import Registry, RegistryError
from test_intake_v2_converter import _collection


SUBJECT = "mcp1c.intake_v2_composition"


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


def _materialized(tmp_path, name: str, *, module: bytes | None = None):
    collection = _collection(
        tmp_path / f"source-{name}",
        common_forms=True,
        common_form_module=module,
    )
    return materialize_generation(
        collection,
        convert_collection(collection),
        tmp_path / f"materialized-{name}",
        generation_id=f"generation-{name}",
    )


def _layers(manifest):
    return {layer.kind: layer for layer in manifest.layers}


def test_full_composition_берёт_все_слои_из_одного_candidate(tmp_path):
    compose_generation = _symbol("compose_generation")
    active = _materialized(tmp_path, "active")
    candidate = _materialized(
        tmp_path,
        "candidate",
        module="Процедура Changed()\nКонецПроцедуры\n".encode(),
    )
    registry = Registry(tmp_path / "data")
    registry.publish_generation(
        registry.stage_generation(active.manifest, active.payloads)
    )
    old_pointer = registry.active_generation_pointer(active.manifest.identity)
    plan = plan_intake(
        IntakeAction.UPDATE_FULL,
        candidate.manifest,
        active=native_generation_view(active.manifest),
    )

    composed = compose_generation(
        plan,
        candidate,
        active_manifest=active.manifest,
        active_payloads=registry.generation_payload_sources(old_pointer),
    )
    staged = registry.stage_generation(composed.manifest, composed.payloads)
    registry.publish_generation(staged, expected_previous=old_pointer)
    new_pointer = registry.active_generation_pointer(active.manifest.identity)

    final = registry.active_generation(active.manifest.identity)
    assert _layers(final)[LayerKind.BASE_STRUCTURE] == _layers(candidate.manifest)[
        LayerKind.BASE_STRUCTURE
    ]
    assert _layers(final)[LayerKind.CODE] == _layers(candidate.manifest)[
        LayerKind.CODE
    ]
    assert {layer.provenance.raw_sha256 for layer in final.layers} == {
        candidate.manifest.raw_sha256
    }
    assert not (registry.data_dir / old_pointer.root_path).exists()
    shutil.rmtree(active.root)
    shutil.rmtree(candidate.root)
    assert Registry(registry.data_dir).restore() == []
    assert (registry.data_dir / new_pointer.root_path / "members.pack").is_file()
    assert (registry.data_dir / new_pointer.root_path / "members.index.json").is_file()
    assert not (registry.data_dir / new_pointer.root_path / "payload").exists()


def test_composition_отклоняет_неполный_b_full_план(tmp_path):
    compose_manifest = _symbol("compose_manifest")
    CompositionError = _symbol("CompositionError")
    active = _materialized(tmp_path, "active")
    candidate = _materialized(
        tmp_path,
        "candidate",
        module="Процедура Changed()\nКонецПроцедуры\n".encode(),
    )
    plan = plan_intake(
        IntakeAction.UPDATE_FULL,
        candidate.manifest,
        active=native_generation_view(active.manifest),
    )
    partial = replace(
        plan,
        layers=tuple(
            replace(layer, decision=LayerDecision.PRESERVE)
            if layer.kind is LayerKind.BASE_STRUCTURE else layer
            for layer in plan.layers
        ),
    )

    with pytest.raises(CompositionError, match="полный комплект"):
        compose_manifest(
            partial,
            candidate.manifest,
            active_manifest=active.manifest,
        )


def test_noop_plan_не_создаёт_composed_generation(tmp_path):
    compose_generation = _symbol("compose_generation")
    active = _materialized(tmp_path, "active")
    candidate = _materialized(tmp_path, "candidate")
    plan = plan_intake(
        IntakeAction.UPDATE_FULL,
        candidate.manifest,
        active=native_generation_view(active.manifest),
    )

    assert plan.no_op
    assert compose_generation(
        plan,
        candidate,
        active_manifest=active.manifest,
        active_payloads=active.payloads,
    ) is None


def test_expected_pointer_CAS_не_затирает_конкурентную_публикацию(tmp_path):
    active = _materialized(tmp_path, "active")
    candidate = _materialized(
        tmp_path,
        "candidate",
        module="Процедура Candidate()\nКонецПроцедуры\n".encode(),
    )
    competitor = _materialized(
        tmp_path,
        "competitor",
        module="Процедура Competitor()\nКонецПроцедуры\n".encode(),
    )
    registry = Registry(tmp_path / "data")
    registry.publish_generation(
        registry.stage_generation(active.manifest, active.payloads)
    )
    expected = registry.active_generation_pointer(active.manifest.identity)
    stale = registry.stage_generation(candidate.manifest, candidate.payloads)
    registry.publish_generation(
        registry.stage_generation(competitor.manifest, competitor.payloads)
    )

    with pytest.raises(RegistryError, match="active.*измен"):
        registry.publish_generation(stale, expected_previous=expected)

    assert registry.active_generation(active.manifest.identity) == competitor.manifest
    assert not stale.root.exists()


def test_full_update_мигрирует_legacy_когда_все_слои_берутся_из_candidate(tmp_path):
    compose_generation = _symbol("compose_generation")
    candidate = _materialized(tmp_path, "candidate")
    legacy = legacy_generation_view(
        candidate.manifest.identity,
        base_sha256="a" * 64,
        base_items_total=10,
        code_sha256="b" * 64,
        code_items_total=20,
    )
    plan = plan_intake(
        IntakeAction.UPDATE_FULL,
        candidate.manifest,
        active=legacy,
    )

    assert plan.preserved_layers == set()
    composed = compose_generation(
        plan,
        candidate,
        active_manifest=None,
        active_payloads={},
    )

    assert composed.manifest == candidate.manifest
    assert composed.payloads == candidate.payloads
