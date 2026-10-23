"""Историческое чтение BSL ограничивает выбранное тело до материализации."""

from __future__ import annotations

import struct
import zlib

import pytest

from module_samples import v8_container_bytes
from mcp1c import module_content
from mcp1c import v8container
from mcp1c.member_pack import MemberPackWriter
from mcp1c.module_content import (
    ContentReadError, ModuleLocator, read_bsl, read_content_bytes,
)
from mcp1c.resource_limits import ResourceLimits
from mcp1c.v8container import (
    BLOCK_HEADER_SIZE, EMPTY_ADDR, HEADER_SIZE, V8Container,
    V8ResourceLimitError,
)


ADDRESS = "ОбщийМодуль.Проба"


def _raw_container(name: str, payload: bytes) -> bytes:
    """Минимальный V8: выбранная запись хранится без deflate."""
    def block(data: bytes) -> bytes:
        header = (
            b"\r\n" + f"{len(data):08x} {len(data):08x} {EMPTY_ADDR:08x}".encode()
            + b" \r\n"
        )
        assert len(header) == BLOCK_HEADER_SIZE
        return header + data

    attrs = block(b"\0" * 20 + name.encode("utf-16-le") + b"\0\0")
    table = block(struct.pack(
        "<III", HEADER_SIZE + BLOCK_HEADER_SIZE + 12,
        HEADER_SIZE + BLOCK_HEADER_SIZE + 12 + len(attrs), 0,
    ))
    return struct.pack("<IIII", EMPTY_ADDR, 512, 0, 0) + table + attrs + block(payload)


@pytest.mark.parametrize("packed", [False, True])
def test_файловый_модуль_строго_ограничен_для_loose_и_pack(
    tmp_path, monkeypatch, packed
):
    monkeypatch.setattr(module_content, "HISTORICAL_MODULE_BYTES", 8, raising=False)
    path = "CommonModules/Probe/Ext/Module.bsl"
    if packed:
        with MemberPackWriter(tmp_path) as pack:
            with pack.entry(path) as writer:
                writer.write(b"abcdefgh")
    else:
        target = tmp_path / path
        target.parent.mkdir(parents=True)
        target.write_bytes(b"abcdefgh")
    locator = ModuleLocator.file(path)
    assert read_bsl(tmp_path, ADDRESS, locator) == "abcdefgh"
    assert read_content_bytes(tmp_path, ADDRESS, locator) == b"abcdefgh"

    if packed:
        other = tmp_path / "oversized"
        other.mkdir()
        with MemberPackWriter(other) as pack:
            with pack.entry(path) as writer:
                writer.write(b"abcdefghi")
        root = other
    else:
        (tmp_path / path).write_bytes(b"abcdefghi")
        root = tmp_path
    with pytest.raises(ContentReadError) as caught:
        read_bsl(root, ADDRESS, locator)
    assert caught.value.category == "budget_exceeded"
    assert read_content_bytes(root, ADDRESS, locator) == b"abcdefghi"


def test_form_container_ограничивает_только_выбранный_bsl(tmp_path, monkeypatch):
    monkeypatch.setattr(module_content, "FORM_MAX_MODULE_BYTES", 8, raising=False)
    data = v8_container_bytes([("form", b"X" * 200), ("module", b"abcdefgh")])
    (tmp_path / "Form.bin").write_bytes(data)
    locator = ModuleLocator.container("Form.bin", "module")
    assert len(data) > 8
    assert read_bsl(tmp_path, ADDRESS, locator) == "abcdefgh"
    assert read_content_bytes(
        tmp_path, ADDRESS, ModuleLocator.container("Form.bin", "form")
    ) == b"X" * 200

    (tmp_path / "Form.bin").write_bytes(
        v8_container_bytes([("form", b"X" * 200), ("module", b"abcdefghi")])
    )
    with pytest.raises(ContentReadError) as caught:
        read_bsl(tmp_path, ADDRESS, locator)
    assert caught.value.category == "budget_exceeded"
    with pytest.raises(ContentReadError) as raw_caught:
        read_content_bytes(tmp_path, ADDRESS, locator)
    assert raw_caught.value.category == "budget_exceeded"
    assert read_content_bytes(
        tmp_path, ADDRESS, ModuleLocator.container("Form.bin", "form")
    ) == b"X" * 200


