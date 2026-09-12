from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import anyio
import pytest
from mcp import ClientSession
from mcp.shared.memory import create_client_server_memory_streams

from mcp1c.reference_provider import ReferenceService
from mcp1c.registry import Registry
from mcp1c.server import build_server
from mcp1c.capability_modules.forms import tools as forms_tools


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


def _server(tmp_path, *, enabled=()):
    return build_server(
        Registry(tmp_path),
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
    compile_schema = tools[-3].input_schema
    assert "specification" in compile_schema["properties"]
    assert "ManagedFormSpec" in json.dumps(compile_schema, ensure_ascii=False)


@pytest.mark.anyio
async def test_agent_проходит_rules_compile_check_decompile_через_mcp(tmp_path):
    server = _server(tmp_path, enabled=("forms",))

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
                    listed = await session.list_tools()
                    rules = await session.call_tool(
                        "get_managed_form_rules", {"topic": "overview"}
                    )
                    compiled = await session.call_tool(
                        "compile_managed_form", {"specification": _payload()}
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
    assert json.loads(checked.content[0].text)["coverage"]["bsl_static"] == "passed"
    assert json.loads(decompiled.content[0].text)["specification"] == (
        compile_payload["specification"]
    )


@pytest.mark.anyio
async def test_ошибка_контракта_forms_остаётся_mcp_tool_error(tmp_path):
    server = _server(tmp_path, enabled=("forms",))

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
                        "compile_managed_form", {"specification": {}}
                    )
            finally:
                tasks.cancel_scope.cancel()

    assert result.is_error is True
    assert "specification.schema_version" in result.content[0].text


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
