"""Спайк типовых намерений для поиска процедур.

Скрипт ничего не меняет в рабочем поиске. Он сравнивает нынешний top-10 с
компактным каталогом стандартных обработчиков и отдельно показывает цену
наивного варианта, где те же подсказки размножаются по всем реализациям.

Вывод обезличен: только агрегаты, канонические имена платформенных событий и
размеры. Имена источников, объектов, модулей и физические пути не печатаются.

Запуск из корня проекта::

    PYTHONPATH=src .venv/bin/python \
      tools/lab/measure_standard_procedure_intents.py --data data
"""

from __future__ import annotations

import argparse
import gc
import json
import logging
import math
import pickle
import time
import tracemalloc
from pathlib import Path

from mcp1c.registry import Registry
from mcp1c.search import Doc, SearchIndex, normalize
from mcp1c.standard_procedure_intents import (
    STANDARD_PROCEDURE_INTENTS as INTENTS,
    StandardProcedureIntent as Intent,
    recognize_standard_procedure_intent as recognize,
)


def resolve(toc, name: str, scope_modules: frozenset[str] | None = None):
    """Разрешить имя через полный TOC, включая неэкспортные процедуры."""
    found = toc.по_имени(name)
    if scope_modules is None:
        return found
    allowed = {module.casefold() for module in scope_modules}
    return [item for item in found if item.модуль.casefold() in allowed]


def _object_scope_modules(toc, module: str) -> frozenset[str]:
    """Модули объекта так же, как object-ветка production ``scope``.

    Первые две части — вид и имя объекта. Для общих модулей и общих форм такой
    адрес уже является точным модулем; для документа/справочника префикс
    включает модуль объекта, менеджера и формы.
    """
    parts = module.split(".")
    if len(parts) < 2:
        return frozenset({module})
    scope = ".".join(parts[:2])
    exact = [item for item in toc.модули if item.casefold() == scope.casefold()]
    if exact:
        return frozenset(exact)
    prefix = scope.casefold() + "."
    return frozenset(
        item for item in toc.модули if item.casefold().startswith(prefix)
    )


def load_suite(path: Path) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1:
        raise ValueError("набор намерений имеет неизвестную schema_version")
    if payload.get("domain") != "standard-procedure-intents":
        raise ValueError("набор намерений имеет неверный domain")
    cases = payload.get("cases")
    names = {intent.name for intent in INTENTS}
    if not isinstance(cases, list) or not cases:
        raise ValueError("набор намерений не содержит запросов")
    seen: set[str] = set()
    for case in cases:
        if not isinstance(case, dict) or not isinstance(case.get("query"), str):
            raise ValueError("запрос набора намерений некорректен")
        key = normalize(case["query"])
        if key in seen:
            raise ValueError("запрос набора намерений повторяется")
        seen.add(key)
        if case.get("expected") not in names | {None}:
            raise ValueError("ожидаемое намерение отсутствует в каталоге")
        if case.get("origin") not in {
            "manual", "synthetic", "control", "holdout", "holdout-control"
        }:
            raise ValueError("происхождение запроса намерений некорректно")
    return cases


def _percent(part: int, total: int) -> float:
    return round(part / total * 100, 2) if total else 0.0


def _percentile(values: list[int], fraction: float) -> float:
    ordered = sorted(values)
    index = max(0, math.ceil(len(ordered) * fraction) - 1)
    return round(ordered[index] / 1_000_000, 4)


def _latency(values: list[int]) -> dict[str, float]:
    return {
        "p50_ms": _percentile(values, 0.50),
        "p95_ms": _percentile(values, 0.95),
        "p99_ms": _percentile(values, 0.99),
        "max_ms": round(max(values) / 1_000_000, 4),
    }


def _catalog_text(intent: Intent) -> str:
    markers = []
    for pattern in intent.patterns:
        for group in pattern:
            markers.extend(marker.removeprefix("=") for marker in group)
    return " ".join(dict.fromkeys(markers))


