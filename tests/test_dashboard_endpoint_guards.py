"""Каждый JSON-маршрут дашборда закрыт для анонима, когда токены заданы.

Проверка доступа объявляется декоратором (`read_endpoint`/`admin_endpoint`).
Тест обходит весь список маршрутов, а не выборку: новый маршрут, забывший
декоратор, ловится здесь, а не в проде.
"""

from __future__ import annotations

import re

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from mcp1c.dashboard_runtime import DASHBOARD_ON, routes
from mcp1c.registry import Registry

from conftest import build_configuration, write_export

# Сознательно открытые без токена точки.
_PUBLIC: set[str] = set()


def _app(tmp_path) -> tuple[Starlette, list[Route]]:
    data_dir = tmp_path / "data"
    incoming = tmp_path / "incoming"
    data_dir.mkdir()
    incoming.mkdir()
    registry = Registry(data_dir)
    registry.add_configuration(write_export(incoming, build_configuration()))
    static_dir = tmp_path / "dist"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<!doctype html>", encoding="utf-8")
    table = routes(registry, mode=DASHBOARD_ON, static_dir=static_dir)
    return Starlette(routes=table), table


def _concrete(path: str) -> str:
    return re.sub(r"\{[^}]+\}", "x", path)


def test_аноним_не_получает_ответ_ни_одного_json_маршрута(tmp_path, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "reader-token")
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    app, table = _app(tmp_path)
    client = TestClient(app, headers={"origin": "http://testserver"})
    checked = []
    for route in table:
        if not isinstance(route, Route) or not route.path.startswith("/api/"):
            continue
        if route.path in _PUBLIC:
            continue
        for method in sorted((route.methods or {"GET"}) - {"HEAD"}):
            response = client.request(method, _concrete(route.path), json={})
            assert response.status_code in (401, 403), (
                method,
                route.path,
                response.status_code,
            )
            assert "error" in response.json(), (method, route.path)
            checked.append((method, route.path))
    assert len(checked) >= 30, checked


@pytest.mark.parametrize(
    "path",
    ["/api/v1/dashboard/bootstrap", "/api/v1/sources"],
)
def test_токен_чтения_проходит_декоратор(tmp_path, monkeypatch, path):
    monkeypatch.setenv("API_TOKEN", "reader-token")
    app, _ = _app(tmp_path)
    client = TestClient(app)

    response = client.get(path, headers={"x-api-token": "reader-token"})

    assert response.status_code == 200
