"""SPA входит в Python-артефакты и используется Docker без Node runtime."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from mcp1c.dashboard_runtime import DEFAULT_DASHBOARD_DIST


ROOT = Path(__file__).parents[1]


def test_spa_объявляет_встроенный_favicon_без_отдельного_http_запроса() -> None:
    for path in (
        ROOT / "dashboard" / "index.html",
        DEFAULT_DASHBOARD_DIST / "index.html",
    ):
        index = path.read_text(encoding="utf-8")
        assert 'rel="icon"' in index
        assert 'href="data:image/svg+xml,' in index


def test_spa_применяет_сохранённую_тему_до_первого_кадра() -> None:
    for path in (
        ROOT / "dashboard" / "index.html",
        DEFAULT_DASHBOARD_DIST / "index.html",
    ):
        index = path.read_text(encoding="utf-8")
        assert "mcp1c-dashboard-ui" in index
        assert "document.documentElement.dataset.theme" in index
        assert index.index("document.documentElement.dataset.theme") < index.index(
            'type="module"'
        )


def test_package_data_включает_index_и_assets() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    patterns = project["tool"]["setuptools"]["package-data"]["mcp1c"]

    assert patterns == ["dashboard_dist/*", "dashboard_dist/assets/*"]
    assert DEFAULT_DASHBOARD_DIST == ROOT / "src" / "mcp1c" / "dashboard_dist"
    index = (DEFAULT_DASHBOARD_DIST / "index.html").read_text(encoding="utf-8")
    references = re.findall(r'(?:src|href)="/([^"?#]+)', index)
    assert references
    assert all((DEFAULT_DASHBOARD_DIST / item).is_file() for item in references)


def test_docker_кладёт_тот_же_dist_в_python_package() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert (
        "COPY --from=dashboard-build --chown=10001:10001 /dashboard/dist "
        "/app/src/mcp1c/dashboard_dist"
    ) in dockerfile
    runtime = dockerfile.split("FROM runtime-base AS runtime", 1)[1]
    assert "node_modules" not in runtime
    assert "MCP1C_DASHBOARD_DIST" not in runtime


def test_ci_проверяет_frontend_sdist_и_wheel_из_sdist() -> None:
    workflow = (ROOT / ".github" / "workflows" / "tests.yml").read_text(
        encoding="utf-8"
    )

    assert "npm test -- --run" in workflow
    assert "python tools/sync_dashboard_assets.py --check" in workflow
    assert "python tools/check_dashboard_artifacts.py" in workflow
    assert "python -m pip wheel --no-build-isolation --no-deps" in workflow
