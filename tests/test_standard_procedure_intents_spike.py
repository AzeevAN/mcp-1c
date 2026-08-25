"""Спайк типовых намерений остаётся воспроизводимым и fail-closed."""

from __future__ import annotations

from pathlib import Path

from mcp1c.modules_index import Оглавление
from tools.lab.measure_standard_procedure_intents import (
    _object_scope_modules,
    load_suite,
    recognize,
    resolve,
)


SUITE = Path("tests/queries/standard-procedure-intents.json")


def test_зафиксированный_набор_показывает_честный_результат_спайка():
    cases = load_suite(SUITE)
    rows = [(case, recognize(case["query"])) for case in cases]
    positives = [(case, got) for case, got in rows if case["expected"]]
    controls = [(case, got) for case, got in rows if case["expected"] is None]
    holdout = [
        (case, got)
        for case, got in rows
        if case["origin"] in {"holdout", "holdout-control"}
    ]

    assert len(cases) == 90
    assert sum(case["expected"] == got for case, got in positives) == 59
    assert all(got is None for _, got in controls)
    assert sum(case["expected"] == got for case, got in holdout) == 35


def test_точное_типовое_имя_распознаётся_без_фразы():
    assert recognize("ОбработкаПроверкиЗаполнения") == (
        "ОбработкаПроверкиЗаполнения"
    )
    assert recognize("обработка проверки заполнения") == (
        "ОбработкаПроверкиЗаполнения"
    )


def test_неоднозначная_запись_не_выбирает_обработчик_наугад():
    assert recognize("обработчик записи объекта") is None


def test_scope_сводит_одно_имя_к_одному_модулю(tmp_path):
    первый = tmp_path / "CommonModules" / "Первый" / "Ext"
    второй = tmp_path / "CommonModules" / "Второй" / "Ext"
    первый.mkdir(parents=True)
    второй.mkdir(parents=True)
    (первый / "Module.bsl").write_text(
        "Процедура ПередЗаписью()\nКонецПроцедуры\n",
        encoding="utf-8",
    )
    (второй / "Module.bsl").write_text(
        "Процедура ПередЗаписью()\nКонецПроцедуры\n"
        "Процедура Другая()\nКонецПроцедуры\n",
        encoding="utf-8",
    )
    toc = Оглавление.построить(tmp_path)

    global_matches = resolve(toc, "ПередЗаписью")
    assert len(global_matches) == 2

    scoped = resolve(
        toc,
        "ПередЗаписью",
        frozenset({global_matches[0].модуль}),
    )
    assert len(scoped) == 1
    assert scoped[0].модуль == global_matches[0].модуль

    assert resolve(toc, "ПриЗаписи", frozenset({global_matches[0].модуль})) == []


def test_scope_объекта_может_честно_остаться_неоднозначным(tmp_path):
    объект = tmp_path / "Documents" / "Пример" / "Ext"
    форма = tmp_path / "Documents" / "Пример" / "Forms" / "Форма" / "Ext"
    объект.mkdir(parents=True)
    форма.mkdir(parents=True)
    (объект / "ObjectModule.bsl").write_text(
        "Процедура ПередЗаписью()\nКонецПроцедуры\n",
        encoding="utf-8",
    )
    (форма / "Form" / "Module.bsl").parent.mkdir(parents=True)
    (форма / "Form" / "Module.bsl").write_text(
        "Процедура ПередЗаписью()\nКонецПроцедуры\n",
        encoding="utf-8",
    )
    toc = Оглавление.построить(tmp_path)
    found = resolve(toc, "ПередЗаписью")

    modules = _object_scope_modules(toc, found[0].модуль)

    assert len(resolve(toc, "ПередЗаписью", modules)) == 2