def test_raw_record_не_обходит_лимит_и_дочитывается_только_до_границы(tmp_path,
                                                                     monkeypatch):
    monkeypatch.setattr(module_content, "FORM_MAX_MODULE_BYTES", 8, raising=False)
    locator = ModuleLocator.container("flat.Form", "module")
    (tmp_path / "flat.Form").write_bytes(_raw_container("module", b"abcdefgh"))
    assert read_bsl(tmp_path, ADDRESS, locator) == "abcdefgh"
    (tmp_path / "flat.Form").write_bytes(_raw_container("module", b"abcdefghi"))
    with pytest.raises(ContentReadError) as caught:
        read_bsl(tmp_path, ADDRESS, locator)
    assert caught.value.category == "budget_exceeded"


def test_deflate_bomb_останавливается_по_лимиту_выхода(tmp_path, monkeypatch):
    monkeypatch.setattr(module_content, "FORM_MAX_MODULE_BYTES", 8, raising=False)
    (tmp_path / "Form.bin").write_bytes(
        v8_container_bytes([("module", bytes(range(256)) * 16)])
    )
    with pytest.raises(ContentReadError) as caught:
        read_bsl(tmp_path, ADDRESS, ModuleLocator.container("Form.bin", "module"))
    assert caught.value.category == "budget_exceeded"


def test_selected_record_не_собирается_целиком_до_проверки(monkeypatch):
    for data in (
        v8_container_bytes([("module", b"Q" * 10_000)]),
        _raw_container("module", b"Q" * 10_000),
    ):
        with V8Container(data) as container:
            selected_addr = container.entry("module")._data_addr
            real = V8Container._read_document

            def checked(self, addr):
                assert addr != selected_addr
                return real(self, addr)

            with monkeypatch.context() as patch:
                patch.setattr(V8Container, "_read_document", checked)
                with pytest.raises(V8ResourceLimitError):
                    container.read_bounded("module", 8)


def test_bounded_reader_сохраняет_общий_бюджет_и_отношение_сжатия():
    data = v8_container_bytes([("module", b"A" * 500)])
    ratio_limits = ResourceLimits(10, 1024, 2048, 2)
    with V8Container(data, limits=ratio_limits) as container:
        with pytest.raises(V8ResourceLimitError):
            container.read_bounded("module", 800)

    total_limits = ResourceLimits(10, 1024, 100, 1000)
    with V8Container(data, limits=total_limits) as container:
        with pytest.raises(V8ResourceLimitError):
            container.read_bounded("module", 800)


def test_ложно_завышенный_размер_документа_не_обходит_коэффициент_сжатия():
    raw = bytearray(v8_container_bytes([("module", b"A" * 500)]))
    with V8Container(raw) as container:
        addr = container.entry("module")._data_addr
    declared = int(raw[addr + 2 : addr + 10], 16)
    raw[addr + 2 : addr + 10] = f"{declared + 1000:08x}".encode("ascii")
    # Размер физически возможен, но payload цепочки короче декларации:
    # проверка ratio должна считать реально прочитанные сжатые байты.
    raw.extend(b"\0" * 1000)
    limits = ResourceLimits(10, 2048, 4096, 2)
    with V8Container(raw, limits=limits) as container:
        with pytest.raises(V8ResourceLimitError):
            container.read_bounded("module", 800)


def test_хвост_после_deflate_eof_не_передаётся_распаковщику(monkeypatch):
    compressor = zlib.compressobj(wbits=-15)
    compressed = compressor.compress(b"abcd") + compressor.flush()
    data = _raw_container("module", compressed + b"X" * 200_000)
    real = zlib.decompressobj

    class Guard:
        def __init__(self, *args):
            self.inner = real(*args)

        @property
        def eof(self):
            return self.inner.eof

        @property
        def unconsumed_tail(self):
            return self.inner.unconsumed_tail

        def decompress(self, payload, max_length):
            assert not self.inner.eof
            return self.inner.decompress(payload, max_length)

        def flush(self, max_length):
            return self.inner.flush(max_length)

    monkeypatch.setattr(v8container.zlib, "decompressobj", Guard)
    with V8Container(data) as container:
        assert container.read_bounded("module", 8) == b"abcd"


def test_compiled_locator_по_прежнему_не_читает_тело(tmp_path, monkeypatch):
    monkeypatch.setattr(module_content, "HISTORICAL_MODULE_BYTES", 1, raising=False)
    locator = ModuleLocator.compiled("Module.Module")
    with pytest.raises(ContentReadError) as caught:
        read_bsl(tmp_path, ADDRESS, locator)
    assert caught.value.category == "compiled_without_source"
