"""Публичные проверки воспроизводимости цепочки поставки."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
LOCK_FILES = (
    "requirements-lock.txt",
    "requirements-dev-lock.txt",
    "requirements-build-lock.txt",
    "requirements-audit-lock.txt",
    "requirements-lock-tool.txt",
)


def _text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _requirement_blocks(path: str) -> list[str]:
    blocks: list[str] = []
    current: list[str] = []
    for line in _text(path).splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line[0].isspace():
            if current:
                blocks.append("\n".join(current))
            current = [line]
        elif current:
            current.append(line)
    if current:
        blocks.append("\n".join(current))
    return blocks


def test_public_dependency_inputs_keep_lower_bounds() -> None:
    runtime = _text("requirements.txt")
    development = _text("requirements-dev.txt")
    project = tomllib.loads(_text("pyproject.toml"))["project"]

    assert "mcp>=2.0" in runtime
    assert "numpy>=2.0" in runtime
    assert "snowballstemmer>=3.0" in runtime
    assert "pytest>=8" in development
    assert project["dependencies"] == [
        "mcp>=2.0",
        "numpy>=2.0",
        "snowballstemmer>=3.0",
    ]


def test_runtime_and_tool_inputs_are_separate() -> None:
    runtime = _text("requirements.txt")
    for tool in ("build", "pip-audit", "pip-tools", "twine"):
        assert tool not in runtime

    assert re.fullmatch(
        r"build==\d+(?:\.\d+)+\n"
        r"setuptools==\d+(?:\.\d+)+\n"
        r"twine==\d+(?:\.\d+)+\n"
        r"wheel==\d+(?:\.\d+)+\n?",
        _text("requirements-build.txt"),
    )
    assert re.fullmatch(
        r"pip-audit==\d+(?:\.\d+)+\n?",
        _text("requirements-audit.txt"),
    )
    assert re.fullmatch(
        r"uv==\d+(?:\.\d+)+\n?",
        _text("requirements-lock-tool.in"),
    )


def test_all_installation_locks_pin_versions_and_hashes() -> None:
    for path in LOCK_FILES:
        blocks = _requirement_blocks(path)
        assert blocks, path
        for block in blocks:
            first_line = block.splitlines()[0]
            assert "==" in first_line, f"{path}: {first_line}"
            assert "--hash=sha256:" in block, f"{path}: {first_line}"
            assert "file:" not in block
            assert "http://" not in block
            assert "https://" not in block


def test_build_backend_is_pinned() -> None:
    build_system = tomllib.loads(_text("pyproject.toml"))["build-system"]
    assert build_system["requires"] == ["setuptools==84.0.0"]


def test_docker_uses_digest_and_hashed_runtime_lock() -> None:
    dockerfile = _text("Dockerfile")
    assert re.search(
        r"^FROM python:3\.12-slim@sha256:[0-9a-f]{64} AS runtime-base$",
        dockerfile,
        re.M,
    )
    assert re.search(
        r"^FROM node:22\.13\.1-bookworm-slim@sha256:[0-9a-f]{64} AS dashboard-build$",
        dockerfile,
        re.M,
    )
    assert "COPY requirements-lock.txt ." in dockerfile
    assert "pip install --no-cache-dir --require-hashes -r requirements-lock.txt" in dockerfile
    assert "pip install --no-cache-dir -r requirements.txt" not in dockerfile


def test_all_github_actions_are_pinned_to_commit_sha() -> None:
    uses: list[tuple[Path, str, str]] = []
    pattern = re.compile(r"^\s*- uses:\s+([^#\s]+)\s*(?:#\s*(.+))?$", re.M)
    for path in sorted((ROOT / ".github" / "workflows").glob("*.yml")):
        for match in pattern.finditer(path.read_text(encoding="utf-8")):
            uses.append((path, match.group(1), match.group(2) or ""))

    assert uses
    for path, action, version_comment in uses:
        _, separator, ref = action.rpartition("@")
        assert separator and re.fullmatch(r"[0-9a-f]{40}", ref), f"{path}: {action}"
        assert re.search(r"\bv\d+(?:\.\d+)+\b", version_comment), f"{path}: {action}"


def test_tests_and_package_jobs_install_hashed_locks() -> None:
    workflow = _text(".github/workflows/tests.yml")
    assert "pip install --require-hashes -r requirements-dev-lock.txt" in workflow
    assert "pip install --require-hashes -r requirements-build-lock.txt" in workflow
    assert "python -m build --no-isolation" in workflow
    assert "python -m pip install build twine" not in workflow


def test_scheduled_audit_checks_lock_and_publishes_sbom() -> None:
    workflow = _text(".github/workflows/supply-chain.yml")
    assert "schedule:" in workflow
    assert "pip install --require-hashes -r requirements-audit-lock.txt" in workflow
    assert "python -m pip_audit" in workflow
    assert "--require-hashes" in workflow
    assert "--disable-pip" in workflow
    assert "-r requirements-lock.txt" in workflow
    assert "-f cyclonedx-json" in workflow
    assert "actions/upload-artifact@" in workflow
