"""Чтение внутреннего контейнера 1С:Предприятие 8.

Один формат используют `.hbk` (справка платформы), `.cf` (конфигурация),
`.epf` / `.erf` (внешние обработки и отчёты), `.dt` частично. Поэтому ридер
здесь общий: сегодня им разбираем синтакс-помощник, завтра — тексты модулей
из выгруженной конфигурации.

Зависимостей нет: только стандартная библиотека. Внешний `7z` не нужен —
`.hbk` вопреки распространённому заблуждению **не zip-архив**, и попытка
открыть его `zipfile` заканчивается `BadZipFile`. Коварство в том, что
`zipfile.is_zipfile()` при этом возвращает True: она ищет сигнатуру
центрального каталога где угодно в файле и натыкается на случайное
совпадение внутри сжатых данных.

Структура
---------
Заголовок, 16 байт::

    uint32 next_page_addr    0x7FFFFFFF — продолжения нет
    uint32 page_size         обычно 512
    uint32 storage_ver
    uint32 reserved

Дальше — «документы», каждый разбит на блоки. Заголовок блока, 31 байт::

    \\r\\n<doc_size:8hex> <block_size:8hex> <next_addr:8hex> \\r\\n

`doc_size` осмыслен только в первом блоке цепочки. Чтение идёт по
`next_addr`, пока не встретится 0x7FFFFFFF.

Корневой документ лежит сразу за заголовком (смещение 16) и содержит таблицу
элементов по 12 байт: адрес атрибутов, адрес данных, служебное поле.

Атрибуты элемента::

    uint64 creation_time      тики по 1/10000 секунды от 0001-01-01
    uint64 modification_time
    uint32 reserved
    ...    имя файла, UTF-16LE

Данные элемента обычно сжаты raw deflate (без zlib-обёртки, wbits=-15).
Иногда лежат как есть — в частности, если внутри вложенный контейнер.
"""

from __future__ import annotations

import mmap
import struct
import zlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterator

from .resource_limits import (
    V8_LIMITS,
    ResourceBudget,
    ResourceLimitError,
    ResourceLimits,
    decompress_raw_deflate,
)

HEADER_SIZE = 16
BLOCK_HEADER_SIZE = 31
EMPTY_ADDR = 0x7FFFFFFF
CONTAINER_MAGIC = b"\xff\xff\xff\x7f"

# 1С считает время в тиках по 1/10000 секунды от начала первого года.
_TICKS_PER_SECOND = 10_000
_EPOCH_1C = datetime(1, 1, 1)


class V8ContainerError(Exception):
    """Файл не является контейнером 1С или повреждён."""


class V8ResourceLimitError(V8ContainerError):
    """Контейнер корректен структурно, но превысил вычислительный бюджет."""


def _ticks_to_datetime(ticks: int) -> datetime | None:
    if ticks <= 0:
        return None
    try:
        return _EPOCH_1C + timedelta(seconds=ticks / _TICKS_PER_SECOND)
    except (OverflowError, ValueError):
        return None


def is_container(data: bytes) -> bool:
    """Похоже ли начало данных на контейнер 1С.

    Признак — `page_size` в разумных пределах, а не первое поле заголовка.
    Первое поле держали за сигнатуру и требовали ровно `FF FF FF 7F`, но это
    `next_page_addr`, адрес свободной страницы: `0x7FFFFFFF` означает всего
    лишь «свободных нет». Файлы, где список непуст, несут там настоящее
    смещение и получали отказ «не контейнер 1С» — неверный по существу.
    Проверено на поставке 8.3.27.2130: `shlang_ru.hbk`, `shclang_ru.hbk` и
    `1cv8_ru.hbk` читаются штатным кодом, `page_size` у всех
    512.

    Проверка стала слабее, и случайные данные иногда будут её проходить. Это
    приемлемо: разбор блоков дальше падает `V8ContainerError` — тем же
    исключением, которое ловит вызывающий.
    """
    if len(data) < HEADER_SIZE:
        return False
    page_size = struct.unpack_from("<I", data, 4)[0]
    if not 0 < page_size <= 1 << 20:
        return False
    # Размер страницы всегда степень двойки, обычно 512. Условие отсекает
    # случайные совпадения, которые дало бы одно лишь ограничение сверху.
    return page_size & (page_size - 1) == 0


