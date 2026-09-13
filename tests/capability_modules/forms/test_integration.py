from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import threading

import anyio
import pytest
from mcp import ClientSession
from mcp.shared.memory import create_client_server_memory_streams

from mcp1c.reference_provider import ReferenceService
from mcp1c.model import Configuration, Field, MetadataObject
from mcp1c.registry import Registry
from mcp1c.server import build_server
from mcp1c.capability_modules.forms import tools as forms_tools
from conftest import write_export


FIXTURES = Path(__file__).with_name("fixtures")
CORE_TOOL_COUNT = 11
FORM_TOOLS = [
    "get_managed_form_rules",
    "compile_managed_form",
    "decompile_managed_form",
    "check_managed_form",
]


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _payload() -> dict:
    return json.loads((FIXTURES / "minimal_form.json").read_text(encoding="utf-8"))


def _server(tmp_path, *, enabled=(), registry=None):
    registry = registry or Registry(tmp_path)
    return build_server(
        registry,
        reference=ReferenceService.discover(tmp_path, database_path="off"),
        enabled_capabilities=enabled,
    )


def test_off_в_чистом_process_не_импортирует_forms_и_не_добавляет_tools(tmp_path):
    probe = """
import asyncio
import json
import sys
from pathlib import Path
from mcp1c.reference_provider import ReferenceService
from mcp1c.registry import Registry
from mcp1c.server import build_server

data = Path(sys.argv[1])
server = build_server(
    Registry(data),
    reference=ReferenceService.discover(data, database_path="off"),
    enabled_capabilities=(),
)
print(json.dumps({
    "forms_imported": any(
        name == "mcp1c.capability_modules.forms"
        or name.startswith("mcp1c.capability_modules.forms.")
        for name in sys.modules
    ),
    "tools": [tool.name for tool in asyncio.run(server.list_tools())],
}))
"""
    result = subprocess.run(
        [sys.executable, "-c", probe, str(tmp_path)],
        cwd=Path(__file__).resolve().parents[3],
        env={**os.environ, "PYTHONPATH": "src", "MCP1C_CAPABILITIES": "off"},
        check=True,
        capture_output=True,
        text=True,
    )
    payload = json.loads(result.stdout)

    assert payload["forms_imported"] is False
    assert len(payload["tools"]) == CORE_TOOL_COUNT
    assert not set(FORM_TOOLS).intersection(payload["tools"])


def test_on_добавляет_ровно_четыре_forms_tools_в_стабильном_порядке(tmp_path):
    tools = asyncio.run(_server(tmp_path, enabled=("forms",)).list_tools())
    names = [tool.name for tool in tools]

    assert names[-4:] == FORM_TOOLS
    assert len(names) == CORE_TOOL_COUNT + 4
    rules_schema = tools[-4].input_schema
    assert "query" in rules_schema["properties"]
    assert "terminology" in json.dumps(rules_schema, ensure_ascii=False)
    assert "русскому или английскому" in tools[-4].description
    compile_schema = tools[-3].input_schema
    assert "specification" in compile_schema["properties"]
    assert "configuration" in compile_schema["properties"]
    schema_text = json.dumps(compile_schema, ensure_ascii=False)
    assert "ManagedFormSpec" in schema_text
    assert all(
        event in schema_text
        for event in (
            "OnReadAtServer",
            "BeforeWrite",
            "BeforeWriteAtServer",
            "OnWriteAtServer",
            "AfterWriteAtServer",
            "AfterWrite",
        )
    )


