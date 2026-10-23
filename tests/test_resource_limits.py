"""Каждая ось вычислительного бюджета проверяется отдельно."""

from __future__ import annotations

import io
import struct
import zipfile

import pytest

from mcp1c import resource_limits as _m2_resource_limits
from mcp1c.resource_limits import (
    LimitedReader,
    LimitedZipFile,
    ResourceBudget,
    ResourceLimitError,
    ResourceLimits,
)


LIMITS = ResourceLimits(
    max_entries=2,
    max_entry_bytes=10,
    max_total_bytes=15,
    max_compression_ratio=4,
)


@pytest.mark.parametrize(
    ("members", "message"),
    [
        ([('a', 1, 1), ('b', 1, 1), ('c', 1, 1)], "число записей"),
        ([('a', 11, 11)], "одна запись|запись"),
        ([('a', 8, 8), ('b', 8, 8)], "суммарный"),
        ([('a', 9, 2)], "коэффициент сжатия"),
    ],
)
def test_заявленный_состав_проверяет_каждую_ось(members, message):
    budget = ResourceBudget(LIMITS, "архив")

    with pytest.raises(ResourceLimitError, match=message):
        budget.validate_members(members)


def test_реальное_чтение_останавливается_если_заголовок_соврал():
    budget = ResourceBudget(LIMITS, "архив")
    stream = LimitedReader(io.BytesIO(b"x" * 11), budget, "entry")

    with pytest.raises(ResourceLimitError, match="при чтении"):
        stream.read()


def test_внешний_zip_отклоняется_по_числу_записей():
    blob = io.BytesIO()
    with zipfile.ZipFile(blob, "w") as archive:
        archive.writestr("a", b"1")
        archive.writestr("b", b"2")
        archive.writestr("c", b"3")
    blob.seek(0)

    with pytest.raises(ResourceLimitError, match="число записей"):
        LimitedZipFile(blob, limits=LIMITS)


def _m2_eocd(entries=0, size_cd=0, *, comment=b""):
    return struct.pack(
        "<4s4H2LH", b"PK\x05\x06", 0, 0, entries, entries, size_cd, 0, len(comment)
    ) + comment


def _m2_zip64_eocd(entries=0, size_cd=0, *, comment=b"", force_sentinels=False):
    record = struct.pack(
        "<4sQ2H2L4Q", b"PK\x06\x06", 44, 45, 45, 0, 0, entries, entries, size_cd, 0
    )
    locator = struct.pack("<4sLQL", b"PK\x06\x07", 0, 0, 1)
    return record + locator + _m2_eocd(
        0xFFFF if force_sentinels else min(entries, 0xFFFF),
        0xFFFFFFFF if force_sentinels else min(size_cd, 0xFFFFFFFF), comment=comment
    )


def _m2_oversized_directory(kind):
    if kind == "entries":
        # Превышены и max_entries=2, и суммарный бюджет файлов с каталогами.
        return _m2_eocd(entries=5)
    if kind == "directory-bytes":
        return _m2_eocd(size_cd=29_534_471)
    if kind == "zip64-entries":
        return _m2_zip64_eocd(entries=2**32 + 1)
    if kind == "zip64-directory-bytes":
        return _m2_zip64_eocd(size_cd=2**32 + 1)
    raise AssertionError(f"Неизвестная синтетическая ось: {kind}")


def _m2_limits(kind):
    # Для ZIP64 16-битный sentinel 65535 сам по себе ниже бюджета: важен wide counter.
    return ResourceLimits(
        max_entries=100_000 if kind == "zip64-entries" else 2,
        max_entry_bytes=10, max_total_bytes=15, max_compression_ratio=4,
    )


def _m2_forbid_zip_constructor(self, *args, **kwargs):
    # ZipFile.__del__ может вызваться даже после отказа в __init__.
    self.fp = None
    raise AssertionError("Центральный каталог передан ZipFile до проверки бюджета")


def _m2_zip_bytes(members, *, comment=b""):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)
        archive.comment = comment
    return stream.getvalue()


@pytest.mark.parametrize("kind", ["entries", "directory-bytes", "zip64-entries", "zip64-directory-bytes"])
def test_limited_zip_отказывает_по_eocd_до_конструктора_zipfile(kind, monkeypatch):
    stream = io.BytesIO(_m2_oversized_directory(kind))
    monkeypatch.setattr(zipfile.ZipFile, "__init__", _m2_forbid_zip_constructor)

    with pytest.raises(ResourceLimitError):
        LimitedZipFile(stream, limits=_m2_limits(kind))

    assert not stream.closed


