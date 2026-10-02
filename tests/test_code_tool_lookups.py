"""Ускоренные поиски инструментов по коду дают тот же ответ, что перебор.

`_close_matches` отсекает кандидатов по верхней оценке `quick_ratio` и не
считает дорогой `ratio()` для тех, кто уже не войдёт в первые `n`; подсказка
«Возможно, имелось в виду» обязана совпасть с `difflib` побайтно — та же
выдача, тот же порядок, те же ничьи. `_module_owner_lookup` находит владельца
перекрытого модуля словарём вместо перебора объектов — и того же владельца.
"""

from __future__ import annotations

import difflib
import random

import pytest

from mcp1c import tools


def _адреса(rnd: random.Random, count: int) -> list[str]:
    виды = ["Справочник", "Документ", "ОбщийМодуль", "Обработка", "РегистрСведений"]
    слова = ["Номенклатура", "Контрагенты", "Заказ", "Клиента", "Цены", "Склад",
             "Продажи", "Обмен", "Данными", "Печать", "Чек", "ККМ"]
    модули = ["МодульОбъекта", "МодульМенеджера", "Модуль", "Форма.ФормаЭлемента"]
    результат = []
    for _ in range(count):
        имя = "".join(rnd.sample(слова, rnd.randint(1, 3)))
        результат.append(f"{rnd.choice(виды)}.{имя}.{rnd.choice(модули)}")
    return результат


def _опечатка(rnd: random.Random, текст: str) -> str:
    символы = list(текст)
    for _ in range(rnd.randint(1, 4)):
        позиция = rnd.randrange(len(символы))
        действие = rnd.random()
        if действие < 0.4:
            символы[позиция] = rnd.choice("аеоьъ")
        elif действие < 0.7 and len(символы) > 1:
            del символы[позиция]
        else:
            символы.insert(позиция, rnd.choice("иы"))
    return "".join(символы)


@pytest.mark.parametrize("seed", range(40))
def test_совпадает_с_difflib_на_адресах_модулей(seed):
    rnd = random.Random(seed)
    адреса = _адреса(rnd, rnd.randint(0, 400))
    if адреса and rnd.random() < 0.5:
        адреса += rnd.sample(адреса, min(5, len(адреса)))  # повторы
    запрос = _опечатка(rnd, rnd.choice(адреса)) if адреса else "Справочник.Нет"

    for cutoff in (0.0, 0.45, 0.8):
        assert tools._close_matches(запрос, адреса, n=5, cutoff=cutoff) == (
            difflib.get_close_matches(запрос, адреса, n=5, cutoff=cutoff)
        )


def test_ничьи_разрешаются_как_в_difflib():
    # Одинаковая оценка у всех: первые пять выбираются по строке, по убыванию.
    адреса = [f"М{буква}" for буква in "абвгдежзик"]

    assert tools._close_matches("Мя", адреса, n=5, cutoff=0.45) == (
        difflib.get_close_matches("Мя", адреса, n=5, cutoff=0.45)
    )


def _владелец_перебором(module: str, objects) -> str | None:
    """Прежний порядок `_extension_overrides`: длинное имя раньше короткого."""
    упорядочены = sorted(
        objects, key=lambda value: (-len(value), value.casefold(), value)
    )
    return next(
        (name for name in упорядочены if tools._belongs_to_object(module, name)),
        None,
    )


@pytest.mark.parametrize("seed", range(20))
def test_владелец_перекрытого_модуля_как_при_переборе(seed):
    rnd = random.Random(seed)
    виды = ["Справочник", "Документ", "ОбщийМодуль", "Обработка", "ОбщаяФорма"]
    имена = ["Номенклатура", "НОМЕНКЛАТУРА", "Заказ", "ЗаказКлиента", "Цены", "А"]
    objects = sorted(
        {f"{rnd.choice(виды)}.{rnd.choice(имена)}" for _ in range(rnd.randint(0, 25))}
    )
    if rnd.random() < 0.3:
        objects.append(rnd.choice(виды))  # имя-префикс без точки
    модули = [
        f"{rnd.choice(виды)}.{rnd.choice(имена)}"
        + rnd.choice(["", ".МодульОбъекта", ".Форма.ФормаЭлемента.Форма", "Модуль"])
        for _ in range(30)
    ]
    модули = [
        модуль.upper() if rnd.random() < 0.2 else модуль for модуль in модули
    ]

    владелец = tools._module_owner_lookup(objects)

    for модуль in модули:
        assert владелец(модуль) == _владелец_перебором(модуль, objects)
