"""JSON API «Связей» сохраняет окрестность и серверную раскладку."""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from mcp1c.dashboard_runtime import DASHBOARD_ON, routes
from mcp1c.graph_view import DEFAULT_LIMIT, bounds, neighbourhood
from mcp1c.model import Configuration, Field, MetadataObject
from mcp1c.registry import Registry

from conftest import build_configuration, write_export


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


def _registry(tmp_path, *names: str) -> Registry:
    data = tmp_path / "data"
    incoming = tmp_path / "incoming"
    data.mkdir()
    incoming.mkdir()
    registry = Registry(data)
    for name in names or ("ТестоваяКонфигурация",):
        registry.add_configuration(write_export(incoming, build_configuration(name)))
    return registry


def _star_registry(tmp_path, neighbours: int) -> Registry:
    config = Configuration(
        name="Звезда", synonym="Звезда", version="1.0", platform="8.3.23.1997"
    )
    subject = MetadataObject(
        full_name="Справочник.Склады", kind="Справочник", name="Склады"
    )
    config.objects = {subject.full_name: subject}
    for number in range(neighbours):
        document = MetadataObject(
            full_name=f"Документ.Д{number:03d}",
            kind="Документ",
            name=f"Д{number:03d}",
            attributes=[Field(name="Склад", types=[subject.full_name])],
        )
        config.objects[document.full_name] = document
    data = tmp_path / "data"
    incoming = tmp_path / "incoming"
    data.mkdir()
    incoming.mkdir()
    registry = Registry(data)
    registry.add_configuration(write_export(incoming, config))
    return registry


def test_graph_api_путь_из_меню_выбирает_первую_конфигурацию(tmp_path):
    registry = _registry(tmp_path, "БетаКонфигурация", "АльфаКонфигурация")

    with _client(registry, tmp_path) as client:
        response = client.get("/api/v1/graph")

    assert response.status_code == 200
    assert response.json() == {
        "api_version": "v1",
        "configuration_names": ["АльфаКонфигурация", "БетаКонфигурация"],
        "configuration": "АльфаКонфигурация",
        "name": "",
        "limit": DEFAULT_LIMIT,
        "limit_options": [15, 30, 60, 150, 400],
        "state": "awaiting_object",
        "message": (
            "Введите полное имя объекта или возьмите его со страницы «Запросы»."
        ),
        "suggestions": [],
        "graph": None,
    }


def test_graph_api_возвращает_ту_же_окрестность_раскладку_и_направление(tmp_path):
    registry = _registry(tmp_path)
    context = registry.resolve("ТестоваяКонфигурация")
    expected = neighbourhood(
        context.configuration.graph,
        "Справочник.Контрагенты",
        limit=DEFAULT_LIMIT,
    )

    with _client(registry, tmp_path) as client:
        response = client.get(
            "/api/v1/graph",
            params={
                "config": "ТестоваяКонфигурация",
                "name": "Справочник.Контрагенты",
                "limit": DEFAULT_LIMIT,
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["state"] == "ready"
    graph = payload["graph"]
    assert graph["total"] == expected.total
    assert graph["shown"] == expected.shown
    assert graph["truncated"] == expected.truncated
    assert graph["depth"] == 1
    assert graph["bounds"] == pytest.approx(list(bounds(expected)))
    assert graph["subject"]["name"] == expected.subject.name
    assert graph["subject"]["object_url"].startswith("/object?config=")
    assert [node["name"] for node in graph["nodes"]] == [
        node.name for node in expected.nodes
    ]
    assert [node["x"] for node in graph["nodes"]] == pytest.approx(
        [node.x for node in expected.nodes]
    )
    assert [link["outgoing"] for link in graph["links"]] == [
        link.outgoing for link in expected.links
    ]
    assert all(link["title"] for link in graph["links"])


def test_graph_api_явно_показывает_усечение(tmp_path):
    registry = _star_registry(tmp_path, neighbours=50)

    with _client(registry, tmp_path) as client:
        response = client.get(
            "/api/v1/graph",
            params={"name": "Справочник.Склады", "limit": 10},
        )

    graph = response.json()["graph"]
    assert graph["total"] == 50
    assert graph["shown"] == 10
    assert graph["truncated"] is True


def test_graph_api_опечатке_предлагает_похожие_объекты(tmp_path):
    registry = _registry(tmp_path)

    with _client(registry, tmp_path) as client:
        response = client.get(
            "/api/v1/graph", params={"name": "Справочник.Контрагент"}
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["state"] == "not_found"
    assert "нет объекта" in payload["message"]
    assert payload["suggestions"][0]["name"] == "Справочник.Контрагенты"
    assert payload["suggestions"][0]["graph_url"].startswith("/graph?config=")


def test_graph_api_изолированный_объект_объясняет_словами(tmp_path):
    registry = _star_registry(tmp_path, neighbours=0)

    with _client(registry, tmp_path) as client:
        response = client.get(
            "/api/v1/graph",
            params={"name": "Справочник.Склады"},
        )

    payload = response.json()
    assert payload["state"] == "isolated"
    assert "не ссылается" in payload["message"]
    assert payload["graph"]["total"] == 0


def test_graph_api_пустой_registry_называет_состояние(tmp_path):
    registry = Registry(tmp_path / "data")

    with _client(registry, tmp_path) as client:
        response = client.get("/api/v1/graph")

    assert response.status_code == 200
    payload = response.json()
    assert payload["configuration_names"] == []
    assert payload["state"] == "empty_registry"
    assert "Не загружено ни одной конфигурации" in payload["message"]


def test_graph_api_требует_токен_чтения(tmp_path, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "test-read-token")
    registry = _registry(tmp_path)

    with _client(registry, tmp_path) as client:
        denied = client.get("/api/v1/graph")
        allowed = client.get(
            "/api/v1/graph", headers={"x-api-token": "test-read-token"}
        )

    assert denied.status_code == 401
    assert allowed.status_code == 200


@pytest.mark.parametrize(
    ("given", "expected"),
    [("не число", DEFAULT_LIMIT), ("0", DEFAULT_LIMIT), ("900", 400)],
)
def test_graph_api_нормализует_предел_к_безопасному_диапазону(
    tmp_path, given, expected
):
    registry = _registry(tmp_path)

    with _client(registry, tmp_path) as client:
        response = client.get("/api/v1/graph", params={"limit": given})

    assert response.status_code == 200
    assert response.json()["limit"] == expected
