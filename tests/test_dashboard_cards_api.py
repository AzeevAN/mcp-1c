"""JSON API карточек сохраняет буквальный ответ MCP и безопасный HTML."""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from mcp1c import tools
from mcp1c.dashboard_backend import render_markdown
from mcp1c.dashboard_runtime import DASHBOARD_ON, routes
from mcp1c.registry import Registry
from mcp1c.store import save_syntax
from mcp1c.syntax_model import SyntaxIndex, SyntaxItem, SyntaxLink, SyntaxLinkSnapshot

from conftest import build_configuration, write_export, write_syntax


def _client(registry: Registry, tmp_path) -> TestClient:
    return TestClient(
        Starlette(
            routes=routes(
                registry,
                mode=DASHBOARD_ON,
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


def test_card_api_неизвестную_подробность_нормализует_к_fields(tmp_path):
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


def _navigation_registry(tmp_path) -> Registry:
    owner_id = "objects/Query/Item"
    links = [
        SyntaxLink(
            section="methods",
            label=f"Метод{i:02d}",
            href=f"methods/Method{i:02d}.html",
            target_id=f"objects/Query/methods/Method{i:02d}",
        )
        for i in range(49)
    ]
    links.extend([
        SyntaxLink("see_also", "Нет страницы", "missing.html"),
        SyntaxLink("methods", "Неоднозначный", "methods/A.html",
                   "objects/Query/methods/A"),
        SyntaxLink("methods", "Поздний", "methods/Late.html",
                   "objects/Query/methods/Late"),
    ])
    index = SyntaxIndex(platforms=["8.3.19"], source="synthetic-8.3.19")
    index.add(SyntaxItem(
        id=owner_id,
        kind="object",
        name_ru="Запрос",
        description="Объект запроса.",
        link_snapshots=[SyntaxLinkSnapshot("8.3.19", owner_id, links)],
    ))
    for i in range(49):
        index.add(SyntaxItem(
            id=f"objects/Query/methods/Method{i:02d}",
            kind="method", name_ru=f"Метод{i:02d}", parent_ru="Запрос",
            description="Тестовый метод.",
        ))
    for item_id in ("A", "B"):
        index.add(SyntaxItem(
            id=f"objects/Query/methods/{item_id}",
            kind="method", name_ru="Неоднозначный", parent_ru="Запрос",
            description="Тестовый метод.",
        ))
    index.add(SyntaxItem(
        id="objects/Query/methods/Late",
        kind="method", name_ru="Поздний", parent_ru="Запрос",
        description="Метод будущей версии.", since="8.3.20",
    ))
    registry = Registry(tmp_path / "data")
    source = save_syntax(index, tmp_path / "syntax.json.gz")
    registry.add_syntax(source)
    config = build_configuration()
    config.platform = "8.3.19"
    registry.add_configuration(write_export(tmp_path, config))
    return registry


def test_syntax_card_api_отдаёт_структурированные_переходы_и_страницы(tmp_path):
    registry = _navigation_registry(tmp_path)
    params = {"config": "ТестоваяКонфигурация", "name": "Запрос"}

    with _client(registry, tmp_path) as client:
        first = client.get("/api/v1/cards/syntax", params={**params, "detail": "full"})
        second = client.get(
            "/api/v1/cards/syntax",
            params={**params, "detail": "links", "links_offset": "50"},
        )

    assert first.status_code == 200
    assert second.status_code == 200
    first_payload = first.json()
    second_payload = second.json()
    assert first_payload["detail_levels"] == ["brief", "fields", "full", "links"]
    assert first_payload["markdown"] == tools.get_syntax(
        registry, "Запрос", config=params["config"], detail="full"
    )
    assert len(first_payload["navigation"]) == 1
    page = first_payload["navigation"][0]
    assert page["state"] == "known"
    assert page["platform"] == "8.3.19"
    assert page["total"] == 52
    assert page["offset"] == 0
    assert page["next_offset"] == 50
    assert len(page["items"]) == 50
    assert page["items"][0] == {
        "section": "methods", "label": "Метод00", "address": "Запрос.Метод00",
        "status": "ready", "target_name": "Метод00",
    }
    assert page["items"][-1]["status"] == "unresolved"
    assert page["items"][-1]["address"] == ""

    following = second_payload["navigation"][0]
    assert following["offset"] == 50
    assert following["next_offset"] is None
    assert len(following["items"]) == 2
    assert [item["status"] for item in following["items"]] == ["ambiguous", "unavailable"]
    assert all(item["address"] == "" for item in following["items"])
    assert "`Запрос.Метод00`" not in second_payload["markdown"]


@pytest.mark.parametrize("raw_offset", ["-1", "abc", "1000001"])
def test_syntax_card_api_отклоняет_некорректное_смещение(tmp_path, raw_offset):
    registry = _navigation_registry(tmp_path)

    with _client(registry, tmp_path) as client:
        response = client.get(
            "/api/v1/cards/syntax",
            params={"name": "Запрос", "detail": "links", "links_offset": raw_offset},
        )

    assert response.status_code == 422
    assert "links_offset" in response.json()["error"]
