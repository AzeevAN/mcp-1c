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
    Проверено на поставке 8.3.27.2130: `shquery_ru.hbk`, `shlang_ru.hbk`,
    `shclang_ru.hbk`, `1cv8_ru.hbk` читаются штатным кодом, `page_size` у всех
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
            chunk = self._data[start : start + block_size]
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

        for offset in range(0, len(table), 12):
            attrs_addr, data_addr, _ = struct.unpack_from("<III", table, offset)
            if attrs_addr == EMPTY_ADDR:
                continue

            attrs = self._read_document(attrs_addr)
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