def test_compile_schema_публикует_фактические_минимумы_массивов(tmp_path):
    tools = asyncio.run(_server(tmp_path, enabled=("forms",)).list_tools())
    definitions = tools[-3].input_schema["$defs"]

    for definition, property_name, minimum in (
        ("ManagedFormSpec", "attributes", 1),
        ("ManagedFormSpec", "elements", 1),
        ("ManagedFormSpec", "events", 1),
        ("CompositeTypeSpec", "variants", 2),
        ("ValueTableTypeSpec", "columns", 1),
        ("InputFieldSpec", "choice_list", 1),
        ("RadioButtonFieldSpec", "choice_list", 2),
        ("TableSpec", "columns", 1),
        ("PageSpec", "children", 1),
        ("PagesSpec", "pages", 1),
        ("UsualGroupSpec", "children", 1),
    ):
        assert definitions[definition]["properties"][property_name][
            "minItems"
        ] == minimum

    assert "minItems" not in definitions["ManagedFormSpec"]["properties"][
        "commands"
    ]
    for definition in (
        "ButtonGroupSpec",
        "PopupSpec",
        "CommandBarSpec",
        "AutoCommandBarSpec",
        "ContextMenuSpec",
    ):
        assert "minItems" not in definitions[definition]["properties"][
            "children"
        ]


def test_compile_schema_объясняет_registry_ссылки_и_условные_поля(tmp_path):
    tools = asyncio.run(_server(tmp_path, enabled=("forms",)).list_tools())
    definitions = tools[-3].input_schema["$defs"]

    patterns = {
        "MetadataObjectTypeSpec": (
            "object",
            ("Обработка.Импорт", "Документ.Заказ"),
            "ВнешняяОбработка.Импорт",
        ),
        "MetadataReferenceTypeSpec": (
            "object",
            ("Справочник.Товары", "Перечисление.ВидыОпераций"),
            "Обработка.Импорт",
        ),
        "DynamicListTypeSpec": (
            "main_table",
            ("Справочник.Товары", "РегистрСведений.Цены"),
            "Обработка.Импорт",
        ),
    }
    for definition, (property_name, accepted, rejected) in patterns.items():
        pattern = definitions[definition]["properties"][property_name]["pattern"]
        assert all(re.fullmatch(pattern, value) for value in accepted)
        assert re.fullmatch(pattern, rejected) is None

    assert definitions["ButtonSpec"]["allOf"] == [
        {
            "if": {
                "properties": {"command_kind": {"const": "item_standard"}},
                "required": ["command_kind"],
            },
            "then": {"required": ["command_owner"]},
            "else": {"not": {"required": ["command_owner"]}},
        }
    ]
    assert definitions["CommandSourceSpec"]["allOf"] == [
        {
            "if": {
                "properties": {"kind": {"const": "item"}},
                "required": ["kind"],
            },
            "then": {"required": ["item"]},
            "else": {"not": {"required": ["item"]}},
        }
    ]
    command_schema = definitions["ButtonSpec"]["properties"]["command"]
    assert "только имя команды" in command_schema["description"]
    assert "Form.Command." in command_schema["description"]


def _registry_with_object(tmp_path) -> Registry:
    registry = Registry(tmp_path / "data")
    object_name = "Обработка.НоваяОбработка"
    config = Configuration(name="КонтекстА", platform="8.3.23.1997")
    config.objects[object_name] = MetadataObject(
        full_name=object_name,
        kind="Обработка",
        name="НоваяОбработка",
        attributes=[
            Field("Комментарий", types=["Строка"]),
            Field("Активен", types=["Булево"]),
        ],
    )
    config.objects["Справочник.Товары"] = MetadataObject(
        full_name="Справочник.Товары",
        kind="Справочник",
        name="Товары",
        attributes=[Field("Наименование", types=["Строка"])],
    )
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    registry.add_configuration(write_export(incoming, config))
    return registry


