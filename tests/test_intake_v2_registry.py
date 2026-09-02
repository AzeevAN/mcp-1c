"""RED-контракты атомарного generation bundle в Registry.

Все payload синтетические и живут только в ``tmp_path``. Проверки не
обращаются к рабочему ``data/`` и не подключают новый intake к действующим
операциям Registry до появления единого publisher.
"""

from __future__ import annotations

import importlib
import json
from dataclasses import replace

import pytest

from conftest import build_configuration, write_export
from mcp1c.registry import Registry


SUBJECT = "mcp1c.intake_v2_registry"


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


def _manifest(tmp_path, generation_id: str, *, suffix: str = ""):
    CandidateTransport = _symbol("CandidateTransport")
    ExportIdentity = _symbol("ExportIdentity")
    GenerationManifest = _symbol("GenerationManifest")
    LayerKind = _symbol("LayerKind")
    LayerManifest = _symbol("LayerManifest")
    LayerPayload = _symbol("LayerPayload")
    LayerPayloadSource = _symbol("LayerPayloadSource")
    LayerState = _symbol("LayerState")
    hash_layer_payload = _symbol("hash_layer_payload")
    hash_layer_semantic = _symbol("hash_layer_semantic")

    base = tmp_path / f"base{suffix}.json"
    code = tmp_path / f"code{suffix}.json"
    base_semantic = {
        "name": "DemoConfiguration",
        "synonym": f"Demo{suffix}",
        "version": "1.0",
        "vendor": "Example",
        "schema_version": "1",
        "objects": [],
    }
    code_semantic = {"modules": []}
    base.write_bytes(
        LayerPayload(LayerKind.BASE_STRUCTURE, base_semantic).to_json_bytes()
    )
    code.write_bytes(LayerPayload(LayerKind.CODE, code_semantic).to_json_bytes())
    manifest = GenerationManifest(
        format_version=1,
        generation_id=generation_id,
        identity=ExportIdentity.configuration("DemoConfiguration"),
        parser_version=1,
        selection_version=1,
        source_transport=CandidateTransport.INCOMING,
        origin_name=f"demo{suffix}.zip",
        raw_sha256=("a" if not suffix else "b") * 64,
        layers=(
            LayerManifest(
                kind=LayerKind.BASE_STRUCTURE,
                state=LayerState.READY,
                content_sha256=hash_layer_semantic(
                    LayerKind.BASE_STRUCTURE, base_semantic
                ),
                payload_sha256=hash_layer_payload(LayerKind.BASE_STRUCTURE, base),
                relative_path="layers/base-structure.json",
                items_total=1,
            ),
            LayerManifest(
                kind=LayerKind.EXTENDED_STRUCTURE,
                state=LayerState.UNAVAILABLE,
            ),
            LayerManifest(
                kind=LayerKind.FORMS,
                state=LayerState.UNAVAILABLE,
            ),
            LayerManifest(
                kind=LayerKind.CODE,
                state=LayerState.READY,
                content_sha256=hash_layer_semantic(LayerKind.CODE, code_semantic),
                payload_sha256=hash_layer_payload(LayerKind.CODE, code),
                relative_path="layers/code.json",
                items_total=0,
            ),
            LayerManifest(
                kind=LayerKind.ROLES,
                state=LayerState.ERROR,
                error="непрочитан синтетический Rights.xml",
            ),
        ),
    )
    return manifest, {
        LayerKind.BASE_STRUCTURE: LayerPayloadSource(base),
        LayerKind.CODE: LayerPayloadSource(code),
    }


def test_stage_проверяет_каждый_ready_payload_и_не_хранит_error_payload(tmp_path):
    BundleStoreError = _symbol("BundleStoreError")
    GenerationBundleStore = _symbol("GenerationBundleStore")
    LayerKind = _symbol("LayerKind")

    manifest, payloads = _manifest(tmp_path, "generation-001")
    store = GenerationBundleStore(tmp_path / "data")
    staged = store.stage(manifest, payloads)

    assert staged.manifest == manifest
    assert staged.root.name.startswith(".staging-generation-001-")
    assert (staged.root / "manifest.json").read_bytes() == manifest.to_json_bytes()
    assert not (staged.root / "layers/roles.json").exists()
    assert store.verify(staged) == manifest

    (staged.root / "layers/code.json").write_bytes(b"changed")
    with pytest.raises(BundleStoreError, match="code.*контрольная сумма"):
        store.verify(staged)

    extra = tmp_path / "extra.json"
    extra.write_bytes(b"{}")
    with pytest.raises(BundleStoreError, match="лишн|точно"):
        store.stage(manifest, {**payloads, LayerKind.ROLES: extra})


