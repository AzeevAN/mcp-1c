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
from mcp1c.source_modes import ActivationComponent, ActivationManifest, ActivationMode
from test_intake_v2_extensions import _materialized, _stage_active_base


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


def test_relative_data_root_нормализуется_до_staging(tmp_path, monkeypatch):
    registry_root = tmp_path / "data"
    monkeypatch.chdir(tmp_path)

    registry = Registry("data")

    assert registry.data_dir == registry_root.resolve()


def test_generation_recovery_reads_legacy_record_without_detached(tmp_path):
    GenerationRecovery = _symbol("GenerationRecovery")
    RecoveryPhase = _symbol("RecoveryPhase")
    manifest, payloads = _manifest(tmp_path, "generation-legacy-recovery")
    registry = Registry(tmp_path / "data")
    staged = registry.stage_generation(manifest, payloads)

    recovery = GenerationRecovery.from_dict(
        {
            "previous": None,
            "staged": staged.pointer.to_dict(),
            "phase": RecoveryPhase.PREPARED.value,
        }
    )

    assert recovery.detached == ()
    assert "detached" not in recovery.to_dict()


@pytest.mark.parametrize("extension_count", [0, 1, 2])
def test_crash_recovery_removes_exact_detached_extension_roots(
    tmp_path, monkeypatch, extension_count
):
    _base_collection, base = _materialized(tmp_path, "recovery-base")
    registry = Registry(tmp_path / "data")
    registry.publish_generation(
        _stage_active_base(registry, base)
    )
    child_pointers = []
    for index in range(extension_count):
        _collection, extension = _materialized(
            tmp_path,
            f"recovery-extension-{index}",
            configuration_name=f"RecoveryExtension{index}",
            extension=True,
        )
        pointer = registry.publish_generation(
            registry.stage_generation(extension.manifest, extension.payloads)
        )
        child_pointers.append(pointer)

    unrelated_root = (
        registry.data_dir
        / "generations"
        / ("f" * 64)
        / "unrelated-generation"
    )
    unrelated_root.mkdir(parents=True)
    (unrelated_root / "keep.txt").write_text("keep", encoding="utf-8")

    source = tmp_path / "source-a"
    source.mkdir()
    exported = write_export(source, build_configuration(name="DemoConfiguration"))

    def crash_after_switch(_checkpoint):
        raise SystemExit("synthetic crash after durable pointer switch")

    monkeypatch.setattr(
        registry, "_after_generation_pointer_switch", crash_after_switch
    )
    with pytest.raises(SystemExit, match="synthetic crash"):
        registry.add_configuration(exported, keep_source=False)

    recovery = registry._generation_store.read_recovery()
    assert recovery is not None
    assert recovery.detached == tuple(child_pointers)

    restarted = Registry(registry.data_dir)
    restarted.recover_generation_publish()
    assert restarted.restore() == []
    assert unrelated_root.is_dir()
    for child in child_pointers:
        assert not (registry.data_dir / child.root_path).exists()


def test_detached_root_cleanup_failure_is_retried_idempotently(
    tmp_path, monkeypatch
):
    _base_collection, base = _materialized(tmp_path, "retry-base")
    registry = Registry(tmp_path / "data")
    registry.publish_generation(
        _stage_active_base(registry, base)
    )
    children = []
    for index in range(2):
        _collection, extension = _materialized(
            tmp_path,
            f"retry-extension-{index}",
            configuration_name=f"RetryExtension{index}",
            extension=True,
        )
        children.append(
            registry.publish_generation(
                registry.stage_generation(extension.manifest, extension.payloads)
            )
        )
    source = tmp_path / "retry-source-a"
    source.mkdir()

    def crash_after_switch(_checkpoint):
        raise SystemExit("synthetic retry crash")

    monkeypatch.setattr(
        registry, "_after_generation_pointer_switch", crash_after_switch
    )
    with pytest.raises(SystemExit, match="synthetic retry crash"):
        registry.add_configuration(
            write_export(source, build_configuration(name="DemoConfiguration")),
            keep_source=False,
        )

    restarted = Registry(registry.data_dir)
    original_remove = restarted._generation_store.remove_pointer_root
    failed = False

    def fail_second_once(pointer):
        nonlocal failed
        if pointer == children[1] and not failed:
            failed = True
            raise OSError("synthetic detached cleanup failure")
        original_remove(pointer)

    monkeypatch.setattr(
        restarted._generation_store, "remove_pointer_root", fail_second_once
    )
    with pytest.raises(OSError, match="synthetic detached cleanup failure"):
        restarted.recover_generation_publish()

    assert restarted.generation_recovery_path.is_file()
    assert not (registry.data_dir / children[0].root_path).exists()
    assert (registry.data_dir / children[1].root_path).is_dir()

    monkeypatch.setattr(
        restarted._generation_store, "remove_pointer_root", original_remove
    )
    restarted.recover_generation_publish()
    assert not restarted.generation_recovery_path.exists()
    assert all(
        not (registry.data_dir / child.root_path).exists() for child in children
    )


