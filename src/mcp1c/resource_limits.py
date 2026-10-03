"""Единый вычислительный бюджет недоверенных архивов и потоков.

Лимит загружаемого файла относится только к сжатому представлению. Здесь
граница ставится там, где появляются реальные байты: перед распаковкой по
метаданным архива и повторно во время чтения, если заголовок соврал.
"""

from __future__ import annotations

import os
import re
import zipfile
import zlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterable


MIB = 1024 * 1024
GIB = 1024 * MIB


class ResourceLimitError(ValueError):
    """Недоверенный источник требует больше разрешённого бюджета."""


@dataclass(frozen=True, slots=True)
class ResourceLimits:
    max_entries: int
    max_entry_bytes: int
    max_total_bytes: int
    max_compression_ratio: int
    # Записи не крупнее порога не проверяются по коэффициенту сжатия: малый
    # шаблон из повторов жмётся сильно, а ущерба от него нет — объём всё
    # равно ограничен абсолютными пределами записи и суммы.
    ratio_min_bytes: int = 0


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


INTAKE_MAX_UNPACKED_ENV = "MCP1C_INTAKE_MAX_UNPACKED"

# Профиль полной файловой выгрузки (browser-upload, incoming, а с 2026-10-03
# и локальные ZIP и каталоги /config-sources) выше ARCHIVE_LIMITS: полные
# файловые выгрузки больше гигабайта легальны. Дефолты взяты с запасом от
# замера реальных выгрузок (2026-10-02): крупнейшая — 2,01 ГиБ распакованных,
# 33 189 записей; самая большая запись — 713 МиБ (.cf родителя); больше всего
# записей — 37 037; сильнее всего сжат XML-шаблон на 2 МиБ — 243,6:1, а среди
# записей крупнее 16 МиБ — 146,3:1. Deflate даёт до ~1032:1, поэтому 512:1
# отсекает максимально сжатые бомбы, не задевая выгрузки.
INTAKE_DEFAULT_MAX_UNPACKED = 16 * GIB
# Описания метаданных, формы (XML и контейнеры), `Package.bin` и дескрипторы
# ролей разбираются в DOM целиком, и один такой файл под пределом записи
# 4 ГиБ поднял бы в памяти десятки гигабайт: ElementTree на XML в 59,1 МиБ
# даёт +1,15 ГиБ RSS (×20). Крупнейшие такие файлы десяти реальных выгрузок
# (2026-10-02): `Form.xml` — 3,52 МиБ, контейнер формы — 2,09 МиБ,
# `Package.bin` — 1,2 МиБ, описание объекта — 0,94 МиБ. 24 МиБ — запас ×6,8,
# около 0,5 ГиБ RSS на пределе. Тела макетов (`Ext/Template.xml`, до
# 59,1 МиБ) собираются, но не разбираются; `Rights.xml` (до 13,95 МиБ)
# разбирается потоком; модули — не XML. Им этот предел не нужен.
INTAKE_MAX_PARSED_XML_BYTES = 24 * MIB
INTAKE_MAX_ENTRY_BYTES = 4 * GIB
INTAKE_MAX_ENTRIES = 100_000
INTAKE_MAX_COMPRESSION_RATIO = 512
INTAKE_RATIO_MIN_BYTES = 1 * MIB

_SIZE_RE = re.compile(r"(\d+)\s*([KMGT]?)(?:I?B)?", re.IGNORECASE)
_SIZE_UNITS = {"": 1, "K": 1024, "M": MIB, "G": GIB, "T": 1024 * GIB}


def _parse_size(name: str, raw: str) -> int:
    match = _SIZE_RE.fullmatch(raw.strip())
    if match is None:
        raise ValueError(
            f"{name} должен быть целым числом байт либо с суффиксом "
            f"K/M/G/T (например 16GiB). Получено: {raw!r}."
        )
    value = int(match.group(1)) * _SIZE_UNITS[match.group(2).upper()]
    if value <= 0:
        raise ValueError(f"{name} должен быть положительным. Получено: {raw!r}.")
    return value


