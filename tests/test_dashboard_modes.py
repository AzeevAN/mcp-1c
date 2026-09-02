"""Режимы дашборда не должны менять MCP и владение Registry."""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from mcp1c import tools
from mcp1c.dashboard_runtime import (
    DASHBOARD_OFF,
    DASHBOARD_ON,
    DashboardModeError,
    dashboard_mode,
    routes,
)
from mcp1c.registry import STATUS_ERROR, Registry


def test_по_умолчанию_включён_современный_дашборд(monkeypatch):
    monkeypatch.delenv("MCP1C_DASHBOARD", raising=False)

    assert dashboard_mode() == DASHBOARD_ON


@pytest.mark.parametrize("mode", [DASHBOARD_ON, DASHBOARD_OFF])
def test_поддерживаются_два_явных_режима(monkeypatch, mode):
    monkeypatch.setenv("MCP1C_DASHBOARD", mode)

    assert dashboard_mode() == mode


def test_опечатка_в_режиме_останавливает_запуск(monkeypatch):
    monkeypatch.setenv("MCP1C_DASHBOARD", "react")

    with pytest.raises(DashboardModeError, match="on, off"):
        dashboard_mode()


def test_off_не_регистрирует_ни_html_ни_api(tmp_path):
    registry = Registry(tmp_path / "data")

    assert routes(registry, mode=DASHBOARD_OFF) == []


