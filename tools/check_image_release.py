#!/usr/bin/env python3
"""Проверить version contract перед публикацией OCI-образа."""

from __future__ import annotations

import argparse
import re
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STABLE_TAG = re.compile(r"^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
PACKAGE_VERSION = re.compile(r'^__version__ = "([^"]+)"$', re.MULTILINE)
IMAGE = "ghcr.io/azeevan/mcp-1c"


def validate_release(
    tag: str,
    project_version: str,
    package_version: str,
    compose: str,
    env_example: str,
) -> str:
    match = STABLE_TAG.fullmatch(tag)
    if match is None:
        raise ValueError("Нужен стабильный tag вида vMAJOR.MINOR.PATCH.")
    version = tag.removeprefix("v")
    if int(match.group(1)) < 2:
        raise ValueError("Единый image-контракт публикуется только начиная с v2.")
    if project_version != version or package_version != version:
        raise ValueError(
            "Release tag, pyproject.toml и mcp1c.__version__ должны совпадать."
        )
    expected = f"{IMAGE}:{version}"
    for name, text in (("compose.yaml", compose), (".env.example", env_example)):
        if expected not in text:
            raise ValueError(f"{name} не закрепляет release image {expected}.")
    return version


def repository_version() -> tuple[str, str]:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    package = (ROOT / "src" / "mcp1c" / "__init__.py").read_text(
        encoding="utf-8"
    )
    match = PACKAGE_VERSION.search(package)
    if match is None:
        raise ValueError("Не найдена mcp1c.__version__.")
    return project["project"]["version"], match.group(1)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tag")
    args = parser.parse_args()
    project_version, package_version = repository_version()
    try:
        version = validate_release(
            args.tag,
            project_version,
            package_version,
            (ROOT / "compose.yaml").read_text(encoding="utf-8"),
            (ROOT / ".env.example").read_text(encoding="utf-8"),
        )
    except ValueError as error:
        parser.error(str(error))
    print(version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
