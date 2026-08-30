"""JSON API страницы «Запросы» сохраняет поисковый контракт classic UI."""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from mcp1c.dashboard import MAX_QUERY_PHRASES
from mcp1c.dashboard_runtime import DASHBOARD_SPA, routes
from mcp1c.registry import Registry
from mcp1c.search import MAX_QUERY_CHARS

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


def _registry(tmp_path, *, platform: str = "8.3.23.1997") -> Registry:
    data = tmp_path / "data"
    incoming = tmp_path / "incoming"
    data.mkdir()
    incoming.mkdir()
    configuration = build_configuration()
    configuration.platform = platform
    registry = Registry(data)
    registry.add_configuration(write_export(incoming, configuration))
    return registry


def test_get_queries_api_описывает_пустое_состояние_и_границы(tmp_path):
    registry = Registry(tmp_path / "data")

    with _client(registry, tmp_path) as client:
        response = client.get("/api/v1/queries")

    assert response.status_code == 200
    assert response.json() == {
        "api_version": "v1",
        "configuration_names": [],
        "default_configuration": "",
        "scopes": [
            {"id": "objects", "label": "Объекты", "requires_configuration": True},
            {"id": "fields", "label": "Реквизиты", "requires_configuration": True},
            {
                "id": "syntax",
                "label": "Справка платформы",
                "requires_configuration": False,
            },
        ],
        "limits": {
            "phrases": MAX_QUERY_PHRASES,
            "phrase_chars": MAX_QUERY_CHARS,
            "results_per_phrase": 5,
        },
        "availability": {
            "configurations": False,
            "syntax": False,
        },
    }


def test_queries_api_требует_токен_чтения(tmp_path, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "test-read-token")
    registry = Registry(tmp_path / "data")

    with _client(registry, tmp_path) as client:
        denied_get = client.get("/api/v1/queries")
        denied_post = client.post(
            "/api/v1/queries",
            json={"config": "", "scope": "syntax", "phrases": ["СтрНайти"]},
        )
        allowed = client.get(
            "/api/v1/queries", headers={"x-api-token": "test-read-token"}
        )

    assert denied_get.status_code == 401
    assert denied_post.status_code == 401
    assert allowed.status_code == 200


def test_post_queries_api_возвращает_те_же_попадания_и_ссылки(tmp_path):
    registry = _registry(tmp_path)
    registry.add_syntax(write_syntax(tmp_path / "data" / "index" / "syntax"))

    with _client(registry, tmp_path) as client:
        response = client.post(
            "/api/v1/queries",
            json={
                "config": "ТестоваяКонфигурация",
                "scope": "objects",
                "phrases": [
                    "контрагенты",
                    "реализация услуг",
                    "контрагенты реализация",
                ],
            },
        )

    assert response.status_code == 200
    payload = response.json()
    assert payload["request"] == {
        "config": "ТестоваяКонфигурация",
        "scope": "objects",
        "phrases": [
            "контрагенты",
            "реализация услуг",
            "контрагенты реализация",
        ],
    }
    first = payload["results"][0]
    assert first["phrase"] == "контрагенты"
    assert first["hits"][0]["title"] == "Справочник.Контрагенты"
    assert first["hits"][0]["kind"] == "Справочник"
    assert first["hits"][0]["reason"] == "псевдоним из словаря"
    assert first["hits"][0]["card_url"].startswith(
        "/object?config=%D0%A2%D0%B5%D1%81%D1%82"
    )
    assert first["alias_url"].startswith("/dictionary?config=")
    assert len(first["hits"]) <= 5

    partial = payload["results"][2]
    assert partial["hits"]
    assert all(hit["reason"] for hit in partial["hits"])
    assert any(
        hit["reason"] == "совпала часть слов запроса"
        for hit in partial["hits"]
    )


def test_queries_api_показывает_скрытые_версией_попадания(tmp_path):
    registry = _registry(tmp_path, platform="8.3.5.1570")
    registry.add_syntax(write_syntax(tmp_path / "data" / "index" / "syntax"))

    with _client(registry, tmp_path) as client:
        response = client.post(
            "/api/v1/queries",
            json={
                "config": "ТестоваяКонфигурация",
                "scope": "syntax",
                "phrases": ["СтрНайти"],
            },
        )

    assert response.status_code == 200
    result = response.json()["results"][0]
    assert all("СтрНайти" not in hit["title"] for hit in result["hits"])
    assert any(
        hidden["title"] == "СтрНайти" and "появился в 8.3.6" in hidden["reason"]
        for hidden in result["hidden"]
    )


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        (
            {"config": "", "scope": "modules", "phrases": ["контрагенты"]},
            "Неизвестная область поиска",
        ),
        (
            {"config": "", "scope": "objects", "phrases": []},
            "Не указано ни одной фразы",
        ),
        (
            {
                "config": "",
                "scope": "objects",
                "phrases": ["контрагенты"] * (MAX_QUERY_PHRASES + 1),
            },
            "не более 32 фраз",
        ),
        (
            {
                "config": "",
                "scope": "objects",
                "phrases": ["я" * (MAX_QUERY_CHARS + 1)],
            },
            "не более 4096 символов",
        ),
    ],
)
def test_queries_api_отклоняет_неверный_контракт_до_поиска(
    tmp_path, payload, message
):
    registry = Registry(tmp_path / "data")

    with _client(registry, tmp_path) as client:
        response = client.post("/api/v1/queries", json=payload)

    assert response.status_code == 422
    assert message in response.json()["error"]


def test_queries_api_объясняет_отсутствующую_справку(tmp_path):
    registry = _registry(tmp_path)

    with _client(registry, tmp_path) as client:
        response = client.post(
            "/api/v1/queries",
            json={
                "config": "ТестоваяКонфигурация",
                "scope": "syntax",
                "phrases": ["СтрНайти"],
            },
        )

    assert response.status_code == 409
    assert response.json() == {"error": "Справка платформы не подключена."}