def test_spa_отдаёт_api_и_понятный_ответ_без_сборки(tmp_path):
    registry = Registry(tmp_path / "data")
    app = Starlette(
        routes=routes(
            registry,
            mode=DASHBOARD_ON,
            static_dir=tmp_path / "dashboard-dist",
        )
    )

    with TestClient(app) as client:
        bootstrap = client.get("/api/v1/dashboard/bootstrap")
        page = client.get("/", headers={"accept": "text/html"})

    assert bootstrap.status_code == 200
    assert bootstrap.json() == {
        "api_version": "v1",
        "dashboard_mode": "on",
        "server": {"status": "ok", "version": "2.0.1"},
        "permissions": {"read": True, "admin": False},
        "authentication": {
            "read_required": False,
            "admin_available": False,
            "session_level": None,
        },
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
    app = Starlette(routes=routes(registry, mode=DASHBOARD_ON, static_dir=static_dir))

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
    monkeypatch.setenv("ADMIN_TOKEN", "test-admin-token")
    registry = Registry(tmp_path / "data")
    static_dir = tmp_path / "dashboard-dist"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<div id=root></div>", encoding="utf-8")
    app = Starlette(routes=routes(registry, mode=DASHBOARD_ON, static_dir=static_dir))

    with TestClient(app) as client:
        page = client.get("/login")
        response = client.post(
            "/login", data={"token": "test-read-token"}, follow_redirects=False
        )
        reader = client.get("/api/v1/dashboard/bootstrap").json()
        client.cookies.clear()
        client.post("/login", data={"token": "test-admin-token"})
        admin = client.get("/api/v1/dashboard/bootstrap").json()

    assert page.status_code == 200
    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert "mcp1c_session=" in response.headers["set-cookie"]
    assert reader["permissions"] == {"read": True, "admin": False}
    assert reader["authentication"] == {
        "read_required": True,
        "admin_available": True,
        "session_level": "read",
    }
    assert admin["permissions"] == {"read": True, "admin": True}
    assert admin["authentication"]["session_level"] == "admin"


def test_spa_без_сессии_перенаправляет_прямую_ссылку_на_вход(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("API_TOKEN", "test-read-token")
    registry = Registry(tmp_path / "data")
    static_dir = tmp_path / "dashboard-dist"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<div id=root></div>", encoding="utf-8")
    app = Starlette(routes=routes(registry, mode=DASHBOARD_ON, static_dir=static_dir))

    with TestClient(app) as client:
        denied = client.get("/sources", follow_redirects=False)
        login_page = client.get("/login")
        client.post("/login", data={"token": "test-read-token"})
        allowed = client.get("/sources")

    assert denied.status_code == 303
    assert denied.headers["location"] == "/login?next=%2Fsources"
    assert login_page.status_code == 200
    assert allowed.status_code == 200


def test_spa_api_источников_требует_токен_чтения(tmp_path, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "test-read-token")
    registry = Registry(tmp_path / "data")
    app = Starlette(
        routes=routes(
            registry,
            mode=DASHBOARD_ON,
            static_dir=tmp_path / "dashboard-dist",
        )
    )

    with TestClient(app) as client:
        denied_sources = client.get("/api/v1/sources")
        denied_journal = client.get(
            "/api/v1/sources/coverage?source_id=example"
        )
        allowed_sources = client.get(
            "/api/v1/sources", headers={"x-api-token": "test-read-token"}
        )

    assert denied_sources.status_code == 401
    assert denied_journal.status_code == 401
    assert allowed_sources.status_code == 200


def test_bootstrap_считает_синтетические_источники(
    tmp_path, реестр_с_кодом
):
    app = Starlette(
        routes=routes(
            реестр_с_кодом,
            mode=DASHBOARD_ON,
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


def test_sources_api_группирует_конфигурацию_модули_и_расширение(
    tmp_path, корень_кода, реестр_из_кода, архив_кода
):
    registry = реестр_из_кода(корень_кода, name="Пример")
    registry.add_modules(
        архив_кода(корень_кода, extension="Доп"),
        configuration="Пример",
    )
    app = Starlette(
        routes=routes(
            registry,
            mode=DASHBOARD_ON,
            static_dir=tmp_path / "dashboard-dist",
        )
    )

    with TestClient(app) as client:
        response = client.get("/api/v1/sources")

    assert response.status_code == 200
    payload = response.json()
    assert payload["api_version"] == "v1"
    assert payload["permissions"] == {"read": True, "admin": False}
    assert payload["references"] == []
    assert len(payload["configurations"]) == 1
    configuration = payload["configurations"][0]
    assert {
        "id": configuration["id"],
        "version": configuration["version"],
        "platform": configuration["platform"],
        "objects": configuration["objects"],
    } == {
        "id": "Пример",
        "version": "1.0",
        "platform": "8.3.23.1997",
        "objects": 2,
    }
    assert configuration["source"]["kind"] == "configuration"
    assert [corpus["kind"] for corpus in configuration["corpora"]] == [
        "modules",
        "extension",
    ]
    for corpus in configuration["corpora"]:
        assert corpus["phase"] == "ready"
        assert corpus["coverage"]["modules"]["total"] == 3
        assert corpus["coverage"]["form_structures"]["total"] == 1
        assert corpus["coverage"]["form_modules"]["total"] == 1
        assert corpus["journal"].startswith("logs/code-")

    with TestClient(app) as client:
        journal = client.get(configuration["corpora"][0]["journal_url"])

    assert journal.status_code == 200
    assert journal.json()["schema_version"] == 1
    assert journal.json()["kind"] == "module_coverage"


def test_sources_api_показывает_безопасную_причину_только_admin(
    tmp_path,
    корень_кода,
    реестр_из_кода,
    monkeypatch,
):
    monkeypatch.setenv("API_TOKEN", "read-token")
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    registry = реестр_из_кода(корень_кода, name="Пример")
    loaded = registry.resolve("Пример").modules
    raw = (
        "private-export.zip: индекс кода не построился — "
        "IndexError: string index out of range"
    )
    with registry._lock:
        loaded.готов = False
        loaded.source.status = STATUS_ERROR
        loaded.source.error = raw
    app = Starlette(
        routes=routes(
            registry,
            mode=DASHBOARD_ON,
            static_dir=tmp_path / "dashboard-dist",
        )
    )

    with TestClient(app) as client:
        reader = client.get(
            "/api/v1/sources",
            headers={"x-api-token": "read-token"},
        ).json()
        admin = client.get(
            "/api/v1/sources",
            headers={"x-api-token": "admin-token"},
        ).json()

    reader_corpus = reader["configurations"][0]["corpora"][0]
    admin_corpus = admin["configurations"][0]["corpora"][0]
    assert reader_corpus["error"] == ""
    assert admin_corpus["error"] == (
        "индекс кода не построился — "
        "IndexError: string index out of range"
    )
    assert "private-export.zip" not in admin_corpus["error"]
    assert admin_corpus["journal"] == ""
    assert admin_corpus["journal_url"] == ""
    assert loaded.source.error == raw


def test_sources_api_при_двойной_смене_поколения_просит_повторить(
    tmp_path, monkeypatch
):
    """Публикация нового поколения не должна превращаться во внешний 500."""
    registry = Registry(tmp_path / "data")
    monkeypatch.setattr(
        tools,
        "_sources_snapshot_is_current",
        lambda registry, capture: False,
    )
    app = Starlette(
        routes=routes(
            registry,
            mode=DASHBOARD_ON,
            static_dir=tmp_path / "dashboard-dist",
        )
    )

    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get("/api/v1/sources")

    assert response.status_code == 409
    assert response.json() == {
        "error": (
            "Источники изменились дважды; повторите запрос после завершения "
            "загрузки."
        )
    }
