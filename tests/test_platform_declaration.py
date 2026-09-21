import pytest
import json
from mcp1c.intake_v2 import ExportIdentity
from mcp1c.intake_v2_registry import GenerationPointer
from mcp1c.registry import Registry, RegistryError

from mcp1c.source_modes import (
    ActivationComponent,
    ActivationManifest,
    ActivationMode,
    PlatformDeclaration,
)


def _registry(tmp_path, mode=ActivationMode.B_FULL):
    registry = Registry(tmp_path / "data")
    identity = ExportIdentity.configuration("Demo")
    activation = ActivationManifest(
        mode=mode,
        identity_incarnation="inc-1",
        physical_generation_root_id="root-1",
        configuration_version="1",
        main=ActivationComponent("source-b" if mode is ActivationMode.B_FULL else "source-a", "candidate.zip", "a" * 64, "b" * 64),
        extensions=(),
        expected_previous_activation=None,
        transaction_id="tx-1",
        recovery_id="recovery-1",
    )
    pointer = GenerationPointer.for_manifest(
        type("Manifest", (), {"identity": identity, "generation_id": "g1", "sha256": "c" * 64})(),
        activation=activation,
    )
    registry._generation_pointers[identity.grouping_key] = pointer
    return registry


@pytest.mark.parametrize("version", ["8.3.24", "8.3.24.1234"])
def test_platform_declaration_accepts_three_or_four_components(version):
    assert PlatformDeclaration(version).to_dict() == {
        "version": version,
        "source": "user",
        "status": "declared",
    }


@pytest.mark.parametrize("version", ["8.3", "8.3.24.1.5", "latest", "8.3.x"])
def test_platform_declaration_rejects_non_numeric_shape(version):
    with pytest.raises(ValueError, match="3 или 4"):
        PlatformDeclaration(version)


def test_platform_declaration_rejects_other_provenance():
    with pytest.raises(ValueError, match="source=user"):
        PlatformDeclaration("8.3.24", source="source-a")


def test_registry_persists_and_clears_b_full_platform_declaration(tmp_path):
    registry = _registry(tmp_path)
    saved = registry.set_platform_version("Demo", "8.3.24.1234")
    assert saved.status == "declared"
    assert registry.platform_declaration("Demo").version == "8.3.24.1234"
    payload = json.loads(registry.registry_path.read_text(encoding="utf-8"))
    payload.pop("generation_manifests", None)
    registry.registry_path.write_text(json.dumps(payload), encoding="utf-8")
    restored = Registry(registry.data_dir)
    assert restored.restore() == []
    assert restored.platform_declaration("Demo").version == "8.3.24.1234"
    identity = ExportIdentity.configuration("Demo")
    restored._generation_pointers[identity.grouping_key] = registry._generation_pointers[identity.grouping_key]
    restored.clear_platform_version("Demo")
    assert restored.platform_declaration("Demo") is None


def test_registry_does_not_declare_platform_for_a_only(tmp_path):
    registry = _registry(tmp_path, ActivationMode.A_ONLY)
    with pytest.raises(RegistryError, match="только для B_FULL"):
        registry.set_platform_version("Demo", "8.3.24")
