"""Сравнить thread-local Snowball и сериализацию общим lock на code-corpus.

Каждый повтор запускается в отдельном процессе, одновременно строит индексы
указанных read-only копий каталогов модулей и печатает только обезличенные
агрегаты. Запуск из корня проекта:

    .venv/bin/python tools/lab/measure_stemmer_concurrency.py \
      --modules-root /tmp/corpus-a --modules-root /tmp/corpus-b --repeat 3
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import math
import resource
import statistics
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path


ROOT = Path(__file__).parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from mcp1c import search  # noqa: E402
from mcp1c.module_content import LocatorIdentity  # noqa: E402
from mcp1c.registry import Registry  # noqa: E402


SCHEMA = "stemmer-concurrency-v1"
STRATEGIES = ("thread-local", "lock")


def _peak_rss_mib() -> float:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    divisor = 1024 * 1024 if sys.platform == "darwin" else 1024
    return value / divisor


def _steady_rss_mib() -> float:
    if sys.platform.startswith("linux"):
        status = Path("/proc/self/status").read_text(encoding="utf-8")
        for line in status.splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024
        raise RuntimeError("VmRSS отсутствует")
    if sys.platform != "darwin":
        raise RuntimeError("текущий RSS не поддержан на этой платформе")

    class TimeValue(ctypes.Structure):
        _fields_ = (("seconds", ctypes.c_int), ("microseconds", ctypes.c_int))

    class MachTaskBasicInfo(ctypes.Structure):
        _fields_ = (
            ("virtual_size", ctypes.c_uint64),
            ("resident_size", ctypes.c_uint64),
            ("resident_size_max", ctypes.c_uint64),
            ("user_time", TimeValue),
            ("system_time", TimeValue),
            ("policy", ctypes.c_int),
            ("suspend_count", ctypes.c_int),
        )

    library = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
    library.mach_task_self.restype = ctypes.c_uint
    info = MachTaskBasicInfo()
    count = ctypes.c_uint(ctypes.sizeof(info) // ctypes.sizeof(ctypes.c_uint))
    result = library.task_info(
        library.mach_task_self(),
        20,
        ctypes.byref(info),
        ctypes.byref(count),
    )
    if result != 0:
        raise RuntimeError("task_info не вернул текущий RSS")
    return info.resident_size / (1024 * 1024)


def _configure_strategy(strategy: str) -> None:
    search._STEMMER_LOCAL = threading.local()
    with search._STEM_CACHE_LOCK:
        search._STEM_CACHE.clear()
    if strategy == "thread-local":
        search._STEMMER_FACTORY = lambda: search.snowballstemmer.stemmer(
            "russian"
        )
        return
    if strategy != "lock":
        raise ValueError("неизвестная стратегия")

    shared = search.snowballstemmer.stemmer("russian")
    shared_lock = threading.Lock()

    class LockedStemmer:
        def stemWord(self, token: str) -> str:
            with shared_lock:
                return shared.stemWord(token)

    proxy = LockedStemmer()
    search._STEMMER_FACTORY = lambda: proxy


def _result_digest(indexes) -> str:
    digest = hashlib.sha256()
    for index in indexes:
        counts = (
            len(index.оглавление.модули),
            len(index.оглавление.имена),
            index.вызовы.рёбер,
            len(index.формы.модули),
            len(index.поиск.docs),
        )
        digest.update(repr(counts).encode("ascii"))
        for query in ("процедура", "функция", "проверить"):
            for hit in index.поиск.search(query, limit=20):
                digest.update(hit.doc.id.encode("utf-8"))
                digest.update(b"\0")
    return digest.hexdigest()


def _worker(roots: list[Path], strategy: str) -> dict[str, object]:
    _configure_strategy(strategy)
    started = time.perf_counter()

    def build(item):
        ordinal, root = item
        return Registry._построить_индекс_кода(
            f"source-{ordinal}",
            root,
            LocatorIdentity(
                f"source-{ordinal}",
                hashlib.sha256(f"source-{ordinal}".encode("ascii")).hexdigest(),
                ordinal,
            ),
        )

    with ThreadPoolExecutor(max_workers=len(roots)) as executor:
        indexes = list(executor.map(build, enumerate(roots, start=1)))
    elapsed = time.perf_counter() - started
    return {
        "schema": SCHEMA,
        "completed": True,
        "strategy": strategy,
        "roots_total": len(roots),
        "elapsed_seconds": elapsed,
        "steady_rss_mib": _steady_rss_mib(),
        "peak_rss_mib": _peak_rss_mib(),
        "result_digest": _result_digest(indexes),
    }


def _nearest(values: list[float], fraction: float) -> float:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


def _summary(rows: list[dict[str, object]]) -> dict[str, object]:
    def metric(name: str) -> dict[str, float]:
        values = [float(row[name]) for row in rows]
        return {
            "p50": round(_nearest(values, 0.50), 3),
            "p95": round(_nearest(values, 0.95), 3),
            "mean": round(statistics.fmean(values), 3),
        }

    return {
        "elapsed_seconds": metric("elapsed_seconds"),
        "steady_rss_mib": metric("steady_rss_mib"),
        "peak_rss_mib": metric("peak_rss_mib"),
        "stable_results": len({str(row["result_digest"]) for row in rows}) == 1,
        "result_digest": str(rows[0]["result_digest"]),
    }


def _run_parent(roots: list[Path], repeat: int) -> dict[str, object]:
    rows = {strategy: [] for strategy in STRATEGIES}
    for _ in range(repeat):
        for strategy in STRATEGIES:
            command = [
                sys.executable,
                str(Path(__file__).resolve()),
                "--worker",
                "--strategy",
                strategy,
            ]
            for root in roots:
                command.extend(("--modules-root", str(root)))
            result = subprocess.run(
                command,
                check=False,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                raise RuntimeError("дочерний замер не завершён")
            rows[strategy].append(json.loads(result.stdout))
    summaries = {
        strategy: _summary(values) for strategy, values in rows.items()
    }
    digests = {
        str(row["result_digest"])
        for values in rows.values()
        for row in values
    }
    return {
        "schema": SCHEMA,
        "completed": True,
        "roots_total": len(roots),
        "repeat": repeat,
        "equivalent_results": len(digests) == 1,
        "strategies": summaries,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--modules-root",
        action="append",
        type=Path,
        required=True,
        help="read-only копия сохранённого каталога модулей; можно повторять",
    )
    parser.add_argument("--repeat", type=int, default=3)
    parser.add_argument("--worker", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--strategy", choices=STRATEGIES)
    args = parser.parse_args(argv)
    if args.repeat < 1:
        parser.error("--repeat должен быть больше нуля")
    if not args.modules_root or any(not path.is_dir() for path in args.modules_root):
        parser.error("каждый --modules-root должен быть каталогом")
    try:
        payload = (
            _worker(args.modules_root, args.strategy)
            if args.worker
            else _run_parent(args.modules_root, args.repeat)
        )
    except Exception:
        print(json.dumps({"schema": SCHEMA, "completed": False}))
        return 2
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
