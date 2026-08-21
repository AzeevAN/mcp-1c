"""Отзывчивость сервера во время холодной фоновой сборки индексов кода.

Запускается рядом с отдельным контейнером, которому смонтирована КОПИЯ
каталога данных без файлов `*.modules-*` в `index/cache`:

    .venv/bin/python tools/lab/measure_background_responsiveness.py \
        http://127.0.0.1:5002 /путь/к/копии/data

Раз в секунду одновременно вызывает `/health` и настоящий MCP-инструмент
`search_objects`. Имена конфигураций и содержимое ответов не печатает.
Завершается, когда у каждого источника кода появились четыре файла кэша и
все записи `registry.json` перешли в `ready`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", help="Базовый URL сервера, без /mcp")
    parser.add_argument("data", type=Path, help="Смонтированная копия data")
    parser.add_argument("--query", default="номенклатура")
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--timeout", type=float, default=300.0)
    return parser.parse_args()


def _code_rows(registry_path: Path) -> list[dict]:
    try:
        payload = json.loads(registry_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return [
        row
        for row in payload.get("sources", [])
        if row.get("kind") in ("modules", "extension")
    ]


def _cache_count(cache_dir: Path) -> int:
    return len(list(cache_dir.glob("*.modules-*")))


def _get_health(base_url: str) -> tuple[int, dict]:
    with urlopen(f"{base_url}/health", timeout=10) as response:
        return response.status, json.loads(response.read())


async def _wait_health(base_url: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            status, _ = await asyncio.to_thread(_get_health, base_url)
            if status == 200:
                return
        except (OSError, URLError, ValueError):
            pass
        await asyncio.sleep(0.2)
    raise TimeoutError("сервер не открыл /health")


async def _timed_health(base_url: str) -> tuple[float, bool]:
    started = time.perf_counter()
    try:
        status, payload = await asyncio.to_thread(_get_health, base_url)
        ok = status == 200 and payload.get("status") == "ok"
    except (OSError, URLError, ValueError):
        ok = False
    return (time.perf_counter() - started) * 1000, ok


async def _timed_search(
    session: ClientSession, query: str
) -> tuple[float, bool]:
    started = time.perf_counter()
    try:
        result = await session.call_tool(
            "search_objects",
            {"query": query, "limit": 5},
            read_timeout_seconds=10,
        )
        ok = not bool(getattr(result, "isError", False))
    except Exception:
        ok = False
    return (time.perf_counter() - started) * 1000, ok


def _distribution(values: list[float]) -> dict[str, float]:
    ordered = sorted(values)
    p95_index = max(0, min(len(ordered) - 1, int(len(ordered) * 0.95) - 1))
    return {
        "median_ms": round(statistics.median(ordered), 3),
        "p95_ms": round(ordered[p95_index], 3),
        "worst_ms": round(max(ordered), 3),
    }


async def _main() -> int:
    args = _arguments()
    base_url = args.url.rstrip("/")
    registry_path = args.data / "registry.json"
    cache_dir = args.data / "index" / "cache"

    health_times: list[float] = []
    search_times: list[float] = []
    health_failures = 0
    search_failures = 0
    completed = False

    await _wait_health(base_url, args.timeout)
    # До открытия порта startup ещё наполняет реестр и фоновые потоки могут
    # успеть сохранить промежуточный снимок. Считать состав раньше нельзя:
    # временные две строки вместо трёх сделали бы условие завершения вечным.
    expected_sources = len(_code_rows(registry_path))
    if expected_sources == 0:
        raise SystemExit("в registry.json нет источников modules/extension")
    expected_cache = expected_sources * 4
    async with streamable_http_client(f"{base_url}/mcp") as streams:
        read, write, *_ = streams
        async with ClientSession(read, write) as session:
            await session.initialize()
            started = time.monotonic()
            deadline = started + args.timeout
            while time.monotonic() < deadline:
                cycle = time.monotonic()
                health, search = await asyncio.gather(
                    _timed_health(base_url),
                    _timed_search(session, args.query),
                )
                health_ms, health_ok = health
                search_ms, search_ok = search
                health_times.append(health_ms)
                search_times.append(search_ms)
                health_failures += int(not health_ok)
                search_failures += int(not search_ok)

                rows = _code_rows(registry_path)
                if (
                    len(rows) == expected_sources
                    and all(row.get("status") == "ready" for row in rows)
                    and _cache_count(cache_dir) >= expected_cache
                    and len(health_times) >= 3
                ):
                    completed = True
                    break
                await asyncio.sleep(
                    max(0.0, args.interval - (time.monotonic() - cycle))
                )

    result = {
        "completed": completed,
        "samples": len(health_times),
        "duration_s": round(time.monotonic() - started, 3),
        "health": _distribution(health_times),
        "search_objects": _distribution(search_times),
        "health_failures": health_failures,
        "search_failures": search_failures,
        "cache_files": _cache_count(cache_dir),
        "expected_cache_files": expected_cache,
    }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if completed else 2


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
