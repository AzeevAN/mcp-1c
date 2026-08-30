"""Подписанный артефакт проверяется до первого открытия SQLite."""

from __future__ import annotations

import zipfile

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from mcp1c.reference_provider import ReferenceService, SignedArtifactVerifier

from reference_fixture import (
    build_reference_artifact,
    build_reference_database,
    manifest_bytes,
    reference_manifest,
)


def _key_material():
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    return private, {"synthetic-ephemeral": public}


def _signed(tmp_path, **kwargs):
    private, keys = _key_material()
    database = build_reference_database(tmp_path / "source.sqlite3")
    artifact = build_reference_artifact(
        tmp_path / "reference" / "reference.mcp1cref",
        database,
        private,
        **kwargs,
    )
    return artifact, SignedArtifactVerifier(keys), private, database


def test_корректная_detached_подпись_даёт_ready(tmp_path):
    artifact, verifier, _, _ = _signed(tmp_path)

    service = ReferenceService.discover(tmp_path, verifier=verifier)

    assert service.database_path != artifact
    assert service.status.state == "ready"
    assert service.status.signature == "ed25519"
    assert service.status.key_id == "synthetic-ephemeral"
    assert service.provider is not None
    assert service.provider.search("образец")["results"][0]["id"] == "bsl/Example"


@pytest.mark.parametrize("missing", ["manifest", "signature"])
def test_отсутствие_manifest_или_signature_fail_closed(tmp_path, missing):
    artifact, verifier, _, _ = _signed(
        tmp_path,
        include_manifest=missing != "manifest",
        include_signature=missing != "signature",
    )

    service = ReferenceService.discover(tmp_path, verifier=verifier)

    assert artifact.is_file()
    assert service.status.state == "untrusted"
    assert service.provider is None


def test_неизвестный_key_id_fail_closed(tmp_path):
    artifact, _, _, _ = _signed(tmp_path, key_id="unknown")
    verifier = SignedArtifactVerifier({})

    service = ReferenceService.discover(tmp_path, verifier=verifier)

    assert artifact.is_file()
    assert service.status.state == "untrusted"
    assert service.status.key_id == "unknown"
    assert service.provider is None


@pytest.mark.parametrize("signature", [b"x", b"x" * 64])
def test_неверная_или_обрезанная_подпись_fail_closed(tmp_path, signature):
    artifact, verifier, private, database = _signed(tmp_path)
    build_reference_artifact(
        artifact,
        database,
        private,
        signature=signature,
    )

    service = ReferenceService.discover(tmp_path, verifier=verifier)

    assert service.status.state == "untrusted"
    assert service.provider is None


def test_изменённый_manifest_отклоняется_до_открытия_sqlite(tmp_path, monkeypatch):
    artifact, verifier, private, database = _signed(tmp_path)
    original = reference_manifest(
        database.read_bytes(),
        key_id="synthetic-ephemeral",
        logical_sha256="0" * 64,
    )
    signed = manifest_bytes(original)
    build_reference_artifact(
        artifact,
        database,
        private,
        manifest_override={"logical_sha256": "1" * 64},
        signed_manifest=signed,
    )
    opened: list[object] = []
    monkeypatch.setattr(
        "mcp1c.reference_provider._connect",
        lambda path: opened.append(path),
    )

    service = ReferenceService.discover(tmp_path, verifier=verifier)

    assert service.status.state == "untrusted"
    assert service.provider is None
    assert opened == []


def test_изменённая_sqlite_отклоняется_до_открытия(tmp_path, monkeypatch):
    artifact, verifier, private, database = _signed(tmp_path)
    raw = bytearray(database.read_bytes())
    raw[-1] ^= 1
    build_reference_artifact(
        artifact,
        database,
        private,
        database_bytes=bytes(raw),
    )
    opened: list[object] = []
    monkeypatch.setattr(
        "mcp1c.reference_provider._connect",
        lambda path: opened.append(path),
    )

    service = ReferenceService.discover(tmp_path, verifier=verifier)

    assert service.status.state == "corrupt"
    assert service.provider is None
    assert opened == []


def test_подмена_файлового_хеша_отклоняется_до_открытия(tmp_path, monkeypatch):
    artifact, verifier, _, _ = _signed(
        tmp_path,
        manifest_override={"artifact_sha256": "0" * 64},
    )
    opened: list[object] = []
    monkeypatch.setattr(
        "mcp1c.reference_provider._connect",
        lambda path: opened.append(path),
    )

    service = ReferenceService.discover(tmp_path, verifier=verifier)

    assert artifact.is_file()
    assert service.status.state == "corrupt"
    assert service.provider is None
    assert opened == []


def test_подмена_логического_хеша_отклоняется_после_доверия(tmp_path):
    _, verifier, _, _ = _signed(
        tmp_path,
        manifest_override={"logical_sha256": "0" * 64},
    )

    service = ReferenceService.discover(tmp_path, verifier=verifier)

    assert service.status.state == "corrupt"
    assert service.provider is None


def test_гонка_замены_candidate_не_устанавливает_непроверенные_байты(tmp_path):
    private, keys = _key_material()
    source = build_reference_database(tmp_path / "source.sqlite3")
    candidate = build_reference_artifact(
        tmp_path / "candidate.mcp1cref", source, private
    )
    expected = candidate.read_bytes()
    forged = tmp_path / "forged.mcp1cref"
    with zipfile.ZipFile(forged, "w") as bundle:
        bundle.writestr("reference.sqlite3", b"forged")
    base = SignedArtifactVerifier(keys)

    class SwapOriginalAfterVerification:
        def verify(self, artifact, extraction_dir):
            result = base.verify(artifact, extraction_dir)
            candidate.write_bytes(forged.read_bytes())
            return result

    service = ReferenceService.discover(tmp_path, verifier=SwapOriginalAfterVerification())

    installed = service.install_candidate(candidate)

    assert installed.state == "pending_restart"
    assert service.managed_path.read_bytes() == expected
    restarted = ReferenceService.discover(tmp_path, verifier=base)
    assert restarted.status.state == "ready"


def test_прямая_установка_ограничивает_bundle_до_проверки(tmp_path, monkeypatch):
    import mcp1c.reference_provider as reference_provider

    candidate = tmp_path / "oversized.mcp1cref"
    candidate.write_bytes(b"x" * 17)
    monkeypatch.setattr(reference_provider, "MAX_REFERENCE_ARTIFACT_BYTES", 16)
    service = ReferenceService.discover(tmp_path)

    with pytest.raises(
        reference_provider.ReferenceValidationError,
        match="Размер подписанного артефакта недопустим",
    ):
        service.install_candidate(candidate)

    assert candidate.is_file()
    assert not service.managed_path.exists()
