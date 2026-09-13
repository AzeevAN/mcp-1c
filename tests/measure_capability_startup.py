"""Воспроизводимый синтетический замер startup off/on capability-модуля."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import statistics
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
CHILD = """
import json, os, resource, sys, tempfile, time
with tempfile.TemporaryDirectory() as directory:
    mode = os.environ["MCP1C_CAPABILITY_BENCH_MODE"]
    with open(os.path.join(directory, "server-settings.json"), "w") as stream:
        json.dump({
            "version": 1,
            "capabilities": {"enabled": [] if mode == "off" else [mode]},
        }, stream)
    started = time.perf_counter()
    from mcp1c.capabilities import CapabilityRuntime, resolve_capability_settings
    from mcp1c.reference_provider import ReferenceService
    from mcp1c.registry import Registry
    from mcp1c.server import build_server
    registry = Registry(directory)
    registry.startup()
    reference = ReferenceService.discover(directory, database_path="off")
    store, names = resolve_capability_settings(directory, environment="unknown")
    build_server(
        registry,
        reference=reference,
        enabled_capabilities=names,
        capability_runtime=CapabilityRuntime(store, active=names),
    )
rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
if sys.platform != "darwin":
    rss *= 1024
print(json.dumps({
    "ms": (time.perf_counter() - started) * 1000,
    "rss_mib": rss / 1024 / 1024,
}))
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=10)
    args = parser.parse_args(argv)
    if args.runs < 1:
        parser.error("--runs должен быть положительным")

    modes = ("off", "forms")
    measured = {mode: [] for mode in modes}
    for run in range(args.runs):
        # Чередование первого режима уменьшает систематический эффект прогрева
        # файлового кэша между двумя независимыми процессами.
        order = modes if run % 2 == 0 else tuple(reversed(modes))
        for mode in order:
            environment = {
                **os.environ,
                "MCP1C_CAPABILITY_BENCH_MODE": mode,
                "PYTHONPATH": str(ROOT / "src"),
            }
            output = subprocess.check_output(
                [sys.executable, "-c", CHILD],
                cwd=ROOT,
                env=environment,
                text=True,
            )
            measured[mode].append(json.loads(output))

    for mode in modes:
        rows = measured[mode]
        print(json.dumps({
            "mode": mode,
            "runs": len(rows),
            "median_ms": round(statistics.median(row["ms"] for row in rows), 3),
            "min_ms": round(min(row["ms"] for row in rows), 3),
            "max_ms": round(max(row["ms"] for row in rows), 3),
            "median_rss_mib": round(
                statistics.median(row["rss_mib"] for row in rows), 3
            ),
            "min_rss_mib": round(min(row["rss_mib"] for row in rows), 3),
            "max_rss_mib": round(max(row["rss_mib"] for row in rows), 3),
        }, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
