#!/usr/bin/env python3
"""Проверить и атомарно синхронизировать встроенный пакет общей справки."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from mcp1c.reference_provider import (  # noqa: E402
    REFERENCE_DATABASE_MEMBER,
    REFERENCE_MANIFEST_MEMBER,
    REFERENCE_SIGNATURE_MEMBER,
    ReferenceService,
    SignedArtifactVerifier,
)


DEFAULT_SOURCE = ROOT / "reference-lab" / "dist" / "reference.mcp1cref"
DEFAULT_DESTINATION = ROOT / "release-assets" / "reference" / "reference.mcp1cref"
EXPECTED_MEMBERS = (
    REFERENCE_MANIFEST_MEMBER,
    REFERENCE_SIGNATURE_MEMBER,
    REFERENCE_DATABASE_MEMBER,
)
_CHUNK = 1024 * 1024


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()


def _verify(path: Path) -> dict[str, Any]:
    resolved = path.expanduser().resolve(strict=True)
    with zipfile.ZipFile(resolved) as bundle:
        members = tuple(bundle.namelist())
    if len(members) != len(EXPECTED_MEMBERS) or set(members) != set(
        EXPECTED_MEMBERS
    ):
        raise ValueError(
            ".mcp1cref должен содержать ровно manifest.json, manifest.sig "
            "и reference.sqlite3."
        )

    with tempfile.TemporaryDirectory(
        prefix="mcp1c-reference-sync-verify."
    ) as directory:
        service = ReferenceService.discover(
            Path(directory),
            embedded_path=resolved,
            verifier=SignedArtifactVerifier(),
        )
        try:
            if service.status.state != "ready" or service.provider is None:
                raise ValueError(service.status.message)
            return {
                "path": str(resolved),
                "size": resolved.stat().st_size,
                "sha256": _sha256(resolved),
                "members": list(EXPECTED_MEMBERS),
                "schema_version": service.status.schema_version,
                "key_id": service.status.key_id,
                "items": service.status.items,
            }
        finally:
            service.close()


def sync_reference_asset(
    source: Path = DEFAULT_SOURCE,
    destination: Path = DEFAULT_DESTINATION,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Проверить source и заменить destination только проверенной точной копией."""
    source_report = _verify(source)
    source_path = source.expanduser().resolve()
    target = destination.expanduser().resolve()
    current_sha256 = _sha256(target) if target.is_file() else None
    if dry_run:
        return {
            "action": (
                "unchanged"
                if current_sha256 == source_report["sha256"]
                else "would_update"
            ),
            "source": source_report,
            "destination": str(target),
            "destination_sha256": current_sha256,
        }

    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_candidate = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    candidate = Path(raw_candidate)
    try:
        with os.fdopen(descriptor, "wb") as output, source_path.open(
            "rb"
        ) as input_stream:
            shutil.copyfileobj(input_stream, output, length=_CHUNK)
            output.flush()
            os.fsync(output.fileno())
        candidate.chmod(0o644)
        candidate_report = _verify(candidate)
        if candidate_report["sha256"] != source_report["sha256"]:
            raise ValueError("SHA-256 временной копии не совпал с исходным пакетом.")
        os.replace(candidate, target)
        directory = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        candidate.unlink(missing_ok=True)

    destination_report = _verify(target)
    if destination_report["sha256"] != source_report["sha256"]:
        raise RuntimeError("Синхронизированный пакет отличается от исходного.")
    return {
        "action": (
            "updated" if current_sha256 != source_report["sha256"] else "refreshed"
        ),
        "source": source_report,
        "destination": destination_report,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--destination", type=Path, default=DEFAULT_DESTINATION)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="проверить пакет и показать, нужна ли замена, ничего не записывая",
    )
    args = parser.parse_args(argv)
    try:
        report = sync_reference_asset(
            args.source,
            args.destination,
            dry_run=args.dry_run,
        )
    except (OSError, ValueError, zipfile.BadZipFile) as error:
        parser.error(str(error))
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
