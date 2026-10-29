"""Контракт «не найдено»: точный адрес без объекта — ошибка, пустой поиск — обычный ответ."""

from __future__ import annotations

import anyio
import pytest
from mcp import ClientSession
from mcp.shared.memory import create_client_server_memory_streams

from mcp1c.server import build_server


ПОДСКАЗКА = "Возможно, имелось в виду"



def _вызвать(реестр, инструмент: str, аргументы: dict):
    """Синхронная обёртка: async-тест через pytest-плагин anyio держит
    аргументы последнего теста (и реестр с пакетом кода) до следующего
    async-теста, и глобальный кэш разборов не освобождается."""
    return anyio.run(_вызвать_async, реестр, инструмент, аргументы)


async def _вызвать_async(реестр, инструмент: str, аргументы: dict):
    server = build_server(реестр)
    async with create_client_server_memory_streams() as (клиент, удалённый):
        async with anyio.create_task_group() as задачи:
            задачи.start_soon(
                server._lowlevel_server.run,
                *удалённый,
                server._lowlevel_server.create_initialization_options(),
            )
            try:
                async with ClientSession(*клиент) as сессия:
                    await сессия.initialize()
                    return await сессия.call_tool(инструмент, аргументы)
            finally:
                задачи.cancel_scope.cancel()


ОТСУТСТВУЮЩИЕ = [
    pytest.param(
        "get_object", {"full_name": "Документ.РеализацияТоваровУслу", "config": "Пример"},
        "нет объекта `Документ.РеализацияТоваровУслу`", True, id="get_object",
    ),
    pytest.param(
        "get_related", {"full_name": "Документ.РеализацияТоваровУслу", "config": "Пример"},
        "нет объекта `Документ.РеализацияТоваровУслу`", False, id="get_related",
    ),
    pytest.param(
        "get_procedure", {"address": "ОбщийМодуль.ОбщийПримр", "config": "Пример"},
        "Модуль `ОбщийМодуль.ОбщийПримр` в загруженном коде не найден", True,
        id="get_procedure-нет-модуля",
    ),
    pytest.param(
        "get_procedure", {"address": "ОбщийМодуль.ОбщийПример::Сложитт", "config": "Пример"},
        "нет процедуры `Сложитт`", True, id="get_procedure-нет-процедуры",
    ),
    pytest.param(
        "get_callers", {"address": "ОбщийМодуль.ОбщийПримр::Сложить", "config": "Пример"},
        "Модуль `ОбщийМодуль.ОбщийПримр` в загруженном коде не найден", True,
        id="get_callers-нет-модуля",
    ),
    pytest.param(
        "get_callers", {"address": "ОбщийМодуль.ОбщийПример::Сложитт", "config": "Пример"},
        "нет процедуры `Сложитт`", True, id="get_callers-нет-процедуры",
    ),
]


@pytest.mark.parametrize(("инструмент", "аргументы", "текст", "с_подсказкой"), ОТСУТСТВУЮЩИЕ)
def test_инструмент_нет_адреса_ошибка(
    реестр_с_кодом, инструмент, аргументы, текст, с_подсказкой
):
    результат = _вызвать(реестр_с_кодом, инструмент, аргументы)
    ответ = результат.content[0].text
    assert результат.is_error is True, ответ
    assert текст in ответ
    if с_подсказкой:
        assert ПОДСКАЗКА in ответ


def test_поиск_без_результатов_не_ошибка(реестр_с_кодом):
    """Страж: нулевая выдача поиска — успешный ответ."""
    результат = _вызвать(
        реестр_с_кодом, "search_objects", {"query": "ЯЯЯНетТакогоСлова", "config": "Пример"}
    )
    assert результат.is_error is False
    assert "ничего не найдено" in результат.content[0].text


@pytest.mark.parametrize(
    ("инструмент", "аргументы", "ожидается"),
    [
        pytest.param(
            "get_object", {"full_name": "Документ.РеализацияТоваровУслуг", "config": "Пример"},
            "РеализацияТоваровУслуг", id="get_object",
        ),
        pytest.param(
            "get_related", {"full_name": "Документ.РеализацияТоваровУслуг", "config": "Пример"},
            "Связи", id="get_related",
        ),
        pytest.param(
            "get_procedure", {"address": "ОбщийМодуль.ОбщийПример::Сложить", "config": "Пример"},
            "Сложить", id="get_procedure",
        ),
        pytest.param(
            "get_callers", {"address": "ОбщийМодуль.ОбщийПример::Сложить", "config": "Пример"},
            "Сложить", id="get_callers",
        ),
    ],
)
def test_существующий_объект_не_ошибка(реестр_с_кодом, инструмент, аргументы, ожидается):
    """Страж: найденный адрес отвечает обычным текстом."""
    результат = _вызвать(реестр_с_кодом, инструмент, аргументы)
    assert результат.is_error is False, результат.content[0].text
    assert ожидается in результат.content[0].text
