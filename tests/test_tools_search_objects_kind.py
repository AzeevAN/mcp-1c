"""Что видит агент, когда `search_objects` ограничен видом объекта.

Независимая проверка инструментов 2026-10-03: агент искал роль через
`kind="Роль"`, получил «ничего не найдено» и решил, что ролей с таким именем
нет. Роли в поиск объектов не входят — их права живут в отдельном ролевом
слое, — а неизвестный вид фильтр отбрасывал молча, так что пустой ответ
выглядел как факт о конфигурации, а не как неподдерживаемый вид.
"""

from __future__ import annotations

import pytest

from mcp1c.registry import Registry
from mcp1c.tools import search_objects

from conftest import build_configuration, write_export


@pytest.fixture
def registry(tmp_path):
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    registry = Registry(tmp_path / "data")
    registry.add_configuration(write_export(incoming, build_configuration()))
    return registry


@pytest.mark.parametrize("kind", ["Роль", "Role", "роли"])
def test_вид_роль_ведёт_к_ролевым_инструментам(registry, kind):
    answer = search_objects(registry, "Контрагенты", kind=kind)

    assert "ничего не найдено" not in answer
    assert "find_roles_for_access" in answer
    assert "get_role_access" in answer


def test_отсутствующий_вид_называет_доступные_виды(registry):
    answer = search_objects(registry, "Контрагенты", kind="Catalog")

    assert "ничего не найдено" not in answer
    assert "«Catalog»" in answer
    assert "Справочник" in answer
    assert "Документ" in answer
    # Подсказка про роли уместна только для вида «Роль».
    assert "find_roles_for_access" not in answer


def test_существующий_вид_ищет_как_прежде(registry):
    answer = search_objects(registry, "Контрагенты", kind="Справочник")

    assert "`Справочник.Контрагенты`" in answer


def test_пустая_выдача_по_существующему_виду_не_меняется(registry):
    answer = search_objects(registry, "Жирафы", kind="Документ")

    assert "ничего не найдено" in answer
    assert "Доступные виды" not in answer
