"""Задержка инструментов по структуре и коду на реальном каталоге данных.

Замер повторяет вызовы так, как их делает агент: прогрев, затем медиана из
`--repeat` повторов. Первый вызов на поколении оплачивает ленивые агрегаты и
в медиану не входит — его цена печатается отдельно строкой `first`.

`Registry.restore()` пишет кэш индексов в каталог данных, поэтому мерить
лучше на копии (на APFS копия-клон почти бесплатна):

    cp -cR data /tmp/mcp1c-measure
    PYTHONPATH=src .venv/bin/python tools/measure_tool_latency.py \\
        --data /tmp/mcp1c-measure --config ИмяКонфигурации
"""

from __future__ import annotations

import argparse
import gc
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from mcp1c import tools  # noqa: E402
from mcp1c.registry import Registry  # noqa: E402


def _measure(fn, repeat: int) -> tuple[float, float]:
    started = time.perf_counter()
    fn()
    first = (time.perf_counter() - started) * 1000
    samples = []
    for _ in range(repeat):
        started = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - started) * 1000)
    return first, statistics.median(samples)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data", required=True, help="каталог Registry (копия)")
    parser.add_argument("--config", required=True, help="конфигурация с кодом")
    parser.add_argument("--object", default="", help="объект для get_object/get_related")
    parser.add_argument("--repeat", type=int, default=20)
    args = parser.parse_args(argv)

    registry = Registry(Path(args.data))
    registry.restore()
    # Полная сборка мусора после подъёма тысяч объектов иначе случайно
    # попадает в первый замеряемый вызов и выдаёт себя за его цену.
    gc.collect()
    context = registry.resolve(args.config)
    loaded = context.modules
    if loaded is None or loaded.оглавление is None:
        print(f"У конфигурации {args.config} нет загруженного кода.", file=sys.stderr)
        return 2
    obj = args.object or min(context.configuration.config.objects)
    toc = loaded.оглавление
    # Самый длинный по числу процедур модуль — худший случай оглавления.
    module = max(toc.модули, key=lambda name: (len(toc.модуля(name)), name))
    records = toc.модуля(module)
    procedure = records[len(records) // 2].имя if records else ""
    typo = module[:-3] + "ьл"

    calls = {
        "get_object fields": lambda: tools.get_object(registry, obj, args.config, "fields"),
        "get_object full": lambda: tools.get_object(registry, obj, args.config, "full"),
        "list_configurations": lambda: tools.list_configurations(registry),
        "get_related": lambda: tools.get_related(registry, obj, args.config),
        "get_procedure (оглавление модуля)": lambda: tools.get_procedure(registry, module, args.config),
        "get_procedure (процедура)": lambda: tools.get_procedure(
            registry, f"{module}::{procedure}", args.config
        ),
        "get_procedure (опечатка в модуле)": lambda: tools.get_procedure(registry, typo, args.config),
        "get_callers": lambda: tools.get_callers(registry, f"{module}::{procedure}", args.config),
        "search_procedures": lambda: tools.search_procedures(
            registry, "заполнить реквизиты", args.config
        ),
    }
    print(
        f"{args.config}: модулей {len(toc.модули)}, объект {obj}, "
        f"модуль {module} ({len(records)} процедур)"
    )
    for label, fn in calls.items():
        first, median = _measure(fn, args.repeat)
        print(f"{label:36s} first {first:8.2f} ms  median {median:8.2f} ms")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
