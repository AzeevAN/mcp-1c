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
            rules_schema = tools["get_managed_form_rules"].input_schema
            if "query" not in rules_schema.get("properties", {}):
                raise RuntimeError("Двуязычный поиск терминов не опубликован.")

            specification = json.loads(FIXTURE.read_text(encoding="utf-8"))
            specification["elements"].append(
                {
                    "kind": "label_decoration",
                    "name": "Пояснение",
                    "title": {"ru": "Проверьте параметры"},
                    "hyperlink": True,
                }
            )
            specification["elements"].append(
                {
                    "kind": "label_field",
                    "name": "Итог",
                    "data_path": "ПервоеЗначение",
                    "hyperlink": True,
                    "read_only": True,
                }
            )
            specification["elements"].append(
                {
                    "kind": "radio_button_field",
                    "name": "Режим",
                    "data_path": "ВтороеЗначение",
                    "choice_list": [
                        {"value": "A", "presentation": {"ru": "Первый"}},
                        {"value": "B", "presentation": {"ru": "Второй"}},
                    ],
                }
            )
            specification["elements"].append(
                {
                    "kind": "command_bar",
                    "name": "Действия",
                    "horizontal_location": "right",
                    "command_source": {"kind": "form"},
                    "children": [
                        {
                            "kind": "button",
                            "name": "ПроверитьНаПанели",
                            "command": "Проверить",
                        },
                        {
                            "kind": "popup",
                            "name": "Дополнительно",
                            "title": {"ru": "Дополнительно"},
                            "children": [
                                {
                                    "kind": "button",
                                    "name": "ПроверитьДополнительно",
                                    "command": "Проверить",
                                },
                                {
                                    "kind": "button_group",
                                    "name": "ГруппаДополнительныхДействий",
                                    "representation": "compact",
                                    "command_source": {
                                        "kind": "form_global_commands"
                                    },
                                    "children": [
                                        {
                                            "kind": "button",
                                            "name": "ПроверитьВГруппе",
                                            "command": "Проверить",
                                        }
                                    ],
                                }
                            ],
                        }
                    ],
                }
            )
            specification["attributes"].append(
                {
                    "name": "Строки",
                    "type": {
                        "kind": "value_table",
                        "columns": [
                            {
                                "name": "Значение",
                                "type": {"kind": "string", "length": 100},
                            }
                        ],
                    },
                }
            )
            specification["elements"].append(
                {
                    "kind": "table",
                    "name": "СтрокиПоле",
                    "data_path": "Строки",
                    "columns": [
                        {
                            "kind": "input_field",
                            "name": "СтрокиЗначение",
                            "data_path": "Строки.Значение",
                        }
                    ],
                    "auto_command_bar": {
                        "kind": "auto_command_bar",
                        "autofill": False,
                        "children": [
                            {
                                "kind": "button",
                                "name": "ДобавитьСтроку",
                                "command": "Add",
                                "command_kind": "item_standard",
                                "command_owner": "СтрокиПоле",
                            }
                        ],
                    },
                    "context_menu": {
                        "kind": "context_menu",
                        "autofill": False,
                        "children": [
                            {
                                "kind": "button",
                                "name": "УдалитьСтрокуИзМеню",
                                "command": "Delete",
                                "command_kind": "item_standard",
                                "command_owner": "СтрокиПоле",
                            }
                        ],
                    },
                }
            )
            specification["events"].append(
                {
                    "owner": "Пояснение",
                    "event": "URLProcessing",
                    "handler": "ПояснениеОбработкаСсылки",
                }
            )
            specification["events"].append(
                {
                    "owner": "Итог",
                    "event": "Click",
                    "handler": "ИтогНажатие",
                }
            )
            specification["events"].append(
                {
                    "owner": "Режим",
                    "event": "OnChange",
                    "handler": "РежимИзменён",
                }
            )
            rules = await session.call_tool(
                "get_managed_form_rules", {"topic": "overview"}
            )
            terminology_results = []
            for query in ("панель команд", "command bar", "CommandBar"):
                result = await session.call_tool(
                    "get_managed_form_rules",
                    {"topic": "terminology", "query": query},
                )
                terminology_results.append(json.loads(result.content[0].text))
            command_source_terms = []
            for query in (
                "источник команд",
                "ИсточникКоманд",
                "command source",
                "CommandSource",
            ):
                result = await session.call_tool(
                    "get_managed_form_rules",
                    {"topic": "terminology", "query": query},
                )
                command_source_terms.append(json.loads(result.content[0].text))
            auto_command_bar_terms = []
            for query in (
                "автоматическая панель команд",
                "automatic command bar",
                "AutoCommandBar",
                "auto_command_bar",
            ):
                result = await session.call_tool(
                    "get_managed_form_rules",
                    {"topic": "terminology", "query": query},
                )
                auto_command_bar_terms.append(
                    json.loads(result.content[0].text)
                )
            context_menu_terms = []
            for query in (
                "контекстное меню",
                "context menu",
                "ContextMenu",
                "context_menu",
            ):
                result = await session.call_tool(
                    "get_managed_form_rules",
                    {"topic": "terminology", "query": query},
                )
                context_menu_terms.append(json.loads(result.content[0].text))
            compiled = await session.call_tool(
                "compile_managed_form", {"specification": specification}
            )
            if compiled.is_error:
                raise RuntimeError(compiled.content[0].text)
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
            canonical_matches = [
                [match["canonical"] for match in result["matches"]]
                for result in terminology_results
            ]
            if canonical_matches != [["command_bar"]] * 3:
                raise RuntimeError("Русский и английский поиск терминов расходятся.")
            command_source_matches = [
                [match["canonical"] for match in result["matches"]]
                for result in command_source_terms
            ]
            if command_source_matches != [["command_source"]] * 4:
                raise RuntimeError("Источник команд не найден двуязычным поиском.")
            auto_command_bar_matches = [
                [match["canonical"] for match in result["matches"]]
                for result in auto_command_bar_terms
            ]
            if auto_command_bar_matches != [["auto_command_bar"]] * 4:
                raise RuntimeError(
                    "Автоматическая панель не найдена двуязычным поиском."
                )
            context_menu_matches = [
                [match["canonical"] for match in result["matches"]]
                for result in context_menu_terms
            ]
            if context_menu_matches != [["context_menu"]] * 4:
                raise RuntimeError(
                    "Контекстное меню не найдено двуязычным поиском."
                )
            if (
                "<Autofill>false</Autofill>" not in form_xml
                or "Form.Item.СтрокиПоле.StandardCommand.Add" not in form_xml
                or "Form.Item.СтрокиПоле.StandardCommand.Delete" not in form_xml
            ):
                raise RuntimeError(
                    "Командные контейнеры таблицы скомпилированы неверно."
                )
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
                "bilingual_term_search": "command_bar",
                "command_source": "form_and_form_global_commands",
                "auto_command_bar": "table_autofill_false_with_button",
                "context_menu": "table_autofill_false_with_button",
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
