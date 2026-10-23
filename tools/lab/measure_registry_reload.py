"""Цена повторного `startup()` реестра — того, что делает `POST /admin/reload`.

Меряется на КОПИИ каталога данных: startup пишет кэш индексов и
registry.json, поэтому боевой `data/` сюда передавать нельзя.

Что печатается (без имён конфигураций — в публичный CHANGELOG идут только
числа):

- RSS после первого startup (тёплый кэш) и пик RSS во время второго;
- время второго startup;
- сколько файлов кэша индексов переписано вторым startup (по mtime_ns);
- время открытия ролевых SQLite-кэшей и доля `PRAGMA quick_check(1)` в нём.

RSS снимается фоновым потоком раз в 5 мс из `task_info` (macOS) или
`/proc/self/statm` (Linux): `ru_maxrss` держит пик первого старта и второй
за ним не виден.

Штамп кэша включает sha256 пакета `src/mcp1c`, поэтому после любой правки
кода первый startup собирает индексы заново и его память искажает замер.
Первый прогон с `0` повторов прогревает кэш, второй меряет.

Запуск:

    rsync -a --exclude incoming data/ /tmp/data-copy/
    .venv/bin/python tools/lab/measure_registry_reload.py /tmp/data-copy 0
    .venv/bin/python tools/lab/measure_registry_reload.py /tmp/data-copy 2
"""
from __future__ import annotations

import ctypes
import ctypes.util
import gc
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, "src")

from mcp1c import role_access  # noqa: E402
from mcp1c.registry import Registry  # noqa: E402


def _rss_reader():
    if sys.platform == "darwin":
        libc = ctypes.CDLL(ctypes.util.find_library("c"))

        class _Info(ctypes.Structure):
            _fields_ = [
                ("virtual_size", ctypes.c_uint64),
                ("resident_size", ctypes.c_uint64),
                ("resident_size_max", ctypes.c_uint64),
                ("user_time", ctypes.c_uint64),
                ("system_time", ctypes.c_uint64),
                ("policy", ctypes.c_int),
                ("suspend_count", ctypes.c_int),
            ]

        task = libc.mach_task_self()
        flavor = 20  # MACH_TASK_BASIC_INFO
        count = ctypes.c_uint(ctypes.sizeof(_Info) // 4)

        def read() -> int:
            info = _Info()
            size = ctypes.c_uint(count.value)
            libc.task_info(task, flavor, ctypes.byref(info), ctypes.byref(size))
            return info.resident_size

        return read
    page = os.sysconf("SC_PAGE_SIZE")

    def read_linux() -> int:
        with open("/proc/self/statm", encoding="ascii") as stream:
            return int(stream.read().split()[1]) * page

    return read_linux


RSS = _rss_reader()


def мб(value: int) -> str:
    return f"{value / 1024 / 1024:.0f} МБ"


class Пик:
    def __init__(self) -> None:
        self.максимум = 0
        self._стоп = threading.Event()
        self._поток = threading.Thread(target=self._цикл, daemon=True)

    def _цикл(self) -> None:
        while not self._стоп.is_set():
            self.максимум = max(self.максимум, RSS())
            self._стоп.wait(0.005)

    def __enter__(self) -> "Пик":
        self.максимум = RSS()
        self._поток.start()
        return self

    def __exit__(self, *exc) -> None:
        self._стоп.set()
        self._поток.join()
        self.максимум = max(self.максимум, RSS())


def снимок_кэша(cache_dir: Path) -> dict[str, int]:
    result = {}
    for path in cache_dir.rglob("*"):
        if path.is_file():
            result[str(path.relative_to(cache_dir))] = path.stat().st_mtime_ns
    return result


def время_открытия_ролей() -> tuple[list[float], list[float]]:
    """Обернуть `_open_readonly` и отдельно засечь `quick_check` тех же файлов."""
    открытия: list[float] = []
    original = role_access._open_readonly

    def засечь(path, layer, **kwargs):
        start = time.perf_counter()
        try:
            return original(path, layer, **kwargs)
        finally:
            открытия.append(time.perf_counter() - start)
            засечь.пути.append(Path(path))

    засечь.пути = []
    role_access._open_readonly = засечь
    return открытия, засечь


def quick_check(path: Path) -> float:
    uri = f"file:{path.resolve()}?mode=ro&immutable=1"
    connection = sqlite3.connect(uri, uri=True)
    try:
        start = time.perf_counter()
        connection.execute("PRAGMA quick_check(1)").fetchone()
        return time.perf_counter() - start
    finally:
        connection.close()


def main() -> int:
    data_dir = Path(sys.argv[1]).resolve()
    повторов = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    открытия, обёртка = время_открытия_ролей()

    registry = Registry(data_dir)
    start = time.perf_counter()
    registry.startup()
    print(f"первый startup        : {time.perf_counter() - start:.2f} с")
    print(f"конфигураций          : {len(registry.configurations)}")
    gc.collect()

    for номер in range(1, повторов + 1):
        открытия.clear()
        обёртка.пути.clear()
        до = снимок_кэша(registry.cache_dir)
        gc.collect()
        база = RSS()
        with Пик() as пик:
            start = time.perf_counter()
            registry.startup()
            длительность = time.perf_counter() - start
        gc.collect()
        после = снимок_кэша(registry.cache_dir)
        переписано = sum(
            1 for name, mtime in после.items() if до.get(name) != mtime
        )
        проверки = [quick_check(path) for path in обёртка.пути if path.exists()]
        print(
            f"reload #{номер}: {длительность:.2f} с; RSS до {мб(база)}, "
            f"пик {мб(пик.максимум)} (+{мб(пик.максимум - база)}), "
            f"после {мб(RSS())}; файлов кэша переписано {переписано} "
            f"из {len(после)}; открытий ролей {len(открытия)} "
            f"за {sum(открытия) * 1000:.1f} мс, из них quick_check "
            f"{sum(проверки) * 1000:.1f} мс"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