@pytest.mark.anyio
async def test_compile_сам_берёт_платформу_и_проверяет_ссылки_из_registry(tmp_path):
    registry = _registry_with_object(tmp_path)
    server = _server(tmp_path, enabled=("forms",), registry=registry)
    specification = _payload()
    specification.pop("platform_version", None)
    specification["attributes"][0] = {
        "name": "Объект",
        "type": {
            "kind": "metadata_object",
            "object": "Обработка.НоваяОбработка",
        },
        "main": True,
    }
    specification["attributes"][1]["type"] = {
        "kind": "metadata_reference",
        "object": "Справочник.Товары",
    }
    specification["elements"][0]["children"][0].update(
        {"name": "Комментарий", "data_path": "Объект.Комментарий"}
    )

    result = await server.call_tool(
        "compile_managed_form",
        {"specification": specification, "configuration": "КонтекстА"},
    )
    assert result.is_error is False
    payload = json.loads(result.content[0].text)

    assert payload["status"] == "compiled"
    assert payload["specification"]["platform_version"] == "8.3.23.1997"
    assert payload["coverage"]["configuration_links"] == "passed"
    assert any(
        item["code"] == "registry_links_verified"
        for item in payload["diagnostics"]
    )


@pytest.mark.anyio
async def test_отсутствующая_ссылка_не_блокирует_генерацию_но_даёт_warning(tmp_path):
    registry = _registry_with_object(tmp_path)
    specification = _payload()
    specification["attributes"][0] = {
        "name": "Объект",
        "type": {
            "kind": "metadata_object",
            "object": "Обработка.НовыйОбъект",
        },
        "main": True,
    }
    specification["elements"][0]["children"][0].update(
        {"name": "Поле", "data_path": "Объект.Поле"}
    )

    tools = {item.name: item.function for item in forms_tools.load(registry)}
    result = await tools["compile_managed_form"](specification)
    payload = json.loads(result)

    assert payload["status"] == "compiled"
    assert payload["artifacts"]
    assert payload["coverage"]["configuration_links"] == "warning"
    assert any(
        item["code"] == "metadata_object_not_found"
        for item in payload["diagnostics"]
    )


@pytest.mark.anyio
async def test_registry_проверяет_ссылки_в_одиночном_и_составном_типе(tmp_path):
    registry = _registry_with_object(tmp_path)
    specification = _payload()
    specification["attributes"][0]["type"] = {
        "kind": "metadata_reference",
        "object": "Справочник.Товары",
    }
    specification["attributes"][1]["type"] = {
        "kind": "composite",
        "variants": [
            {"kind": "metadata_reference", "object": "Документ.Заказ"},
            {"kind": "string", "length": 20},
        ],
    }

    tools = {item.name: item.function for item in forms_tools.load(registry)}
    result = await tools["compile_managed_form"](specification)
    payload = json.loads(result)

    assert payload["status"] == "compiled"
    assert payload["coverage"]["configuration_links"] == "warning"
    warnings = [
        item for item in payload["diagnostics"]
        if item["code"] == "metadata_reference_not_found"
    ]
    assert [item["path"] for item in warnings] == [
        "$.attributes[1].type.variants[0].object"
    ]


@pytest.mark.anyio
async def test_registry_проверяет_основную_таблицу_и_поле_dynamic_list(tmp_path):
    registry = _registry_with_object(tmp_path)
    specification = _payload()
    specification["attributes"][0]["type"] = {
        "kind": "dynamic_list",
        "main_table": "Справочник.Товары",
        "dynamic_data_read": True,
    }
    specification["elements"][0]["children"][0]["data_path"] = (
        "ПервоеЗначение.Наименование"
    )

    tools = {item.name: item.function for item in forms_tools.load(registry)}
    payload = json.loads(await tools["compile_managed_form"](specification))

    assert payload["status"] == "compiled"
    assert payload["coverage"]["configuration_links"] == "passed"


@pytest.mark.anyio
async def test_доказанный_конфликт_типа_поля_отклоняет_артефакты(tmp_path):
    registry = _registry_with_object(tmp_path)
    specification = _payload()
    specification["attributes"][0] = {
        "name": "Объект",
        "type": {
            "kind": "metadata_object",
            "object": "Обработка.НоваяОбработка",
        },
        "main": True,
    }
    specification["elements"][0]["children"][0] = {
        "kind": "check_box_field",
        "name": "Комментарий",
        "data_path": "Объект.Комментарий",
    }

    tools = {item.name: item.function for item in forms_tools.load(registry)}
    result = await tools["compile_managed_form"](specification)
    payload = json.loads(result)

    assert payload["status"] == "rejected"
    assert payload["artifacts"] == []
    assert payload["coverage"]["configuration_links"] == "failed"
    assert any(
        item["code"] == "registry_field_type_conflict"
        for item in payload["diagnostics"]
    )


