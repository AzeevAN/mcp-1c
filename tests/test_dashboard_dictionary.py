"""Правка словаря через браузер.

Словарь — единственное место, куда знание попадает не из выгрузки, поэтому
правка требует `ADMIN_TOKEN`: на общем сервере одна неудачная запись тихо
ломает поиск всем. Чтение открыто — с него начинается разбор «почему поиск
так себя ведёт».

Проверяется наблюдаемое: после правки поиск ведёт себя иначе, а не «в json
появилась строка».
"""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.testclient import TestClient

from mcp1c import dashboard
from mcp1c.registry import Registry

from conftest import build_configuration, write_export


def client_for(tmp_path) -> tuple[TestClient, Registry]:
    data_dir = tmp_path / "data"
    incoming = tmp_path / "incoming"
    data_dir.mkdir()
    incoming.mkdir()
    registry = Registry(data_dir)
    registry.add_configuration(write_export(incoming, build_configuration()))
    app = Starlette(routes=dashboard.routes(registry))
    return TestClient(app), registry


def test_страница_показывает_псевдоним_с_происхождением(tmp_path):
    client, registry = client_for(tmp_path)
    registry.dictionary.add_alias("склад хранения", ["Справочник.Контрагенты"],
                                  "ТестоваяКонфигурация")

    response = client.get("/dictionary?config=ТестоваяКонфигурация")

    assert response.status_code == 200
    assert "склад хранения" in response.text
    assert "Справочник.Контрагенты" in response.text
    # Происхождение — с него начинается разбор, чьё правило сработало.
    assert "локальный" in response.text


def test_добавление_псевдонима_без_токена_отклоняется(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "секрет")
    client, registry = client_for(tmp_path)

    response = client.post(
        "/dictionary/alias",
        data={"phrase": "поставщики", "targets": "Справочник.Контрагенты",
              "config": "ТестоваяКонфигурация"},
    )

    assert response.status_code == 403
    assert registry.dictionary.aliases == {}


def test_без_переменной_окружения_правки_нет(tmp_path, monkeypatch):
    monkeypatch.delenv("ADMIN_TOKEN", raising=False)
    client, registry = client_for(tmp_path)

    response = client.post(
        "/dictionary/alias",
        data={"phrase": "поставщики", "targets": "Справочник.Контрагенты"},
    )

    assert response.status_code == 404
    assert registry.dictionary.aliases == {}


def test_добавленный_псевдоним_меняет_выдачу_поиска(tmp_path, monkeypatch):
    """Главная проверка: правка действует сразу, без перезапуска.

    Псевдоним не участвует в построении постингов, поэтому индексы не
    пересобираются — но выдача обязана измениться тем же запросом.
    """
    monkeypatch.setenv("ADMIN_TOKEN", "секрет")
    client, registry = client_for(tmp_path)
    client.post("/login", data={"token": "секрет"})

    прогон = client.post(
        "/queries",
        data={"config": "ТестоваяКонфигурация", "scope": "objects",
              "phrases": "кто нам возит"},
    )
    assert "Справочник.Контрагенты" not in прогон.text

    response = client.post(
        "/dictionary/alias",
        data={"phrase": "кто нам возит", "targets": "Справочник.Контрагенты",
              "config": "ТестоваяКонфигурация"},
        follow_redirects=False,
    )
    assert response.status_code == 303

    снова = client.post(
        "/queries",
        data={"config": "ТестоваяКонфигурация", "scope": "objects",
              "phrases": "кто нам возит"},
    )
    assert "Справочник.Контрагенты" in снова.text
    assert "псевдоним" in снова.text


def test_удаление_псевдонима(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "секрет")
    client, registry = client_for(tmp_path)
    registry.dictionary.add_alias("склад хранения", ["Справочник.Контрагенты"],
                                  "ТестоваяКонфигурация")
    client.post("/login", data={"token": "секрет"})

    response = client.post(
        "/dictionary/alias/remove",
        data={"phrase": "склад хранения", "config": "ТестоваяКонфигурация"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert registry.dictionary.aliases_for(
        "ТестоваяКонфигурация", with_builtin=False
    ) == {}


def test_добавление_группы_синонимов(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "секрет")
    client, registry = client_for(tmp_path)
    client.post("/login", data={"token": "секрет"})

    response = client.post(
        "/dictionary/synonyms",
        data={"words": "возчик перевозчик экспедитор"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert ["возчик", "перевозчик", "экспедитор"] in registry.dictionary.synonym_groups
    # Группа из одного слова бессмысленна — заводить нечего.
    отказ = client.post("/dictionary/synonyms", data={"words": "возчик"})
    assert отказ.status_code == 200
    assert "class=error" in отказ.text


def test_правка_переживает_перезапуск(tmp_path, monkeypatch):
    """Словарь пишется на диск, а не живёт в памяти процесса."""
    monkeypatch.setenv("ADMIN_TOKEN", "секрет")
    client, registry = client_for(tmp_path)
    client.post("/login", data={"token": "секрет"})

    client.post(
        "/dictionary/alias",
        data={"phrase": "кто нам возит", "targets": "Справочник.Контрагенты",
              "config": "ТестоваяКонфигурация"},
    )

    заново = Registry(tmp_path / "data")
    assert заново.dictionary.aliases_for(
        "ТестоваяКонфигурация", with_builtin=False
    ) == {"кто нам возит": ["Справочник.Контрагенты"]}


def test_фраза_не_исполняется_как_разметка(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "секрет")
    client, registry = client_for(tmp_path)
    registry.dictionary.add_alias("<script>alert(1)</script>",
                                  ["Справочник.Контрагенты"])

    response = client.get("/dictionary")

    assert "<script>alert(1)</script>" not in response.text
    assert "&lt;script&gt;" in response.text


def test_прогон_запросов_ведёт_к_заведению_псевдонима(tmp_path):
    """Ради этого всё и делается: от промаха до лечения — один переход."""
    client, _ = client_for(tmp_path)

    response = client.post(
        "/queries",
        data={"config": "ТестоваяКонфигурация", "scope": "objects",
              "phrases": "контрагенты"},
    )

    assert "/dictionary?" in response.text
    assert "phrase=" in response.text


def test_группа_синонимов_снимается_из_браузера(tmp_path, monkeypatch):
    """Завести можно было, снять — нет. Асимметрия, вылезшая при работе."""
    monkeypatch.setenv("ADMIN_TOKEN", "секрет")
    client, registry = client_for(tmp_path)
    registry.dictionary.add_synonyms(["возчик", "перевозчик"])
    client.post("/login", data={"token": "секрет"})

    страница = client.get("/dictionary").text
    assert "возчик, перевозчик" in страница

    ответ = client.post(
        "/dictionary/synonyms/remove",
        data={"words": "возчик перевозчик"},
        follow_redirects=False,
    )

    assert ответ.status_code == 303
    assert registry.dictionary.synonym_groups == []


def test_снятие_группы_без_токена_отклоняется(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_TOKEN", "секрет")
    client, registry = client_for(tmp_path)
    registry.dictionary.add_synonyms(["возчик", "перевозчик"])

    ответ = client.post(
        "/dictionary/synonyms/remove", data={"words": "возчик перевозчик"}
    )

    assert ответ.status_code == 403
    assert len(registry.dictionary.synonym_groups) == 1
