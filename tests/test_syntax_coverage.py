"""Покрытие: под какие конфигурации справок не хватает, а какие лишние.

Справок нужно столько, сколько различных платформ у загруженных конфигураций.
Знает об этом только сервер: он видит и платформы конфигураций, и версии
справок. Молчать нельзя — человек не догадается, что ответы про его базу
собраны по чужой версии.
"""

from __future__ import annotations

from starlette.applications import Starlette
from starlette.testclient import TestClient

from mcp1c import dashboard
from mcp1c.registry import Registry
from mcp1c.tools import list_configurations

from conftest import build_configuration, write_export
from test_registry_syntax_versions import справка


def реестр(tmp_path, справки: list[str], платформы: dict[str, str]) -> Registry:
    registry = Registry(tmp_path / "data")
    incoming = tmp_path / "incoming"
    for платформа in справки:
        registry.add_syntax(справка(incoming, платформа, имена=("Найти",)))
    for имя, платформа in платформы.items():
        config = build_configuration(имя)
        config.platform = платформа
        registry.add_configuration(write_export(incoming, config))
    return registry


def test_покрытие_называет_недостающую_справку(tmp_path):
    registry = реестр(
        tmp_path,
        ["8.3.5.1570"],
        {"Ювелирный": "8.3.5.1570", "Розница": "8.3.23.1997"},
    )

    покрытие = registry.syntax_coverage()

    assert покрытие["missing"] == [
        {"platform": "8.3.23.1997", "configurations": ["Розница"]}
    ]
    assert покрытие["unused"] == []


def test_покрытие_называет_лишнюю_справку(tmp_path):
    registry = реестр(
        tmp_path,
        ["8.3.5.1570", "8.3.19.1417"],
        {"Ювелирный": "8.3.5.1570"},
    )

    покрытие = registry.syntax_coverage()

    assert покрытие["missing"] == []
    assert покрытие["unused"] == ["8.3.19.1417"]


def test_покрытие_считает_по_релизу_а_не_по_сборке(tmp_path):
    """Справка приходит сборкой, конфигурация живёт на своей. Требовать
    совпадения четырёх чисел — значит объявлять недостающей справку, которая
    описывает ровно ту же платформу."""
    registry = реестр(tmp_path, ["8.3.5.1570"], {"Ювелирный": "8.3.5.1234"})

    покрытие = registry.syntax_coverage()

    assert покрытие["missing"] == []
    assert покрытие["unused"] == []


def test_список_конфигураций_называет_чего_не_хватает(tmp_path):
    registry = реестр(
        tmp_path,
        ["8.3.5.1570", "8.3.19.1417"],
        {"Ювелирный": "8.3.5.1570", "Розница": "8.3.23.1997"},
    )

    ответ = list_configurations(registry)

    assert "Справки платформы" in ответ
    assert "не хватает" in ответ.lower()
    assert "8.3.23.1997" in ответ and "Розница" in ответ
    # Лишняя справка — не ошибка, но занимает память и вводит в заблуждение.
    assert "не используется" in ответ.lower()
    assert "8.3.19.1417" in ответ


def test_страница_обзора_показывает_покрытие(tmp_path):
    registry = реестр(
        tmp_path,
        ["8.3.5.1570"],
        {"Ювелирный": "8.3.5.1570", "Розница": "8.3.23.1997"},
    )
    client = TestClient(Starlette(routes=dashboard.routes(registry)))

    страница = client.get("/").text

    assert "Справки платформы" in страница
    assert "не хватает" in страница.lower()
    assert "8.3.23.1997" in страница and "Розница" in страница


def test_прогон_запросов_называет_скрытое_по_версии(tmp_path):
    """Дашборд фильтровал по версии молча: элемент просто выпадал из выдачи.

    Человек делает из этого неверный вывод — «метод не нашёлся», хотя метод
    есть и недоступен только в его версии. Через MCP сервер это говорит, и
    дашборд обязан показывать то же: он для того и заведён, чтобы видеть, что
    получит агент.
    """
    registry = Registry(tmp_path / "data")
    incoming = tmp_path / "incoming"
    registry.add_syntax(справка(incoming, "8.3.5.1570", имена=("Найти",)))
    новая = справка(incoming, "8.3.27.2130", имена=("Найти", "СтрРазделить"))
    from mcp1c.store import load_syntax, save_syntax

    индекс = load_syntax(новая)
    for item in индекс.items.values():
        if item.name_ru == "СтрРазделить":
            item.since = "8.3.6"
    save_syntax(индекс, новая)
    registry.add_syntax(новая)
    config = build_configuration("Ювелирный")
    config.platform = "8.3.5.1570"
    registry.add_configuration(write_export(incoming, config))
    client = TestClient(Starlette(routes=dashboard.routes(registry)))

    страница = client.post(
        "/queries",
        data={"config": "Ювелирный", "scope": "syntax", "phrases": "СтрРазделить"},
    ).text

    assert "СтрРазделить" in страница
    assert "8.3.6" in страница
    assert "скрыт" in страница.lower()