@pytest.mark.anyio
async def test_явная_версия_не_может_противоречить_registry(tmp_path):
    registry = _registry_with_object(tmp_path)
    specification = _payload()
    specification["platform_version"] = "8.3.27"

    tools = {item.name: item.function for item in forms_tools.load(registry)}
    result = await tools["compile_managed_form"](specification)
    payload = json.loads(result)

    assert payload["status"] == "rejected"
    assert payload["artifacts"] == []
    assert any(
        item["code"] == "platform_version_conflicts_with_registry"
        for item in payload["diagnostics"]
    )


@pytest.mark.anyio
async def test_empty_registry_не_блокирует_автономную_генерацию(tmp_path):
    registry = Registry(tmp_path / "data")
    tools = {item.name: item.function for item in forms_tools.load(registry)}

    result = await tools["compile_managed_form"](_payload())
    payload = json.loads(result)

    assert payload["status"] == "compiled"
    assert payload["coverage"]["configuration_links"] == "not_checked"
    assert any(
        item["code"] == "registry_context_unavailable"
        for item in payload["diagnostics"]
    )


@pytest.mark.anyio
async def test_при_нескольких_контекстах_агент_выбирает_configuration(tmp_path):
    registry = _registry_with_object(tmp_path)
    second = Configuration(name="КонтекстБ", platform="8.3.23.1997")
    incoming = tmp_path / "incoming-b"
    incoming.mkdir()
    registry.add_configuration(write_export(incoming, second))
    tools = {item.name: item.function for item in forms_tools.load(registry)}

    without_selection = json.loads(
        await tools["compile_managed_form"](_payload())
    )
    selected = json.loads(
        await tools["compile_managed_form"](
            _payload(),
            configuration="КонтекстА",
        )
    )

    assert without_selection["status"] == "compiled"
    assert without_selection["coverage"]["configuration_links"] == "not_checked"
    assert selected["coverage"]["configuration_links"] == "passed"


@pytest.mark.anyio
async def test_три_операции_используют_один_registry_контекст(tmp_path):
    registry = _registry_with_object(tmp_path)
    specification = _payload()
    specification["attributes"][0] = {
        "name": "Объект",
        "type": {
            "kind": "metadata_object",
            "object": "Обработка.НоваяОбработка",
        },
        "main": True,
    }
    specification["attributes"][1]["type"] = {
        "kind": "metadata_reference",
        "object": "Справочник.Товары",
    }
    specification["elements"][0]["children"][0].update(
        {"name": "Активен", "data_path": "Объект.Активен"}
    )
    specification["elements"][0]["children"][2] = {
        "kind": "button",
        "name": "Записать",
        "command": "Write",
        "command_kind": "form_standard",
    }
    specification["events"].append(
        {"event": "OnReadAtServer", "handler": "ПриЧтенииНаСервере"}
    )
    tools = {item.name: item.function for item in forms_tools.load(registry)}
    compiled = json.loads(await tools["compile_managed_form"](specification))
    artifacts = {item["path"]: item for item in compiled["artifacts"]}
    xml = artifacts["Forms/ФормаПараметров/Ext/Form.xml"]["content"]
    module = artifacts["Forms/ФормаПараметров/Ext/Form/Module.bsl"]["content"]

    decompiled = json.loads(
        await tools["decompile_managed_form"](
            xml,
            "ФормаПараметров",
            module,
            configuration="КонтекстА",
        )
    )
    checked = json.loads(
        await tools["check_managed_form"](
            xml,
            "ФормаПараметров",
            module,
            configuration="КонтекстА",
        )
    )

    assert decompiled["specification"] == compiled["specification"]
    assert decompiled["coverage"]["configuration_links"] == "passed"
    assert checked["coverage"]["configuration_links"] == "passed"
    assert all(
        any(
            item["code"] == "registry_links_verified"
            for item in result["diagnostics"]
        )
        for result in (decompiled, checked)
    )


