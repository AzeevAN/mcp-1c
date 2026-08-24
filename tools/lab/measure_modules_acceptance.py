"""Обезличенный замер живой приёмки провайдера кода.

Печатает один JSON без имён источников, объектов и физических путей:
представления локаторов, покрытие форм, категории ограничений, время старта
и пиковую память процесса, разрешение вызовов с привязками метаданных и
стоимость чтения записи ``module`` из плоского контейнера формы. Запуск из
корня проекта:

    PYTHONPATH=src .venv/bin/python tools/lab/measure_modules_acceptance.py \
      --data data
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import resource
import sys
import time
from collections import Counter
from pathlib import Path

from mcp1c.module_content import ContentReadError, read_bsl
from mcp1c.registry import Registry


def _peak_rss_mib() -> float:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # macOS возвращает байты, Linux — КиБ.
    divisor = 1024 * 1024 if sys.platform == "darwin" else 1024
    return round(value / divisor, 1)


def _locator_kind(locator) -> str:
    path = locator.relative_path.casefold()
    if locator.kind == "compiled":
        return "compiled"
    if locator.kind == "container":
        if path.endswith("/form.bin"):
            return "tree_form_container"
        if path.endswith(".form"):
            return "flat_form_container"
        return "other_container"
    if path.endswith(".txt"):
        return "flat_text"
    if path.endswith(".bsl"):
        return "tree_bsl"
    return "other_file"


def _percent(part: int, total: int) -> float:
    return round(part / total * 100, 2) if total else 0.0


def _call_metrics(loaded, graph=None) -> dict:
    calls = loaded.вызовы
    toc = loaded.оглавление
    if calls is None or toc is None:
        raise RuntimeError("индекс вызовов не готов")

    declared = {
        (item.модуль.casefold(), item.имя.casefold()) for item in toc.все()
    }
    declared_modules = {module.casefold() for module in toc.модули}
    compiled_modules = {
        module.casefold() for module in toc.модули if toc.скомпилирован(module)
    }
    metadata_edges = (
        [edge for edge in graph.edges if edge.kind in ("handler", "method")]
        if graph is not None
        else []
    )
    metadata_outcomes: Counter[str] = Counter()
    for edge in metadata_edges:
        target = edge.target.casefold()
        if (target, edge.via.casefold()) in declared:
            metadata_outcomes["resolved"] += 1
        elif target in compiled_modules:
            metadata_outcomes["compiled_without_source"] += 1
        elif target in declared_modules:
            metadata_outcomes["procedure_missing"] += 1
        else:
            metadata_outcomes["module_missing"] += 1
    resolved_metadata = metadata_outcomes["resolved"]
    combined_total = calls.рёбер + len(metadata_edges)
    combined_resolved = calls.разрешённых + resolved_metadata
    return {
        "bsl": {
            "total": calls.рёбер,
            "resolved": calls.разрешённых,
            "resolved_percent": _percent(calls.разрешённых, calls.рёбер),
        },
        "metadata": {
            "total": len(metadata_edges),
            "resolved": resolved_metadata,
            "resolved_percent": _percent(resolved_metadata, len(metadata_edges)),
            "unresolved_categories": {
                key: value
                for key, value in sorted(metadata_outcomes.items())
                if key != "resolved"
            },
        },
        "combined": {
            "total": combined_total,
            "resolved": combined_resolved,
            "resolved_percent": _percent(combined_resolved, combined_total),
        },
    }


def _latency_summary(values_ns: list[int]) -> dict[str, float]:
    if not values_ns:
        return {"mean_ms": 0.0, "p50_ms": 0.0, "p95_ms": 0.0,
                "p99_ms": 0.0, "max_ms": 0.0}
    ordered = sorted(values_ns)

    def percentile(fraction: float) -> float:
        index = max(0, math.ceil(len(ordered) * fraction) - 1)
        return round(ordered[index] / 1_000_000, 3)

    return {
        "mean_ms": round(sum(ordered) / len(ordered) / 1_000_000, 3),
        "p50_ms": percentile(0.50),
        "p95_ms": percentile(0.95),
        "p99_ms": percentile(0.99),
        "max_ms": round(ordered[-1] / 1_000_000, 3),
    }


def _measure_flat_form_reads(loaded_sources: list, repeat_passes: int = 3) -> dict:
    targets = []
    for loaded in loaded_sources:
        if loaded.каталог is None:
            continue
        targets.extend(
            (loaded, entry)
            for entry in loaded.каталог.entries.values()
            if entry.locator is not None
            and entry.locator.kind == "container"
            and entry.locator.relative_path.casefold().endswith(".form")
            and entry.locator.entry.casefold() == "module"
        )

    first_pass: list[int] = []
    readable = []
    failed = 0
    for loaded, entry in targets:
        started = time.perf_counter_ns()
        try:
            read_bsl(loaded.корень, entry.address, entry.locator)
        except ContentReadError:
            failed += 1
            continue
        first_pass.append(time.perf_counter_ns() - started)
        readable.append((loaded, entry))

    repeated: list[int] = []
    for _ in range(repeat_passes):
        for loaded, entry in readable:
            started = time.perf_counter_ns()
            read_bsl(loaded.корень, entry.address, entry.locator)
            repeated.append(time.perf_counter_ns() - started)

    return {
        "readable_containers": len(readable),
        "failed_containers": failed,
        "first_measured_pass": _latency_summary(first_pass),
        "repeat_passes": repeat_passes,
        "repeated_reads": len(repeated),
        "repeated": _latency_summary(repeated),
    }


def _aggregate_call_metrics(rows: list[dict]) -> dict:
    result = {}
    for layer in ("bsl", "metadata", "combined"):
        total = sum(row["call_resolution"][layer]["total"] for row in rows)
        resolved = sum(
            row["call_resolution"][layer]["resolved"] for row in rows
        )
        result[layer] = {
            "total": total,
            "resolved": resolved,
            "resolved_percent": _percent(resolved, total),
        }
        if layer == "metadata":
            categories: Counter[str] = Counter()
            for row in rows:
                categories.update(
                    row["call_resolution"][layer]["unresolved_categories"]
                )
            result[layer]["unresolved_categories"] = dict(sorted(categories.items()))
    return result


def _source_row(number: int, loaded, graph=None) -> dict:
    catalog = loaded.каталог
    forms = loaded.формы
    if catalog is None or forms is None or not loaded.готов:
        raise RuntimeError("индекс кода не готов")

    locators: Counter[str] = Counter()
    evidence: Counter[str] = Counter()
    for entry in catalog.entries.values():
        if entry.locator is not None:
            locators[_locator_kind(entry.locator)] += 1
        elif entry.compiled:
            locators["compiled"] += 1
        else:
            locators["unavailable"] += 1
        evidence.update(source.kind for source in entry.form_sources)

    problems = Counter(dict(catalog.problem_counts))
    problems.update(dict(forms.problem_counts))
    if catalog.coverage.compiled:
        problems["compiled_without_source"] += catalog.coverage.compiled

    return {
        "source_number": number,
        "source_kind": loaded.source.kind,
        "items_total": loaded.source.items_total,
        "selection_version": loaded.source.selection_version,
        "locator_kinds": dict(sorted(locators.items())),
        "form_evidence": dict(sorted(evidence.items())),
        "catalog_coverage": catalog.coverage.as_dict(),
        "forms": {
            "total": len(forms.модули),
            "full": forms.полных,
            "partial": forms.частичных,
            "unread": forms.непрочитанных,
            "broken": forms.битых,
            "unknown_markers": forms.неизвестных_маркеров,
            "known_partial": forms.известных_неполных,
            "budget_exceeded": forms.превышений_бюджета,
        },
        "call_resolution": _call_metrics(loaded, graph),
        "problem_categories": dict(sorted(problems.items())),
    }


def _failure(reason: str) -> int:
    print(
        json.dumps(
            {
                "schema": "modules-acceptance-v2",
                "completed": False,
                "error": reason,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", default="data", help="каталог данных сервера")
    parser.add_argument("--timeout", type=float, default=120.0)
    args = parser.parse_args(argv)

    # Реестр журналирует только агрегаты, но lab-контракт ещё строже: stdout
    # содержит ровно один JSON, stderr пуст, чтобы результат можно было
    # приложить к публичному замеру без последующей ручной очистки.
    logging.disable(logging.CRITICAL)
    started = time.monotonic()
    try:
        registry = Registry(Path(args.data))
        startup_problems = registry.startup()
        if not registry.wait_for_module_builds(timeout=args.timeout):
            return _failure("индексы не готовы до истечения timeout")
        loaded_sources = [loaded for _, loaded in sorted(registry.modules.items())]
        rows = []
        for number, loaded in enumerate(loaded_sources, start=1):
            graph = None
            suffix = ":modules"
            if loaded.source.id.endswith(suffix):
                configuration = registry.configurations.get(
                    loaded.source.id[:-len(suffix)]
                )
                if configuration is not None:
                    graph = configuration.graph
            rows.append(_source_row(number, loaded, graph))
        if not rows:
            return _failure("кодовые источники не найдены")
        flat_form_read = _measure_flat_form_reads(loaded_sources)
    except Exception:
        return _failure("замер не завершён")

    payload = {
        "schema": "modules-acceptance-v2",
        "completed": True,
        "sources_total": len(rows),
        "startup_problems_total": len(startup_problems),
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "peak_rss_mib": _peak_rss_mib(),
        "call_resolution": _aggregate_call_metrics(rows),
        "flat_form_module_read": flat_form_read,
        "sources": rows,
    }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
