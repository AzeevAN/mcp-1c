"""Внутренний startup-контракт необязательных capability-модулей."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

import anyio
import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.memory import create_client_server_memory_streams

from mcp1c.capabilities import (
    CapabilityRuntime,
    CapabilitySettingsStore,
    MAX_SERVER_SETTINGS_BYTES,
    CapabilityConfigurationError,
    CapabilityContractError,
    CapabilityDefinition,
    CapabilityModule,
    CapabilityTool,
    load_capability_modules,
    resolve_capability_settings,
)
from mcp1c.reference_provider import ReferenceService
from mcp1c.registry import Registry
from mcp1c import server as server_module
from mcp1c.server import build_server


CORE_TOOLS = [
    "list_configurations",
    "list_extensions",
    "search_objects",
    "search_procedures",
    "get_procedure",
    "get_module_source_file",
    "get_callers",
    "get_object",
    "get_related",
    "compare_configurations",
    "search_syntax",
    "get_syntax",
]
REMOVED_AUTHORING_TOOLS = {
    "get_managed_form_rules", "compile_managed_form", "decompile_managed_form",
    "check_managed_form", "get_metadata_authoring_rules",
    "compile_metadata_object", "check_metadata_artifacts",
}


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _server(tmp_path, *, enabled_capabilities=()):
    reference = ReferenceService.discover(tmp_path / "missing-reference")
    return build_server(
        Registry(tmp_path),
        reference=reference,
        enabled_capabilities=(*enabled_capabilities, "module_source_download"),
    )


def _names(server) -> list[str]:
    return [tool.name for tool in asyncio.run(server.list_tools())]


def test_default_не_публикует_и_не_импортирует_capability_модули(tmp_path):
    sys.modules.pop("mcp1c.capability_modules.forms", None)
    sys.modules.pop("mcp1c.capability_modules.metadata_authoring", None)

    server = _server(tmp_path)

    assert _names(server) == CORE_TOOLS
    assert "mcp1c.capability_modules.forms" not in sys.modules
    assert "mcp1c.capability_modules.metadata_authoring" not in sys.modules
    assert REMOVED_AUTHORING_TOOLS.isdisjoint(_names(server))


@pytest.mark.anyio
async def test_stdio_не_публикует_инструмент_без_http_маршрута(tmp_path):
    server = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m", "mcp1c.server", "--transport", "stdio",
            "--data", str(tmp_path),
        ],
        env={**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")},
    )
    async with stdio_client(server) as streams:
        async with ClientSession(*streams) as session:
            await session.initialize()
            names = {tool.name for tool in (await session.list_tools()).tools}

    assert "get_procedure" in names
    assert "get_module_source_file" not in names


def test_off_не_импортирует_и_не_инициализирует_синтетический_модуль(
    tmp_path,
    monkeypatch,
):
    import_marker = tmp_path / "imported"
    init_marker = tmp_path / "initialized"
    module_name = "synthetic_capability_canary"
    (tmp_path / f"{module_name}.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(import_marker)!r}).write_text('imported')\n"
        "def load():\n"
        f"    Path({str(init_marker)!r}).write_text('initialized')\n"
        "    return ()\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    definitions = {
        "canary": CapabilityDefinition("canary", f"{module_name}:load")
    }

    modules = load_capability_modules((), definitions=definitions)

    assert modules == ()
    assert module_name not in sys.modules
    assert not import_marker.exists()
    assert not init_marker.exists()


def test_enabled_импортирует_и_инициализирует_только_выбранный_canary(
    tmp_path,
    monkeypatch,
):
    import_marker = tmp_path / "imported"
    init_marker = tmp_path / "initialized"
    module_name = "synthetic_enabled_capability_canary"
    (tmp_path / f"{module_name}.py").write_text(
        "from pathlib import Path\n"
        f"Path({str(import_marker)!r}).write_text('imported')\n"
        "def load():\n"
        f"    Path({str(init_marker)!r}).write_text('initialized')\n"
        "    return ()\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    definitions = {
        "canary": CapabilityDefinition("canary", f"{module_name}:load")
    }

    modules = load_capability_modules(("canary",), definitions=definitions)

    assert modules == (CapabilityModule("canary", ()),)
    assert import_marker.read_text(encoding="utf-8") == "imported"
    assert init_marker.read_text(encoding="utf-8") == "initialized"


def test_loader_передаёт_модулю_только_его_явную_зависимость(
    tmp_path,
    monkeypatch,
):
    received = tmp_path / "received"
    module_name = "synthetic_dependency_capability_canary"
    (tmp_path / f"{module_name}.py").write_text(
        "from pathlib import Path\n"
        "def load(dependency):\n"
        f"    Path({str(received)!r}).write_text(dependency)\n"
        "    return ()\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    definitions = {
        "canary": CapabilityDefinition("canary", f"{module_name}:load")
    }

    modules = load_capability_modules(
        ("canary",),
        definitions=definitions,
        dependencies={"canary": "registry-view"},
    )

    assert modules == (CapabilityModule("canary", ()),)
    assert received.read_text(encoding="utf-8") == "registry-view"


def test_main_игнорирует_устаревшую_env_переменную(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP1C_CAPABILITIES", "unknown")
    captured = {}

    class FakeRegistry:
        configurations = [object()]

        def __init__(self, data):
            self.data = data

        def startup(self):
            return []

        def snapshot(self):
            return self

    class FakeServer:
        def run(self, **kwargs):
            pass

    monkeypatch.setattr(server_module, "Registry", FakeRegistry)
    monkeypatch.setattr(
        server_module,
        "build_server",
        lambda registry, **kwargs: (
            captured.update(enabled=kwargs["enabled_capabilities"]) or FakeServer()
        ),
    )

    assert server_module.main(
        ["--data", str(tmp_path), "--transport", "stdio"]
    ) == 0
    assert captured["enabled"] == ("module_source_download",)


def test_server_settings_переживают_restart_независимо_от_env(tmp_path, monkeypatch):
    store = CapabilitySettingsStore(tmp_path / "data")
    store.save(("reference", "module_source_download"))
    monkeypatch.setenv("MCP1C_CAPABILITIES", "unknown")

    resolved_store, enabled = resolve_capability_settings(tmp_path / "data")

    assert enabled == ("module_source_download", "reference")
    assert resolved_store.load() == ("module_source_download", "reference")


def test_enable_и_disable_применяются_двумя_независимыми_startup(
    tmp_path,
):
    data = tmp_path / "data"
    store = CapabilitySettingsStore(data)
    probe = """