def _measure_index(factory) -> dict:
    gc.collect()
    tracemalloc.start()
    started = time.perf_counter_ns()
    docs = factory()
    index = SearchIndex(
        docs,
        field_weights={"name": 6.0, "keys": 2.0},
        exact_fields=("name",),
        synonyms={},
    )
    index.search("ОбработкаПроведения", limit=1)
    build_ns = time.perf_counter_ns() - started
    state = index.export_state()
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    serialized = len(pickle.dumps(state, protocol=5))
    result = {
        "documents": len(docs),
        "build_ms": round(build_ns / 1_000_000, 3),
        "traced_current_kib": round(current / 1024, 1),
        "traced_peak_kib": round(peak / 1024, 1),
        "serialized_state_kib": round(serialized / 1024, 1),
    }
    del index, docs, state
    gc.collect()
    return result


def _ready_sources(registry: Registry):
    return [
        loaded
        for loaded in registry.modules.values()
        if loaded.готов
        and loaded.оглавление is not None
        and loaded.поиск is not None
    ]


def measure(data: Path, suite: Path, repeat: int) -> dict:
    cases = load_suite(suite)
    registry = Registry(data)
    registry.restore()
    sources = _ready_sources(registry)
    if not sources:
        raise RuntimeError("нет готового корпуса кода для живого замера")

    recognition_rows = []
    for case in cases:
        got = recognize(case["query"])
        recognition_rows.append((case, got))
    positives = [(case, got) for case, got in recognition_rows if case["expected"]]
    controls = [(case, got) for case, got in recognition_rows if case["expected"] is None]
    manual = [(case, got) for case, got in positives if case["origin"] == "manual"]
    holdout = [(case, got) for case, got in recognition_rows
               if case["origin"] in {"holdout", "holdout-control"}]

    recognition_times: list[int] = []
    for _ in range(repeat):
        for case in cases:
            started = time.perf_counter_ns()
            recognize(case["query"])
            recognition_times.append(time.perf_counter_ns() - started)

    baseline_total = 0
    baseline_top10 = 0
    baseline_top1 = 0
    scope_present = 0
    scope_exact = 0
    scope_absent = 0
    scope_absent_correct = 0
    resolution_times: list[int] = []
    object_scope_cases = 0
    object_scope_exact = 0
    object_scope_ambiguous = 0
    object_scope_max = 0
    object_resolution_times: list[int] = []
    global_counts = {intent.name: 0 for intent in INTENTS}
    for loaded in sources:
        toc = loaded.оглавление
        search = loaded.поиск
        assert toc is not None and search is not None
        for intent in INTENTS:
            found = resolve(toc, intent.name)
            global_counts[intent.name] += len(found)
            if not found:
                continue
            scope_present += 1
            module = found[0].модуль
            started = time.perf_counter_ns()
            scoped = resolve(toc, intent.name, frozenset({module}))
            resolution_times.append(time.perf_counter_ns() - started)
            if len(scoped) == 1:
                scope_exact += 1

            object_scopes = {
                _object_scope_modules(toc, item.модуль) for item in found
            }
            for modules in object_scopes:
                started = time.perf_counter_ns()
                object_scoped = resolve(toc, intent.name, modules)
                object_resolution_times.append(time.perf_counter_ns() - started)
                object_scope_cases += 1
                object_scope_max = max(object_scope_max, len(object_scoped))
                if len(object_scoped) == 1:
                    object_scope_exact += 1
                elif len(object_scoped) > 1:
                    object_scope_ambiguous += 1
            occupied = {item.модуль.casefold() for item in found}
            absent_module = next(
                (candidate for candidate in toc.модули
                 if candidate.casefold() not in occupied),
                None,
            )
            if absent_module is not None:
                scope_absent += 1
                if not resolve(toc, intent.name, frozenset({absent_module})):
                    scope_absent_correct += 1

        for case, _ in positives:
            expected = case["expected"]
            if not toc.известно(expected):
                continue
            baseline_total += 1
            hits = search.search(case["query"], limit=10)
            names = [hit.doc.payload.имя.casefold() for hit in hits]
            target = expected.casefold()
            if names and names[0] == target:
                baseline_top1 += 1
            if target in names:
                baseline_top10 += 1

    def compact_docs():
        return [
            Doc(
                id=intent.name,
                fields={"name": intent.name, "keys": _catalog_text(intent)},
            )
            for intent in INTENTS
        ]

    def expanded_docs():
        docs = []
        for source_number, loaded in enumerate(sources, 1):
            toc = loaded.оглавление
            assert toc is not None
            for intent in INTENTS:
                keys = _catalog_text(intent)
                for item in toc.по_имени(intent.name):
                    docs.append(
                        Doc(
                            id=(
                                f"{source_number}:{item.модуль}::{item.имя}"
                            ),
                            fields={"name": item.имя, "keys": keys},
                        )
                    )
        return docs

    compact_cost = _measure_index(compact_docs)
    expanded_cost = _measure_index(expanded_docs)

    global_messages = [
        (
            f"Распознано стандартное событие {name}. "
            f"Найдено {count} реализаций. Укажите объект или модуль в scope."
        )
        for name, count in global_counts.items()
    ]

    return {
        "schema_version": 1,
        "catalog": {
            "intents": len(INTENTS),
            "patterns": sum(len(intent.patterns) for intent in INTENTS),
            "marker_groups": sum(
                len(pattern)
                for intent in INTENTS
                for pattern in intent.patterns
            ),
        },
        "suite": {
            "cases": len(cases),
            "positive": len(positives),
            "controls": len(controls),
            "manual": len(manual),
            "holdout": len(holdout),
        },
        "recognition": {
            "positive_correct": sum(case["expected"] == got for case, got in positives),
            "positive_percent": _percent(
                sum(case["expected"] == got for case, got in positives),
                len(positives),
            ),
            "manual_correct": sum(case["expected"] == got for case, got in manual),
            "holdout_correct": sum(case["expected"] == got for case, got in holdout),
            "holdout_percent": _percent(
                sum(case["expected"] == got for case, got in holdout),
                len(holdout),
            ),
            "control_abstained": sum(got is None for _, got in controls),
            "control_abstained_percent": _percent(
                sum(got is None for _, got in controls), len(controls)
            ),
            "wrong_or_false": [
                {"case": index + 1, "expected": case["expected"], "got": got}
                for index, (case, got) in enumerate(recognition_rows)
                if case["expected"] != got
            ],
            "latency": _latency(recognition_times),
        },
        "live_corpus": {
            "ready_sources": len(sources),
            "procedures": sum(
                loaded.оглавление.сводка().процедур
                for loaded in sources
                if loaded.оглавление is not None
            ),
            "standard_declarations": sum(global_counts.values()),
            "max_declarations_for_one_intent": max(global_counts.values()),
            "baseline": {
                "case_source_pairs": baseline_total,
                "top1": baseline_top1,
                "top1_percent": _percent(baseline_top1, baseline_total),
                "top10": baseline_top10,
                "top10_percent": _percent(baseline_top10, baseline_total),
            },
            "exact_module_scope": {
                "cases": scope_present,
                "exact_one": scope_exact,
            },
            "object_scope": {
                "cases": object_scope_cases,
                "exact_one": object_scope_exact,
                "ambiguous": object_scope_ambiguous,
                "max_matches": object_scope_max,
                "latency": _latency(object_resolution_times),
            },
            "scope_absent": {
                "cases": scope_absent,
                "correct_empty": scope_absent_correct,
            },
            "resolution_latency": _latency(resolution_times),
            "global_answer_chars": {
                "min": min(map(len, global_messages)),
                "max": max(map(len, global_messages)),
            },
        },
        "index_cost": {
            "method": "tracemalloc и pickle state; это не RSS процесса",
            "compact_one_document_per_intent": compact_cost,
            "expanded_one_document_per_declaration": expanded_cost,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=Path("data"))
    parser.add_argument(
        "--suite",
        type=Path,
        default=Path("tests/queries/standard-procedure-intents.json"),
    )
    parser.add_argument("--repeat", type=int, default=500)
    args = parser.parse_args()
    if args.repeat < 1:
        parser.error("--repeat должен быть положительным")
    logging.basicConfig(level=logging.CRITICAL)
    report = measure(args.data, args.suite, args.repeat)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