@pytest.mark.anyio
async def test_agent_проходит_rules_compile_check_decompile_через_mcp(tmp_path):
    server = _server(tmp_path, enabled=("forms",))

    async with create_client_server_memory_streams() as (
        client_streams,
        server_streams,
    ):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(
                server._lowlevel_server.run,
                *server_streams,
                server._lowlevel_server.create_initialization_options(),
            )
            try:
                async with ClientSession(*client_streams) as session:
                    await session.initialize()
                    specification = _payload()
                    specification["platform_version"] = "8.3.24"
                    listed = await session.list_tools()
                    rules = await session.call_tool(
                        "get_managed_form_rules", {"topic": "overview"}
                    )
                    compiled = await session.call_tool(
                        "compile_managed_form", {"specification": specification}
                    )
                    compile_payload = json.loads(compiled.content[0].text)
                    artifacts = {
                        item["path"]: item for item in compile_payload["artifacts"]
                    }
                    checked = await session.call_tool(
                        "check_managed_form",
                        {
                            "form_xml": artifacts[
                                "Forms/ФормаПараметров/Ext/Form.xml"
                            ]["content"],
                            "form_name": "ФормаПараметров",
                            "platform_version": "8.3.24",
                            "module_bsl": artifacts[
                                "Forms/ФормаПараметров/Ext/Form/Module.bsl"
                            ]["content"],
                        },
                    )
                    decompiled = await session.call_tool(
                        "decompile_managed_form",
                        {
                            "form_xml": artifacts[
                                "Forms/ФормаПараметров/Ext/Form.xml"
                            ]["content"],
                            "form_name": "ФормаПараметров",
                            "platform_version": "8.3.24",
                        },
                    )
            finally:
                tasks.cancel_scope.cancel()

    assert [tool.name for tool in listed.tools][-4:] == FORM_TOOLS
    assert all(
        result.is_error is False for result in (rules, compiled, checked, decompiled)
    )
    assert json.loads(rules.content[0].text)["topic"] == "overview"
    assert compile_payload["status"] == "compiled"
    assert compile_payload["specification"]["platform_version"] == "8.3.24"
    assert json.loads(checked.content[0].text)["coverage"]["bsl_static"] == "passed"
    assert json.loads(decompiled.content[0].text)["specification"] == (
        compile_payload["specification"]
    )


@pytest.mark.anyio
async def test_ошибка_контракта_forms_возвращает_структурированный_rejected(tmp_path):
    server = _server(tmp_path, enabled=("forms",))
    specification = _payload()
    specification["events"] = []
    specification["elements"][0]["children"] = []

    async with create_client_server_memory_streams() as (client_streams, server_streams):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(
                server._lowlevel_server.run,
                *server_streams,
                server._lowlevel_server.create_initialization_options(),
            )
            try:
                async with ClientSession(*client_streams) as session:
                    await session.initialize()
                    result = await session.call_tool(
                        "compile_managed_form", {"specification": specification}
                    )
            finally:
                tasks.cancel_scope.cancel()

    assert result.is_error is False
    payload = json.loads(result.content[0].text)
    assert payload["status"] == "rejected"
    assert payload["artifacts"] == []
    assert payload["coverage"]["structural"] == "failed"
    assert {
        (item["code"], item["path"])
        for item in payload["diagnostics"]
    } >= {
        ("empty_collection", "$.events"),
        ("empty_collection", "$.elements[0].children"),
    }
    assert payload["instructions"] == [
        "Исправьте поля по diagnostics и повторите compile_managed_form.",
        "Не передавайте пустой массив: удалите необязательное поле либо "
        "добавьте минимум один поддержанный элемент.",
    ]


