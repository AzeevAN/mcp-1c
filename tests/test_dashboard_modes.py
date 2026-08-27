"""Режимы дашборда не должны менять MCP и владение Registry."""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from mcp1c.dashboard_runtime import (
    DASHBOARD_CLASSIC,
    DASHBOARD_OFF,
    DASHBOARD_SPA,
    DashboardModeError,
    dashboard_mode,
    routes,
)
from mcp1c.registry import Registry


def test_по_умолчанию_остаётся_классический_дашборд(monkeypatch):
    monkeypatch.delenv("MCP1C_DASHBOARD", raising=False)

    assert dashboard_mode() == DASHBOARD_CLASSIC


@pytest.mark.parametrize("mode", [DASHBOARD_OFF, DASHBOARD_CLASSIC, DASHBOARD_SPA])
def test_поддерживаются_три_явных_режима(monkeypatch, mode):
    monkeypatch.setenv("MCP1C_DASHBOARD", mode)

    assert dashboard_mode() == mode


def test_опечатка_в_режиме_останавливает_запуск(monkeypatch):
    monkeypatch.setenv("MCP1C_DASHBOARD", "react")

    with pytest.raises(DashboardModeError, match="off, classic, spa"):
        dashboard_mode()


def test_off_не_регистрирует_ни_html_ни_api(tmp_path):
    registry = Registry(tmp_path / "data")

    assert routes(registry, mode=DASHBOARD_OFF) == []


def test_classic_оставляет_прежние_маршруты(tmp_path):
    registry = Registry(tmp_path / "data")
    paths = [route.path for route in routes(registry, mode=DASHBOARD_CLASSIC)]

    assert "/" in paths
    assert "/sources" in paths
    assert "/api/v1/dashboard/bootstrap" not in paths


def test_spa_отдаёт_api_и_понятный_ответ_без_сборки(tmp_path):
    registry = Registry(tmp_path / "data")
    app = Starlette(
        routes=routes(
            registry,
            mode=DASHBOARD_SPA,
            static_dir=tmp_path / "dashboard-dist",
        )
    )

    with TestClient(app) as client:
        bootstrap = client.get("/api/v1/dashboard/bootstrap")
        page = client.get("/", headers={"accept": "text/html"})

    assert bootstrap.status_code == 200
    assert bootstrap.json() == {
        "api_version": "v1",
        "dashboard_mode": "spa",
        "server": {"status": "ok", "version": "0.8.0"},
        "permissions": {"read": True, "admin": False},
        "summary": {
            "configurations": 0,
            "metadata_objects": 0,
            "code_corpora": 0,
            "reference_sources": 0,
        },
    }
    assert page.status_code == 503
    assert "npm run build" in page.text


def test_spa_раздаёт_index_и_маршруты_клиента(tmp_path):
    registry = Registry(tmp_path / "data")
    static_dir = tmp_path / "dashboard-dist"
    static_dir.mkdir()
    (static_dir / "index.html").write_text(
        "<!doctype html><title>Новый дашборд</title><div id=app></div>",
        encoding="utf-8",
    )
    app = Starlette(routes=routes(registry, mode=DASHBOARD_SPA, static_dir=static_dir))

    with TestClient(app) as client:
        root = client.get("/")
        nested = client.get("/sources")

    assert root.status_code == 200
    assert nested.status_code == 200
    assert "Новый дашборд" in root.text
    assert nested.text == root.text


def test_spa_оставляет_единую_серверную_проверку_токена(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("API_TOKEN", "test-read-token")
    registry = Registry(tmp_path / "data")
    static_dir = tmp_path / "dashboard-dist"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<div id=root></div>", encoding="utf-8")
    app = Starlette(routes=routes(registry, mode=DASHBOARD_SPA, static_dir=static_dir))

    with TestClient(app) as client:
        page = client.get("/login")
        response = client.post(
            "/login", data={"token": "test-read-token"}, follow_redirects=False
        )

    assert page.status_code == 200
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert "mcp1c_session=" in response.headers["set-cookie"]


def test_bootstrap_считает_синтетические_источники(
    tmp_path, реестр_с_кодом
):
    app = Starlette(
        routes=routes(
            реестр_с_кодом,
            mode=DASHBOARD_SPA,
            static_dir=tmp_path / "dashboard-dist",
        )
    )

    with TestClient(app) as client:
        summary = client.get("/api/v1/dashboard/bootstrap").json()["summary"]

    assert summary == {
        "configurations": 1,
        "metadata_objects": 2,
        "code_corpora": 1,
        "reference_sources": 0,
    }