def test_stage_не_следует_по_symlink_generation_root(tmp_path):
    BundleStoreError = _symbol("BundleStoreError")
    GenerationBundleStore = _symbol("GenerationBundleStore")
    manifest, payloads = _manifest(tmp_path, "generation-001")
    data = tmp_path / "data"
    outside = tmp_path / "outside"
    data.mkdir()
    outside.mkdir()
    (data / "generations").symlink_to(outside, target_is_directory=True)

    with pytest.raises(BundleStoreError, match="symlink"):
        GenerationBundleStore(data).stage(manifest, payloads)
    assert not list(outside.iterdir())


def test_stage_не_позволяет_слою_подменить_manifest(tmp_path):
    BundleStoreError = _symbol("BundleStoreError")
    GenerationBundleStore = _symbol("GenerationBundleStore")
    manifest, payloads = _manifest(tmp_path, "generation-001")
    layers = tuple(manifest.layers)
    layers = (
        replace(layers[0], relative_path="manifest.json"),
        *layers[1:],
    )

    with pytest.raises(BundleStoreError, match="layers"):
        GenerationBundleStore(tmp_path / "data").stage(
            replace(manifest, layers=layers), payloads
        )


def test_publish_переключает_pointer_и_оставляет_только_active_generation(tmp_path):
    LayerKind = _symbol("LayerKind")
    first, first_payloads = _manifest(tmp_path, "generation-001")
    second, second_payloads = _manifest(
        tmp_path, "generation-002", suffix="-changed"
    )
    registry = Registry(tmp_path / "data")

    registry.publish_generation(
        registry.stage_generation(first, first_payloads)
    )
    first_pointer = registry.active_generation_pointer(first.identity)
    assert registry.active_generation(first.identity) == first

    registry.publish_generation(
        registry.stage_generation(second, second_payloads)
    )
    second_pointer = registry.active_generation_pointer(second.identity)

    assert second_pointer.generation_id == "generation-002"
    assert registry.active_generation(second.identity) == second
    assert not (registry.data_dir / first_pointer.root_path).exists()
    assert (registry.data_dir / second_pointer.root_path).is_dir()
    assert not list((registry.data_dir / "generations").glob(".staging-*"))

    raw = json.loads(registry.registry_path.read_text(encoding="utf-8"))
    assert len(raw["generation_manifests"]) == 1
    assert raw["generation_manifests"][0] == second_pointer.to_dict()
    assert set(raw["generation_manifests"][0]) == {
        "identity",
        "generation_id",
        "manifest_path",
        "manifest_sha256",
        "root_path",
    }
    assert LayerKind.ROLES.value not in raw["generation_manifests"][0]

    restarted = Registry(registry.data_dir)
    assert restarted.restore() == []
    assert restarted.active_generation(second.identity) == second


def test_registry_snapshot_фиксирует_одну_пару_pointer_manifest(tmp_path):
    manifest, payloads = _manifest(tmp_path, "generation-001")
    registry = Registry(tmp_path / "data")
    registry.publish_generation(registry.stage_generation(manifest, payloads))

    snapshot = registry.snapshot()
    generation = snapshot.generations[manifest.identity.grouping_key]

    assert generation.pointer == registry.active_generation_pointer(manifest.identity)
    assert generation.manifest == manifest
    assert generation.pointer.generation_id == generation.manifest.generation_id
    with pytest.raises(TypeError):
        snapshot.generations[manifest.identity.grouping_key] = generation


def test_generation_publish_инвалидирует_старый_registry_snapshot(tmp_path):
    first, first_payloads = _manifest(tmp_path, "generation-001")
    second, second_payloads = _manifest(
        tmp_path, "generation-002", suffix="-changed"
    )
    registry = Registry(tmp_path / "data")
    registry.publish_generation(registry.stage_generation(first, first_payloads))
    old_snapshot = registry.snapshot()

    registry.publish_generation(registry.stage_generation(second, second_payloads))
    new_snapshot = registry.snapshot()

    assert not registry.snapshot_is_current(old_snapshot)
    assert (
        old_snapshot.generations[first.identity.grouping_key].manifest.generation_id
        == "generation-001"
    )
    assert (
        new_snapshot.generations[first.identity.grouping_key].manifest.generation_id
        == "generation-002"
    )


