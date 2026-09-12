"""Воспроизводимый замер десяти параллельных операций первой Forms-вертикали."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import statistics
import time

from mcp1c.capability_modules.forms import tools
from mcp1c.capability_modules.forms.rules import RULE_TOPICS


FIXTURE = Path(__file__).parent / "capability_modules/forms/fixtures/minimal_form.json"


async def _run_once() -> dict[str, float | int]:
    specification = json.loads(FIXTURE.read_text(encoding="utf-8"))
    request_bytes = len(
        json.dumps(
            specification,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )
    started = time.perf_counter()
    responses = await asyncio.gather(
        *(tools._compile_tool(specification) for _ in range(10))
    )
    batch_ms = (time.perf_counter() - started) * 1000
    compiled = json.loads(responses[0])
    artifacts = {item["path"]: item["content"] for item in compiled["artifacts"]}
    form_xml = artifacts["Forms/ФормаПараметров/Ext/Form.xml"]
    module_bsl = artifacts["Forms/ФормаПараметров/Ext/Form/Module.bsl"]
    checked = await tools._check_tool(form_xml, "ФормаПараметров", module_bsl)
    decompiled = await tools._decompile_tool(form_xml, "ФормаПараметров", module_bsl)
    rules_bytes = max(
        len(tools._rules_tool(topic).encode("utf-8")) for topic in RULE_TOPICS
    )
    return {
        "batch_ms": batch_ms,
        "request_bytes": request_bytes,
        "compile_response_bytes": len(responses[0].encode("utf-8")),
        "check_response_bytes": len(checked.encode("utf-8")),
        "decompile_response_bytes": len(decompiled.encode("utf-8")),
        "max_rules_response_bytes": rules_bytes,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=10)
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("--runs должен быть положительным")
    rows = [asyncio.run(_run_once()) for _ in range(args.runs)]
    stable = {key: rows[0][key] for key in rows[0] if key != "batch_ms"}
    print(
        json.dumps(
            {
                "runs": args.runs,
                "clients_per_run": 10,
                "workers": tools.MAX_CONCURRENT_OPERATIONS,
                "pending_limit": tools.MAX_PENDING_OPERATIONS,
                "timeout_seconds": tools._GATE.timeout_seconds,
                "median_batch_ms": round(
                    statistics.median(float(row["batch_ms"]) for row in rows),
                    3,
                ),
                "min_batch_ms": round(min(float(row["batch_ms"]) for row in rows), 3),
                "max_batch_ms": round(max(float(row["batch_ms"]) for row in rows), 3),
                **stable,
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
