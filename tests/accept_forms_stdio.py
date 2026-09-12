"""Внешняя stdio-приёмка Forms через официальный MCP ClientSession."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/capability_modules/forms/fixtures/minimal_form.json"
FORM_TOOLS = (
    "get_managed_form_rules",
    "compile_managed_form",
    "decompile_managed_form",
    "check_managed_form",
)


async def _session(mode: str, data_dir: Path) -> dict[str, object]:
    server = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "mcp1c.server",
            "--data",
            str(data_dir),
            "--transport",
            "stdio",
        ],
        env={
            **os.environ,
            "PYTHONPATH": os.environ.get(
                "MCP1C_ACCEPT_PYTHONPATH", str(ROOT / "src")
            ),
            "MCP1C_CAPABILITIES": mode,
        },
    )
    async with stdio_client(server) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            listed = await session.list_tools()
            tools = {tool.name: tool for tool in listed.tools}
            present = tuple(name for name in tools if name in FORM_TOOLS)
            if mode == "off":
                if present:
                    raise RuntimeError("Forms-инструменты видны при off.")
                return {"mode": mode, "forms_tools": 0}

            if present != FORM_TOOLS:
                raise RuntimeError(f"Неверный порядок Forms tools: {present!r}")
            compile_schema = tools["compile_managed_form"].input_schema
            schema_text = json.dumps(compile_schema, ensure_ascii=False)
            if "ManagedFormSpec" not in schema_text or "additionalProperties" not in schema_text:
                raise RuntimeError("Строгая вложенная specification-схема не опубликована.")

            specification = json.loads(FIXTURE.read_text(encoding="utf-8"))
            rules = await session.call_tool(
                "get_managed_form_rules", {"topic": "overview"}
            )
            compiled = await session.call_tool(
                "compile_managed_form", {"specification": specification}
            )
            compiled_payload = json.loads(compiled.content[0].text)
            artifacts = {
                item["path"]: item["content"]
                for item in compiled_payload["artifacts"]
            }
            form_xml = artifacts["Forms/ФормаПараметров/Ext/Form.xml"]
            module_bsl = artifacts[
                "Forms/ФормаПараметров/Ext/Form/Module.bsl"
            ]
            checked = await session.call_tool(
                "check_managed_form",
                {
                    "form_xml": form_xml,
                    "form_name": "ФормаПараметров",
                    "module_bsl": module_bsl,
                },
            )
            decompiled = await session.call_tool(
                "decompile_managed_form",
                {
                    "form_xml": form_xml,
                    "form_name": "ФормаПараметров",
                    "module_bsl": module_bsl,
                },
            )
            invalid = dict(specification)
            invalid["unknown"] = True
            rejected = await session.call_tool(
                "compile_managed_form", {"specification": invalid}
            )
            results = (rules, compiled, checked, decompiled)
            if any(result.is_error for result in results) or not rejected.is_error:
                raise RuntimeError("Внешняя MCP-последовательность дала неверный статус.")
            return {
                "mode": mode,
                "forms_tools": len(present),
                "sequence": "rules_compile_check_decompile",
                "compiled_status": compiled_payload["status"],
                "checked_bsl": json.loads(checked.content[0].text)["coverage"][
                    "bsl_static"
                ],
                "roundtrip": (
                    json.loads(decompiled.content[0].text)["specification"]
                    == compiled_payload["specification"]
                ),
                "unknown_field_rejected": True,
            }


async def _main() -> None:
    with TemporaryDirectory(prefix="mcp1c-forms-stdio-") as temporary:
        root = Path(temporary)
        for mode in ("off", "forms"):
            print(
                json.dumps(
                    await _session(mode, root / mode),
                    ensure_ascii=False,
                )
            )


if __name__ == "__main__":
    asyncio.run(_main())