@pytest.mark.anyio
async def test_schema_подсказки_не_заменяют_предметную_диагностику(tmp_path):
    server = _server(tmp_path, enabled=("forms",))
    specification = _payload()
    specification["attributes"][0]["type"] = {
        "kind": "metadata_object",
        "object": "ВнешняяОбработка.НеверныйПрефикс",
    }
    specification["elements"][0]["children"][2].update(
        {"command": "Add", "command_kind": "item_standard"}
    )

    async with create_client_server_memory_streams() as (
        client_streams,
        server_streams,
    ):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(
                server._lowlevel_server.run,
                *server_streams,
                server._lowlevel_server.create_initialization_options(),
            )
            try:
                async with ClientSession(*client_streams) as session:
                    await session.initialize()
                    result = await session.call_tool(
                        "compile_managed_form", {"specification": specification}
                    )
            finally:
                tasks.cancel_scope.cancel()

    assert result.is_error is False
    payload = json.loads(result.content[0].text)
    assert payload["status"] == "rejected"
    assert {
        (item["code"], item["path"])
        for item in payload["diagnostics"]
    } >= {
        ("invalid_metadata_object", "$.attributes[0].type.object"),
        ("missing_key", "$.elements[0].children[2].command_owner"),
    }


@pytest.mark.anyio
async def test_mcp_не_теряет_неизвестные_поля_до_проверки_контракта(tmp_path):
    server = _server(tmp_path, enabled=("forms",))
    payload = _payload()
    payload["unknown"] = True

    async with create_client_server_memory_streams() as (client_streams, server_streams):
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(
                server._lowlevel_server.run,
                *server_streams,
                server._lowlevel_server.create_initialization_options(),
            )
            try:
                async with ClientSession(*client_streams) as session:
                    await session.initialize()
                    result = await session.call_tool(
                        "compile_managed_form", {"specification": payload}
                    )
            finally:
                tasks.cancel_scope.cancel()

    assert result.is_error is True
    assert "unknown" in result.content[0].text


@pytest.mark.anyio
async def test_десять_mcp_клиентов_forms_не_блокируют_core_tool(tmp_path, monkeypatch):
    release = threading.Event()
    server = _server(tmp_path, enabled=("forms",))
    results = []
    compiler = forms_tools.compile_managed_form

    def compile_stub(specification):
        release.wait(2)
        return compiler(specification)

    async def call(name: str, arguments: dict) -> None:
        async with create_client_server_memory_streams() as (
            client_streams,
            server_streams,
        ):
            async with anyio.create_task_group() as tasks:
                tasks.start_soon(
                    server._lowlevel_server.run,
                    *server_streams,
                    server._lowlevel_server.create_initialization_options(),
                )
                try:
                    async with ClientSession(*client_streams) as session:
                        await session.initialize()
                        results.append(await session.call_tool(name, arguments))
                finally:
                    tasks.cancel_scope.cancel()

    monkeypatch.setattr(forms_tools, "compile_managed_form", compile_stub)
    async with anyio.create_task_group() as clients:
        for _ in range(10):
            clients.start_soon(
                call,
                "compile_managed_form",
                {"specification": _payload()},
            )
        with anyio.fail_after(1):
            while forms_tools._GATE.pending < 10:
                await anyio.sleep(0.001)
        with anyio.fail_after(1):
            await call("list_configurations", {})
        assert results[-1].is_error is False
        release.set()

    assert len(results) == 11
    assert all(result.is_error is False for result in results)


def test_docker_context_включает_forms_python_и_manifest():
    dockerignore = (Path(__file__).resolve().parents[3] / ".dockerignore").read_text(
        encoding="utf-8"
    )

    assert "!src/mcp1c/capability_modules/forms/*.py" in dockerignore
    assert "!src/mcp1c/capability_modules/forms/manifest.json" in dockerignore
