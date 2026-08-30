"""Поисковые формулировки поверх справки платформы."""

from __future__ import annotations

from mcp1c.search import Doc, SearchIndex, index_syntax
from mcp1c.search_keys import SEARCH_KEYS, coverage, keys_text
from mcp1c.syntax_model import KIND_METHOD, SyntaxIndex, SyntaxItem


def test_ключи_привязываются_к_известным_страницам():
    итог = coverage(set(SEARCH_KEYS))

    assert итог.total == len(SEARCH_KEYS)
    assert итог.attached == len(SEARCH_KEYS)
    assert итог.lost == []
    assert итог.ok
    assert итог.as_warning() == ""


def test_непривязанный_ключ_виден_в_предупреждении():
    итог = coverage(set())

    assert итог.attached == 0
    assert итог.lost == list(SEARCH_KEYS)
    assert "потеряно" in итог.as_warning()
    assert "методам платформы" in итог.as_warning()


def test_метод_платформы_находится_по_описанию_действия():
    target_id = "objects/catalog213/catalog393/QueryResultSelection/methods/Next556"
    syntax = SyntaxIndex(platforms=[], source="search-keys-test", language="ru")
    item = SyntaxItem(
        id=target_id,
        kind=KIND_METHOD,
        name_ru="Следующий",
        name_en="Next",
        parent_ru="ВыборкаИзРезультатаЗапроса",
        description="Получает следующую запись.",
    )
    syntax.add(item)

    hits = index_syntax(syntax).search(
        "как перебрать строки в выборке результата запроса", limit=5
    )

    assert hits[0].doc.id == target_id
    assert "перебрать строки" not in item.description
    assert not hasattr(item, "keys")


def test_элемент_без_ключей_ищется_по_имени():
    syntax = SyntaxIndex(platforms=[], source="search-keys-test", language="ru")
    syntax.add(
        SyntaxItem(
            id="objects/Sin",
            kind=KIND_METHOD,
            name_ru="Sin",
            name_en="Sin",
        )
    )

    assert keys_text("objects/Sin") == ""
    assert [hit.doc.id for hit in index_syntax(syntax).search("Sin", limit=5)] == [
        "objects/Sin"
    ]


def test_повтор_слова_в_ключах_не_умножает_вес():
    один = Doc(id="один", fields={"keys": "дата"})
    пять = Doc(id="пять", fields={"keys": "дата\nдата\nдата\nдата\nдата"})

    hits = {
        hit.doc.id: hit.score
        for hit in SearchIndex([один, пять]).search("дата", limit=5)
    }

    assert hits["один"] == hits["пять"]


def test_в_описании_повтор_по_прежнему_копится():
    один = Doc(id="один", fields={"description": "дата"})
    пять = Doc(id="пять", fields={"description": "дата дата дата дата дата"})

    hits = {
        hit.doc.id: hit.score
        for hit in SearchIndex([один, пять]).search("дата", limit=5)
    }

    assert hits["пять"] > hits["один"]
