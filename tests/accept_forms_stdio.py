"""Внешняя stdio-приёмка Forms через официальный MCP ClientSession."""

from __future__ import annotations

import asyncio
import copy
import json
import os
from pathlib import Path
import re
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
METADATA_TOOLS = (
    "get_metadata_authoring_rules",
    "compile_metadata_object",
    "check_metadata_artifacts",
)


def _context() -> dict[str, str]:
    return {"owner": "Обработка.ТестоваяОбработка", "role": "custom"}


def _list_choice_spec(owner: str, role: str) -> dict[str, object]:
    return {
        "schema_version": 2,
        "form_name": "ФормаСписка" if role == "list" else "ФормаВыбора",
        "context": {"owner": owner, "role": role},
        "format_version": "2.20",
        "title": {"ru": "Список"},
        "attributes": [{
            "name": "Список",
            "type": {
                "kind": "dynamic_list",
                "main_table": owner,
                "dynamic_data_read": True,
            },
            "main": True,
        }],
        "elements": [{
            "kind": "table",
            "name": "Список",
            "data_path": "Список",
            "columns": [{
                "kind": "input_field",
                "name": "ОсновноеПоле",
                "data_path": "Список.ОсновноеПоле",
            }],
        }],
        "commands": [],
        "events": [],
    }


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
            metadata_present = tuple(
                name for name in tools if name in METADATA_TOOLS
            )
            if metadata_present != METADATA_TOOLS:
                raise RuntimeError(
                    f"Неверный порядок Metadata Authoring tools: {metadata_present!r}"
                )
            compile_schema = tools["compile_managed_form"].input_schema
            schema_text = json.dumps(compile_schema, ensure_ascii=False)
            if "ManagedFormSpec" not in schema_text or "additionalProperties" not in schema_text:
                raise RuntimeError("Строгая вложенная specification-схема не опубликована.")
            definitions = compile_schema["$defs"]
            specification_definition = definitions["ManagedFormSpec"]
            if specification_definition["properties"]["schema_version"].get(
                "const"
            ) != 2:
                raise RuntimeError("Forms принимает только schema_version=2.")
            if "context" not in specification_definition.get("required", []):
                raise RuntimeError("Forms schema не требует context.")
            context_definition = definitions["FormContextSpec"]
            if set(context_definition.get("required", [])) != {"owner", "role"}:
                raise RuntimeError("Forms context обязан содержать owner и role.")
            by_name = {tool.name: tool for tool in listed.tools}
            for operation in ("decompile_managed_form", "check_managed_form"):
                required = by_name[operation].input_schema.get("required", [])
                if "context" not in required:
                    raise RuntimeError(f"{operation} не требует Forms context.")
            format_pattern = definitions["ManagedFormSpec"]["properties"][
                "format_version"
            ]["pattern"]
            if not re.fullmatch(format_pattern, "2.20") or re.fullmatch(
                format_pattern, "latest"
            ):
                raise RuntimeError("Схема потеряла безопасный формат версии Form.xml.")
            for definition, property_name, minimum in (
                ("ManagedFormSpec", "attributes", 1),
                ("ManagedFormSpec", "elements", 1),
                ("CompositeTypeSpec", "variants", 2),
                ("RadioButtonFieldSpec", "choice_list", 2),
            ):
                actual = definitions[definition]["properties"][property_name].get(
                    "minItems"
                )
                if actual != minimum:
                    raise RuntimeError(
                        f"Схема потеряла {definition}.{property_name} minItems."
                    )
            if "events" not in specification_definition.get("required", []):
                raise RuntimeError("Forms schema не требует поле events.")
            if "minItems" in specification_definition["properties"]["events"]:
                raise RuntimeError("Forms schema запрещает пустой массив events.")
            for definition, property_name, accepted, rejected in (
                (
                    "MetadataObjectTypeSpec",
                    "object",
                    "Документ.Заказ",
                    "ВнешняяОбработка.Импорт",
                ),
                (
                    "MetadataReferenceTypeSpec",
                    "object",
                    "Перечисление.ВидыОпераций",
                    "Обработка.Импорт",
                ),
                (
                    "DynamicListTypeSpec",
                    "main_table",
                    "РегистрСведений.Цены",
                    "Обработка.Импорт",
                ),
            ):
                pattern = definitions[definition]["properties"][property_name][
                    "pattern"
                ]
                if not re.fullmatch(pattern, accepted) or re.fullmatch(
                    pattern, rejected
                ):
                    raise RuntimeError(
                        f"Схема потеряла формат {definition}.{property_name}."
                    )
            if "allOf" not in definitions["ButtonSpec"]:
                raise RuntimeError("Схема не объясняет условный command_owner.")
            if "allOf" not in definitions["CommandSourceSpec"]:
                raise RuntimeError("Схема не объясняет условный item.")
            rules_schema = tools["get_managed_form_rules"].input_schema
            if "query" not in rules_schema.get("properties", {}):
                raise RuntimeError("Двуязычный поиск терминов не опубликован.")

            specification = json.loads(FIXTURE.read_text(encoding="utf-8"))
            specification["format_version"] = "2.20"
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
            bsl_rules = await session.call_tool(
                "get_managed_form_rules", {"topic": "commands_events"}
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
                    "context": _context(),
                    "module_bsl": module_bsl,
                },
            )
            invalid_conversion = await session.call_tool(
                "check_managed_form",
                {
                    "form_xml": form_xml,
                    "form_name": "ФормаПараметров",
                    "context": _context(),
                    "module_bsl": module_bsl
                    + (
                        "\r\n&НаКлиенте\r\n"
                        "Процедура ОшибочноеПреобразование()\r\n\r\n"
                        '\tЗначение = РеквизитФормыВЗначение("Объект");'
                        "\r\n\r\nКонецПроцедуры\r\n"
                    ),
                },
            )
            decompiled = await session.call_tool(
                "decompile_managed_form",
                {
                    "form_xml": form_xml,
                    "form_name": "ФормаПараметров",
                    "context": _context(),
                    "module_bsl": module_bsl,
                },
            )
            record_context = {
                "owner": "РегистрСведений.ТестовыйРегистр",
                "role": "record",
            }
            record_specification = {
                "schema_version": 2,
                "form_name": "ФормаЗаписи",
                "context": record_context,
                "format_version": "2.20",
                "title": {"ru": "Запись тестового регистра"},
                "attributes": [
                    {
                        "name": "Запись",
                        "type": {
                            "kind": "metadata_object",
                            "object": "РегистрСведений.ТестовыйРегистр",
                        },
                        "main": True,
                        "saved_data": True,
                    }
                ],
                "elements": [
                    {
                        "kind": "input_field",
                        "name": "Значение",
                        "data_path": "Запись.Значение",
                    }
                ],
                "commands": [],
                "events": [],
            }
            record_rules = await session.call_tool(
                "get_managed_form_rules", {"topic": "attributes"}
            )
            record_compiled = await session.call_tool(
                "compile_managed_form",
                {"specification": record_specification},
            )
            if record_compiled.is_error:
                raise RuntimeError(record_compiled.content[0].text)
            record_compiled_payload = json.loads(
                record_compiled.content[0].text
            )
            record_artifacts = {
                item["path"]: item["content"]
                for item in record_compiled_payload["artifacts"]
            }
            record_xml = record_artifacts["Forms/ФормаЗаписи/Ext/Form.xml"]
            record_module = record_artifacts[
                "Forms/ФормаЗаписи/Ext/Form/Module.bsl"
            ]
            record_checked = await session.call_tool(
                "check_managed_form",
                {
                    "form_xml": record_xml,
                    "form_name": "ФормаЗаписи",
                    "context": record_context,
                    "module_bsl": record_module,
                },
            )
            record_decompiled = await session.call_tool(
                "decompile_managed_form",
                {
                    "form_xml": record_xml,
                    "form_name": "ФормаЗаписи",
                    "context": record_context,
                    "module_bsl": record_module,
                },
            )
            record_checked_payload = json.loads(record_checked.content[0].text)
            record_decompiled_payload = json.loads(
                record_decompiled.content[0].text
            )
            record_rule_codes = {
                item["code"]
                for item in json.loads(record_rules.content[0].text)["rules"]
            }
            bsl_rule_codes = {
                item["code"]
                for item in json.loads(bsl_rules.content[0].text)["rules"]
            }
            invalid_conversion_payload = json.loads(
                invalid_conversion.content[0].text
            )
            invalid_conversion_codes = {
                item["code"]
                for item in invalid_conversion_payload["diagnostics"]
            }
            if (
                record_checked.is_error
                or record_decompiled.is_error
                or record_compiled_payload["coverage"]["structural"] != "passed"
                or record_checked_payload["coverage"]["structural"] != "passed"
                or record_decompiled_payload["coverage"]["structural"]
                != "passed"
                or record_decompiled_payload["specification"]
                != record_compiled_payload["specification"]
                or "information_register_record_main_attribute"
                not in record_rule_codes
                or "cfg:InformationRegisterRecordManager.ТестовыйРегистр"
                not in record_xml
                or "<MainAttribute>true</MainAttribute>" not in record_xml
                or "<SavedData>true</SavedData>" not in record_xml
                or "<DataPath>Запись.Значение</DataPath>" not in record_xml
                or "Данные записи: Запись.<Реквизит>" not in record_module
                or 'РеквизитФормыВЗначение("Запись")' not in record_module
                or "managed_form_bsl_data_access" not in bsl_rule_codes
                or invalid_conversion_payload["coverage"]["bsl_static"]
                != "failed"
                or "form_value_conversion_requires_server_context"
                not in invalid_conversion_codes
            ):
                raise RuntimeError(
                    "Внешняя MCP-последовательность role=record дала неверный результат."
                )
            document_context = {
                "owner": "Документ.ТестовыйДокумент",
                "role": "object",
            }
            document_specification = {
                "schema_version": 2,
                "form_name": "ФормаДокумента",
                "context": document_context,
                "format_version": "2.20",
                "title": {"ru": "Форма тестового документа"},
                "attributes": [
                    {
                        "name": "Объект",
                        "type": {
                            "kind": "metadata_object",
                            "object": "Документ.ТестовыйДокумент",
                        },
                        "main": True,
                    }
                ],
                "elements": [
                    {
                        "kind": "input_field",
                        "name": "Дата",
                        "data_path": "Объект.Дата",
                    }
                ],
                "commands": [],
                "events": [],
            }
            document_rules = await session.call_tool(
                "get_managed_form_rules", {"topic": "specification"}
            )
            document_compiled = await session.call_tool(
                "compile_managed_form",
                {"specification": document_specification},
            )
            if document_compiled.is_error:
                raise RuntimeError(document_compiled.content[0].text)
            document_compiled_payload = json.loads(
                document_compiled.content[0].text
            )
            document_artifacts = {
                item["path"]: item["content"]
                for item in document_compiled_payload["artifacts"]
            }
            document_xml = document_artifacts[
                "Forms/ФормаДокумента/Ext/Form.xml"
            ]
            document_module = document_artifacts[
                "Forms/ФормаДокумента/Ext/Form/Module.bsl"
            ]
            document_checked = await session.call_tool(
                "check_managed_form",
                {
                    "form_xml": document_xml,
                    "form_name": "ФормаДокумента",
                    "context": document_context,
                    "module_bsl": document_module,
                },
            )
            document_decompiled = await session.call_tool(
                "decompile_managed_form",
                {
                    "form_xml": document_xml,
                    "form_name": "ФормаДокумента",
                    "context": document_context,
                    "module_bsl": document_module,
                },
            )
            document_checked_payload = json.loads(
                document_checked.content[0].text
            )
            document_decompiled_payload = json.loads(
                document_decompiled.content[0].text
            )
            document_rule_codes = {
                item["code"]
                for item in json.loads(document_rules.content[0].text)["rules"]
            }
            if (
                document_checked.is_error
                or document_decompiled.is_error
                or document_compiled_payload["coverage"]["structural"] != "passed"
                or document_checked_payload["coverage"]["structural"] != "passed"
                or document_decompiled_payload["coverage"]["structural"]
                != "passed"
                or document_decompiled_payload["specification"]
                != document_compiled_payload["specification"]
                or "object_form_owner_context" not in document_rule_codes
                or "cfg:DocumentObject.ТестовыйДокумент" not in document_xml
                or "<MainAttribute>true</MainAttribute>" not in document_xml
                or "<DataPath>Объект.Дата</DataPath>" not in document_xml
                or "PostAndClose" in document_xml
            ):
                raise RuntimeError(
                    "Внешняя MCP-последовательность формы документа дала неверный результат."
                )
            for owner, role in (
                ("Справочник.ТестовыйСправочник", "list"),
                ("Документ.ТестовыйДокумент", "choice"),
            ):
                list_specification = _list_choice_spec(owner, role)
                list_compiled = await session.call_tool(
                    "compile_managed_form",
                    {"specification": list_specification},
                )
                if list_compiled.is_error:
                    raise RuntimeError(list_compiled.content[0].text)
                list_payload = json.loads(list_compiled.content[0].text)
                list_artifacts = {
                    item["path"]: item["content"]
                    for item in list_payload["artifacts"]
                }
                list_xml = list_artifacts[
                    f"Forms/{list_specification['form_name']}/Ext/Form.xml"
                ]
                list_module = list_artifacts[
                    f"Forms/{list_specification['form_name']}/Ext/Form/Module.bsl"
                ]
                common_arguments = {
                    "form_xml": list_xml,
                    "form_name": list_specification["form_name"],
                    "context": list_specification["context"],
                    "module_bsl": list_module,
                }
                list_checked = await session.call_tool(
                    "check_managed_form", common_arguments
                )
                list_decompiled = await session.call_tool(
                    "decompile_managed_form", common_arguments
                )
                list_checked_payload = json.loads(list_checked.content[0].text)
                list_decompiled_payload = json.loads(
                    list_decompiled.content[0].text
                )
                choice_markers = (
                    "<WindowOpeningMode>LockOwnerWindow</WindowOpeningMode>" in list_xml
                    and "<ChoiceMode>true</ChoiceMode>" in list_xml
                )
                if (
                    list_checked.is_error
                    or list_decompiled.is_error
                    or list_checked_payload["coverage"]["structural"] != "passed"
                    or list_decompiled_payload["specification"]
                    != list_payload["specification"]
                    or choice_markers != (role == "choice")
                ):
                    raise RuntimeError(
                        f"Forms {owner} role={role} не прошёл внешний roundtrip."
                    )
            metadata_rules = await session.call_tool(
                "get_metadata_authoring_rules", {"topic": "document"}
            )
            metadata_specification = {
                "schema_version": 1,
                "object_ref": "Документ.ТестовыйДокумент",
                "format_version": "2.20",
                "identity": "60000000-0000-0000-0000-000000000001",
                "synonym": "Тестовый документ",
                "number_length": 11,
                "number_allowed_length": "Variable",
                "number_periodicity": "Nonperiodical",
                "check_unique": True,
                "autonumbering": True,
                "posting": "Deny",
                "real_time_posting": "Deny",
                "attributes": [],
                "forms": [
                    {
                        "name": "ФормаДокумента",
                        "synonym": "Форма документа",
                        "default": True,
                        "form_xml": document_xml,
                        "module_bsl": document_module,
                    }
                ],
            }
            metadata_compiled = await session.call_tool(
                "compile_metadata_object",
                {"specification": metadata_specification},
            )
            if metadata_compiled.is_error:
                raise RuntimeError(metadata_compiled.content[0].text)
            metadata_payload = json.loads(metadata_compiled.content[0].text)
            metadata_checked = await session.call_tool(
                "check_metadata_artifacts",
                {
                    "object_ref": metadata_payload["object_ref"],
                    "format_version": metadata_payload["format_version"],
                    "artifacts": metadata_payload["artifacts"],
                },
            )
            metadata_checked_payload = json.loads(
                metadata_checked.content[0].text
            )
            metadata_rules_payload = json.loads(metadata_rules.content[0].text)
            metadata_descriptor = next(
                item["content"]
                for item in metadata_payload["artifacts"]
                if item["path"] == "Documents/ТестовыйДокумент.xml"
            )
            if (
                metadata_rules.is_error
                or metadata_checked.is_error
                or metadata_payload["status"] != "compiled"
                or metadata_checked_payload["status"] != "passed"
                or metadata_rules_payload["posting"] != "Deny"
                or "<Posting>Deny</Posting>" not in metadata_descriptor
                or "Document.ТестовыйДокумент.Form.ФормаДокумента"
                not in metadata_descriptor
                or "Процедура " in document_module
            ):
                raise RuntimeError(
                    "Совместная MCP-последовательность Metadata + Forms для документа дала неверный результат: "
                    + json.dumps(
                        {
                            "rules_error": metadata_rules.is_error,
                            "compile_status": metadata_payload.get("status"),
                            "check_error": metadata_checked.is_error,
                            "check": metadata_checked_payload,
                            "posting_rule": metadata_rules_payload.get("posting"),
                            "has_posting": "<Posting>Deny</Posting>" in metadata_descriptor,
                            "has_default": "Document.ТестовыйДокумент.Form.ФормаДокумента" in metadata_descriptor,
                            "has_procedure": "Процедура " in document_module,
                        },
                        ensure_ascii=False,
                    )
                )
            data_processor_owner = "Обработка.ТестоваяОбработка"
            data_processor_context = {
                "owner": data_processor_owner,
                "role": "object",
            }
            data_processor_form_specification = {
                "schema_version": 2,
                "form_name": "Форма",
                "context": data_processor_context,
                "format_version": "2.20",
                "title": {"ru": "Тестовая обработка"},
                "attributes": [
                    {
                        "name": "Объект",
                        "type": {
                            "kind": "metadata_object",
                            "object": data_processor_owner,
                        },
                        "main": True,
                    },
                    {
                        "name": "Параметр",
                        "type": {"kind": "string", "length": 100},
                    },
                ],
                "elements": [
                    {
                        "kind": "input_field",
                        "name": "Параметр",
                        "data_path": "Параметр",
                    }
                ],
                "commands": [],
                "events": [],
            }
            data_processor_rules = await session.call_tool(
                "get_metadata_authoring_rules", {"topic": "data_processor"}
            )
            data_processor_form_compiled = await session.call_tool(
                "compile_managed_form",
                {"specification": data_processor_form_specification},
            )
            if data_processor_form_compiled.is_error:
                raise RuntimeError(data_processor_form_compiled.content[0].text)
            data_processor_form_payload = json.loads(
                data_processor_form_compiled.content[0].text
            )
            data_processor_form_artifacts = {
                item["path"]: item["content"]
                for item in data_processor_form_payload["artifacts"]
            }
            data_processor_form_xml = data_processor_form_artifacts[
                "Forms/Форма/Ext/Form.xml"
            ]
            data_processor_module = data_processor_form_artifacts[
                "Forms/Форма/Ext/Form/Module.bsl"
            ]
            data_processor_form_checked = await session.call_tool(
                "check_managed_form",
                {
                    "form_xml": data_processor_form_xml,
                    "form_name": "Форма",
                    "context": data_processor_context,
                    "module_bsl": data_processor_module,
                },
            )
            data_processor_metadata_specification = {
                "schema_version": 1,
                "object_ref": data_processor_owner,
                "format_version": "2.20",
                "identity": "74000000-0000-0000-0000-000000000002",
                "synonym": "Тестовая обработка",
                "attributes": [],
                "forms": [
                    {
                        "name": "Форма",
                        "synonym": "Форма",
                        "role": "object",
                        "default": True,
                        "form_xml": data_processor_form_xml,
                        "module_bsl": data_processor_module,
                    }
                ],
            }
            data_processor_metadata_compiled = await session.call_tool(
                "compile_metadata_object",
                {"specification": data_processor_metadata_specification},
            )
            if data_processor_metadata_compiled.is_error:
                raise RuntimeError(
                    data_processor_metadata_compiled.content[0].text
                )
            data_processor_metadata_payload = json.loads(
                data_processor_metadata_compiled.content[0].text
            )
            data_processor_metadata_checked = await session.call_tool(
                "check_metadata_artifacts",
                {
                    "object_ref": data_processor_metadata_payload["object_ref"],
                    "format_version": data_processor_metadata_payload[
                        "format_version"
                    ],
                    "artifacts": data_processor_metadata_payload["artifacts"],
                },
            )
            data_processor_with_command = copy.deepcopy(
                data_processor_form_specification
            )
            data_processor_with_command["elements"].append(
                {
                    "kind": "button",
                    "name": "Записать",
                    "command": "Write",
                    "command_kind": "form_standard",
                }
            )
            data_processor_command_rejected = await session.call_tool(
                "compile_managed_form",
                {"specification": data_processor_with_command},
            )
            data_processor_command_payload = json.loads(
                data_processor_command_rejected.content[0].text
            )
            data_processor_form_check_payload = json.loads(
                data_processor_form_checked.content[0].text
            )
            data_processor_metadata_check_payload = json.loads(
                data_processor_metadata_checked.content[0].text
            )
            data_processor_rules_payload = json.loads(
                data_processor_rules.content[0].text
            )
            data_processor_artifacts = {
                item["path"]: item["content"]
                for item in data_processor_metadata_payload["artifacts"]
            }
            data_processor_descriptor = data_processor_artifacts[
                "DataProcessors/ТестоваяОбработка.xml"
            ]
            if (
                data_processor_rules.is_error
                or data_processor_form_checked.is_error
                or data_processor_metadata_checked.is_error
                or data_processor_command_payload.get("status") != "rejected"
                or "unsupported_owner_role_feature"
                not in {
                    item["code"]
                    for item in data_processor_command_payload.get(
                        "diagnostics", []
                    )
                }
                or data_processor_form_check_payload["coverage"]["structural"]
                != "passed"
                or data_processor_metadata_payload["status"] != "compiled"
                or data_processor_metadata_check_payload["status"] != "passed"
                or len(data_processor_artifacts) != 4
                or data_processor_rules_payload.get("object_ref")
                != "Обработка.<Имя>"
                or "cfg:DataProcessorObject.ТестоваяОбработка"
                not in data_processor_form_xml
                or "<DataPath>Параметр</DataPath>"
                not in data_processor_form_xml
                or "DataProcessorObject.ТестоваяОбработка"
                not in data_processor_descriptor
                or "DataProcessorManager.ТестоваяОбработка"
                not in data_processor_descriptor
                or (
                    "<DefaultForm>DataProcessor.ТестоваяОбработка.Form.Форма"
                    "</DefaultForm>"
                )
                not in data_processor_descriptor
                or "Процедура " in data_processor_module
            ):
                raise RuntimeError(
                    "Совместная MCP-последовательность встроенной обработки "
                    "дала неверный результат."
                )
            invalid = dict(specification)
            invalid["unknown"] = True
            rejected = await session.call_tool(
                "compile_managed_form", {"specification": invalid}
            )
            empty = copy.deepcopy(specification)
            empty["events"] = []
            empty["elements"][0]["children"] = []
            empty_rejected = await session.call_tool(
                "compile_managed_form", {"specification": empty}
            )
            unverified = copy.deepcopy(specification)
            unverified["format_version"] = "2.21"
            unverified_result = await session.call_tool(
                "compile_managed_form", {"specification": unverified}
            )
            unverified_payload = json.loads(unverified_result.content[0].text)
            empty_payload = json.loads(empty_rejected.content[0].text)
            empty_paths = {
                item["path"]
                for item in empty_payload["diagnostics"]
                if item["code"] == "empty_collection"
            }
            results = (rules, compiled, checked, decompiled)
            if (
                any(result.is_error for result in results)
                or not rejected.is_error
                or empty_rejected.is_error
                or unverified_result.is_error
                or empty_payload["status"] != "rejected"
                or empty_paths != {"$.elements[0].children"}
            ):
                raise RuntimeError("Внешняя MCP-последовательность дала неверный статус.")
            if (
                'version="2.20"' not in form_xml
                or unverified_payload["status"] != "compiled"
                or not any(
                    item["code"] == "form_format_compatibility_unverified"
                    and item["status"] == "warning"
                    for item in unverified_payload["diagnostics"]
                )
            ):
                raise RuntimeError("Версионный контракт Form.xml выполнен неверно.")
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
                "format_version": "2.20",
                "unverified_format_version": "compiled_with_warning",
                "array_minima_published": True,
                "empty_arrays_structured_rejected": True,
                "unknown_field_rejected": True,
                "bilingual_term_search": "command_bar",
                "command_source": "form_and_form_global_commands",
                "auto_command_bar": "table_autofill_false_with_button",
                "context_menu": "table_autofill_false_with_button",
                "record_role": "compile_check_decompile_passed",
                "record_roundtrip": True,
                "record_main_attribute": "InformationRegisterRecordManager",
                "record_saved_data": True,
                "document_role": "compile_check_decompile_passed",
                "document_roundtrip": True,
                "document_main_attribute": "DocumentObject",
                "catalog_list_role": "compile_check_decompile_passed",
                "document_choice_role": "compile_check_decompile_passed",
                "choice_markers": "derived_from_role",
                "metadata_document": "compile_check_passed",
                "metadata_forms_composition": True,
                "metadata_document_non_posting": True,
                "metadata_data_processor": "compile_check_passed",
                "data_processor_form": "compile_check_passed",
                "data_processor_artifact_count": len(data_processor_artifacts),
                "data_processor_default_form": True,
                "data_processor_business_bsl": False,
                "data_processor_command_rejected": True,
                "bsl_data_access_rule": True,
                "client_form_value_conversion_rejected": True,
            }


async def _main() -> None:
    with TemporaryDirectory(prefix="mcp1c-forms-stdio-") as temporary:
        root = Path(temporary)
        for mode in ("off", "forms,metadata_authoring"):
            print(
                json.dumps(
                    await _session(mode, root / mode),
                    ensure_ascii=False,
                )
            )


if __name__ == "__main__":
    asyncio.run(_main())