@pytest.mark.parametrize("comment", [b"", b"synthetic archive comment"])
@pytest.mark.parametrize("over_limit", [False, True])
def test_eocd_precheck_учитывает_комментарий_и_возвращает_позицию(comment, over_limit):
    stream = io.BytesIO(_m2_eocd(entries=5 if over_limit else 0, comment=comment))
    stream.seek(3)

    if over_limit:
        with pytest.raises(ResourceLimitError):
            _m2_resource_limits._precheck_central_directory(stream, _m2_limits("entries"))
    else:
        _m2_resource_limits._precheck_central_directory(stream, _m2_limits("entries"))

    assert stream.tell() == 3
    assert not stream.closed


def test_eocd_precheck_принимает_zip64_sentinel_с_допустимыми_wide_counters():
    stream = io.BytesIO(_m2_zip64_eocd(comment=b"zip64 comment", force_sentinels=True))
    stream.seek(7)

    _m2_resource_limits._precheck_central_directory(stream, _m2_limits("entries"))

    assert stream.tell() == 7
    assert not stream.closed


def test_limited_zip_закрывает_свой_дескриптор_при_отказе_eocd(tmp_path, monkeypatch):
    path = tmp_path / "oversized.zip"
    path.write_bytes(_m2_oversized_directory("entries"))
    checked_streams = []
    original = _m2_resource_limits._precheck_central_directory

    def capture(stream, limits):
        checked_streams.append(stream)
        return original(stream, limits)

    monkeypatch.setattr(_m2_resource_limits, "_precheck_central_directory", capture)
    with pytest.raises(ResourceLimitError):
        LimitedZipFile(path, limits=_m2_limits("entries"))

    assert len(checked_streams) == 1
    assert checked_streams[0].closed


def test_limited_zip_не_закрывает_внешний_поток_при_закрытии_wrapper():
    stream = io.BytesIO(_m2_zip_bytes({"a": b"1"}, comment=b"comment"))

    with LimitedZipFile(stream, limits=_m2_limits("entries")) as archive:
        assert archive.read("a") == b"1"

    assert not stream.closed
    stream.seek(0)
    assert stream.read(2) == b"PK"


def test_limited_zip_читает_проверенный_дескриптор_после_замены_пути(tmp_path, monkeypatch):
    path = tmp_path / "source.zip"
    path.write_bytes(_m2_zip_bytes({"a": b"1"}))
    replacement_bytes = _m2_zip_bytes({"b": b"2"})
    replacement = tmp_path / "replacement.zip"
    replacement.write_bytes(replacement_bytes)
    original = _m2_resource_limits._precheck_central_directory

    def swap_path_after_check(stream, limits):
        original(stream, limits)
        replacement.replace(path)

    monkeypatch.setattr(_m2_resource_limits, "_precheck_central_directory", swap_path_after_check)

    with LimitedZipFile(path, limits=_m2_limits("entries")) as archive:
        assert archive.namelist() == ["a"]
        assert archive.read("a") == b"1"

    assert path.read_bytes() == replacement_bytes


@pytest.mark.parametrize("source_kind", ["path", "external-stream"])
def test_limited_zip_закрытие_wrapper_не_закрывает_открытый_member(
    tmp_path, monkeypatch, source_kind
):
    raw = _m2_zip_bytes({"a": b"synthetic"})
    external = io.BytesIO(raw) if source_kind == "external-stream" else None
    archive = reader = None
    checked_streams = []
    original = _m2_resource_limits._precheck_central_directory

    def capture(stream, limits):
        checked_streams.append(stream)
        return original(stream, limits)

    monkeypatch.setattr(_m2_resource_limits, "_precheck_central_directory", capture)
    if external is None:
        source = tmp_path / "source.zip"
        source.write_bytes(raw)
    else:
        source = external

    try:
        archive = LimitedZipFile(source, limits=_m2_limits("entries"))
        reader = archive.open("a")
        assert len(checked_streams) == 1

        archive.close()

        assert not checked_streams[0].closed
        assert reader.read() == b"synthetic"
        reader.close()
        reader = None
        if external is not None:
            assert checked_streams[0] is external
            assert not external.closed
        else:
            assert checked_streams[0].closed
    finally:
        try:
            if reader is not None:
                reader.close()
        finally:
            try:
                if archive is not None:
                    archive.close()
            finally:
                if external is not None:
                    external.close()
                else:
                    for stream in checked_streams:
                        stream.close()
