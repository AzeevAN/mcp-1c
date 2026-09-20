"""Синхронизация проверенного reference bundle в tracked release-assets."""

from __future__ import annotations

import shutil
import zipfile
from pathlib import Path

import pytest

from tools.sync_reference_release_asset import EXPECTED_MEMBERS, sync_reference_asset


ROOT = Path(__file__).resolve().parents[1]
VERIFIED_ARTIFACT = ROOT / "release-assets" / "reference" / "reference.mcp1cref"


def test_sync_атомарно_копирует_только_проверенный_bundle(tmp_path: Path) -> None:
    source = tmp_path / "source.mcp1cref"
    destination = tmp_path / "release-assets" / "reference.mcp1cref"
    shutil.copy2(VERIFIED_ARTIFACT, source)

    report = sync_reference_asset(source, destination)

    assert report["action"] == "updated"
    assert destination.read_bytes() == source.read_bytes()
    assert report["source"]["sha256"] == report["destination"]["sha256"]
    with zipfile.ZipFile(destination) as bundle:
        assert tuple(bundle.namelist()) == EXPECTED_MEMBERS


def test_invalid_signature_не_заменяет_destination(tmp_path: Path) -> None:
    source = tmp_path / "invalid.mcp1cref"
    destination = tmp_path / "reference.mcp1cref"
    with zipfile.ZipFile(VERIFIED_ARTIFACT) as valid, zipfile.ZipFile(
        source, "w"
    ) as invalid:
        for name in valid.namelist():
            payload = valid.read(name)
            if name == "manifest.sig":
                payload = bytes([payload[0] ^ 1]) + payload[1:]
            invalid.writestr(name, payload)
    destination.write_bytes(b"previous verified release")

    with pytest.raises(ValueError, match="Подпись"):
        sync_reference_asset(source, destination)

    assert destination.read_bytes() == b"previous verified release"


def test_dry_run_ничего_не_записывает(tmp_path: Path) -> None:
    source = tmp_path / "source.mcp1cref"
    destination = tmp_path / "reference.mcp1cref"
    shutil.copy2(VERIFIED_ARTIFACT, source)

    report = sync_reference_asset(source, destination, dry_run=True)

    assert report["action"] == "would_update"
    assert not destination.exists()
