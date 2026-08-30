"""JSON API карточек сохраняет буквальный ответ MCP и classic UI."""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from mcp1c import tools
from mcp1c.dashboard import render_markdown
from mcp1c.dashboard_runtime import DASHBOARD_SPA, routes
from mcp1c.registry import Registry

from conftest import build_configuration, write_export, write_syntax


def _client(registry: Registry, tmp_path) -> TestClient:
    return TestClient(
        Starlette(
            routes=routes(
                registry,
                mode=DASHBOARD_SPA,
                static_dir=tmp_path / "dashboard-dist",
            )
        )
    )


def _registry(tmp_path) -> Registry:
    data = tmp_path / "data"
    incoming = tmp_path / "incoming"
    data.mkdir()
    incoming.mkdir()
    registry = Registry(data)
    registry.add_configuration(write_export(incoming, build_configuration()))
    return registry


def test_object_card_api_возвращает_тот_же_markdown_и_безопасный_html(tmp_path):
    registry = _registry(tmp_path)
    expected = tools.get_object(
        registry,
        "Справочник.Контрагенты",
        config="ТестоваяКонфигурация",
        detail="fields",
    )

    with _client(registry, tmp_path) as client:
        response = client.get(
            "/api/v1/cards/object",
            params={
                "config": "ТестоваяКонфигурация",
                "name": "Справочник.Контрагенты",
                "detail": "fields",
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["api_version"] == "v1"
    assert payload["kind"] == "object"
    assert payload["name"] == "Справочник.Контрагенты"
    assert payload["configuration"] == "ТестоваяКонфигурация"
    assert payload["configuration_names"] == ["ТестоваяКонфигурация"]
    assert payload["configuration_required"] is True
    assert payload["detail"] == "fields"
    assert payload["detail_levels"] == ["brief", "fields", "full"]
    assert payload["markdown"] == expected
    assert payload["html"] == render_markdown(expected)
    assert "<h1>" in payload["html"]
    assert "# Справочник" not in payload["html"]


def test_syntax_card_api_работает_без_конфигурации(tmp_path):
    registry = Registry(tmp_path / "data")
    registry.add_syntax(write_syntax(tmp_path / "data" / "index" / "syntax"))
    expected = tools.get_syntax(registry, "СтрНайти", detail="full")

    with _client(registry, tmp_path) as client:
        response = client.get(
            "/api/v1/cards/syntax",
            params={"name": "СтрНайти", "detail": "full"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["kind"] == "syntax"
    assert payload["configuration"] == ""
    assert payload["configuration_names"] == []
    assert payload["configuration_required"] is False
    assert payload["detail"] == "full"
    assert payload["markdown"] == expected
    assert "Находит вхождение подстроки" in payload["html"]


@pytest.mark.parametrize("path", ["object", "syntax"])
def test_card_api_требует_токен_чтения(tmp_path, monkeypatch, path):
    monkeypatch.setenv("API_TOKEN", "test-read-token")
    registry = _registry(tmp_path)

    with _client(registry, tmp_path) as client:
        denied = client.get(
            f"/api/v1/cards/{path}",
            params={"name": "Справочник.Контрагенты"},
        )
        allowed = client.get(
            f"/api/v1/cards/{path}",
            params={"name": "Справочник.Контрагенты"},
            headers={"x-api-token": "test-read-token"},
        )

    assert denied.status_code == 401
    assert allowed.status_code in (200, 409)


@pytest.mark.parametrize("path", ["object", "syntax"])
def test_card_api_отклоняет_пустое_имя(tmp_path, path):
    registry = Registry(tmp_path / "data")

    with _client(registry, tmp_path) as client:
        response = client.get(f"/api/v1/cards/{path}")

    assert response.status_code == 422
    assert response.json() == {"error": "Не указано имя карточки."}


def test_object_card_api_объясняет_отсутствующий_registry(tmp_path):
    registry = Registry(tmp_path / "data")

    with _client(registry, tmp_path) as client:
        response = client.get(
            "/api/v1/cards/object",
            params={"name": "Справочник.Контрагенты"},
        )

    assert response.status_code == 409
    assert "Не загружено ни одной конфигурации" in response.json()["error"]


def test_card_api_неизвестную_подробность_нормализует_как_classic(tmp_path):
    registry = _registry(tmp_path)

    with _client(registry, tmp_path) as client:
        response = client.get(
            "/api/v1/cards/object",
            params={
                "name": "Справочник.Контрагенты",
                "detail": "максимум",
            },
        )

    assert response.status_code == 200
    assert response.json()["detail"] == "fields"