@dataclass(slots=True)
class V8Entry:
    """Элемент контейнера. Данные читаются лениво."""

    name: str
    created: datetime | None
    modified: datetime | None
    _container: "V8Container"
    _data_addr: int
    _size: int = -1

    def read(self) -> bytes:
        return self._container._read_entry_data(self._data_addr)

    @property
    def size(self) -> int:
        """Размер распакованных данных. Требует чтения."""
        if self._size < 0:
            self._size = len(self.read())
        return self._size

    def is_container(self) -> bool:
        return is_container(self.read()[:HEADER_SIZE])

    def open_container(self) -> "V8Container":
        """Вложенный контейнер — например, .cf внутри .dt."""
        return V8Container(self.read(), limits=self._container._limits)


class V8Container:
    """Контейнер 1С, открытый из файла или из памяти.

    Использовать лучше как контекстный менеджер — при открытии из файла
    внутри держится mmap:

        with V8Container("shcntx_ru.hbk") as container:
            data = container.read("objects/Массив.html")
    """

    def __init__(
        self,
        source: str | Path | bytes | bytearray | memoryview,
        *,
        limits: ResourceLimits = V8_LIMITS,
    ):
        self._file = None
        self._mmap: mmap.mmap | None = None
        self._limits = limits
        self._budget = ResourceBudget(limits, "контейнер 1С")

        if isinstance(source, (bytes, bytearray, memoryview)):
            self._data: bytes | mmap.mmap = bytes(source)
        else:
            path = Path(source)
            self._file = path.open("rb")
            self._mmap = mmap.mmap(self._file.fileno(), 0, access=mmap.ACCESS_READ)
            self._data = self._mmap

        if not is_container(self._data[:HEADER_SIZE]):
            self.close()
            raise V8ContainerError(
                "Не контейнер 1С: в заголовке нет правдоподобного размера "
                "страницы. Так выглядят zip-архивы, тексты и вообще всё, что "
                "контейнером не является."
            )

        next_page, self.page_size, self.storage_version, _ = struct.unpack_from(
            "<IIII", self._data, 0
        )
        self._entries: dict[str, V8Entry] = {}
        self._order: list[str] = []
        self._read_table()

    # ------------------------------------------------------------- жизненный цикл

    def close(self) -> None:
        if self._mmap is not None:
            self._mmap.close()
            self._mmap = None
        if self._file is not None:
            self._file.close()
            self._file = None

    def __enter__(self) -> "V8Container":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # ------------------------------------------------------------- чтение блоков

    def _read_document(self, addr: int) -> bytes:
        """Собрать документ из цепочки блоков, начиная с адреса."""
        if addr == EMPTY_ADDR or addr == 0:
            return b""

        chunks: list[bytes] = []
        total = -1
        collected = 0
        visited: set[int] = set()

        while addr != EMPTY_ADDR and addr != 0:
            if addr in visited:
                raise V8ContainerError(f"Циклическая ссылка блоков по адресу {addr}.")
            visited.add(addr)

            header = self._data[addr : addr + BLOCK_HEADER_SIZE]
            if len(header) < BLOCK_HEADER_SIZE or header[:2] != b"\r\n":
                raise V8ContainerError(f"Битый заголовок блока по адресу {addr}.")

            try:
                doc_size = int(header[2:10], 16)
                block_size = int(header[11:19], 16)
                next_addr = int(header[20:28], 16)
            except ValueError as error:
                raise V8ContainerError(
                    f"Нечитаемый заголовок блока по адресу {addr}: {header!r}"
                ) from error

            if total < 0:
                total = doc_size
                if total > self._limits.max_entry_bytes:
                    raise V8ResourceLimitError(
                        f"Документ контейнера размером {total} байт превышает "
                        f"предел {self._limits.max_entry_bytes}."
                    )

            start = addr + BLOCK_HEADER_SIZE
            # Копируется не больше, чем документу ещё недостаёт: block_size из
            # заголовка может указывать за конец файла, и тогда каждый
            # маленький документ копировал бы весь хвост до EOF.
            wanted = min(block_size, total - collected)
            chunk = self._data[start : start + wanted]
            chunks.append(bytes(chunk))
            collected += len(chunk)
            if collected > self._limits.max_entry_bytes:
                raise V8ResourceLimitError(
                    "Цепочка блоков контейнера превысила предел одной записи."
                )

            if total >= 0 and collected >= total:
                break
            addr = next_addr

        document = b"".join(chunks)
        return document[:total] if total >= 0 else document

    def _document_size(self, addr: int) -> int:
        if addr in (EMPTY_ADDR, 0):
            return 0
        header = self._data[addr : addr + BLOCK_HEADER_SIZE]
        if len(header) < BLOCK_HEADER_SIZE or header[:2] != b"\r\n":
            raise V8ContainerError("Битый заголовок блока контейнера.")
        try:
            size = int(header[2:10], 16)
        except ValueError as error:
            raise V8ContainerError("Нечитаемый размер документа контейнера.") from error
        if size > self._limits.max_entry_bytes:
            raise V8ResourceLimitError("Документ контейнера превысил предел записи.")
        return size

    def _iter_document_chunks(self, addr: int) -> Iterator[bytes]:
        """Выбранный документ по частям, сохраняя структурный предел V8."""
        total = self._document_size(addr)
        collected = 0
        visited: set[int] = set()
        while addr not in (EMPTY_ADDR, 0) and collected < total:
            if addr in visited:
                raise V8ContainerError("Циклическая ссылка блоков контейнера.")
            visited.add(addr)
            header = self._data[addr : addr + BLOCK_HEADER_SIZE]
            if len(header) < BLOCK_HEADER_SIZE or header[:2] != b"\r\n":
                raise V8ContainerError("Битый заголовок блока контейнера.")
            try:
                block_size = int(header[11:19], 16)
                next_addr = int(header[20:28], 16)
            except ValueError as error:
                raise V8ContainerError("Нечитаемый заголовок блока контейнера.") from error
            remaining = min(block_size, total - collected)
            offset = addr + BLOCK_HEADER_SIZE
            while remaining:
                chunk = bytes(self._data[offset : offset + min(remaining, 64 * 1024)])
                if not chunk:
                    break
                yield chunk
                size = len(chunk)
                offset += size
                remaining -= size
                collected += size
            addr = next_addr

    # ------------------------------------------------------------- таблица

    def _read_table(self) -> None:
        table = self._read_document(HEADER_SIZE)
        if len(table) % 12:
            # Хвост не кратен записи — читаем столько целых записей, сколько есть.
            table = table[: len(table) - len(table) % 12]
        entries_total = len(table) // 12
        if entries_total > self._limits.max_entries:
            raise V8ResourceLimitError(
                f"Число записей контейнера {entries_total} превышает предел "
                f"{self._limits.max_entries}."
            )

        # До 200 000 записей могли ссылаться на один документ атрибутов до
        # предела записи: каждый читался заново и вне бюджета — часы CPU на
        # одном специально собранном файле. У настоящего контейнера у каждой
        # записи свой документ атрибутов, поэтому повтор — признак подделки.
        seen_attrs: set[int] = set()
        for offset in range(0, len(table), 12):
            attrs_addr, data_addr, _ = struct.unpack_from("<III", table, offset)
            if attrs_addr in (EMPTY_ADDR, 0):
                continue
            if attrs_addr in seen_attrs:
                raise V8ContainerError(
                    f"Повторный адрес атрибутов {attrs_addr} в таблице контейнера."
                )
            seen_attrs.add(attrs_addr)

            attrs = self._read_document(attrs_addr)
            try:
                self._budget.consume(f"attrs@{attrs_addr}", 0, len(attrs))
            except ResourceLimitError as error:
                raise V8ResourceLimitError(str(error)) from error
            if len(attrs) < 20:
                continue

            created_ticks, modified_ticks = struct.unpack_from("<QQ", attrs, 0)
            name = self._decode_name(attrs[20:])
            if not name:
                continue

            entry = V8Entry(
                name=name,
                created=_ticks_to_datetime(created_ticks),
                modified=_ticks_to_datetime(modified_ticks),
                _container=self,
                _data_addr=data_addr,
            )
            if name not in self._entries:
                self._order.append(name)
            self._entries[name] = entry

    @staticmethod
    def _decode_name(raw: bytes) -> str:
        # Имя записано UTF-16LE и добито нулями до конца документа атрибутов.
        if len(raw) % 2:
            raw = raw[:-1]
        name = raw.decode("utf-16-le", errors="replace")
        return name.split("\x00", 1)[0].strip()

    # ------------------------------------------------------------- данные

    def _read_entry_data(self, addr: int) -> bytes:
        raw = self._read_document(addr)
        if not raw:
            return b""
        try:
            return decompress_raw_deflate(raw, self._budget, f"entry@{addr}")
        except ResourceLimitError as error:
            raise V8ResourceLimitError(str(error)) from error
        except zlib.error:
            # Часть элементов лежит без сжатия — например вложенные контейнеры.
            try:
                self._budget.consume(f"entry@{addr}", 0, len(raw))
            except ResourceLimitError as error:
                raise V8ResourceLimitError(str(error)) from error
            return raw

    def _read_entry_data_bounded(self, addr: int, max_output_bytes: int) -> bytes:
        """Распаковать одну запись до лимита, не собирая raw document целиком."""
        if max_output_bytes < 0:
            raise ValueError("предел записи должен быть неотрицательным")
        compressed_size = self._document_size(addr)
        if not compressed_size:
            return b""
        limits = self._limits
        allowed = min(
            max_output_bytes,
            limits.max_entry_bytes,
            limits.max_total_bytes - self._budget.read_total,
            compressed_size * limits.max_compression_ratio,
        )
        decoder = zlib.decompressobj(-15)
        chunks: list[bytes] = []
        decoded_total = 0
        compressed_actual = 0
        try:
            for chunk in self._iter_document_chunks(addr):
                compressed_actual += len(chunk)
                if decoder.eof:
                    # zlib копит весь хвост в unused_data после EOF. Считать
                    # его для прежнего ratio, но больше не кормить decoder.
                    continue
                part = decoder.decompress(chunk, max(1, allowed - decoded_total + 1))
                decoded_total += len(part)
                if decoded_total > allowed or decoder.unconsumed_tail:
                    raise V8ResourceLimitError("Запись контейнера превысила предел чтения.")
                chunks.append(part)
            tail = decoder.flush(max(1, allowed - decoded_total + 1))
            decoded_total += len(tail)
            if decoded_total > allowed:
                raise V8ResourceLimitError("Запись контейнера превысила предел чтения.")
            chunks.append(tail)
            if not decoder.eof:
                raise zlib.error("неполный поток raw deflate")
            if self._budget.ratio_exceeded(decoded_total, compressed_actual):
                raise V8ResourceLimitError("Запись контейнера превысила предел сжатия.")
        except zlib.error:
            # Исторические записи иногда лежат как есть. Повторный проход
            # обрывается при превышении лимита очередным блоком (до 64 КиБ),
            # не создавая большой raw bytes.
            raw_chunks: list[bytes] = []
            raw_total = 0
            raw_allowed = min(
                max_output_bytes,
                limits.max_entry_bytes,
                limits.max_total_bytes - self._budget.read_total,
            )
            for chunk in self._iter_document_chunks(addr):
                raw_total += len(chunk)
                if raw_total > raw_allowed:
                    raise V8ResourceLimitError("Запись контейнера превысила предел чтения.")
                raw_chunks.append(chunk)
            try:
                self._budget.consume(f"entry@{addr}", 0, raw_total)
            except ResourceLimitError as error:
                raise V8ResourceLimitError(str(error)) from error
            return b"".join(raw_chunks)
        try:
            self._budget.consume(f"entry@{addr}", 0, decoded_total)
        except ResourceLimitError as error:
            raise V8ResourceLimitError(str(error)) from error
        return b"".join(chunks)

    # ------------------------------------------------------------- публичное API

    def namelist(self) -> list[str]:
        return list(self._order)

    def entries(self) -> Iterator[V8Entry]:
        for name in self._order:
            yield self._entries[name]

    def __contains__(self, name: str) -> bool:
        return name in self._entries

    def __len__(self) -> int:
        return len(self._entries)

    def entry(self, name: str) -> V8Entry:
        try:
            return self._entries[name]
        except KeyError:
            raise KeyError(f"В контейнере нет элемента: {name}") from None

    def read(self, name: str) -> bytes:
        return self.entry(name).read()

    def read_bounded(self, name: str, max_output_bytes: int) -> bytes:
        """Прочитать одну запись с отдельным пределом распакованного тела."""
        return self._read_entry_data_bounded(self.entry(name)._data_addr, max_output_bytes)

    def read_text(self, name: str, encoding: str = "utf-8") -> str:
        data = self.read(name)
        if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
            return data.decode("utf-16", errors="replace")
        if data[:3] == b"\xef\xbb\xbf":
            return data[3:].decode("utf-8", errors="replace")
        for candidate in (encoding, "utf-8", "cp1251"):
            try:
                return data.decode(candidate)
            except UnicodeDecodeError:
                continue
        return data.decode(encoding, errors="replace")
