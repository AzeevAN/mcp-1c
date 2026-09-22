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
