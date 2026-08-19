"""Стенд замеров: метрики, сохранение прогонов, сравнение, сверка пометок.

Проверяется сам стенд, а не качество поиска. Качество меряется цифрами для
человека и в assert не выносится — пороги в процентах ломались бы от каждой
правки словаря (`AGENTS.md`). Здесь наоборот: цифры задаются вручную, и
проверяется, что стенд считает и показывает именно то, что произошло.
"""

from __future__ import annotations

import json

import pytest

from mcp1c.bench import (
    Case,
    CaseResult,
    Report,
    check_notes,
    compare,
    load_report,
    run,
    save_report,
)
from mcp1c.search import Doc, SearchIndex
from mcp1c.syntax_model import KIND_QUERY_ARTICLE


def _отчёт(*места: int | None) -> Report:
    return Report(
        results=[
            CaseResult(query=f"запрос {n}", expected=["цель"], rank=место)
            for n, место in enumerate(места)
        ]
    )


# ------------------------------------------------------------------ метрики


def test_p_at_k_считается_по_местам():
    отчёт = _отчёт(0, 1, 2, 4, 9, None)

    assert отчёт.total == 6
    assert отчёт.at(1) == 1
    assert отчёт.at(3) == 3
    assert отчёт.at(5) == 4
    assert отчёт.at(10) == 5


def test_промахом_считается_только_ненайденное():
    отчёт = _отчёт(0, 9, None, None)

    assert [r.rank for r in отчёт.failures] == [None, None]


def test_mrr_складывает_обратные_ранги():
    отчёт = _отчёт(0, 1, None)

    assert отчёт.mrr == pytest.approx(1.0 + 0.5)


def test_отрыв_берётся_медианой_и_только_по_первым_местам():
    """Среднее утащил бы один запрос с точным совпадением имени.

    Точное совпадение всей строки даёт множитель 12, то есть отрыв под 90%;
    в среднем такой случай спрятал бы то, что остальные победы шаткие.
    """
    отчёт = Report(
        results=[
            CaseResult(query="а", expected=[], rank=0, separation=0.10),
            CaseResult(query="б", expected=[], rank=0, separation=0.20),
            CaseResult(query="в", expected=[], rank=0, separation=0.90),
            # Не первое место — в медиану не входит вовсе.
            CaseResult(query="г", expected=[], rank=3, separation=0.99),
        ]
    )

    assert отчёт.separation == pytest.approx(0.20)


def test_отрыв_ноль_когда_первых_мест_нет():
    assert _отчёт(2, None).separation == 0.0


# --------------------------------------------------------------- прогон


def _справка() -> SearchIndex:
    return SearchIndex(
        [
            Doc(id="query/DISTINCT", kind=KIND_QUERY_ARTICLE, fields={"name": "РАЗЛИЧНЫЕ"}),
            Doc(id="objects/property.Различный", kind="property",
                fields={"name": "Различный"}),
        ]
    )


def test_прогон_запоминает_выдачу_поимённо():
    отчёт = run(_справка(), [Case(query="РАЗЛИЧНЫЕ", expected=["query/DISTINCT"])])

    (результат,) = отчёт.results
    assert результат.rank == 0
    assert результат.got[0] == "query/DISTINCT"
    assert результат.separation > 0


def test_чужой_домен_первым_отмечается():
    """Ждали статью языка запросов — первым пришло свойство платформы.

    Ровно та мера, которой руками считали «магнит выигрывает 5 вопросов
    из 27».
    """
    отчёт = run(_справка(), [Case(query="Различный", expected=["query/DISTINCT"])])

    (результат,) = отчёт.results
    assert результат.rank != 0
    assert результат.foreign_first


