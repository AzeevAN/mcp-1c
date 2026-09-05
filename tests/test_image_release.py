"""Release workflow публикует только согласованный стабильный OCI-образ."""

from __future__ import annotations

from pathlib import Path

import pytest

from tools.check_image_release import IMAGE, validate_release


ROOT = Path(__file__).resolve().parents[1]


def _contract(version: str = "2.0.0") -> tuple[str, str]:
    value = f"MCP1C_IMAGE={IMAGE}:{version}"
    return value, value


def test_release_contract_принимает_согласованный_stable_v2() -> None:
    compose, env_example = _contract()

    assert validate_release("v2.0.0", "2.0.0", "2.0.0", compose, env_example) == (
        "2.0.0"
    )


def test_release_contract_отклоняет_неподдержанные_tags() -> None:
    compose, env_example = _contract()

    for tag in ("2.0.0", "v2.0", "v2.0.0-rc.1", "v01.0.0", "v1.9.9"):
        with pytest.raises(ValueError):
            validate_release(tag, "2.0.0", "2.0.0", compose, env_example)


def test_release_contract_отклоняет_расхождение_versions_и_defaults() -> None:
    compose, env_example = _contract()

    for project, package, compose_text, env_text in (
        ("2.0.1", "2.0.0", compose, env_example),
        ("2.0.0", "2.0.1", compose, env_example),
        ("2.0.0", "2.0.0", "image: old", env_example),
        ("2.0.0", "2.0.0", compose, "image: old"),
    ):
        with pytest.raises(ValueError):
            validate_release("v2.0.0", project, package, compose_text, env_text)


def test_текущий_установочный_контракт_закрепляет_v2() -> None:
    expected = f"{IMAGE}:2.2.0"

    assert expected in (ROOT / "compose.yaml").read_text(encoding="utf-8")
    assert expected in (ROOT / ".env.example").read_text(encoding="utf-8")


def test_workflow_release_only_multiarch_attested_и_анонимно_проверяемый() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release-image.yml").read_text(
        encoding="utf-8"
    )

    assert "types: [published]" in workflow
    assert "workflow_dispatch:" not in workflow
    assert "\n  push:" not in workflow
    assert "github.event.release.prerelease == false" in workflow
    assert "packages: write" in workflow
    assert "tools/check_image_release.py" in workflow
    assert 'git rev-list -n 1 "${RELEASE_TAG}"' in workflow
    assert 'git merge-base --is-ancestor "${GITHUB_SHA}" origin/main' in workflow
    assert "platforms: linux/amd64,linux/arm64" in workflow
    assert "type=semver,pattern={{version}}" in workflow
    assert "type=semver,pattern={{major}}.{{minor}}" in workflow
    assert "type=semver,pattern={{major}}" in workflow
    assert "provenance: mode=max" in workflow
    assert "sbom: true" in workflow
    assert "push: true" in workflow
    assert 'docker pull "${IMAGE_NAME}@${IMAGE_DIGEST}"' in workflow
