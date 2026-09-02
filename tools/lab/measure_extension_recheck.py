"""Синтетический замер дешёвой перепроверки borrowed extension edges.

Запуск из корня проекта:

    .venv/bin/python tools/lab/measure_extension_recheck.py \
      --base-identities 57892 --edges-per-extension 458 \
      --extensions 0,1,3 --repeat 20

Каждый сценарий выполняется в отдельном процессе, поэтому ``ru_maxrss`` не
наследует пик предыдущего числа расширений. Исходные ZIP не создаются и не
читаются: замер использует только синтетический generation snapshot в памяти.
"""

from __future__ import annotations

import argparse
import json
import resource
import statistics
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from mcp1c.intake_v2_extensions import (  # noqa: E402
    ExtensionStructure,
    resolve_extension_relation_map,
)
from mcp1c.model import Configuration, Field, MetadataObject  # noqa: E402


SCHEMA = "extension-recheck-v1"


def _peak_rss_mib() -> float:
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    divisor = 1024 * 1024 if sys.platform == "darwin" else 1024
    return float(value) / divisor


def _fixture(
    base_identities: int,
    edges_per_extension: int,
    extension_count: int,
) -> tuple[Configuration, tuple[ExtensionStructure, ...]]:
    if base_identities < 1:
        raise ValueError("base-identities должен быть больше нуля")
    if edges_per_extension < 1 and extension_count:
        raise ValueError("для расширения нужен хотя бы один borrowed edge")
    field_count = base_identities - 1
    if edges_per_extension - 1 > field_count:
        raise ValueError("borrowed edges не помещаются в base identities")
    target = MetadataObject(
        full_name="Справочник.Items",
        kind="Справочник",
        name="Items",
        attributes=[Field(f"Field{index:06d}") for index in range(field_count)],
    )
    base = Configuration(
        name="SyntheticConfiguration",
        source_format="source-b",
        objects={target.full_name: target},
    )
    extensions = tuple(
        ExtensionStructure(
            name=f"Extension{ordinal:03d}",
            parent_configuration=base.name,
            own_objects={},
            borrowed_overlays={target.full_name: target},
            borrowed_field_targets=tuple(
                f"{target.full_name}.Field{index:06d}"
                for index in range(max(0, edges_per_extension - 1))
            ),
        )
        for ordinal in range(extension_count)
    )
    return base, extensions


def _child(
    base_identities: int,
    edges_per_extension: int,
    extension_count: int,
    repeat: int,
) -> dict[str, object]:
    base, extensions = _fixture(
        base_identities,
        edges_per_extension,
        extension_count,
    )
    peak_before = _peak_rss_mib()
    peak_after_one = peak_before
    samples: list[float] = []
    for iteration in range(repeat):
        started = time.perf_counter_ns()
        relation_map = resolve_extension_relation_map(
            base,
            {extension.name: extension for extension in extensions},
        )
        relations = tuple(
            relation
            for extension_relations in relation_map.values()
            for relation in extension_relations
        )
        samples.append((time.perf_counter_ns() - started) / 1_000_000)
        expected = extension_count * edges_per_extension
        if len(relations) != expected:
            raise RuntimeError("resolver вернул неверное число borrowed edges")
        if iteration == 0:
            # Peak одного реального recheck, а не накопление allocator arena
            # от двадцати последовательных повторов latency-бенчмарка.
            peak_after_one = _peak_rss_mib()
    return {
        "extensions": extension_count,
        "base_identities": base_identities,
        "borrowed_edges": extension_count * edges_per_extension,
        "repeat": repeat,
        "latency_ms": {
            "median": round(statistics.median(samples), 6),
            "maximum": round(max(samples), 6),
        },
        "peak_rss_mib": round(peak_after_one, 3),
        "peak_rss_delta_mib": round(
            max(0.0, peak_after_one - peak_before),
            3,
        ),
    }


def _extension_counts(value: str) -> tuple[int, ...]:
    try:
        result = tuple(int(item) for item in value.split(","))
    except ValueError as error:
        raise argparse.ArgumentTypeError("extensions должен быть списком чисел") from error
    if not result or any(item < 0 for item in result) or len(set(result)) != len(result):
        raise argparse.ArgumentTypeError(
            "extensions должен быть непустым списком уникальных неотрицательных чисел"
        )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Замер extension borrowed-edge recheck без исходного ZIP",
    )
    parser.add_argument("--base-identities", type=int, default=57_892)
    parser.add_argument("--edges-per-extension", type=int, default=458)
    parser.add_argument("--extensions", type=_extension_counts, default=(0, 1, 3))
    parser.add_argument("--repeat", type=int, default=20)
    parser.add_argument("--child", type=int, default=-1, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.repeat < 1:
        parser.error("repeat должен быть больше нуля")
    if args.child >= 0:
        print(
            json.dumps(
                _child(
                    args.base_identities,
                    args.edges_per_extension,
                    args.child,
                    args.repeat,
                ),
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 0

    scenarios: list[dict[str, object]] = []
    for count in args.extensions:
        command = [
            sys.executable,
            str(Path(__file__).resolve()),
            "--base-identities",
            str(args.base_identities),
            "--edges-per-extension",
            str(args.edges_per_extension),
            "--repeat",
            str(args.repeat),
            "--child",
            str(count),
        ]
        completed = subprocess.run(
            command,
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=True,
        )
        scenarios.append(json.loads(completed.stdout))
    print(
        json.dumps(
            {
                "schema": SCHEMA,
                "method": "saved generation snapshot; source ZIP не читается",
                "scenarios": scenarios,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
