"""JSON API «Словаря» работает поверх того же локального Dictionary."""

from __future__ import annotations

from starlette.applications import Starlette

from conftest import build_configuration, живой_клиент, write_export
from mcp1c.dashboard_runtime import DASHBOARD_SPA, routes
from mcp1c.dictionary import ANY_CONFIGURATION, SOURCE_BUILTIN
from mcp1c.registry import Registry


def _registry(tmp_path, *names: str) -> Registry:
    data = tmp_path / "data"
    exports = tmp_path / "exports"
    data.mkdir()
    exports.mkdir()
    registry = Registry(data)
    for name in names or ("ТестоваяКонфигурация",):
        registry.add_configuration(write_export(exports, build_configuration(name)))
    return registry


def _client(registry: Registry):
    return живой_клиент(Starlette(routes=routes(registry, mode=DASHBOARD_SPA)))


def _login(client, token: str = "admin-token") -> None:
    response = client.post("/login", data={"token": token}, follow_redirects=False)
    assert response.status_code == 303


def test_dictionary_api_из_меню_выбирает_первую_конфигурацию_и_показывает_слои(
    tmp_path,
):
    registry = _registry(tmp_path, "БетаКонфигурация", "АльфаКонфигурация")
    registry.dictionary.add_alias(
        "общая фраза", ["Справочник.Контрагенты"], None
    )
    registry.dictionary.add_alias(
        "локальная фраза", ["Справочник.Контрагенты"], "АльфаКонфигурация"
    )
    registry.dictionary.add_synonyms(["возчик", "перевозчик"])

    with _client(registry) as client:
        response = client.get("/api/v1/dictionary")

    assert response.status_code == 200
    payload = response.json()
    assert payload["configuration_names"] == [
        "АльфаКонфигурация",
        "БетаКонфигурация",
    ]
    assert payload["configuration"] == "АльфаКонфигурация"
    assert payload["permissions"] == {"read": True, "admin": False}
    aliases = {item["phrase"]: item for item in payload["aliases"]}
    assert aliases["общая фраза"]["scope"] == ANY_CONFIGURATION
    assert aliases["общая фраза"]["removable"] is True
    assert aliases["локальная фраза"]["scope"] == "АльфаКонфигурация"
    assert any(
        item["source"] == SOURCE_BUILTIN
        and item["scope"] is None
        and item["removable"] is False
        for item in payload["aliases"]
    )
    assert payload["synonym_groups"] == [["возчик", "перевозчик"]]
    assert payload["stats"]["local_synonym_groups"] == 1
    assert payload["stats"]["local_aliases"] == 2


def test_dictionary_api_пустой_registry_оставляет_общие_правила_видимыми(tmp_path):
    registry = Registry(tmp_path / "data")
    registry.dictionary.add_alias(
        "общая фраза", ["Справочник.Контрагенты"], None
    )

    with _client(registry) as client:
        response = client.get("/api/v1/dictionary")

    payload = response.json()
    assert payload["configuration_names"] == []
    assert payload["configuration"] == ""
    assert any(item["phrase"] == "общая фраза" for item in payload["aliases"])
    assert payload["stats"]["builtin_aliases"] > 0


