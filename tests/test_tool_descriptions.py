"""Контракт, который клиент получает по `tools/list`.

Описания инструментов и параметров — не документация для человека, а
единственное, по чему агент решает, что вызвать и с чем. Пустое описание он
не заметит: просто угадает и ошибётся молча. Поэтому проверяется не текст
(он живой и будет меняться), а то, что контракт вообще заполнен.
"""

from __future__ import annotations

import pytest

from mcp1c.registry import Registry
from mcp1c.server import build_server
from mcp1c.store import save_syntax
from mcp1c.syntax_model import SyntaxIndex, SyntaxItem

from conftest import build_configuration, write_export

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def инструменты(tmp_path):
    """Полный набор регистрируется независимо от состава источников."""

    async def получить():
        registry = Registry(tmp_path / "data")
        incoming = tmp_path / "incoming"
        incoming.mkdir(parents=True, exist_ok=True)
        index = SyntaxIndex(platforms=["8.3.23.1997"], source="test")
        index.add(
            SyntaxItem(
                id="objects/Глобальный контекст/methods/СтрНайти",
                kind="method",
                name_ru="СтрНайти",
                parent_ru="Глобальный контекст",
                description="Описание СтрНайти",
            )
        )
        registry.add_syntax(save_syntax(index, incoming / "8.3.23.1997.json.gz"))
        registry.add_configuration(write_export(incoming, build_configuration()))
        вторая = tmp_path / "вторая"
        вторая.mkdir(parents=True, exist_ok=True)
        registry.add_configuration(
            write_export(вторая, build_configuration(name="ВтораяКонфигурация"))
        )
        server = build_server(registry)
        return await server.list_tools()

    return получить


async def test_у_каждого_инструмента_есть_описание(инструменты):
    for tool in await инструменты():
        assert (tool.description or "").strip(), f"{tool.name}: пустое описание"


async def test_у_каждого_параметра_есть_описание(инструменты):
    """Без описания агент видит только тип и додумывает смысл.

    Так уже было с `config`: тип `str`, и агент подставлял путь к файлу
    вместо имени конфигурации из `list_configurations`.
    """
    for tool in await инструменты():
        свойства = (tool.input_schema or {}).get("properties") or {}
        for имя, поле in свойства.items():
            assert (pole_описание := поле.get("description", "").strip()), (
                f"{tool.name}.{имя}: параметр без описания"
            )
            assert len(pole_описание) > 15, f"{tool.name}.{имя}: описание ни о чём"


async def test_у_связей_нет_параметра_глубины(инструменты):
    """Снят 2026-08-18 вместе с разделом «в радиусе N», который врал.

    Он обещал соседей соседей, а отдавал продолжение списка прямых соседей:
    обход упирался в свой лимит раньше, чем делал второй шаг. Замер — 199
    строк, все с расстоянием 1, ценой 3 000 лишних токенов на вызов.

    Параметр стережётся тестом, потому что соблазн вернуть его велик: он
    выглядит дешёвым улучшением, а на деле список имён без объяснения связи
    бесполезен и показанный целиком.
    """
    (связи,) = [t for t in await инструменты() if t.name == "get_related"]

    свойства = (связи.input_schema or {}).get("properties") or {}

    assert "depth" not in свойства
    assert set(свойства) == {"full_name", "config"}


async def test_поиск_отправляет_к_подробностям(инструменты):
    """Ключевая связка задачи «порядок вызовов»: поиск -> подробности.

    Периодичность регистра и имена полей виртуальных таблиц приходят только
    через `get_object`/`get_syntax`. Агент, пишущий запрос сразу после поиска,
    получает «поле не найдено», поэтому переход обязан быть назван прямо в
    описании поискового инструмента, а не только в `instructions` сервера:
    их часть клиентов сокращает или не показывает вовсе.
    """
    по_имени = {tool.name: tool.description or "" for tool in await инструменты()}

    assert "get_object" in по_имени["search_objects"]
    assert "get_syntax" in по_имени["search_syntax"]


async def test_поиск_процедур_зарегистрирован_с_полным_контрактом(инструменты):
    """Клиент видит явный scope и выбор расширения, а не угадывает их."""
    (поиск,) = [
        tool for tool in await инструменты() if tool.name == "search_procedures"
    ]

    свойства = (поиск.input_schema or {}).get("properties") or {}
    assert set(свойства) == {
        "query",
        "config",
        "extension",
        "scope",
        "limit",
    }
    assert set((поиск.input_schema or {}).get("required") or []) == {"query"}
    assert "экспорт" in (поиск.description or "").lower()
    assert "scope" in свойства["scope"]["description"]
    assert "list_configurations" in свойства["config"]["description"]
    assert свойства["limit"]["minimum"] == 1
    assert свойства["limit"]["maximum"] == 50