def test_pre_switch_rollback_preserves_recorded_detached_root(
    tmp_path, monkeypatch
):
    _base_collection, base = _materialized(tmp_path, "rollback-base")
    _extension_collection, extension = _materialized(
        tmp_path,
        "rollback-extension",
        configuration_name="RollbackExtension",
        extension=True,
    )
    registry = Registry(tmp_path / "data")
    registry.publish_generation(
        _stage_active_base(registry, base)
    )
    child = registry.publish_generation(
        registry.stage_generation(extension.manifest, extension.payloads)
    )
    child_root = registry.data_dir / child.root_path
    source = tmp_path / "rollback-source-a"
    source.mkdir()

    def crash_before_switch(*_args, **_kwargs):
        raise SystemExit("synthetic crash before pointer switch")

    monkeypatch.setattr(registry, "_write_registry_payload", crash_before_switch)
    with pytest.raises(SystemExit, match="synthetic crash before pointer switch"):
        registry.add_configuration(
            write_export(source, build_configuration(name="DemoConfiguration")),
            keep_source=False,
        )

    recovery = registry._generation_store.read_recovery()
    assert recovery is not None
    assert recovery.detached == (child,)

    restarted = Registry(registry.data_dir)
    restarted.recover_generation_publish()
    assert restarted.restore() == []
    assert child_root.is_dir()
    assert restarted.active_generation_pointer(extension.manifest.identity) == child


def test_publish_atomic_root_persists_activation_manifest(tmp_path):
    registry = Registry(tmp_path / "data")
    manifest, payloads = _manifest(tmp_path, "generation-activation")
    activation = ActivationManifest(
        mode=ActivationMode.B_FULL,
        identity_incarnation="inc-1",
        physical_generation_root_id="root-1",
        configuration_version="1.0",
        main=ActivationComponent(
            source="source-b",
            origin="demo.zip",
            raw_sha256="a" * 64,
            payload_sha256="b" * 64,
        ),
        extensions=(),
        expected_previous_activation=None,
        transaction_id="tx-1",
        recovery_id="recovery-1",
    )
    staged = registry.stage_generation(manifest, payloads, activation=activation)
    registry.publish_generation(staged)
    restarted = Registry(tmp_path / "data")
    assert restarted.restore() == []
    assert restarted.active_generation_pointer(manifest.identity).activation == activation


def test_recovery_after_switch_keeps_activation_and_removes_previous_root(
    tmp_path, monkeypatch
):
    first, first_payloads = _manifest(tmp_path, "generation-crash-001")
    second, second_payloads = _manifest(
        tmp_path, "generation-crash-002", suffix="-changed"
    )
    registry = Registry(tmp_path / "data")
    registry.publish_generation(registry.stage_generation(first, first_payloads))
    activation = ActivationManifest(
        mode=ActivationMode.B_FULL,
        identity_incarnation="inc-crash",
        physical_generation_root_id="root-crash-2",
        configuration_version="2.0",
        main=ActivationComponent(
            source="source-b",
            origin="changed.zip",
            raw_sha256="b" * 64,
            payload_sha256="c" * 64,
        ),
        extensions=(),
        expected_previous_activation=None,
        transaction_id="tx-crash",
        recovery_id="recovery-crash",
    )
    staged = registry.stage_generation(
        second, second_payloads, activation=activation
    )

    def crash_after_switch(_checkpoint):
        raise SystemExit("synthetic crash after switch")

    monkeypatch.setattr(registry, "_after_generation_pointer_switch", crash_after_switch)
    with pytest.raises(SystemExit, match="synthetic crash after switch"):
        registry.publish_generation(staged)

    restarted = Registry(registry.data_dir)
    assert restarted.recover_generation_publish() == [
        "generation generation-crash-002: публикация завершена"
    ]
    assert restarted.restore() == []
    pointer = restarted.active_generation_pointer(second.identity)
    assert pointer is not None and pointer.activation == activation
    assert not (registry.data_dir / f"generations/{_symbol('_identity_digest')(first.identity)}/{first.generation_id}").exists()


def test_delete_recreate_rejects_stale_activation_preview(tmp_path):
    registry = Registry(tmp_path / "data")
    first, first_payloads = _manifest(tmp_path, "generation-aba-001")
    stale, stale_payloads = _manifest(
        tmp_path, "generation-aba-stale", suffix="-stale"
    )
    recreated, recreated_payloads = _manifest(
        tmp_path, "generation-aba-recreated", suffix="-recreated"
    )
    def activation(incarnation, root, version, raw, payload):
        return ActivationManifest(
            mode=ActivationMode.B_FULL,
            identity_incarnation=incarnation,
            physical_generation_root_id=root,
            configuration_version=version,
            main=ActivationComponent(
                source="source-b",
                origin=f"{incarnation}.zip",
                raw_sha256=raw * 64,
                payload_sha256=payload * 64,
            ),
            extensions=(),
            expected_previous_activation=None,
            transaction_id=f"tx-{incarnation}",
            recovery_id=f"recovery-{incarnation}",
        )

    registry.publish_generation(
        registry.stage_generation(
            first,
            first_payloads,
            activation=activation("inc-old", "root-old", "1", "a", "b"),
        )
    )
    old_pointer = registry.active_generation_pointer(first.identity)
    stale_staged = registry.stage_generation(
        stale,
        stale_payloads,
        activation=activation("inc-stale", "root-stale", "2", "c", "d"),
    )
    registry.remove("DemoConfiguration")
    registry.publish_generation(
        registry.stage_generation(
            recreated,
            recreated_payloads,
            activation=activation("inc-new", "root-new", "3", "e", "f"),
        )
    )
    with pytest.raises(Exception, match="ожидаем|изменил|stale|устар"):
        registry.publish_generation(stale_staged, expected_previous=old_pointer)


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
