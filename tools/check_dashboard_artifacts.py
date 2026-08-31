#!/usr/bin/env python3
"""Проверить одинаковый состав SPA в source tree, wheel и sdist."""

from __future__ import annotations

import argparse
import hashlib
import re
import tarfile
import zipfile
from pathlib import Path


PREFIX = "mcp1c/dashboard_dist/"


def _digest(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def directory_manifest(root: Path) -> dict[str, str]:
    return {
        path.relative_to(root).as_posix(): _digest(path.read_bytes())
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def wheel_manifest(path: Path) -> dict[str, str]:
    with zipfile.ZipFile(path) as archive:
        return {
            name.split(PREFIX, 1)[1]: _digest(archive.read(name))
            for name in archive.namelist()
            if PREFIX in name and not name.endswith("/")
        }


def sdist_manifest(path: Path) -> dict[str, str]:
    marker = "/src/" + PREFIX
    with tarfile.open(path, "r:gz") as archive:
        return {
            member.name.split(marker, 1)[1]: _digest(
                archive.extractfile(member).read()  # type: ignore[union-attr]
            )
            for member in archive.getmembers()
            if marker in member.name and member.isfile()
        }


def validate_index(root: Path, files: set[str]) -> None:
    index = (root / "index.html").read_text(encoding="utf-8")
    referenced = set(re.findall(r'(?:src|href)="/([^"?#]+)', index))
    missing = referenced - files
    if missing:
        raise ValueError(f"index.html ссылается на отсутствующие assets: {missing}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--wheel", type=Path, action="append", default=[])
    parser.add_argument("--sdist", type=Path, action="append", default=[])
    args = parser.parse_args()

    expected = directory_manifest(args.source)
    validate_index(args.source, set(expected))
    if not expected:
        parser.error("Каталог SPA пуст.")
    for artifact in args.wheel:
        if wheel_manifest(artifact) != expected:
            parser.error(f"SPA в wheel отличается: {artifact}")
    for artifact in args.sdist:
        if sdist_manifest(artifact) != expected:
            parser.error(f"SPA в sdist отличается: {artifact}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
