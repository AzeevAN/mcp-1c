#!/usr/bin/env python3
"""Синхронизировать собранную SPA со статикой Python-пакета."""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "dashboard" / "dist"
TARGET = ROOT / "src" / "mcp1c" / "dashboard_dist"


def manifest(root: Path) -> dict[str, str]:
    if not (root / "index.html").is_file():
        raise FileNotFoundError(f"Нет собранного index.html: {root}")
    return {
        path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def sync() -> None:
    expected = manifest(SOURCE)
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".dashboard-dist-", dir=TARGET.parent))
    try:
        shutil.copytree(SOURCE, temporary, dirs_exist_ok=True)
        if TARGET.exists():
            shutil.rmtree(TARGET)
        temporary.replace(TARGET)
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)
    if manifest(TARGET) != expected:
        raise RuntimeError("Синхронизированная SPA отличается от dashboard/dist.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="только сравнить dashboard/dist со статикой пакета",
    )
    args = parser.parse_args()
    try:
        source = manifest(SOURCE)
        packaged = manifest(TARGET)
    except FileNotFoundError as error:
        if args.check:
            parser.error(str(error))
        sync()
        return 0
    if args.check:
        if source != packaged:
            parser.error(
                "SPA-пакет устарел. Выполните npm run build и затем "
                "python tools/sync_dashboard_assets.py."
            )
        return 0
    sync()
    return 0


if __name__ == "__main__":
    sys.exit(main())
