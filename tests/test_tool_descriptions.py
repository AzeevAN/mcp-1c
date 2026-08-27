"""Контракт, который клиент получает по `tools/list`.

Описания инструментов и параметров — не документация для человека, а
единственное, по чему агент решает, что вызвать и с чем. Пустое описание он
не заметит: просто угадает и ошибётся молча. Поэтому проверяется не текст
(он живой и будет меняться), а то, что контракт вообще заполнен.
"""

from __future__ import annotations

import pytest

from mcp1c.registry import Registry
from mcp1c.server import INSTRUCTIONS, build_server
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
    """Ключевая связка публичного порядка вызовов: поиск -> подробности.

    Периодичность регистра и имена полей виртуальных таблиц приходят только
    через `get_object`/`get_syntax`. Агент, пишущий запрос сразу после поиска,
    получает «поле не найдено», поэтому переход обязан быть назван прямо в
    описании поискового инструмента, а не только в `instructions` сервера:
    их часть клиентов сокращает или не показывает вовсе.
    """
    по_имени = {tool.name: tool.description or "" for tool in await инструменты()}

    assert "get_object" in по_имени["search_objects"]
    assert "get_syntax" in по_имени["search_syntax"]


async def test_initialize_и_tools_list_честно_описывают_версии_языка_запросов(
    инструменты, tmp_path
):
    """Два постоянных MCP-текста не должны противоречить фильтру выдачи.

    В самом `shquery_ru.hbk` нет отметок версий, но это не означает, что язык
    запросов не меняется: подтверждённые границы добавляет курируемая таблица.
    Старый текст смешивал эти два факта и всю сессию говорил агенту, что
    фильтр языка запросов не касается.
    """
    ожидаемый_контракт = (
        "В самом `shquery_ru.hbk` версии появления не записаны; известные "
        "границы заданы курируемой таблицей и при заданном `config` "
        "фильтруются по версии платформы конфигурации."
    )
    server = build_server(Registry(tmp_path / "initialize-data"))
    initialize = server._lowlevel_server.create_initialization_options()
    (поиск,) = [
        tool for tool in await инструменты() if tool.name == "search_syntax"
    ]

    assert initialize.instructions == server.instructions == INSTRUCTIONS
    for текст in (initialize.instructions or "", поиск.description or ""):
        assert ожидаемый_контракт in текст
        assert "язык запросов версий не имеет" not in текст.lower()
        assert "фильтр его не касается" not in текст.lower()


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


async def test_карточка_процедуры_зарегистрирована_с_английскими_параметрами(
    инструменты,
):
    все = await инструменты()
    assert len(все) == 10
    (карточка,) = [tool for tool in все if tool.name == "get_procedure"]

    schema = карточка.input_schema or {}
    свойства = schema.get("properties") or {}
    assert set(свойства) == {
        "address",
        "config",
        "extension",
        "start_line",
        "lines",
    }
    assert set(schema.get("required") or []) == {"address"}
    assert свойства["start_line"]["minimum"] == 0
    assert свойства["lines"]["minimum"] == 1
    assert свойства["lines"]["maximum"] == 200
    assert "оглавлен" in (карточка.description or "").lower()
    assert "тел" in (карточка.description or "").lower()


async def test_обратный_поиск_вызовов_зарегистрирован_десятым_инструментом(
    инструменты,
):
    все = await инструменты()
    assert [tool.name for tool in все][2:5] == [
        "search_procedures",
        "get_procedure",
        "get_callers",
    ]
    (вызовы,) = [tool for tool in все if tool.name == "get_callers"]

    schema = вызовы.input_schema or {}
    свойства = schema.get("properties") or {}
    assert set(свойства) == {"address", "config", "extension", "limit"}
    assert set(schema.get("required") or []) == {"address"}
    assert свойства["limit"]["minimum"] == 1
    assert свойства["limit"]["maximum"] == 50
    assert "мест" in (вызовы.description or "").lower()
    assert "привяз" in (вызовы.description or "").lower()


async def test_tools_list_сохраняет_полный_порядок_десяти_инструментов(
    инструменты,
):
    assert [tool.name for tool in await инструменты()] == [
        "list_configurations",
        "search_objects",
        "search_procedures",
        "get_procedure",
        "get_callers",
        "get_object",
        "get_related",
        "compare_configurations",
        "search_syntax",
        "get_syntax",
    ]


def test_instructions_называет_get_object_шестым_шагом():
    assert "6. `get_object`" in INSTRUCTIONS
    assert "Не пропускайте шаг 6" in INSTRUCTIONS
    assert "Не пропускайте шаг 5" not in INSTRUCTIONS
