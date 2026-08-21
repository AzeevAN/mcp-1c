"""Обезличенный замер живой приёмки провайдера кода.

Печатает один JSON без имён источников, объектов и физических путей:
представления локаторов, покрытие форм, категории ограничений, время старта
и пиковую память процесса. Запуск из корня проекта:

    PYTHONPATH=src .venv/bin/python tools/lab/measure_modules_acceptance.py \
      --data data
"""

from __future__ import annotations

import argparse
import json
import logging
import resource
import sys
import time
from collections import Counter
from pathlib import Path

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


def _source_row(number: int, loaded) -> dict:
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
        "problem_categories": dict(sorted(problems.items())),
    }


def _failure(reason: str) -> int:
    print(
        json.dumps(
            {
                "schema": "modules-acceptance-v1",
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
        rows = [
            _source_row(number, loaded)
            for number, (_, loaded) in enumerate(
                sorted(registry.modules.items()), start=1
            )
        ]
        if not rows:
            return _failure("кодовые источники не найдены")
    except Exception:
        return _failure("замер не завершён")

    payload = {
        "schema": "modules-acceptance-v1",
        "completed": True,
        "sources_total": len(rows),
        "startup_problems_total": len(startup_problems),
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "peak_rss_mib": _peak_rss_mib(),
        "sources": rows,
    }
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