def test_свой_домен_первым_чужим_не_считается():
    """Промах внутри одного домена — не попадание не в ту справку."""
    docs = [
        Doc(id="query/A", kind=KIND_QUERY_ARTICLE, fields={"name": "СОЕДИНЕНИЕ"}),
        Doc(id="query/B", kind=KIND_QUERY_ARTICLE, fields={"name": "СОЕДИНЕНИЕ ЛЕВОЕ"}),
    ]
    отчёт = run(SearchIndex(docs), [Case(query="СОЕДИНЕНИЕ", expected=["query/B"])])

    (результат,) = отчёт.results
    assert результат.rank != 0
    assert not результат.foreign_first


# ------------------------------------------------------ сохранение и сравнение


def test_прогон_переживает_запись_и_чтение(tmp_path):
    исходный = run(_справка(), [Case(query="РАЗЛИЧНЫЕ", expected=["query/DISTINCT"])])

    путь = save_report(исходный, tmp_path / "прогон.json", title="проба")
    поднятый = load_report(путь)

    assert [r.query for r in поднятый.results] == [r.query for r in исходный.results]
    assert [r.rank for r in поднятый.results] == [r.rank for r in исходный.results]
    assert поднятый.hit1 == исходный.hit1


def test_сравнение_называет_сдвинувшиеся_запросы():
    было = _отчёт(0, 0, 3)
    стало = _отчёт(0, 3, 0)

    ходы = compare(было, стало)

    assert {(х.query, х.before, х.after) for х in ходы} == {
        ("запрос 1", 0, 3),
        ("запрос 2", 3, 0),
    }


def test_ухудшения_идут_первыми():
    """Регресс важнее прочитать, чем выигрыш — он и стоит выше."""
    ходы = compare(_отчёт(0, 3), _отчёт(3, 0))

    assert [х.better for х in ходы] == [False, True]


def test_запросы_сверяются_по_тексту_а_не_по_порядку():
    """Порядок в наборе меняется при любой правке файла.

    Привязка по индексу молча сравнивала бы разные запросы и показывала бы
    движение там, где его нет.
    """
    было = Report(results=[
        CaseResult(query="первый", expected=[], rank=0),
        CaseResult(query="второй", expected=[], rank=3),
    ])
    # Тот же результат, но строки переставлены местами.
    стало = Report(results=[
        CaseResult(query="второй", expected=[], rank=3),
        CaseResult(query="первый", expected=[], rank=0),
    ])

    assert compare(было, стало) == []


def test_новый_запрос_ходом_не_считается():
    """Он не сдвинулся — его раньше не было, и сравнивать не с чем."""
    было = Report(results=[CaseResult(query="старый", expected=[], rank=0)])
    стало = Report(results=[
        CaseResult(query="старый", expected=[], rank=0),
        CaseResult(query="новый", expected=[], rank=2),
    ])

    assert compare(было, стало) == []


# --------------------------------------------------------------- пометки


def test_пометка_промаха_на_первом_месте_названа_расхождением():
    """То, что после слоя ключей правилось руками в восьми записях."""
    отчёт = Report(results=[
        CaseResult(query="уникальные записи", expected=[], rank=0,
                   note="ИЗВЕСТНЫЙ ПРОМАХ: РАЗЛИЧНЫЕ не входит в пятёрку"),
    ])

    (расхождение,) = check_notes(отчёт)

    assert "уникальные записи" in расхождение
    assert "первый" in расхождение


def test_пометка_первого_места_на_промахе_названа_расхождением():
    отчёт = Report(results=[
        CaseResult(query="проверка на NULL", expected=[], rank=4,
                   note="первое место: литерал NULL совпал с заголовком"),
    ])

    (расхождение,) = check_notes(отчёт)

    assert "место 5" in расхождение


def test_совпавшая_пометка_молчит():
    отчёт = Report(results=[
        CaseResult(query="а", expected=[], rank=0, note="первое место, всё сошлось"),
        CaseResult(query="б", expected=[], rank=4, note="ИЗВЕСТНЫЙ ПРОМАХ: пятое"),
        CaseResult(query="в", expected=[], rank=0, note=""),
    ])

    assert check_notes(отчёт) == []
