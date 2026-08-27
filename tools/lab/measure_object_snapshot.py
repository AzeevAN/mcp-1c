"""Обезличенный замер цены снимка кода для ``get_object``.

Сравнивает одну конфигурацию с 0, 1 и 3 готовыми расширениями. Сценарий с
одним расширением берёт крупнейшее по ``items_total``: это не случайный
«первый» источник, зависящий от имени. Имена конфигурации, объекта,
расширений и физические пути в JSON не попадают.

``Registry.startup()`` может обновить расходный кэш и ``registry.json``,
поэтому живой замер запускается на изолированной копии ``data/``::

    PYTHONPATH=src .venv/bin/python tools/lab/measure_object_snapshot.py \
      --data /путь/к/копии/data

Latency снимается без ``tracemalloc``. Allocations — отдельные прогоны с
``tracemalloc``: traced current и peak, это не RSS процесса.
"""

from __future__ import annotations

import argparse
import gc
import json
import logging
import math
import time
import tracemalloc
from pathlib import Path

from mcp1c import tools
from mcp1c.registry import (
    KIND_CONFIGURATION,
    KIND_EXTENSION,
    KIND_MODULES,
    Registry,
)

SCHEMA = "object-snapshot-v1"
EXTENSION_COUNTS = (0, 1, 3)


class MeasurementError(Exception):
    """Корпус не позволяет снять требуемые три сценария."""


def _nearest_rank(values: list[int], fraction: float) -> int:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * fraction) - 1)]


def _latency(values_ns: list[int]) -> dict[str, float]:
    return {
        "p50_ms": round(_nearest_rank(values_ns, 0.50) / 1_000_000, 3),
        "p95_ms": round(_nearest_rank(values_ns, 0.95) / 1_000_000, 3),
        "max_ms": round(max(values_ns) / 1_000_000, 3),
    }


def _memory(values: list[int]) -> dict[str, float]:
    return {
        "p50_kib": round(_nearest_rank(values, 0.50) / 1024, 1),
        "p95_kib": round(_nearest_rank(values, 0.95) / 1024, 1),
        "max_kib": round(max(values) / 1024, 1),
    }


def _measure_call(call, *, iterations: int, allocation_runs: int) -> dict:
    expected = call()
    expected_chars = len(expected)

    gc.collect()
    enabled = gc.isenabled()
    gc.disable()
    timings = []
    try:
        for _ in range(iterations):
            started = time.perf_counter_ns()
            response = call()
            timings.append(time.perf_counter_ns() - started)
            if response != expected:
                raise MeasurementError("ответ изменился во время замера")
    finally:
        if enabled:
            gc.enable()

    current_values = []
    peak_values = []
    for _ in range(allocation_runs):
        gc.collect()
        tracemalloc.start()
        try:
            response = call()
            current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        if response != expected:
            raise MeasurementError("ответ изменился во время замера")
        current_values.append(current)
        peak_values.append(peak)

    return {
        "calls": iterations,
        "response_chars": expected_chars,
        "latency": _latency(timings),
        "traced_current": _memory(current_values),
        "traced_peak": _memory(peak_values),
    }


def _target(registry: Registry):
    snapshot = registry.snapshot()
    candidates = []
    for name in snapshot.configuration_names:
        extension_names = snapshot.extension_names(name)
        base = registry.modules.get(f"{name}:modules")
        if base is None or not base.готов:
            continue
        ready_extensions = [
            f"{name}:ext:{extension}"
            for extension in extension_names
            if (
                registry.modules.get(f"{name}:ext:{extension}") is not None
                and registry.modules[f"{name}:ext:{extension}"].готов
            )
        ]
        if len(ready_extensions) >= max(EXTENSION_COUNTS):
            candidates.append((len(ready_extensions), name, ready_extensions))
    if not candidates:
        raise MeasurementError("нужно не меньше трёх готовых расширений")

    available, name, extension_ids = max(
        candidates, key=lambda row: (row[0], row[1])
    )
    extension_ids.sort(
        key=lambda source_id: (
            -registry.sources[source_id].items_total,
            source_id,
        )
    )
    extension_ids = extension_ids[: max(EXTENSION_COUNTS)]
    configuration = registry.configurations[name]
    objects = sorted(
        configuration.config.objects.values(),
        key=lambda item: (item.kind != "Документ", item.full_name),
    )
    if not objects:
        raise MeasurementError("в конфигурации нет объектов")
    return name, configuration, objects[0], extension_ids, available


