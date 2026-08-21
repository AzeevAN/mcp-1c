"""Публичная сводка измерителя модулей на синтетическом
корпусе."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from mcp1c.modules_index import Формы


ROOT = Path(__file__).parents[1]


def test_измеритель_печатает_пять_однозначных_агрегатов(tmp_path):
    корень = tmp_path / "синтетический_корпус"
    модуль = корень / "CommonModules" / "ОбъектА" / "Ext" / "Module.bsl"
    модуль.parent.mkdir(parents=True)
    модуль.write_text(
        "Процедура Первая() Экспорт\n"
        "    Вторая();\n"
        "КонецПроцедуры\n"
        "Процедура Вторая() Экспорт\n"
        "КонецПроцедуры\n",
        encoding="utf-8",
    )
    форма = (
        корень / "Catalogs" / "ОбъектА" / "Forms" / "Основная" / "Ext" /
        "Form.xml"
    )
    форма.parent.mkdir(parents=True)
    форма.write_text(
        "<Form><ChildItems><UsualGroup name=\"Группа\"><ChildItems>"
        "<Button name=\"Кнопка\"><Events>"
        "<Event name=\"OnClick\">Обработчик</Event>"
        "<Event name=\"OnClick\">Обработчик</Event>"
        "</Events></Button></ChildItems></UsualGroup></ChildItems></Form>",
        encoding="utf-8",
    )

    результат = subprocess.run(
        [
            sys.executable,
            "tools/lab/measure_modules_cache.py",
            str(корень),
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    сводка = next(
        json.loads(строка)["aggregates"]
        for строка in результат.stdout.splitlines()
        if строка.startswith("{")
    )

    assert сводка == {
        "calls": 1,
        "elements": 2,
        "event_rows": 2,
        "forms": 1,
        "procedures": 2,
    }
    assert str(корень) not in результат.stdout
    assert корень.name not in результат.stdout

    формы = Формы.построить(корень)
    assert len(формы._события_проц) == 2
    привязки = формы.привязки(
        "Справочник.ОбъектА.Форма.Основная", "Обработчик"
    )
    assert len(привязки) == 1
