"""Опознание контейнера 1С по заголовку.

Заголовок — четыре uint32: `next_page_addr`, `page_size`, `storage_ver`,
`reserved`. Первое поле держали за сигнатуру и требовали ровно `FF FF FF 7F`,
но это адрес свободной страницы, и `0x7FFFFFFF` означает лишь «свободных
нет». Там, где список непуст, стоит настоящее смещение — и валидный файл
получал отказ «не контейнер 1С».

Проверено на поставке 8.3.27.2130: `shquery_ru.hbk` (`next_page=0x00028ccd`),
`shlang_ru.hbk` (`0x00015725`), `shclang_ru.hbk` (`0x00011c76`), `1cv8_ru.hbk`
(`0x001d2338`) — все читаются штатным кодом, у всех `page_size` равен 512.
"""

from __future__ import annotations

import struct

import pytest

from mcp1c.v8container import (
    EMPTY_ADDR,
    HEADER_SIZE,
    V8Container,
    V8ContainerError,
    is_container,
)


def header(next_page: int = EMPTY_ADDR, page_size: int = 512, version: int = 0) -> bytes:
    return struct.pack("<IIII", next_page, page_size, version, 0)


def test_пустой_список_свободных_страниц_принимается():
    """Прежний случай: `0x7FFFFFFF` — свободных страниц нет."""
    assert is_container(header())


def test_непустой_список_свободных_страниц_тоже_принимается():
    """Ровно то, на чём спотыкались `shquery_ru.hbk` и соседи."""
    assert is_container(header(next_page=0x00028CCD))
    assert is_container(header(next_page=0x00015725))
    assert is_container(header(next_page=0x001D2338))


def test_чужой_формат_отвергается():
    """Признак формата — `page_size`, а не первое поле."""
    # ZIP: «PK\x03\x04», дальше версия и флаги — под page_size попадает мусор.
    assert not is_container(b"PK\x03\x04\x14\x00\x00\x00\x08\x00\x00\x00\x00\x00")
    # Текст.
    assert not is_container("# Заголовок markdown, не контейнер".encode("utf-8"))
    # Нулевой размер страницы бессмыслен.
    assert not is_container(header(page_size=0))
    # Страница в гигабайт — тоже.
    assert not is_container(header(page_size=1 << 24))


def test_обрезанный_заголовок_отвергается():
    assert not is_container(b"")
    assert not is_container(header()[:HEADER_SIZE - 1])


def test_мусор_с_правдоподобным_заголовком_даёт_понятную_ошибку(tmp_path):
    """Цена ослабления: случайные данные могут пройти проверку заголовка.

    Тогда разбор обязан упасть `V8ContainerError`, а не чем попало: вызывающий
    ловит именно его, и `TypeError` из глубины прошёл бы мимо обработчика.
    """
    path = tmp_path / "подделка.hbk"
    path.write_bytes(header(next_page=0x1234) + b"\x00" * 512)

    with pytest.raises(V8ContainerError):
        with V8Container(path):
            pass


def test_сообщение_об_отказе_не_поминает_сигнатуру(tmp_path):
    """Формулировка врала: она называла причиной отсутствие `FF FF FF 7F`."""
    path = tmp_path / "заметки.txt"
    path.write_bytes("обычный текст, ничего общего с контейнером".encode("utf-8"))

    with pytest.raises(V8ContainerError) as поймано:
        with V8Container(path):
            pass

    assert "FF FF FF 7F" not in str(поймано.value)