def test_publish_до_pointer_failure_восстанавливает_старое_поколение(
    tmp_path, monkeypatch
):
    RecoveryBlocked = _symbol("RecoveryBlocked")
    first, first_payloads = _manifest(tmp_path, "generation-001")
    second, second_payloads = _manifest(
        tmp_path, "generation-002", suffix="-changed"
    )
    registry = Registry(tmp_path / "data")
    registry.publish_generation(registry.stage_generation(first, first_payloads))
    old_pointer = registry.active_generation_pointer(first.identity)
    staged = registry.stage_generation(second, second_payloads)

    def crash_before_pointer(*_args, **_kwargs):
        raise SystemExit("synthetic crash")

    monkeypatch.setattr(registry, "_write_registry_payload", crash_before_pointer)
    with pytest.raises(SystemExit, match="synthetic crash"):
        registry.publish_generation(staged)

    restarted = Registry(registry.data_dir)
    assert restarted.recover_generation_publish() == [
        "generation generation-002: staging откачен"
    ]
    assert restarted.restore() == []
    assert restarted.active_generation(first.identity) == first
    assert (registry.data_dir / old_pointer.root_path).is_dir()
    assert not (registry.data_dir / staged.pointer.root_path).exists()
    assert not restarted.generation_recovery_path.exists()

    restarted.generation_recovery_path.write_text(
        json.dumps(
            {
                "previous": old_pointer.to_dict(),
                "staged": staged.pointer.to_dict(),
                "phase": "pointer_switched",
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(RecoveryBlocked, match="неоднознач"):
        restarted.recover_generation_publish()


def test_publish_обычный_отказ_pointer_сразу_откатывает_staging(
    tmp_path, monkeypatch
):
    first, first_payloads = _manifest(tmp_path, "generation-001")
    second, second_payloads = _manifest(
        tmp_path, "generation-002", suffix="-changed"
    )
    registry = Registry(tmp_path / "data")
    registry.publish_generation(registry.stage_generation(first, first_payloads))
    staged = registry.stage_generation(second, second_payloads)

    def fail_pointer(*_args, **_kwargs):
        raise OSError("synthetic write failure")

    monkeypatch.setattr(registry, "_write_registry_payload", fail_pointer)
    with pytest.raises(OSError, match="synthetic write failure"):
        registry.publish_generation(staged)

    assert registry.active_generation(first.identity) == first
    assert not (registry.data_dir / staged.pointer.root_path).exists()
    assert not registry.generation_recovery_path.exists()


def test_publish_после_pointer_failure_завершает_новое_поколение(
    tmp_path, monkeypatch
):
    first, first_payloads = _manifest(tmp_path, "generation-001")
    second, second_payloads = _manifest(
        tmp_path, "generation-002", suffix="-changed"
    )
    registry = Registry(tmp_path / "data")
    registry.publish_generation(registry.stage_generation(first, first_payloads))
    old_pointer = registry.active_generation_pointer(first.identity)
    staged = registry.stage_generation(second, second_payloads)

    def crash_after_pointer(_checkpoint):
        raise SystemExit("synthetic crash")

    monkeypatch.setattr(registry, "_after_generation_pointer_switch", crash_after_pointer)
    with pytest.raises(SystemExit, match="synthetic crash"):
        registry.publish_generation(staged)

    restarted = Registry(registry.data_dir)
    assert restarted.recover_generation_publish() == [
        "generation generation-002: публикация завершена"
    ]
    assert restarted.restore() == []
    assert restarted.active_generation(second.identity) == second
    assert not (registry.data_dir / old_pointer.root_path).exists()
    assert (registry.data_dir / staged.pointer.root_path).is_dir()


def test_restore_повреждённого_active_manifest_fail_closed_до_payload(tmp_path):
    BundleStoreError = _symbol("BundleStoreError")
    manifest, payloads = _manifest(tmp_path, "generation-001")
    registry = Registry(tmp_path / "data")
    registry.publish_generation(registry.stage_generation(manifest, payloads))
    pointer = registry.active_generation_pointer(manifest.identity)
    manifest_path = registry.data_dir / pointer.manifest_path
    manifest_path.write_bytes(b"{}")

    restarted = Registry(registry.data_dir)
    with pytest.raises(BundleStoreError, match="manifest.*контрольная сумма"):
        restarted.restore()
    assert restarted.active_generation_pointer(manifest.identity) is None


def test_legacy_view_ничего_не_выдумывает_и_не_создаёт_generation(tmp_path):
    GenerationOrigin = _symbol("GenerationOrigin")
    LayerKind = _symbol("LayerKind")
    LayerState = _symbol("LayerState")
    data = tmp_path / "data"
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    registry = Registry(data)
    registry.add_configuration(
        write_export(incoming, build_configuration(name="LegacyConfiguration"))
    )
    registry.save()

    restarted = Registry(data)
    assert restarted.restore() == []
    view = restarted.generation_view("LegacyConfiguration")
    snapshot_view = restarted.snapshot().generation_view("LegacyConfiguration")

    assert view.origin is GenerationOrigin.LEGACY
    assert snapshot_view == view
    assert view.manifest is None
    assert view.layers[LayerKind.BASE_STRUCTURE].state is LayerState.READY
    assert view.layers[LayerKind.EXTENDED_STRUCTURE].state is LayerState.UNAVAILABLE
    assert view.layers[LayerKind.CODE].state is LayerState.UNAVAILABLE
    assert view.layers[LayerKind.FORMS].state is LayerState.UNAVAILABLE
    assert view.layers[LayerKind.ROLES].state is LayerState.UNAVAILABLE
    assert not (data / "generations").exists()
    assert "generation_manifests" not in json.loads(
        registry.registry_path.read_text(encoding="utf-8")
    )
