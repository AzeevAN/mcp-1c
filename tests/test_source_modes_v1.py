"""Synthetic RED/GREEN contracts for SOURCE-MODES-V1 activation state."""

import json

import pytest

from mcp1c.source_modes import (
    ActivationComponent,
    ActivationManifest,
    ActivationMode,
    ActivationStatus,
    classify_activation,
)
from mcp1c.intake_v2 import ExportIdentity
from mcp1c.intake_v2_registry import GenerationPointer, _identity_digest
from mcp1c.registry import Registry, RegistryError
from conftest import build_configuration, write_export
from test_registry_modules import _выгрузка_в_файлы
from test_intake_v2_extensions import _materialized
from mcp1c import tools


def _manifest(**overrides):
    values = {
        "mode": ActivationMode.B_FULL,
        "identity_incarnation": "inc-1",
        "physical_generation_root_id": "root-1",
        "configuration_version": "cfg-1",
        "main": ActivationComponent(
            source="source-b",
            origin="candidate-b.zip",
            raw_sha256="a" * 64,
            payload_sha256="b" * 64,
        ),
        "extensions": (),
        "expected_previous_activation": None,
        "transaction_id": "tx-1",
        "recovery_id": "recovery-1",
    }
    values.update(overrides)
    return ActivationManifest(**values)


def test_activation_manifest_roundtrip_is_strict():
    manifest = _manifest()
    restored = ActivationManifest.from_dict(json.loads(manifest.to_json()))
    assert restored == manifest
    with pytest.raises(ValueError, match="mode"):
        ActivationManifest.from_dict({**manifest.to_dict(), "mode": "mixed"})


def test_unknown_configuration_version_survives_roundtrip():
    manifest = _manifest(configuration_version="")
    assert ActivationManifest.from_json(manifest.to_json()) == manifest


@pytest.mark.parametrize("version", [None, " cfg-1", "cfg-1 ", "\x00"])
def test_invalid_configuration_version_is_rejected(version):
    with pytest.raises(ValueError, match="configuration_version"):
        _manifest(configuration_version=version)


def test_legacy_activation_is_reload_required_without_rewrite():
    legacy = {"generation_id": "old", "layers": [{"profile": "schema-v1"}, {"profile": "source-b"}]}
    result = classify_activation(legacy)
    assert result.status is ActivationStatus.RELOAD_REQUIRED
    assert result.mode is None
    assert result.raw == legacy


def test_a_only_cannot_have_extensions():
    with pytest.raises(ValueError, match="A_ONLY"):
        _manifest(mode=ActivationMode.A_ONLY, extensions=(_manifest().main,))


def test_activation_mode_rejects_mixed_main_provenance():
    with pytest.raises(ValueError, match="source-a"):
        _manifest(mode=ActivationMode.A_ONLY)
    with pytest.raises(ValueError, match="source-b"):
        _manifest(
            main=ActivationComponent(
                source="source-a",
                origin="candidate-a.zip",
                raw_sha256="a" * 64,
                payload_sha256="b" * 64,
            )
        )


def test_generation_pointer_persists_activation_manifest():
    identity = ExportIdentity.configuration("Demo")
    generation_id = "g1"
    root = f"generations/{_identity_digest(identity)}/{generation_id}"
    pointer = GenerationPointer(
        identity=identity,
        generation_id=generation_id,
        root_path=root,
        manifest_path=f"{root}/manifest.json",
        manifest_sha256="c" * 64,
        activation=_manifest(),
    )
    restored = GenerationPointer.from_dict(pointer.to_dict())
    assert restored.activation == pointer.activation


def _registry_with_pointer(tmp_path, activation):
    incoming = tmp_path / "in"
    incoming.mkdir()
    registry = Registry(tmp_path / "data")
    registry.add_configuration(
        write_export(incoming, build_configuration(name="Розница"))
    )
    identity = ExportIdentity.configuration("Розница")
    root = f"generations/{_identity_digest(identity)}/g1"
    registry._generation_pointers[identity.grouping_key] = GenerationPointer(
        identity=identity,
        generation_id="g1",
        root_path=root,
        manifest_path=f"{root}/manifest.json",
        manifest_sha256="c" * 64,
        activation=activation,
    )
    return registry


def test_reload_required_blocks_configuration_resolution(tmp_path):
    registry = _registry_with_pointer(tmp_path, None)
    with pytest.raises(RegistryError, match="reload_required"):
        registry.resolve("Розница")


@pytest.mark.parametrize("activation", [None, _manifest()])
def test_native_generation_blocks_legacy_code_writer(tmp_path, activation):
    registry = _registry_with_pointer(tmp_path, activation)
    archive = _выгрузка_в_файлы(tmp_path)
    with pytest.raises(RegistryError, match="(update_full|reload_required)"):
        registry.add_modules(archive, configuration="Розница")
    assert "Розница:modules" not in registry.sources


def test_restored_native_without_activation_is_diagnostic_only(tmp_path):
    _collection, materialized = _materialized(tmp_path, "old-native")
    registry = Registry(tmp_path / "data")
    registry.publish_generation(
        registry.stage_generation(materialized.manifest, materialized.payloads)
    )
    restored = Registry(registry.data_dir)
    assert restored.restore() == []

    assert "RELOAD_REQUIRED" in tools.list_configurations(restored)
    assert tools.sources_snapshot(restored).configuration_names == ("DemoConfiguration",)
    with pytest.raises(RegistryError, match="reload_required"):
        restored.resolve("DemoConfiguration")


def test_restored_legacy_a_plus_separate_code_requires_full_reload(tmp_path):
    incoming = tmp_path / "in"
    incoming.mkdir()
    registry = Registry(tmp_path / "data")
    registry.add_configuration(
        write_export(incoming, build_configuration(name="Розница"))
    )
    registry.add_modules(_выгрузка_в_файлы(tmp_path), configuration="Розница")
    registry.save()

    restored = Registry(registry.data_dir)
    assert restored.restore() == []
    activation = restored.active_activation(ExportIdentity.configuration("Розница"))
    assert activation is not None
    assert activation.status is ActivationStatus.RELOAD_REQUIRED
    assert "RELOAD_REQUIRED" in tools.list_configurations(restored)
    diagnostic = tools.sources_snapshot(restored)
    assert diagnostic.configuration_names == ("Розница",)
    assert diagnostic.configurations[0].code == ()
    with pytest.raises(RegistryError, match="reload_required"):
        restored.resolve("Розница")


def test_restored_pure_source_a_remains_readable(tmp_path):
    incoming = tmp_path / "in"
    incoming.mkdir()
    registry = Registry(tmp_path / "data")
    registry.add_configuration(
        write_export(incoming, build_configuration(name="Розница"))
    )
    registry.save()

    restored = Registry(registry.data_dir)
    assert restored.restore() == []
    assert restored.active_activation(ExportIdentity.configuration("Розница")) is None
    assert restored.resolve("Розница").name == "Розница"
