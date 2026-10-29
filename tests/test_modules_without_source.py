"""Модули без исходного текста называются в предупреждении поимённо.

Безымянное «у 3 модулей» не говорит, где искать пробел: агент не отличает
закрытый общий модуль от модуля, который просто не прочитался.
"""

from __future__ import annotations

from dataclasses import fields
from types import SimpleNamespace

import pytest

from mcp1c import tools
from mcp1c.modules_index import Оглавление

ХВОСТ = (
    "Сведения о процедурах, вызовах и перекрытиях приведены только по доступным "
    "исходникам; пустой результат не доказывает отсутствие кода в таких модулях."
)


def _закрытые(корень, имена):
    каталог = корень / "CommonModules"
    каталог.mkdir(exist_ok=True)
    for имя in имена:
        (каталог / f"{имя}.Module").write_bytes(b"compiled")


def _корпус(monkeypatch, корень, имена):
    """Корпус из закрытых общих модулей; счётчик покрытия согласован с оглавлением."""
    _закрытые(корень, имена)
    counts = dict.fromkeys((field.name for field in fields(tools.CodeCoverage)), 0)
    counts.update(
        compiled_without_source=len(имена), problem_categories=(), problems=()
    )
    coverage = tools.CodeCoverage(**counts)
    monkeypatch.setattr(tools, "_code_coverage", lambda loaded, *, include_problem_rows: coverage)
    return SimpleNamespace(оглавление=Оглавление.построить(корень))


def test_скомпилированные_отсортированы_и_это_кортеж(tmp_path):
    _закрытые(tmp_path, ["В", "А", "Б"])

    оглавление = Оглавление.построить(tmp_path)

    assert оглавление.скомпилированные() == (
        "ОбщийМодуль.А",
        "ОбщийМодуль.Б",
        "ОбщийМодуль.В",
    )


def test_один_модуль_без_исходника_назван(monkeypatch, tmp_path):
    loaded = _корпус(monkeypatch, tmp_path, ["А"])

    warning = tools._loaded_partial_warning(loaded)

    assert warning == (
        "В корпусе кода исходный текст недоступен у 1 модуля: "
        f"`ОбщийМодуль.А`. {ХВОСТ}"
    )


def test_корпус_попадает_в_предупреждение(monkeypatch, tmp_path):
    loaded = _корпус(monkeypatch, tmp_path, ["А"])

    warning = tools._loaded_partial_warning(loaded, corpus="конфигурации Пример")

    assert warning.startswith(
        "В корпусе кода конфигурации Пример исходный текст недоступен у 1 модуля: "
        "`ОбщийМодуль.А`. "
    )


def test_семь_модулей_без_исходника(monkeypatch, tmp_path):
    loaded = _корпус(monkeypatch, tmp_path, ["Ж", "А", "Д", "Б", "Г", "В", "Е"])

    warning = tools._loaded_partial_warning(loaded)

    assert warning == (
        "В корпусе кода исходный текст недоступен у 7 модулей: "
        "`ОбщийМодуль.А`, `ОбщийМодуль.Б`, `ОбщийМодуль.В`, `ОбщийМодуль.Г`, "
        f"`ОбщийМодуль.Д` и ещё 2. {ХВОСТ}"
    )


def test_пять_модулей_без_исходника_без_хвоста_и_ещё(monkeypatch, tmp_path):
    loaded = _корпус(monkeypatch, tmp_path, ["А", "Б", "В", "Г", "Д"])

    warning = tools._loaded_partial_warning(loaded)

    assert "у 5 модулей: `ОбщийМодуль.А`, `ОбщийМодуль.Б`, `ОбщийМодуль.В`, " in warning
    assert "`ОбщийМодуль.Д`. Сведения" in warning
    assert "и ещё" not in warning


@pytest.mark.parametrize(
    ("число", "слово"),
    [
        (1, "модуля"),
        (21, "модуля"),
        (101, "модуля"),
        (2, "модулей"),
        (5, "модулей"),
        (11, "модулей"),
        (12, "модулей"),
        (111, "модулей"),
    ],
    ids=lambda value: str(value),
)
def test_падеж_числа_модулей(monkeypatch, tmp_path, число, слово):
    loaded = _корпус(monkeypatch, tmp_path, [f"М{номер:03d}" for номер in range(число)])

    warning = tools._loaded_partial_warning(loaded)

    assert warning is not None
    assert warning.startswith(f"В корпусе кода исходный текст недоступен у {число} {слово}: ")
