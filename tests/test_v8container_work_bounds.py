"""Ограничение работы при чтении таблицы контейнера с общими цепочками."""

from pathlib import Path
import struct
import subprocess
import sys

import pytest

from mcp1c import v8container
from mcp1c.v8container import V8Container


def _block(payload, *, doc_size=None, next_addr=0x7FFFFFFF):
    size = len(payload) if doc_size is None else doc_size
    return (
        b"\r\n"
        + f"{size:08x} {len(payload):08x} {next_addr:08x}".encode("ascii")
        + b" \r\n"
        + payload
    )


def _shared_suffix_container(entries, suffix_blocks):
    """Уникальные начала атрибутов сходятся в одну цепочку пустых блоков."""
    first_attrs = 16 + 31 + 12 * entries
    suffix = first_attrs + entries * 31
    table = b"".join(
        struct.pack("<III", first_attrs + i * 31, 0x7FFFFFFF, 0)
        for i in range(entries)
    )
    parts = [struct.pack("<IIII", 0x7FFFFFFF, 512, 0, 0), _block(table)]
    parts.extend(_block(b"", doc_size=24, next_addr=suffix) for _ in range(entries))
    for i in range(suffix_blocks):
        last = i + 1 == suffix_blocks
        parts.append(_block(
            b"\x00" * 20 + b"x\x00\x00\x00" if last else b"",
            doc_size=24,
            next_addr=0x7FFFFFFF if last else suffix + (i + 1) * 31,
        ))
    return b"".join(parts)


def test_общий_хвост_атрибутов_быстро_отклоняется():
    """Малый файл не должен многократно оплачивать обход общего хвоста."""
    raw = _shared_suffix_container(1600, 16_000)
    assert len(raw) < 1024 * 1024
    code = """
import sys
from mcp1c.v8container import V8Container, V8ContainerError

try:
    with V8Container(sys.stdin.buffer.read()):
        pass
except V8ContainerError:
    pass
else:
    raise AssertionError("Не ограничен суммарный обход общего хвоста атрибутов")
"""
    try:
        completed = subprocess.run(
            [sys.executable, "-c", code],
            input=raw,
            capture_output=True,
            cwd=Path(v8container.__file__).resolve().parents[1],
            timeout=3,
        )
    except subprocess.TimeoutExpired:
        pytest.fail("Чтение таблицы с общим хвостом не завершилось за 3 с")
    assert completed.returncode == 0, completed.stderr.decode("utf-8", errors="replace")


@pytest.mark.parametrize("entries", [1, 2])
def test_короткая_цепочка_с_пустыми_блоками_читается(entries):
    raw = _shared_suffix_container(entries, 4)

    with V8Container(raw) as container:
        assert container.namelist() == ["x"]
        assert container.read("x") == b""


def test_пустая_таблица_контейнера_читается():
    raw = struct.pack("<IIII", 0x7FFFFFFF, 512, 0, 0) + _block(b"")

    with V8Container(raw) as container:
        assert container.namelist() == []
