"""Единый вычислительный бюджет недоверенных архивов и потоков.

Лимит загружаемого файла относится только к сжатому представлению. Здесь
граница ставится там, где появляются реальные байты: перед распаковкой по
метаданным архива и повторно во время чтения, если заголовок соврал.
"""

from __future__ import annotations

import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterable


MIB = 1024 * 1024


class ResourceLimitError(ValueError):
    """Недоверенный источник требует больше разрешённого бюджета."""


@dataclass(frozen=True, slots=True)
class ResourceLimits:
    max_entries: int
    max_entry_bytes: int
    max_total_bytes: int
    max_compression_ratio: int


ARCHIVE_LIMITS = ResourceLimits(
    max_entries=100_000,
    max_entry_bytes=128 * MIB,
    max_total_bytes=1024 * MIB,
    max_compression_ratio=200,
)

V8_LIMITS = ResourceLimits(
    max_entries=200_000,
    max_entry_bytes=512 * MIB,
    max_total_bytes=2 * 1024 * MIB,
    max_compression_ratio=200,
)


class ResourceBudget:
    """Проверка заявленного состава и реально прочитанных байтов."""

    def __init__(
        self,
        limits: ResourceLimits,
        label: str,
        *,
        enforce_content_limits: bool = True,
    ):
        if not isinstance(enforce_content_limits, bool):
            raise TypeError("enforce_content_limits должен быть bool")
        self.limits = limits
        self.label = label
        self.enforce_content_limits = enforce_content_limits
        self.read_total = 0

    def validate_members(
        self, members: Iterable[tuple[str, int, int]]
    ) -> None:
        rows = list(members)
        if not self.enforce_content_limits:
            return
        if len(rows) > self.limits.max_entries:
            raise ResourceLimitError(
                f"{self.label}: число записей {len(rows)} превышает предел "
                f"{self.limits.max_entries}."
            )

        total = 0
        for name, size, compressed_size in rows:
            if size < 0 or compressed_size < 0:
                raise ResourceLimitError(
                    f"{self.label}: запись {name!r} объявила отрицательный размер."
                )
            if size > self.limits.max_entry_bytes:
                raise ResourceLimitError(
                    f"{self.label}: запись {name!r} размером {size} байт "
                    f"превышает предел {self.limits.max_entry_bytes}."
                )
            total += size
            if total > self.limits.max_total_bytes:
                raise ResourceLimitError(
                    f"{self.label}: суммарный распакованный объём превышает "
                    f"предел {self.limits.max_total_bytes} байт."
                )
            if size and (
                compressed_size == 0
                or size > compressed_size * self.limits.max_compression_ratio
            ):
                raise ResourceLimitError(
                    f"{self.label}: коэффициент сжатия записи {name!r} "
                    f"превышает предел {self.limits.max_compression_ratio}:1."
                )

    def consume(self, name: str, entry_total: int, amount: int) -> None:
        if (
            self.enforce_content_limits
            and entry_total + amount > self.limits.max_entry_bytes
        ):
            raise ResourceLimitError(
                f"{self.label}: запись {name!r} при чтении превысила предел "
                f"{self.limits.max_entry_bytes} байт."
            )
        if (
            self.enforce_content_limits
            and self.read_total + amount > self.limits.max_total_bytes
        ):
            raise ResourceLimitError(
                f"{self.label}: реально прочитанный объём превысил предел "
                f"{self.limits.max_total_bytes} байт."
            )
        self.read_total += amount


class LimitedReader:
    """Файловый объект, который не отдаёт больше бюджета даже при ложном header."""

    def __init__(self, stream: BinaryIO, budget: ResourceBudget, name: str):
        self._stream = stream
        self._budget = budget
        self._name = name
        self._read = 0

    def read(self, size: int = -1) -> bytes:
        if self._budget.enforce_content_limits:
            entry_left = self._budget.limits.max_entry_bytes - self._read
            total_left = self._budget.limits.max_total_bytes - self._budget.read_total
            allowed = min(entry_left, total_left)
            requested = allowed + 1 if size < 0 else min(size, allowed + 1)
        else:
            requested = size
        data = self._stream.read(requested)
        self._budget.consume(self._name, self._read, len(data))
        self._read += len(data)
        return data

    def readable(self) -> bool:
        return True

    def close(self) -> None:
        self._stream.close()

    def __enter__(self) -> "LimitedReader":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def __getattr__(self, name: str):
        return getattr(self._stream, name)


class LimitedZipFile:
    """Небольшой façade ZipFile с общей проверкой состава и чтения."""

    def __init__(
        self,
        source: str | Path | BinaryIO,
        *,
        limits: ResourceLimits = ARCHIVE_LIMITS,
        label: str = "ZIP",
    ):
        self._zip = zipfile.ZipFile(source)
        self._infos = [info for info in self._zip.infolist() if not info.is_dir()]
        self._budget = ResourceBudget(limits, label)
        try:
            self._budget.validate_members(
                (info.filename, info.file_size, info.compress_size)
                for info in self._infos
            )
        except Exception:
            self._zip.close()
            raise

    def namelist(self) -> list[str]:
        return [info.filename for info in self._infos]

    def infolist(self) -> list[zipfile.ZipInfo]:
        return list(self._infos)

    def open(self, name: str) -> LimitedReader:
        info = self._zip.getinfo(name)
        return LimitedReader(self._zip.open(info), self._budget, info.filename)

    def read(self, name: str) -> bytes:
        with self.open(name) as stream:
            return stream.read()

    def close(self) -> None:
        self._zip.close()

    def __enter__(self) -> "LimitedZipFile":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def decompress_raw_deflate(
    data: bytes,
    budget: ResourceBudget,
    name: str,
) -> bytes:
    """Распаковать raw deflate, остановившись до превышения любого лимита."""
    limits = budget.limits
    ratio_limit = len(data) * limits.max_compression_ratio
    allowed = min(
        limits.max_entry_bytes,
        limits.max_total_bytes - budget.read_total,
        ratio_limit,
    )
    decoder = zlib.decompressobj(-15)
    decoded = decoder.decompress(data, allowed + 1)
    if len(decoded) > allowed or decoder.unconsumed_tail:
        if allowed == ratio_limit:
            raise ResourceLimitError(
                f"{budget.label}: коэффициент сжатия записи {name!r} "
                f"превышает предел {limits.max_compression_ratio}:1."
            )
        raise ResourceLimitError(
            f"{budget.label}: запись {name!r} при распаковке превысила "
            f"предел {allowed} байт."
        )

    tail = decoder.flush(max(1, allowed - len(decoded) + 1))
    decoded += tail
    if not decoder.eof:
        # Для V8 это означает, что запись, вероятно, лежит без сжатия:
        # вызывающий сохранит прежнюю семантику fallback на исходные байты.
        raise zlib.error("неполный поток raw deflate")
    if len(decoded) > allowed:
        if allowed == ratio_limit:
            raise ResourceLimitError(
                f"{budget.label}: коэффициент сжатия записи {name!r} "
                f"превышает предел {limits.max_compression_ratio}:1."
            )
        raise ResourceLimitError(
            f"{budget.label}: запись {name!r} при распаковке превысила "
            f"предел {allowed} байт."
        )
    budget.consume(name, 0, len(decoded))
    return decoded
