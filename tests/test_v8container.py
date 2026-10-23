"""Опознание контейнера 1С по заголовку.

Заголовок — четыре uint32: `next_page_addr`, `page_size`, `storage_ver`,
`reserved`. Первое поле держали за сигнатуру и требовали ровно `FF FF FF 7F`,
но это адрес свободной страницы, и `0x7FFFFFFF` означает лишь «свободных
нет». Там, где список непуст, стоит настоящее смещение — и валидный файл
получал отказ «не контейнер 1С».

Проверено на поставке 8.3.27.2130: `shlang_ru.hbk` (`0x00015725`),
`shclang_ru.hbk` (`0x00011c76`), `1cv8_ru.hbk`
(`0x001d2338`) — все читаются штатным кодом, у всех `page_size` равен 512.
"""

from __future__ import annotations

import struct

import pytest

from mcp1c.v8container import (
    BLOCK_HEADER_SIZE,
    EMPTY_ADDR,
    HEADER_SIZE,
    V8Container,
    V8ContainerError,
    is_container,
)
from module_samples import v8_container_bytes


def header(next_page: int = EMPTY_ADDR, page_size: int = 512, version: int = 0) -> bytes:
    return struct.pack("<IIII", next_page, page_size, version, 0)


def test_пустой_список_свободных_страниц_принимается():
    """Прежний случай: `0x7FFFFFFF` — свободных страниц нет."""
    assert is_container(header())


def test_непустой_список_свободных_страниц_тоже_принимается():
    """Ровно то, на чём спотыкались несколько соседних HBK-контейнеров."""
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


def test_сжатая_запись_не_распаковывается_без_предела():
    payload = b"x" * (32 * 1024 * 1024)
    raw = v8_container_bytes([("bomb", payload)])
    assert len(raw) < 64 * 1024

    with V8Container(raw) as container:
        with pytest.raises(V8ContainerError, match="предел|сжати"):
            container.read("bomb")


def _shared_attrs_container(entries: int, attrs_size: int) -> bytes:
    """Таблица, где все записи ссылаются на один документ атрибутов.

    Так собранный Form.bin/.hbk заставлял читать один и тот же крупный
    документ заново для каждой из ``entries`` записей вне всякого бюджета.
    """
    from module_samples import _block

    attrs = b"\x00" * 20 + "Имя".encode("utf-16-le") + b"\x00" * (attrs_size - 26)
    table_size = 12 * entries
    attrs_address = HEADER_SIZE + BLOCK_HEADER_SIZE + table_size
    table = struct.pack("<III", attrs_address, EMPTY_ADDR, 0) * entries
    return header() + _block(table) + _block(attrs)


def test_повторный_адрес_атрибутов_отвергается():
    raw = _shared_attrs_container(entries=1000, attrs_size=64 * 1024)

    with pytest.raises(V8ContainerError, match="атрибут"):
        V8Container(raw)


def test_атрибуты_записей_расходуют_бюджет_контейнера():
    from mcp1c.resource_limits import ResourceLimits
    from mcp1c.v8container import V8ResourceLimitError

    raw = v8_container_bytes([(f"entry{index}", b"x") for index in range(10)])
    tight = ResourceLimits(
        max_entries=100,
        max_entry_bytes=1024,
        max_total_bytes=200,
        max_compression_ratio=200,
    )
    with pytest.raises(V8ResourceLimitError, match="предел"):
        V8Container(raw, limits=tight)

    with V8Container(raw) as container:
        assert len(container) == 10
        assert container.read("entry3") == b"x"


def _oversized_blocks_container(entries: int, tail_bytes: int) -> bytes:
    """Атрибуты на разных адресах, но каждый блок заявляет block_size 0x1FFFFFFF.

    doc_size честный (несколько десятков байт), а длина блока указывает за
    конец файла: без обрезки по doc_size каждая запись копировала бы весь
    хвост до EOF.
    """
    table_size = 12 * entries
    cursor = HEADER_SIZE + BLOCK_HEADER_SIZE + table_size
    blocks = []
    addresses = []
    for index in range(entries):
        attrs = b"\x00" * 20 + f"e{index}".encode("utf-16-le") + b"\x00\x00"
        block = (
            b"\r\n"
            + f"{len(attrs):08x} {0x1FFFFFFF:08x} {EMPTY_ADDR:08x}".encode("ascii")
            + b" \r\n"
            + attrs
        )
        addresses.append(cursor)
        cursor += len(block)
        blocks.append(block)
    from module_samples import _block

    table = b"".join(struct.pack("<III", address, EMPTY_ADDR, 0) for address in addresses)
    return header() + _block(table) + b"".join(blocks) + b"\0" * tail_bytes


