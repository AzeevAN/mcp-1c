"""Ненайденная роль: ошибка называет роль, конфигурацию и похожие имена.

Агент опечатывается в имени роли чаще, чем ошибается в самом запросе. Голое
«Роль не найдена» заставляет его гадать; подсказка с близкими именами и
указанием, где взять точное, исправляет вызов за один шаг.
"""

from __future__ import annotations

import pytest

from mcp1c.registry import Registry
from mcp1c.role_access_service import (
    RoleAccessQueryError,
    get_role_access_payload,
    role_objects_payload,
)

from test_role_access_index import _descriptor, _rights
from test_role_access_stage9 import _publish

КОНФИГУРАЦИЯ = "DemoConfiguration"


def _роли():
    return (
        (
            "БезналичнаяТорговля",
            _descriptor(
                "БезналичнаяТорговля",
                uuid="11111111-1111-1111-1111-111111111111",
                synonyms=(("ru", "Безналичная торговля"),),
            ),
            _rights(),
        ),
        (
            "Кладовщик",
            _descriptor(
                "Кладовщик",
                uuid="22222222-2222-2222-2222-222222222222",
                synonyms=(("ru", "Кладовщик склада"),),
            ),
            _rights(),
        ),
    )


@pytest.fixture
def реестр(tmp_path):
    registry = Registry(tmp_path / "data")
    _publish(
        registry,
        tmp_path / "source",
        configuration=КОНФИГУРАЦИЯ,
        generation_id="generation-1",
        roles=_роли(),
    )
    return registry


def _объекты_роли(реестр, роль):
    return role_objects_payload(реестр, роль, config=КОНФИГУРАЦИЯ)


def _права_роли(реестр, роль):
    return get_role_access_payload(реестр, роль, config=КОНФИГУРАЦИЯ)


@pytest.mark.parametrize(
    "вызов", [_права_роли, _объекты_роли], ids=["get_role_access", "role_objects"]
)
def test_ненайденная_роль_называет_конфигурацию_и_похожие(реестр, вызов):
    with pytest.raises(RoleAccessQueryError) as failure:
        вызов(реестр, "БезналичнаяТорговл")

    message = str(failure.value)
    assert "«БезналичнаяТорговл»" in message
    assert КОНФИГУРАЦИЯ in message
    assert "БезналичнаяТорговля" in message
    assert "find_roles_for_access" in message
    assert "Кладовщик" not in message


@pytest.mark.parametrize(
    "вызов", [_права_роли, _объекты_роли], ids=["get_role_access", "role_objects"]
)
def test_ненайденная_роль_без_похожих(реестр, вызов):
    with pytest.raises(RoleAccessQueryError) as failure:
        вызов(реестр, "Зззззз")

    message = str(failure.value)
    assert "«Зззззз»" in message
    assert КОНФИГУРАЦИЯ in message
    assert "Похожие" not in message
    assert "БезналичнаяТорговля" not in message