def measure_registry(
    registry: Registry, *, iterations: int, allocation_runs: int
) -> dict:
    """Измерить готовый Registry, временно фильтруя только его карты в RAM."""
    if iterations < 1 or allocation_runs < 1:
        raise ValueError("число прогонов должно быть положительным")

    name, configuration, obj, extension_ids, available = _target(registry)
    base_id = f"{name}:modules"
    original_configurations = registry.configurations
    original_sources = registry.sources
    original_modules = registry.modules
    fixed_sources = {
        source_id: source
        for source_id, source in original_sources.items()
        if source.kind not in (KIND_CONFIGURATION, KIND_MODULES, KIND_EXTENSION)
        or source_id == configuration.source.id
        or source_id == base_id
    }

    scenarios = []
    try:
        for count in EXTENSION_COUNTS:
            selected = extension_ids[:count]
            # Это лабораторный Registry на изолированной копии data: карты
            # переключаются вне замеряемого вызова, чтобы один корпус честно
            # воспроизводил N=0/1/3 без зависимости результата от имён.
            with registry._lock:
                registry.configurations = {name: configuration}
                registry.sources = {
                    **fixed_sources,
                    **{
                        source_id: original_sources[source_id]
                        for source_id in selected
                    },
                }
                registry.modules = {
                    base_id: original_modules[base_id],
                    **{
                        source_id: original_modules[source_id]
                        for source_id in selected
                    },
                }
                registry._relation_cache.clear()

            rows = {}
            for detail in ("fields", "full"):
                rows[detail] = _measure_call(
                    lambda detail=detail: tools.get_object(
                        registry, obj.full_name, config=name, detail=detail
                    ),
                    iterations=iterations,
                    allocation_runs=allocation_runs,
                )
            scenarios.append(
                {
                    "extensions": count,
                    "extension_items_total": sum(
                        original_sources[source_id].items_total
                        for source_id in selected
                    ),
                    **rows,
                }
            )
    finally:
        with registry._lock:
            registry.configurations = original_configurations
            registry.sources = original_sources
            registry.modules = original_modules
            registry._relation_cache.clear()

    return {
        "schema": SCHEMA,
        "completed": True,
        "method": {
            "latency": "perf_counter_ns без tracemalloc",
            "allocations": "tracemalloc current/peak; это не RSS",
            "one_extension": "крупнейшее по items_total",
        },
        "corpus": {
            "available_extensions": available,
            "object_kind": obj.kind,
            "base_items_total": original_sources[base_id].items_total,
        },
        "iterations": iterations,
        "allocation_runs": allocation_runs,
        "scenarios": scenarios,
    }


def _failure(reason: str) -> int:
    print(
        json.dumps(
            {"schema": SCHEMA, "completed": False, "error": reason},
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path("data"))
    parser.add_argument("--iterations", type=int, default=200)
    parser.add_argument("--allocation-runs", type=int, default=10)
    parser.add_argument("--timeout", type=float, default=180.0)
    args = parser.parse_args(argv)
    if args.iterations < 1 or args.allocation_runs < 1:
        parser.error("число прогонов должно быть положительным")
    if args.timeout < 0:
        parser.error("timeout должен быть неотрицательным")

    logging.disable(logging.CRITICAL)
    try:
        registry = Registry(args.data)
        startup_problems = registry.startup()
        if not registry.wait_for_module_builds(timeout=args.timeout):
            return _failure("индексы не готовы до истечения timeout")
        report = measure_registry(
            registry,
            iterations=args.iterations,
            allocation_runs=args.allocation_runs,
        )
        report["startup_problems_total"] = len(startup_problems)
    except MeasurementError as error:
        return _failure(str(error))
    except Exception:
        return _failure("замер не завершён")

    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