def intake_archive_limits(
    environ: Mapping[str, str] | None = None,
) -> ResourceLimits:
    """Профиль бюджета полной выгрузки (ZIP любого транспорта и каталог).

    Пустое значение переменной означает дефолт: так Compose может передавать
    её всегда, не навязывая собственное число.
    """
    source = os.environ if environ is None else environ
    raw = source.get(INTAKE_MAX_UNPACKED_ENV, "")
    total = (
        _parse_size(INTAKE_MAX_UNPACKED_ENV, raw)
        if raw.strip()
        else INTAKE_DEFAULT_MAX_UNPACKED
    )
    return ResourceLimits(
        max_entries=INTAKE_MAX_ENTRIES,
        max_entry_bytes=min(INTAKE_MAX_ENTRY_BYTES, total),
        max_total_bytes=total,
        max_compression_ratio=INTAKE_MAX_COMPRESSION_RATIO,
        ratio_min_bytes=INTAKE_RATIO_MIN_BYTES,
    )


class ResourceBudget:
    """Проверка заявленного состава и реально прочитанных байтов."""

    def __init__(self, limits: ResourceLimits, label: str):
        self.limits = limits
        self.label = label
        self.read_total = 0

    def ratio_exceeded(self, size: int, compressed_size: int) -> bool:
        if size <= self.limits.ratio_min_bytes:
            return False
        return compressed_size == 0 or (
            size > compressed_size * self.limits.max_compression_ratio
        )

    def validate_members(
        self, members: Iterable[tuple[str, int, int]]
    ) -> None:
        rows = list(members)
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
            if size and self.ratio_exceeded(size, compressed_size):
                raise ResourceLimitError(
                    f"{self.label}: коэффициент сжатия записи {name!r} "
                    f"превышает предел {self.limits.max_compression_ratio}:1."
                )

    def consume(self, name: str, entry_total: int, amount: int) -> None:
        if entry_total + amount > self.limits.max_entry_bytes:
            raise ResourceLimitError(
                f"{self.label}: запись {name!r} при чтении превысила предел "
                f"{self.limits.max_entry_bytes} байт."
            )
        if self.read_total + amount > self.limits.max_total_bytes:
            raise ResourceLimitError(
                f"{self.label}: реально прочитанный объём превысил предел "
                f"{self.limits.max_total_bytes} байт."
            )
        self.read_total += amount


class LimitedReader:
    """Файловый объект, который не отдаёт больше бюджета даже при ложном header.

    Считаются фактически прочитанные байты: заявленный в заголовке размер
    может врать, а коэффициент сжатия при известном ``compressed_size``
    сверяется с тем, что реально вышло из распаковщика.
    """

    def __init__(
        self,
        stream: BinaryIO,
        budget: ResourceBudget,
        name: str,
        *,
        compressed_size: int | None = None,
    ):
        self._stream = stream
        self._budget = budget
        self._name = name
        self._read = 0
        self._compressed_size = compressed_size

    def read(self, size: int = -1) -> bytes:
        limits = self._budget.limits
        entry_left = limits.max_entry_bytes - self._read
        total_left = limits.max_total_bytes - self._budget.read_total
        allowed = max(0, min(entry_left, total_left))
        if self._compressed_size is not None:
            ratio_allowed = max(
                limits.ratio_min_bytes,
                self._compressed_size * limits.max_compression_ratio,
            )
            allowed = max(0, min(allowed, ratio_allowed - self._read))
        requested = allowed + 1 if size < 0 else min(size, allowed + 1)
        data = self._stream.read(requested)
        if self._compressed_size is not None and self._budget.ratio_exceeded(
            self._read + len(data), self._compressed_size
        ):
            raise ResourceLimitError(
                f"{self._budget.label}: коэффициент сжатия записи "
                f"{self._name!r} при чтении превысил предел "
                f"{limits.max_compression_ratio}:1."
            )
        self._budget.consume(self._name, self._read, len(data))
        self._read += len(data)
        return data

    def read1(self, size: int = -1) -> bytes:
        return self.read(size)

    def readinto(self, buffer) -> int:
        data = self.read(len(buffer))
        buffer[: len(data)] = data
        return len(data)

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return False

    def close(self) -> None:
        self._stream.close()

    def __enter__(self) -> "LimitedReader":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # Прежде __getattr__ отдавал исходному потоку всё подряд: readline, seek,
    # readinto читали бы мимо учёта. Потребители зовут только read(), поэтому
    # остальное запрещено явно, а не делегируется молча.
    _PASSTHROUGH = frozenset({"name", "mode", "closed"})

    def __getattr__(self, name: str):
        if name in LimitedReader._PASSTHROUGH:
            return getattr(self._stream, name)
        raise AttributeError(
            f"LimitedReader не поддерживает {name}: чтение идёт только через "
            "read/read1/readinto под бюджетом"
        )


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
