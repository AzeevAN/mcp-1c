"""Проверка подписи встроенного пакета до открытия SQLite."""

from __future__ import annotations

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from mcp1c.reference_provider import ReferenceService, SignedArtifactVerifier
from reference_fixture import build_reference_artifact, build_reference_database


def _key_material():
    private = Ed25519PrivateKey.generate()
    public = private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return private, {"synthetic-ephemeral": public}


def _signed(tmp_path, **kwargs):
    private, keys = _key_material()
    database = build_reference_database(tmp_path / "source.sqlite3")
    artifact = build_reference_artifact(
        tmp_path / "reference" / "reference.mcp1cref", database, private, **kwargs
    )
    return artifact, SignedArtifactVerifier(keys), database, private


def _discover(tmp_path, artifact, verifier):
    return ReferenceService.discover(
        tmp_path, embedded_path=artifact, verifier=verifier
    )


def test_корректная_detached_подпись_даёт_ready(tmp_path):
    artifact, verifier, _, _ = _signed(tmp_path)
    service = _discover(tmp_path, artifact, verifier)
    assert service.database_path != artifact
    assert service.status.state == "ready"
    assert service.status.signature == "ed25519"
    assert service.status.key_id == "synthetic-ephemeral"
    assert service.provider is not None


@pytest.mark.parametrize("missing", ["manifest", "signature"])
def test_неполный_пакет_fail_closed(tmp_path, missing):
    artifact, verifier, _, _ = _signed(
        tmp_path,
        include_manifest=missing != "manifest",
        include_signature=missing != "signature",
    )
    service = _discover(tmp_path, artifact, verifier)
    assert service.status.state == "untrusted"
    assert service.provider is None


def test_неизвестный_key_id_fail_closed(tmp_path):
    artifact, _, _, _ = _signed(tmp_path, key_id="unknown")
    service = _discover(tmp_path, artifact, SignedArtifactVerifier({}))
    assert service.status.state == "untrusted"
    assert service.status.key_id == "unknown"
    assert service.provider is None


@pytest.mark.parametrize("signature", [b"x", b"x" * 64])
def test_неверная_подпись_fail_closed(tmp_path, signature):
    artifact, verifier, database, private = _signed(tmp_path)
    build_reference_artifact(artifact, database, private, signature=signature)
    service = _discover(tmp_path, artifact, verifier)
    assert service.status.state == "untrusted"
    assert service.provider is None


def test_изменённая_sqlite_отклоняется_до_открытия(tmp_path, monkeypatch):
    artifact, verifier, database, private = _signed(tmp_path)
    raw = bytearray(database.read_bytes())
    raw[-1] ^= 1
    build_reference_artifact(
        artifact, database, private, database_bytes=bytes(raw)
    )
    opened: list[object] = []
    monkeypatch.setattr(
        "mcp1c.reference_provider._connect",
        lambda path: opened.append(path),
    )
    service = _discover(tmp_path, artifact, verifier)
    assert service.provider is None
    assert opened == []