import asyncio
import json
import sys
from pathlib import Path

from mcp1c.capabilities import CapabilityRuntime, resolve_capability_settings
from mcp1c.reference_provider import ReferenceService
from mcp1c.registry import Registry
from mcp1c.server import build_server

data = Path(sys.argv[1])
store, enabled = resolve_capability_settings(data)
registry = Registry(data)
registry.startup()
server = build_server(
    registry,
        reference=ReferenceService.discover(data / "missing-reference"),
    enabled_capabilities=enabled,
    capability_runtime=CapabilityRuntime(store, active=enabled),
)
print(json.dumps([tool.name for tool in asyncio.run(server.list_tools())]))
"""

    def startup_tools() -> list[str]:
        result = subprocess.run(
            [sys.executable, "-c", probe, str(data)],
            cwd=Path(__file__).resolve().parents[1],
            env={**os.environ, "PYTHONPATH": "src", "MCP1C_CAPABILITIES": "off"},
            check=True,
            capture_output=True,
            text=True,
        )
        return json.loads(result.stdout)

    store.save(("role_access", "module_source_download"))
    enabled_tools = startup_tools()
    store.save(())
    disabled_tools = startup_tools()

    assert enabled_tools == [*CORE_TOOLS, "find_roles_for_access", "get_role_access"]
    assert disabled_tools == [tool for tool in CORE_TOOLS if tool != "get_module_source_file"]


def test_после_удаления_settings_status_показывает_дефолт(tmp_path, monkeypatch):
    initial = CapabilitySettingsStore(tmp_path / "data")
    initial.save(("reference",))
    monkeypatch.setenv("MCP1C_CAPABILITIES", "reference")
    store, active = resolve_capability_settings(tmp_path / "data")
    runtime = CapabilityRuntime(store, active=active)

    store.path.unlink()

    assert runtime.payload()["desired"] == ["module_source_download"]
    assert runtime.pending_restart() is True


def test_без_server_settings_применяется_дефолт_независимо_от_env(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("MCP1C_CAPABILITIES", "reference")
    store, enabled = resolve_capability_settings(tmp_path / "data")
    runtime = CapabilityRuntime(store, active=enabled)

    assert enabled == ("module_source_download",)
    assert store.path.exists() is False
    assert runtime.payload()["pending_restart"] is False


def test_v1_settings_сохраняют_справку_и_включают_скачивание(tmp_path):
    store = CapabilitySettingsStore(tmp_path)
    store.path.write_text(
        '{"version":1,"capabilities":{"enabled":["reference"]}}',
        encoding="utf-8",
    )

    assert store.load() == ("module_source_download", "reference")

    store.save(("reference",))
    saved = json.loads(store.path.read_text(encoding="utf-8"))
    assert saved["version"] == 2
    assert saved["capabilities"]["enabled"] == ["reference"]
    assert store.load() == ("reference",)


@pytest.mark.parametrize("version", (1, 2))
def test_старые_authoring_settings_не_ломают_запуск_и_очищаются(tmp_path, version):
    store = CapabilitySettingsStore(tmp_path)
    store.path.write_text(
        json.dumps({
            "version": version,
            "capabilities": {"enabled": ["forms", "metadata_authoring", "reference"]},
        }),
        encoding="utf-8",
    )

    expected = ("module_source_download", "reference") if version == 1 else ("reference",)
    assert store.load() == expected
    store.save(store.load())
    assert json.loads(store.path.read_text(encoding="utf-8"))["capabilities"]["enabled"] == list(expected)
    with pytest.raises(CapabilityConfigurationError):
        store.save(("forms",))


def test_v2_settings_позволяют_отключить_скачивание_при_пустом_списке(tmp_path):
    store = CapabilitySettingsStore(tmp_path)
    store.path.write_text(
        '{"version":2,"capabilities":{"enabled":[]}}',
        encoding="utf-8",
    )

    assert store.load() == ()


def test_dashboard_показывает_сохранённое_отключение_до_restart(tmp_path):
    store = CapabilitySettingsStore(tmp_path)
    store.save(("module_source_download",))
    runtime = CapabilityRuntime(store, active=store.load())

    payload = runtime.save_desired(())
    download = next(
        module for module in payload["modules"]
        if module["id"] == "module_source_download"
    )

    assert payload["active"] == ["module_source_download"]
    assert payload["desired"] == []
    assert payload["pending_restart"] is True
    assert download["active"] is True
    assert download["desired"] is False
    assert download["pending_restart"] is True
    restarted = CapabilityRuntime(store, active=store.load()).payload()
    assert restarted["pending_restart"] is False
    assert restarted["active"] == []


def test_status_показывает_выбор_до_restart(tmp_path):
    store = CapabilitySettingsStore(tmp_path)
    store.save(("reference", "module_source_download"))
    runtime = CapabilityRuntime(store, active=store.load())
    store.save(())

    payload = runtime.payload()
    assert payload["available"] == [
        "module_source_download", "reference", "role_access",
    ]
    assert payload["active"] == ["module_source_download", "reference"]
    assert payload["desired"] == []
    assert payload["pending_restart"] is True
    assert {module["id"] for module in payload["modules"]} == {
        "module_source_download", "reference", "role_access",
    }


def test_повреждённые_server_settings_отклоняются_до_registry(
    tmp_path,
    monkeypatch,
):
    data = tmp_path / "data"
    data.mkdir()
    (data / "server-settings.json").write_text(
        '{"version": 1, "capabilities": {"enabled": ["unknown"]}}',
        encoding="utf-8",
    )
    monkeypatch.setattr(
        server_module,
        "Registry",
        lambda path: pytest.fail("Registry не должен создаваться"),
    )

    with pytest.raises(SystemExit) as error:
        server_module.main(["--data", str(data), "--transport", "stdio"])

    assert error.value.code == 2


@pytest.mark.parametrize(
    "payload",
    (
        {},
        {"version": 3, "capabilities": {"enabled": []}},
        {"version": True, "capabilities": {"enabled": []}},
        {"version": 1.0, "capabilities": {"enabled": []}},
        {"version": 1},
        {"version": 1, "capabilities": []},
        {"version": 1, "capabilities": {"enabled": "reference"}},
        {"version": 1, "capabilities": {"enabled": ["reference", "reference"]}},
        {"version": 1, "capabilities": {"enabled": [], "extra": True}},
    ),
)
def test_server_settings_fail_closed_на_неверной_schema(tmp_path, payload):
    store = CapabilitySettingsStore(tmp_path)
    tmp_path.mkdir(exist_ok=True)
    store.path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(CapabilityConfigurationError):
        store.load()


@pytest.mark.parametrize(
    "raw",
    (
        '{"version":2,"version":1,"capabilities":{"enabled":[]}}',
        '{"version":1,"capabilities":{"enabled":[],"enabled":["reference"]}}',
        '{"version":1,"capabilities":{"enabled":[]},"future":NaN}',
    ),
)
def test_server_settings_отклоняет_неоднозначный_json(tmp_path, raw):
    store = CapabilitySettingsStore(tmp_path)
    store.path.write_text(raw, encoding="utf-8")

    with pytest.raises(CapabilityConfigurationError):
        store.load()


def test_server_settings_нормализует_слишком_глубокий_json(tmp_path):
    store = CapabilitySettingsStore(tmp_path)
    store.path.write_text(
        '{"version":1,"capabilities":{"enabled":[]},"future":'
        + "[" * 10000
        + "0"
        + "]" * 10000
        + "}",
        encoding="utf-8",
    )

    with pytest.raises(CapabilityConfigurationError, match="прочитать"):
        store.load()


def test_server_settings_отклоняет_lone_surrogate_до_save(tmp_path):
    store = CapabilitySettingsStore(tmp_path)
    store.path.write_text(
        '{"version":1,"capabilities":{"enabled":[]},"future":"\\ud800"}',
        encoding="utf-8",
    )

    with pytest.raises(CapabilityConfigurationError, match="прочитать"):
        store.load()


def test_server_settings_имеют_лимит_размера(tmp_path):
    store = CapabilitySettingsStore(tmp_path)
    store.path.write_bytes(b" " * (MAX_SERVER_SETTINGS_BYTES + 1))

    with pytest.raises(CapabilityConfigurationError, match="размер"):
        store.load()


def test_server_settings_не_следует_по_symlink(tmp_path):
    outside = tmp_path / "outside.json"
    outside.write_text(
        '{"version":1,"capabilities":{"enabled":[]}}',
        encoding="utf-8",
    )
    data = tmp_path / "data"
    data.mkdir()
    store = CapabilitySettingsStore(data)
    store.path.symlink_to(outside)

    with pytest.raises(CapabilityConfigurationError, match="открыть"):
        store.load()


def test_server_settings_не_зависает_на_fifo(tmp_path):
    if not hasattr(os, "mkfifo"):
        pytest.skip("FIFO недоступен на этой платформе")
    store = CapabilitySettingsStore(tmp_path)
    os.mkfifo(store.path)

    with pytest.raises(CapabilityConfigurationError, match="обычным файлом"):
        store.load()


def test_bounded_read_держит_лимит_даже_если_fstat_устарел(
    tmp_path,
    monkeypatch,
):
    import mcp1c.capabilities as capability_module

    store = CapabilitySettingsStore(tmp_path)
    store.path.write_bytes(b" " * (MAX_SERVER_SETTINGS_BYTES + 1))
    class StaleMetadata:
        st_mode = stat.S_IFREG | 0o600
        st_size = 1

    monkeypatch.setattr(capability_module.os, "fstat", lambda descriptor: StaleMetadata())

    with pytest.raises(CapabilityConfigurationError, match="размер"):
        store.load()


def test_save_атомарно_заменяет_секцию_и_сохраняет_будущие_секции(tmp_path):
    store = CapabilitySettingsStore(tmp_path)
    store.path.write_text(
        json.dumps(
            {
                "version": 1,
                "capabilities": {"enabled": []},
                "future": {"kept": True},
            }
        ),
        encoding="utf-8",
    )

    store.save(("reference",))

    payload = json.loads(store.path.read_text(encoding="utf-8"))
    assert payload["future"] == {"kept": True}
    assert payload["version"] == 2
    assert payload["capabilities"] == {"enabled": ["reference"]}
    assert os.stat(store.path).st_mode & 0o777 == 0o600


def test_ошибка_atomic_replace_не_портит_предыдущие_settings(
    tmp_path,
    monkeypatch,
):
    import mcp1c.capabilities as capability_module

    store = CapabilitySettingsStore(tmp_path)
    store.save(())
    original = store.path.read_bytes()
    monkeypatch.setattr(
        capability_module.os,
        "replace",
        lambda source, target: (_ for _ in ()).throw(OSError("synthetic")),
    )

    with pytest.raises(CapabilityConfigurationError, match="сохранить"):
        store.save(("reference",))

    assert store.path.read_bytes() == original
    assert list(tmp_path.glob(".server-settings.json.tmp-*")) == []


def test_ошибка_directory_fsync_после_replace_считается_применённой(
    tmp_path,
    monkeypatch,
):
    import mcp1c.capabilities as capability_module

    store = CapabilitySettingsStore(tmp_path)
    store.save(())
    original_fsync = capability_module.os.fsync

    def fsync_with_directory_failure(descriptor):
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise OSError("synthetic directory fsync")
        return original_fsync(descriptor)

    monkeypatch.setattr(capability_module.os, "fsync", fsync_with_directory_failure)

    assert store.save(("reference",)) == ("reference",)
    assert store.load() == ("reference",)
    assert list(tmp_path.glob(".server-settings.json.tmp-*")) == []


@pytest.mark.parametrize("transport", ("stdio", "streamable-http"))
def test_main_передаёт_capability_в_оба_транспорта(
    transport,
    tmp_path,
    monkeypatch,
):
    captured = {}

    class FakeRegistry:
        configurations = [object()]

        def __init__(self, data):
            self.data = data

        def startup(self):
            return []

        def snapshot(self):
            return self

    class FakeServer:
        def run(self, **kwargs):
            captured["run"] = kwargs

    def fake_build(registry, **kwargs):
        captured["enabled"] = kwargs["enabled_capabilities"]
        return FakeServer()

    monkeypatch.setenv("MCP1C_CAPABILITIES", "reference")
    monkeypatch.setattr(server_module, "Registry", FakeRegistry)
    monkeypatch.setattr(server_module, "build_server", fake_build)
    monkeypatch.setattr(
        server_module,
        "_run_streamable_http",
        lambda server, **kwargs: captured.update(http=kwargs),
    )

    assert server_module.main(
        ["--data", str(tmp_path), "--transport", transport]
    ) == 0
    assert captured["enabled"] == ("module_source_download",)
    assert ("run" in captured) is (transport == "stdio")
    assert ("http" in captured) is (transport == "streamable-http")


@pytest.mark.anyio
async def test_authoring_не_публикуется_в_mcp_сессии(tmp_path):
    server = _server(tmp_path)
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
                    names = {tool.name for tool in (await session.list_tools()).tools}
            finally:
                tasks.cancel_scope.cancel()
    assert names.isdisjoint(REMOVED_AUTHORING_TOOLS)
    assert "get_object" in names
    assert "search_syntax" in names


def _noop() -> str:
    return "ok"


@pytest.mark.parametrize(
    "name",
        ("list_configurations", "search_reference", "find_roles_for_access"),
)
def test_имя_модуля_не_может_конфликтовать_с_ядром(tmp_path, name):
    server = _server(tmp_path)
    module = CapabilityModule(
        "synthetic",
        (CapabilityTool(name, _noop, "Синтетика."),),
    )

    with pytest.raises(CapabilityContractError, match=name):
        server.add_capability_modules((module,))

    assert _names(server) == CORE_TOOLS


def test_два_модуля_не_могут_тихо_объявить_одно_имя(tmp_path):
    server = _server(tmp_path)
    modules = (
        CapabilityModule(
            "first",
            (CapabilityTool("shared_status", _noop, "Первый."),),
        ),
        CapabilityModule(
            "second",
            (CapabilityTool("shared_status", _noop, "Второй."),),
        ),
    )

    with pytest.raises(CapabilityContractError, match="shared_status"):
        server.add_capability_modules(modules)

    assert _names(server) == CORE_TOOLS


def test_malformed_tool_даёт_контрактный_отказ_без_сырого_traceback(tmp_path):
    server = _server(tmp_path)
    malformed = (
        CapabilityModule(None, (CapabilityTool("valid", _noop, "Описание."),)),
        CapabilityModule(
            "synthetic",
            (CapabilityTool(None, _noop, "Описание."),),
        ),
        CapabilityModule(
            "synthetic",
            (CapabilityTool("valid", _noop, None),),
        ),
    )

    for module in malformed:
        with pytest.raises(CapabilityContractError):
            server.add_capability_modules((module,))

    assert _names(server) == CORE_TOOLS


def test_ошибка_добавления_откатывает_весь_набор(tmp_path, monkeypatch):
    server = _server(tmp_path)
    modules = (
        CapabilityModule(
            "synthetic",
            (
                CapabilityTool("synthetic_first", _noop, "Первый."),
                CapabilityTool("synthetic_second", _noop, "Второй."),
            ),
        ),
    )
    original = server._tool_manager.add_tool

    def fail_second(function, **kwargs):
        if kwargs.get("name") == "synthetic_second":
            raise RuntimeError("synthetic failure")
        return original(function, **kwargs)

    monkeypatch.setattr(server._tool_manager, "add_tool", fail_second)

    with pytest.raises(RuntimeError, match="synthetic failure"):
        server.add_capability_modules(modules)

    assert _names(server) == CORE_TOOLS


def test_public_startup_документирует_settings_без_bootstrap_env():
    root = Path(__file__).resolve().parents[1]
    compose = (root / "compose.yaml").read_text(encoding="utf-8")
    example = (root / ".env.example").read_text(encoding="utf-8")
    readme = (root / "README.md").read_text(encoding="utf-8")
    tools_doc = (root / "docs" / "tools.md").read_text(encoding="utf-8")
    operations = (root / "docs" / "operations.md").read_text(encoding="utf-8")

    assert "MCP1C_CAPABILITIES" not in compose
    assert "MCP1C_CAPABILITIES" not in example
    assert "MCP1C_CAPABILITIES" not in readme
    assert "](docs/operations.md#внутренние-capability-модули)" in readme
    assert "«Дополнительные модули»" in readme
    assert "`1c-form-creator`" in tools_doc
    assert "`1c-metadata-creator`" in tools_doc
    assert "`diagnostics_status`" not in tools_doc
    assert "`diagnostics_status`" not in readme
    assert "`PUT` того же admin-" in tools_doc
    assert "`data/server-settings.json`" in operations
    assert "`PUT /api/v1/capabilities`" in operations
    assert "schema v2" in operations
    assert "module_source_download" in operations
    assert "capabilities.enabled" in operations
