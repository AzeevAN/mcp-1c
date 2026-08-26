"""Каждая ось вычислительного бюджета проверяется отдельно."""

from __future__ import annotations

import io
import zipfile

import pytest

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