def test_длина_блока_за_пределом_документа_не_копирует_хвост():
    import time

    raw = _oversized_blocks_container(entries=1000, tail_bytes=50 * 1024 * 1024)
    started = time.perf_counter()
    with V8Container(raw) as container:
        assert len(container) == 1000
        assert container.namelist()[7] == "e7"
        # Бюджет списан по реально скопированным байтам атрибутов, не по хвосту.
        assert container._budget.read_total < 1000 * 64
    assert time.perf_counter() - started < 2.0


def test_нулевой_адрес_атрибутов_пропускается_как_пустой():
    from module_samples import _block

    raw = v8_container_bytes([("first", b"1")])
    # Две записи с attrs_addr == 0: это «пусто», а не повтор адреса.
    table_start = HEADER_SIZE + BLOCK_HEADER_SIZE
    first = raw[table_start : table_start + 12]
    attrs_addr, data_addr, _ = struct.unpack("<III", first)
    table = first + struct.pack("<III", 0, 0, 0) * 2
    shift = 24
    table = struct.pack("<III", attrs_addr + shift, data_addr + shift, 0) + table[12:]
    rebuilt = header() + _block(table) + raw[table_start + 12 :]

    with V8Container(rebuilt) as container:
        assert container.namelist() == ["first"]
        assert container.read("first") == b"1"




def _m3_v8_chain(kind):
    from module_samples import _block

    attrs = _block(b"\0" * 20 + "module".encode("utf-16-le") + b"\0\0")
    attrs_addr = HEADER_SIZE + BLOCK_HEADER_SIZE + 12
    data_addr = attrs_addr + len(attrs)
    table = _block(struct.pack("<III", attrs_addr, data_addr, 0))

    def page_header(total, block_size, next_addr=EMPTY_ADDR):
        value = (b"\r\n" + f"{total:08x} {block_size:08x} {next_addr:08x}".encode("ascii")
                 + b" \r\n")
        assert len(value) == BLOCK_HEADER_SIZE
        return value

    if kind == "partial-overlap":
        payload = bytearray(b"\xff" * 64)
        second_header = page_header(65, 1)
        payload[8:8 + BLOCK_HEADER_SIZE] = second_header
        payload[8 + BLOCK_HEADER_SIZE] = ord("B")
        next_addr = data_addr + BLOCK_HEADER_SIZE + 8
        body = page_header(65, len(payload), next_addr) + bytes(payload)
        expected = None
    elif kind == "total-past-file":
        body = page_header(4096, 4) + b"\xffABC"
        expected = None
    else:
        first, second, gap = b"\xffABCD", b"EFGH", b"\0" * 7
        total = len(first) + len(second)
        next_addr = data_addr + BLOCK_HEADER_SIZE + len(first) + len(gap)
        body = (page_header(total, len(first), next_addr) + first + gap
                + page_header(total, len(second)) + second)
        expected = b"\xffABCDEFGH"
    return header() + table + attrs + body, expected


@pytest.mark.parametrize("reader", ["read", "read_bounded"])
@pytest.mark.parametrize("kind", ["partial-overlap", "total-past-file"])
def test_v8_отвергает_перекрытие_блоков_и_невозможный_total(reader, kind):
    raw, _expected = _m3_v8_chain(kind)

    with pytest.raises(V8ContainerError):
        with V8Container(raw) as container:
            if reader == "read":
                container.read("module")
            else:
                container.read_bounded("module", 8192)


@pytest.mark.parametrize("reader", ["read", "read_bounded"])
def test_v8_читает_неперекрывающуюся_цепочку_с_промежутком(reader):
    raw, expected = _m3_v8_chain("valid")

    with V8Container(raw) as container:
        actual = (container.read("module") if reader == "read"
                  else container.read_bounded("module", 8192))

    assert actual == expected
