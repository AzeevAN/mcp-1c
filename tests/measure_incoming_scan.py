"""Синтетический замер первого показа `incoming/`: время `scan()` и число fsync.

Запуск из корня репозитория:

    PYTHONPATH=src .venv/bin/python tests/measure_incoming_scan.py --files 100

Каждый файл — 1 КиБ случайных байт, поэтому хеширование почти бесплатно и
замер показывает именно стоимость записи `incoming-state.json`.
"""

from __future__ import annotations

import argparse
import os
import statistics
import tempfile
import time
from pathlib import Path
from unittest import mock

from mcp1c.incoming import IncomingScanner
from mcp1c.registry import Registry


def measure(files: int) -> tuple[float, int]:
    with tempfile.TemporaryDirectory() as directory:
        registry = Registry(Path(directory) / "data")
        registry.incoming_dir.mkdir(parents=True, exist_ok=True)
        old = time.time() - 60
        for index in range(files):
            path = registry.incoming_dir / f"f{index:04}.zip"
            path.write_bytes(os.urandom(1024))
            os.utime(path, (old, old))
        scanner = IncomingScanner(registry)
        real = os.fsync
        count = 0

        def counting(descriptor: int) -> None:
            nonlocal count
            count += 1
            real(descriptor)

        with mock.patch("os.fsync", counting):
            started = time.perf_counter()
            scanner.scan()
            elapsed = time.perf_counter() - started
        return elapsed, count


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--files", type=int, default=100)
    parser.add_argument("--rounds", type=int, default=5)
    options = parser.parse_args()
    results = [measure(options.files) for _ in range(options.rounds)]
    times = [elapsed for elapsed, _count in results]
    print(
        f"files={options.files} median={statistics.median(times) * 1000:.1f} ms "
        f"min={min(times) * 1000:.1f} ms fsync={results[0][1]}"
    )


if __name__ == "__main__":
    main()