def test_dictionary_api_добавляет_псевдоним_и_сразу_меняет_поиск(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    registry = _registry(tmp_path)

    with _client(registry) as client:
        _login(client)
        added = client.post(
            "/api/v1/dictionary/aliases",
            json={
                "phrase": "кто нам возит",
                "targets": ["Справочник.Контрагенты"],
                "config": "ТестоваяКонфигурация",
            },
        )
        search = client.post(
            "/api/v1/queries",
            json={
                "config": "ТестоваяКонфигурация",
                "scope": "objects",
                "phrases": ["кто нам возит"],
            },
        )

    assert added.status_code == 200
    assert added.json()["changed"] == {
        "phrase": "кто нам возит",
        "targets": ["Справочник.Контрагенты"],
        "scope": "ТестоваяКонфигурация",
    }
    first = search.json()["results"][0]["hits"][0]
    assert first["id"] == "Справочник.Контрагенты"
    assert "псевдоним" in first["reason"]
    reloaded = Registry(registry.data_dir)
    assert reloaded.dictionary.aliases_for(
        "ТестоваяКонфигурация", with_builtin=False
    )["кто нам возит"] == ["Справочник.Контрагенты"]


def test_dictionary_api_снимает_точный_локальный_слой_псевдонима(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    registry = _registry(tmp_path)
    registry.dictionary.add_alias(
        "склад", ["Справочник.Контрагенты"], None
    )
    registry.dictionary.add_alias(
        "склад", ["Документ.Приход"], "ТестоваяКонфигурация"
    )

    with _client(registry) as client:
        _login(client)
        response = client.post(
            "/api/v1/dictionary/aliases/remove",
            json={"phrase": "склад", "scope": "ТестоваяКонфигурация"},
        )

    assert response.status_code == 200
    assert registry.dictionary.aliases[ANY_CONFIGURATION]["склад"] == [
        "Справочник.Контрагенты"
    ]
    assert "склад" not in registry.dictionary.aliases["ТестоваяКонфигурация"]


def test_dictionary_api_добавляет_и_снимает_группу_по_полному_составу(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    registry = _registry(tmp_path)

    with _client(registry) as client:
        _login(client)
        added = client.post(
            "/api/v1/dictionary/synonyms",
            json={"words": ["экспедитор", "возчик", "перевозчик"]},
        )
        removed = client.post(
            "/api/v1/dictionary/synonyms/remove",
            json={"words": ["возчик", "перевозчик", "экспедитор"]},
        )
        missing = client.post(
            "/api/v1/dictionary/synonyms/remove",
            json={"words": ["возчик", "перевозчик"]},
        )

    assert added.status_code == 200
    assert added.json()["changed"] == {
        "words": ["возчик", "перевозчик", "экспедитор"]
    }
    assert removed.status_code == 200
    assert missing.status_code == 404
    assert registry.dictionary.synonym_groups == []


def test_dictionary_api_отклоняет_ошибочные_значения_без_записи(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    registry = _registry(tmp_path)

    with _client(registry) as client:
        _login(client)
        alias = client.post(
            "/api/v1/dictionary/aliases",
            json={"phrase": "пустая цель", "targets": [], "config": ""},
        )
        synonyms = client.post(
            "/api/v1/dictionary/synonyms", json={"words": ["одно"]}
        )

    assert alias.status_code == 422
    assert synonyms.status_code == 422
    assert registry.dictionary.aliases == {}
    assert registry.dictionary.synonym_groups == []


def test_dictionary_api_правки_требуют_администратора(tmp_path, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "read-token")
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    registry = _registry(tmp_path)

    with _client(registry) as client:
        _login(client, "read-token")
        page = client.get("/api/v1/dictionary")
        denied = client.post(
            "/api/v1/dictionary/aliases",
            json={
                "phrase": "склад",
                "targets": ["Справочник.Контрагенты"],
                "config": "",
            },
        )

    assert page.status_code == 200
    assert page.json()["permissions"] == {"read": True, "admin": False}
    assert denied.status_code == 403
    assert registry.dictionary.aliases == {}


def test_dictionary_api_чтение_требует_токен(tmp_path, monkeypatch):
    monkeypatch.setenv("API_TOKEN", "read-token")
    registry = _registry(tmp_path)

    with _client(registry) as client:
        denied = client.get("/api/v1/dictionary")
        allowed = client.get(
            "/api/v1/dictionary", headers={"x-api-token": "read-token"}
        )

    assert denied.status_code == 401
    assert allowed.status_code == 200


def test_dictionary_api_cookie_правка_отвергает_чужой_origin(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("ADMIN_TOKEN", "admin-token")
    registry = _registry(tmp_path)

    with _client(registry) as client:
        _login(client)
        denied = client.post(
            "/api/v1/dictionary/aliases",
            headers={"origin": "http://sibling.test"},
            json={
                "phrase": "склад",
                "targets": ["Справочник.Контрагенты"],
                "config": "",
            },
        )

    assert denied.status_code == 403
    assert registry.dictionary.aliases == {}
