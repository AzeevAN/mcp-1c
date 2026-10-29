"""`search_syntax` по имени объекта конфигурации подсказывает про `get_object`.

Запрос `ТоварыНаСкладах` в справке платформы находит только шаблоны
(`РегистрНакопления.<Имя регистра накопления>.Остатки`) и молчит, что таблицы
запроса самого регистра с подставленными полями лежат в `get_object`, разделе
«Таблицы запроса». Когда выбрана конфигурация и в ней есть объект с таким
именем, выдача говорит об этом прямо — в том числе при пустой выдаче справки.
"""

from __future__ import annotations

import pytest

from mcp1c.model import MetadataObject
from mcp1c.registry import Registry
from mcp1c.server import build_server
from mcp1c.store import save_syntax
from mcp1c.syntax_model import SyntaxIndex, SyntaxItem
from mcp1c.tools import search_syntax

from conftest import build_configuration, write_export

ПЛАТФОРМА = "8.3.27.2130"
ОБЪЕКТ = "РегистрНакопления.ТоварыНаСкладах"
ПОДСКАЗКА = (
    "В конфигурации есть `РегистрНакопления.ТоварыНаСкладах`: таблицы запроса "
    "с подставленными полями — `get_object`, раздел «Таблицы запроса»."
)


def _справка(*items: SyntaxItem) -> SyntaxIndex:
    index = SyntaxIndex(platforms=[ПЛАТФОРМА], source="test")
    for item in items:
        index.add(item)
    return index


_СТРНАЙТИ = SyntaxItem(
    id="objects/Глобальный контекст/methods/СтрНайти",
    kind="method",
    name_ru="СтрНайти",
    parent_ru="Глобальный контекст",
    description="Находит вхождение подстроки.",
)
_ШАБЛОН = SyntaxItem(
    id="tables/c1/t1",
    kind="query_table",
    name_ru="РегистрНакопления.<Имя регистра накопления>.Остатки",
    description="Остатки регистра накопления.",
)
# Метод с тем же именем, что и объект: выдача справки не пуста.
_СОВПАДАЮЩЕЕ_ИМЯ = SyntaxItem(
    id="objects/Глобальный контекст/methods/ТоварыНаСкладах",
    kind="method",
    name_ru="ТоварыНаСкладах",
    parent_ru="Глобальный контекст",
    description="Метод с именем объекта.",
)


def _конфигурация():
    конфигурация = build_configuration()
    конфигурация.platform = ПЛАТФОРМА
    конфигурация.objects[ОБЪЕКТ] = MetadataObject(
        full_name=ОБЪЕКТ,
        kind="РегистрНакопления",
        name="ТоварыНаСкладах",
        synonym="Товары на складах",
        props={"register_kind": "Остатки"},
    )
    return конфигурация


def _реестр(tmp_path, *items: SyntaxItem, с_конфигурацией: bool = True) -> Registry:
    входящие = tmp_path / "incoming"
    входящие.mkdir(parents=True, exist_ok=True)
    реестр = Registry(tmp_path / "data")
    реестр.add_syntax(save_syntax(_справка(*items), входящие / f"{ПЛАТФОРМА}.json.gz"))
    if с_конфигурацией:
        реестр.add_configuration(write_export(входящие, _конфигурация()))
    return реестр


def test_имя_объекта_конфигурации_даёт_подсказку(tmp_path):
    реестр = _реестр(tmp_path, _СТРНАЙТИ, _ШАБЛОН, _СОВПАДАЮЩЕЕ_ИМЯ)

    ответ = search_syntax(реестр, "ТоварыНаСкладах")

    assert "- `Глобальный контекст.ТоварыНаСкладах`" in ответ
    assert ПОДСКАЗКА in ответ


def test_при_пустой_выдаче_справки_подсказка_тоже_есть(tmp_path):
    реестр = _реестр(tmp_path, _СТРНАЙТИ, _ШАБЛОН)

    # Нижний регистр в справке ничего не находит, а объект конфигурации — да.
    ответ = search_syntax(реестр, "товарынаскладах")

    assert "ничего не найдено" in ответ
    assert ПОДСКАЗКА in ответ


def test_имя_объекта_без_учёта_регистра(tmp_path):
    реестр = _реестр(tmp_path, _СТРНАЙТИ, _ШАБЛОН, _СОВПАДАЮЩЕЕ_ИМЯ)

    ответ = search_syntax(реестр, "ТОВАРЫНАСКЛАДАХ")

    assert "- `Глобальный контекст.ТоварыНаСкладах`" in ответ
    assert ПОДСКАЗКА in ответ


def test_полное_имя_объекта_даёт_подсказку(tmp_path):
    реестр = _реестр(tmp_path, _СТРНАЙТИ, _ШАБЛОН)

    ответ = search_syntax(реестр, ОБЪЕКТ)

    assert ПОДСКАЗКА in ответ


def test_без_конфигурации_подсказки_нет(tmp_path):
    реестр = _реестр(tmp_path, _СТРНАЙТИ, _ШАБЛОН, с_конфигурацией=False)

    ответ = search_syntax(реестр, "ТоварыНаСкладах")

    assert "В конфигурации есть" not in ответ
    assert "get_object" not in ответ


def test_обычный_запрос_без_подсказки(tmp_path):
    реестр = _реестр(tmp_path, _СТРНАЙТИ, _ШАБЛОН)

    ответ = search_syntax(реестр, "СтрНайти")

    assert "- `Глобальный контекст.СтрНайти`" in ответ
    assert "В конфигурации есть" not in ответ


def test_частичное_совпадение_имени_не_даёт_подсказки(tmp_path):
    реестр = _реестр(tmp_path, _СТРНАЙТИ, _ШАБЛОН)

    ответ = search_syntax(реестр, "ТоварыНа")

    assert "В конфигурации есть" not in ответ


# ------------------------------------------------------------ tools/list


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_описание_kind_упоминает_get_object(tmp_path):
    реестр = _реестр(tmp_path, _СТРНАЙТИ)

    инструменты = await build_server(реестр).list_tools()

    (поиск,) = [tool for tool in инструменты if tool.name == "search_syntax"]
    свойства = (поиск.input_schema or {}).get("properties") or {}
    assert (
        "таблицы объекта конфигурации — в `get_object`"
        in свойства["kind"]["description"]
    )
