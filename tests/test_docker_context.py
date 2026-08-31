"""Production Docker-сборка получает только публичный Git-контекст."""

from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_dockerignore_закрыт_по_умолчанию() -> None:
    rules = [
        line.strip()
        for line in (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]

    assert rules[0] == "**"
    assert {"!requirements-lock.txt", "!src/mcp1c/*.py"} <= set(rules)
    assert {
        "!dashboard/package-lock.json",
        "!dashboard/src/**/*.tsx",
    } <= set(rules)
    assert {
        "src/*",
        "src/mcp1c/*",
        "src/mcp1c/dashboard_dist/*",
        "src/mcp1c/dashboard_dist/assets/*",
        "dashboard/*",
        "dashboard/src/**",
    } <= set(rules)
    assert not {
        "!data",
        "!.env",
        "!docs",
        "!tests",
        "!tools",
        "!src/**",
        "!dashboard/**",
    } & set(rules)


def test_dockerfile_копирует_только_разрешённые_деревья() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    copy_lines = [
        line.strip()
        for line in dockerfile.splitlines()
        if line.lstrip().startswith(("COPY ", "ADD "))
    ]

    assert copy_lines == [
        "COPY dashboard/package.json dashboard/package-lock.json ./",
        "COPY dashboard/ ./",
        "COPY requirements-lock.txt .",
        "COPY src/ ./src/",
        "COPY --from=dashboard-build --chown=10001:10001 /dashboard/dist /app/src/mcp1c/dashboard_dist",
    ]
    assert "COPY ." not in dockerfile
    assert "ADD " not in dockerfile


def test_release_использует_git_context_точного_sha() -> None:
    workflow = (ROOT / ".github" / "workflows" / "release-image.yml").read_text(
        encoding="utf-8"
    )

    assert 'context: "{{defaultContext}}"' in workflow
    assert "context: ." not in workflow
    assert 'release_sha}" != "${GITHUB_SHA}' in workflow


def test_local_build_архивирует_только_чистый_head() -> None:
    script = (ROOT / "tools" / "build_image.py").read_text(encoding="utf-8")

    for contract in (
        '"status", "--porcelain", "--untracked-files=normal"',
        '"archive", "--format=tar", "HEAD"',
        '"build",',
        '"--target",',
        '"runtime",',
        '"-",',
    ):
        assert contract in script
